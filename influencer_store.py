import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import aiohttp

from supabase_client import (
    get_supabase_config,
    get_headers,
    _rest_call,
    get_brand_dossier,
)
from influencer_agent import (
    SEED_CREATORS,
    OUTREACH_TTL_HOURS,
    AUTO_APPROVE_HOURS,
    SOURCE_URL_LOCK_MINUTES,
    build_campaign_entities,
    score_and_match_creators,
    generate_outreach_email,
    validate_start_date,
)

from creator_scraper import discover_niche_creators

# In-memory cache used seamlessly if Supabase influencer_* tables aren't migrated yet
_MEM_CREATORS: dict[str, dict] = {c["id"]: dict(c) for c in SEED_CREATORS}
_MEM_CAMPAIGNS: dict[str, dict] = {}
_MEM_CAMPAIGN_CREATORS: dict[str, list[dict]] = {}
_MEM_MESSAGES: dict[str, list[dict]] = {}

_CREATOR_DB_COLS = {
    "id", "name", "handle", "profile_url", "is_sample", "platform",
    "verticals", "tone_tags", "followers_count", "engagement_rate",
    "min_rate", "currency", "acceptance_rate", "on_time_rate",
    "bio", "email", "approval_status",
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


_SEED_BY_ID = {c["id"]: c for c in SEED_CREATORS}


async def _upsert_creator_rows(session, rest_url: str, upsert_h: dict, rows: list[dict]) -> bool:
    """Upsert creators into Supabase, stripping extra keys or unmigrated columns cleanly."""
    clean_rows = [{k: v for k, v in r.items() if k in _CREATOR_DB_COLS} for r in rows]
    ok, code, err = await _rest_call(
        session, "POST", f"{rest_url}/influencer_creators?on_conflict=id", upsert_h, clean_rows
    )
    if ok:
        return True
    if code == 400 and "PGRST204" in str(err):
        trimmed = [
            {k: v for k, v in r.items() if k not in ("profile_url", "is_sample")}
            for r in clean_rows
        ]
        ok2, _, _ = await _rest_call(
            session, "POST", f"{rest_url}/influencer_creators?on_conflict=id", upsert_h, trimmed
        )
        return ok2
    return False


async def discover_creators_for_brand(
    brand_domain: str = "",
    custom_query: str = "",
    platforms: list[str] | None = None,
    limit_per_source: int = 8,
) -> dict:
    """Scrape live public creators matching the target brand's niche and persist to Supabase."""
    dossier = await get_brand_dossier(brand_domain) if brand_domain else None
    if not dossier and brand_domain:
        dossier = {"brand": {"domain": brand_domain, "brand_name": brand_domain}, "items": [], "documents": []}

    scraped = await discover_niche_creators(
        dossier=dossier,
        custom_query=custom_query,
        platforms=platforms,
        limit_per_source=limit_per_source,
    )
    creators = scraped.get("creators") or []

    saved_to_supabase = False
    if creators:
        for cr in creators:
            _MEM_CREATORS[cr["id"]] = dict(cr)

        base_url, key = get_supabase_config()
        rest_url = f"{base_url}/rest/v1"
        upsert_h = get_headers(key, prefer="resolution=merge-duplicates,return=minimal")
        insert_h = get_headers(key, prefer="return=minimal")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            saved_to_supabase = await _upsert_creator_rows(session, rest_url, upsert_h, creators)
            if brand_domain:
                await _rest_call(
                    session,
                    "POST",
                    f"{rest_url}/brand_events",
                    insert_h,
                    [{
                        "brand_domain": brand_domain,
                        "extraction_id": f"disc_{int(_now_utc().timestamp())}",
                        "event_type": "influencer_discovery",
                        "payload": {
                            "search_context": scraped.get("context"),
                            "sources": scraped.get("sources"),
                            "discovered_count": len(creators),
                            "creators_manifest": [
                                {
                                    "id": c["id"],
                                    "name": c["name"],
                                    "platform": c["platform"],
                                    "profile_url": c["profile_url"],
                                    "discovery_source": c.get("discovery_source"),
                                }
                                for c in creators
                            ],
                        },
                        "created_at": _now_iso(),
                    }],
                )

    return {
        "brand_domain": brand_domain,
        "context": scraped.get("context"),
        "creators": creators,
        "count": len(creators),
        "sources": scraped.get("sources"),
        "saved_to_supabase": saved_to_supabase,
    }


async def list_creators(platform: str = "", approval_status: str = "") -> dict:
    """List creators from Supabase influencer_creators (seeding pool on first access) with fallback."""
    base_url, key = get_supabase_config()
    rest_url = f"{base_url}/rest/v1"
    read_h = get_headers(key, prefer="return=representation")
    upsert_h = get_headers(key, prefer="resolution=merge-duplicates,return=minimal")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        ok, code, rows = await _rest_call(
            session, "GET", f"{rest_url}/influencer_creators?select=*&order=followers_count.desc", read_h
        )
        if ok and isinstance(rows, list):
            existing_ids = {r.get("id") for r in rows}
            missing = [c for c in SEED_CREATORS if c["id"] not in existing_ids]
            if missing:
                await _upsert_creator_rows(session, rest_url, upsert_h, missing)
                rows.extend([dict(c) for c in missing])
            # Hydrate discovered creators' profile_url from brand_events manifest across cold starts
            needs_manifest = any(
                not r.get("profile_url") and r.get("id") not in _SEED_BY_ID and r.get("id") not in _MEM_CREATORS
                for r in rows
            )
            if needs_manifest:
                ok_ev, _, ev_rows = await _rest_call(
                    session,
                    "GET",
                    f"{rest_url}/brand_events?event_type=eq.influencer_discovery&select=payload&order=created_at.desc&limit=50",
                    read_h,
                )
                if ok_ev and isinstance(ev_rows, list):
                    for ev in ev_rows:
                        manifest = (ev.get("payload") or {}).get("creators_manifest") or []
                        for m in manifest:
                            cid = m.get("id")
                            if cid and cid not in _MEM_CREATORS and m.get("profile_url"):
                                _MEM_CREATORS[cid] = {
                                    "profile_url": m["profile_url"],
                                    "is_sample": False,
                                }
            source = "supabase"
        else:
            rows = list(_MEM_CREATORS.values())
            source = "memory_fallback"

    # Enrich profile_url / is_sample cleanly and hide any removed demo placeholders
    enriched = []
    for r in rows:
        if r.get("approval_status") == "removed" or str(r.get("email") or "").endswith("@creators.local"):
            continue
        item = dict(r)
        seed_ref = _SEED_BY_ID.get(item.get("id")) or _MEM_CREATORS.get(item.get("id"))
        if seed_ref:
            if not item.get("profile_url"):
                item["profile_url"] = seed_ref.get("profile_url", "")
            if "is_sample" not in item or item.get("is_sample") is None:
                item["is_sample"] = seed_ref.get("is_sample", False)
        if "is_sample" not in item:
            item["is_sample"] = not bool(item.get("profile_url"))
        enriched.append(item)
    rows = enriched

    if platform:
        rows = [r for r in rows if r.get("platform") == platform]
    if approval_status:
        rows = [r for r in rows if r.get("approval_status") == approval_status]

    rows.sort(key=lambda r: int(r.get("followers_count") or 0), reverse=True)
    return {"creators": rows, "count": len(rows), "storage": source}


async def register_creator(payload: dict) -> dict:
    """Register a new creator (/creator/new onboarding flow, starts pending admin approval)."""
    import uuid

    name = (payload.get("name") or "").strip()
    handle = (payload.get("handle") or "").strip()
    platform = (payload.get("platform") or "twitter_x").strip()
    if not name or not handle:
        return {"error": "Creator name and handle are required"}

    verticals = payload.get("verticals") or []
    if isinstance(verticals, str):
        verticals = [v.strip().lower() for v in verticals.split(",") if v.strip()]
    tone_tags = payload.get("tone_tags") or []
    if isinstance(tone_tags, str):
        tone_tags = [t.strip().lower() for t in tone_tags.split(",") if t.strip()]

    profile_url = (payload.get("profile_url") or "").strip()
    if not profile_url:
        clean_h = handle.lstrip("@")
        if platform == "linkedin":
            slug = clean_h[3:] if clean_h.startswith("in/") else clean_h
            profile_url = f"https://www.linkedin.com/in/{slug}"
        else:
            profile_url = f"https://x.com/{clean_h}"

    creator = {
        "id": f"cr_{uuid.uuid4().hex[:8]}",
        "name": name,
        "handle": handle,
        "profile_url": profile_url,
        "is_sample": False,
        "platform": platform if platform in ("twitter_x", "linkedin") else "twitter_x",
        "verticals": verticals,
        "tone_tags": tone_tags,
        "followers_count": int(payload.get("followers_count") or 5000),
        "engagement_rate": float(payload.get("engagement_rate") or 3.5),
        "min_rate": float(payload.get("min_rate") or 250),
        "currency": payload.get("currency") or "USD",
        "acceptance_rate": 0.9,
        "on_time_rate": 0.95,
        "bio": (payload.get("bio") or "").strip(),
        "email": (payload.get("email") or "").strip(),
        "approval_status": "pending",
        "created_at": _now_iso(),
    }
    _MEM_CREATORS[creator["id"]] = creator

    base_url, key = get_supabase_config()
    rest_url = f"{base_url}/rest/v1"
    upsert_h = get_headers(key, prefer="resolution=merge-duplicates,return=minimal")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        ok = await _upsert_creator_rows(session, rest_url, upsert_h, [creator])

    return {"creator": creator, "saved_to_supabase": ok}


async def set_creator_approval(creator_id: str, approval_status: str) -> dict | None:
    """Approve or reject a creator onboarding request."""
    if approval_status not in ("approved", "rejected", "pending"):
        return None
    if creator_id in _MEM_CREATORS:
        _MEM_CREATORS[creator_id]["approval_status"] = approval_status

    base_url, key = get_supabase_config()
    rest_url = f"{base_url}/rest/v1"
    patch_h = get_headers(key, prefer="return=representation")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        ok, _, rows = await _rest_call(
            session,
            "PATCH",
            f"{rest_url}/influencer_creators?id=eq.{quote(creator_id, safe='')}",
            patch_h,
            {"approval_status": approval_status},
        )
        if ok and isinstance(rows, list) and rows:
            _MEM_CREATORS[creator_id] = rows[0]
            return rows[0]
    return _MEM_CREATORS.get(creator_id)


async def _persist_campaign_bundle(campaign: dict, cc_rows: list[dict], messages: list[dict] | None = None):
    """Save campaign, shortlist rows, and messages to memory + Supabase (plus brand_events snapshot)."""
    cid = campaign["id"]
    _MEM_CAMPAIGNS[cid] = campaign
    _MEM_CAMPAIGN_CREATORS[cid] = cc_rows
    if messages is not None:
        _MEM_MESSAGES[cid] = messages

    base_url, key = get_supabase_config()
    rest_url = f"{base_url}/rest/v1"
    upsert_h = get_headers(key, prefer="resolution=merge-duplicates,return=minimal")
    insert_h = get_headers(key, prefer="return=minimal")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
        ok_camp, _, _ = await _rest_call(
            session, "POST", f"{rest_url}/influencer_campaigns?on_conflict=id", upsert_h, [campaign]
        )
        if ok_camp and cc_rows:
            ok_cc, code_cc, err_cc = await _rest_call(
                session, "POST", f"{rest_url}/influencer_campaign_creators?on_conflict=id", upsert_h, cc_rows
            )
            if not ok_cc and code_cc == 400 and "PGRST204" in str(err_cc):
                trimmed_cc = [{k: v for k, v in r.items() if k != "creator_profile_url"} for r in cc_rows]
                await _rest_call(
                    session, "POST", f"{rest_url}/influencer_campaign_creators?on_conflict=id", upsert_h, trimmed_cc
                )
        if ok_camp and messages:
            new_msgs = [m for m in messages if not m.get("id")]
            if new_msgs:
                await _rest_call(session, "POST", f"{rest_url}/influencer_messages", insert_h, new_msgs)

        # Also record campaign lifecycle event in brand_events if brand exists
        await _rest_call(
            session,
            "POST",
            f"{rest_url}/brand_events",
            insert_h,
            [{
                "brand_domain": campaign["brand_domain"],
                "extraction_id": cid,
                "event_type": f"influencer_campaign_{campaign['status']}",
                "payload": {
                    "campaign": campaign,
                    "creators_count": len(cc_rows),
                    "creators": [
                        {
                            "creator_name": r.get("creator_name"),
                            "handle": r.get("creator_handle"),
                            "platform": r.get("platform"),
                            "status": r.get("status"),
                            "fee": r.get("agreed_fee"),
                        }
                        for r in cc_rows
                    ],
                },
                "created_at": _now_iso(),
            }],
        )


async def create_campaign(brand_domain: str, prompt: str, overrides: dict | None = None) -> dict:
    """Create a campaign brief from the brand's 5 .txt Knowledge Base docs and match creators."""
    if overrides and overrides.get("start_date"):
        ok_dt, msg_dt = validate_start_date(overrides["start_date"])
        if not ok_dt:
            return {"error": msg_dt}

    dossier = await get_brand_dossier(brand_domain) if brand_domain else None
    if overrides and overrides.get("auto_discover"):
        req_plats = overrides.get("platforms") or None
        await discover_creators_for_brand(brand_domain=brand_domain, platforms=req_plats, limit_per_source=6)
    creators_res = await list_creators()
    creators = creators_res.get("creators") or list(_MEM_CREATORS.values())

    campaign, cc_rows, init_msgs = build_campaign_entities(
        brand_domain=brand_domain,
        prompt=prompt,
        dossier=dossier,
        creators=creators,
        overrides=overrides,
    )
    await _persist_campaign_bundle(campaign, cc_rows, init_msgs)
    return {
        "campaign": campaign,
        "creators": cc_rows,
        "messages": init_msgs,
    }


async def list_campaigns(brand_domain: str = "") -> dict:
    """List all influencer campaigns and their creator statuses."""
    base_url, key = get_supabase_config()
    rest_url = f"{base_url}/rest/v1"
    read_h = get_headers(key, prefer="return=representation")

    campaigns = []
    cc_all = []
    source = "memory_fallback"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        q_url = f"{rest_url}/influencer_campaigns?select=*&order=created_at.desc&limit=50"
        if brand_domain:
            q_url += f"&brand_domain=eq.{quote(brand_domain, safe='')}"
        ok, _, rows = await _rest_call(session, "GET", q_url, read_h)
        if ok and isinstance(rows, list):
            source = "supabase"
            campaigns = rows
            _, _, cc_rows = await _rest_call(
                session, "GET", f"{rest_url}/influencer_campaign_creators?select=*&limit=500", read_h
            )
            if isinstance(cc_rows, list):
                cc_all = cc_rows

    if source != "supabase":
        campaigns = sorted(_MEM_CAMPAIGNS.values(), key=lambda c: c.get("created_at", ""), reverse=True)
        if brand_domain:
            campaigns = [c for c in campaigns if c.get("brand_domain") == brand_domain]
        for c in campaigns:
            cc_all.extend(_MEM_CAMPAIGN_CREATORS.get(c["id"], []))

    cc_by_camp = {}
    for row in cc_all:
        if row.get("status") == "removed":
            continue
        cc_by_camp.setdefault(row["campaign_id"], []).append(row)

    enriched = []
    for camp in campaigns:
        c_copy = dict(camp)
        c_copy["creators"] = cc_by_camp.get(camp["id"], [])
        enriched.append(c_copy)

    return {"campaigns": enriched, "count": len(enriched), "storage": source}


async def get_campaign_detail(campaign_id: str) -> dict | None:
    """Fetch single campaign, its shortlisted creators, and 1:1 messages."""
    base_url, key = get_supabase_config()
    rest_url = f"{base_url}/rest/v1"
    read_h = get_headers(key, prefer="return=representation")
    enc = quote(campaign_id, safe="")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        ok, _, rows = await _rest_call(session, "GET", f"{rest_url}/influencer_campaigns?id=eq.{enc}&limit=1", read_h)
        if ok and isinstance(rows, list) and rows:
            camp = rows[0]
            _, _, cc = await _rest_call(
                session, "GET", f"{rest_url}/influencer_campaign_creators?campaign_id=eq.{enc}&order=match_score.desc", read_h
            )
            _, _, msgs = await _rest_call(
                session, "GET", f"{rest_url}/influencer_messages?campaign_id=eq.{enc}&order=created_at.asc", read_h
            )
            _MEM_CAMPAIGNS[campaign_id] = camp
            _MEM_CAMPAIGN_CREATORS[campaign_id] = [r for r in (cc if isinstance(cc, list) else []) if r.get("status") != "removed"]
            _MEM_MESSAGES[campaign_id] = msgs if isinstance(msgs, list) else []
            return {
                "campaign": camp,
                "creators": _MEM_CAMPAIGN_CREATORS[campaign_id],
                "messages": _MEM_MESSAGES[campaign_id],
            }

    camp = _MEM_CAMPAIGNS.get(campaign_id)
    if not camp:
        return None
    return {
        "campaign": camp,
        "creators": _MEM_CAMPAIGN_CREATORS.get(campaign_id, []),
        "messages": _MEM_MESSAGES.get(campaign_id, []),
    }


async def _append_message(campaign_id: str, sender_role: str, message: str, creator_id: str | None = None) -> dict:
    msg = {
        "campaign_id": campaign_id,
        "creator_id": creator_id,
        "sender_role": sender_role,
        "message": message,
        "created_at": _now_iso(),
    }
    _MEM_MESSAGES.setdefault(campaign_id, []).append(msg)

    base_url, key = get_supabase_config()
    rest_url = f"{base_url}/rest/v1"
    insert_h = get_headers(key, prefer="return=minimal")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        await _rest_call(session, "POST", f"{rest_url}/influencer_messages", insert_h, [msg])
    return msg


async def transition_campaign(campaign_id: str, action: str, payload: dict | None = None) -> dict:
    """Execute campaign-level actions: approve, fund, source_url, rematch, resend, cancel."""
    payload = payload or {}
    detail = await get_campaign_detail(campaign_id)
    if not detail:
        return {"error": f"Campaign '{campaign_id}' not found"}

    camp = dict(detail["campaign"])
    cc_rows = [dict(r) for r in detail["creators"]]
    now = _now_utc()
    now_iso = now.isoformat()

    if action == "approve":
        if camp["status"] not in ("shortlist_ready", "draft"):
            return {"error": f"Cannot approve shortlist from state '{camp['status']}'"}
        camp["status"] = "approved"
        camp["updated_at"] = now_iso
        await _persist_campaign_bundle(camp, cc_rows)
        await _append_message(campaign_id, "agent", "Shortlist approved. Proceed to Fund campaign to initiate creator outreach.")

    elif action == "fund":
        if camp["status"] not in ("approved", "shortlist_ready"):
            return {"error": f"Cannot fund campaign in state '{camp['status']}'"}
        wallet_amt = float(payload.get("wallet_applied") or 0.0)
        camp["status"] = "funded"
        camp["payment_status"] = "funded"
        camp["wallet_applied"] = min(wallet_amt, float(camp.get("budget") or 0))
        camp["updated_at"] = now_iso

        expires_iso = (now + timedelta(hours=OUTREACH_TTL_HOURS)).isoformat()
        for r in cc_rows:
            if r["status"] in ("shortlisted", "expired"):
                r["status"] = "invited"
                r["outreach_expires_at"] = expires_iso
                r["updated_at"] = now_iso
        await _persist_campaign_bundle(camp, cc_rows)
        await _append_message(
            campaign_id,
            "agent",
            f"Campaign funded ({camp['currency']} {camp['budget']:g}, {camp['commission_pct']}% platform commission). "
            f"Sent automated outreach with 48h SLA to {len(cc_rows)} creator(s).",
        )

    elif action == "source_url":
        src_url = (payload.get("source_post_url") or "").strip()
        if not src_url:
            return {"error": "source_post_url is required"}
        locked_at = camp.get("source_post_locked_at")
        if locked_at:
            try:
                if now > datetime.fromisoformat(str(locked_at).replace("Z", "+00:00")):
                    return {"error": "Source tweet URL locked 1 hour after initial submission."}
            except Exception:
                pass
        else:
            camp["source_post_locked_at"] = (now + timedelta(minutes=SOURCE_URL_LOCK_MINUTES)).isoformat()

        camp["source_post_url"] = src_url
        camp["updated_at"] = now_iso
        await _persist_campaign_bundle(camp, cc_rows)
        await _append_message(campaign_id, "agent", f"Source tweet URL set to {src_url} (locks in 60 minutes).")

    elif action == "rematch":
        exclude_ids = {r["creator_id"] for r in cc_rows}
        dossier = await get_brand_dossier(camp["brand_domain"])
        creators_res = await list_creators()
        pool = creators_res.get("creators") or list(_MEM_CREATORS.values())
        spec = {
            "platforms": camp.get("platforms") or ["twitter_x"],
            "deliverables": camp.get("deliverables") or {},
            "budget": camp.get("budget") or 2500,
            "tone": camp.get("tone") or "product-focused",
            "goal": camp.get("goal") or "",
            "creator_count": max(3, len(cc_rows)),
        }
        matched = score_and_match_creators(pool, dossier, spec, exclude_ids=exclude_ids)
        if not matched["shortlist"]:
            matched = score_and_match_creators(pool, dossier, spec)
        new_cc = []
        import uuid
        for item in matched["shortlist"]:
            cr = item["creator"]
            new_cc.append({
                "id": f"cc_{uuid.uuid4().hex[:8]}",
                "campaign_id": campaign_id,
                "creator_id": cr["id"],
                "creator_name": cr["name"],
                "creator_handle": cr["handle"],
                "platform": cr["platform"],
                "deliverable_type": item["deliverable_type"],
                "match_score": item["match_score"],
                "score_breakdown": item["score_breakdown"],
                "agreed_fee": item["agreed_fee"],
                "status": "invited" if camp["payment_status"] == "funded" else "shortlisted",
                "outreach_email": generate_outreach_email(cr, camp, item["deliverable_type"], item["agreed_fee"]),
                "outreach_expires_at": (now + timedelta(hours=OUTREACH_TTL_HOURS)).isoformat() if camp["payment_status"] == "funded" else None,
                "submission_url": "",
                "submission_notes": "",
                "auto_approve_at": None,
                "payout_status": "pending",
                "created_at": now_iso,
                "updated_at": now_iso,
            })
        cc_rows = new_cc
        camp["updated_at"] = now_iso
        await _persist_campaign_bundle(camp, cc_rows)
        await _append_message(campaign_id, "agent", f"Rematched {len(cc_rows)} fresh creator(s) for campaign.")

    elif action == "resend":
        expires_iso = (now + timedelta(hours=OUTREACH_TTL_HOURS)).isoformat()
        resent = 0
        for r in cc_rows:
            if r["status"] in ("invited", "expired", "shortlisted"):
                r["status"] = "invited"
                r["outreach_expires_at"] = expires_iso
                r["updated_at"] = now_iso
                resent += 1
        camp["updated_at"] = now_iso
        await _persist_campaign_bundle(camp, cc_rows)
        await _append_message(campaign_id, "agent", f"Resent 48-hour outreach links to {resent} pending creator(s).")

    elif action == "cancel":
        accepted_any = any(r["status"] in ("accepted", "submitted", "changes_requested", "approved") for r in cc_rows)
        if accepted_any:
            return {"error": "Cannot cancel outright once a creator has accepted; use change requests or individual refunds instead."}
        camp["status"] = "cancelled"
        camp["payment_status"] = "refunded" if camp.get("payment_status") == "funded" else "unpaid"
        for r in cc_rows:
            if r["status"] in ("invited", "shortlisted"):
                r["status"] = "declined"
                r["payout_status"] = "refunded"
                r["updated_at"] = now_iso
        camp["updated_at"] = now_iso
        await _persist_campaign_bundle(camp, cc_rows)
        await _append_message(campaign_id, "agent", "Campaign cancelled before any creator acceptance; funds refunded.")

    else:
        return {"error": f"Unknown campaign action '{action}'"}

    return await get_campaign_detail(campaign_id)


async def transition_campaign_creator(campaign_id: str, creator_row_id: str, action: str, payload: dict | None = None) -> dict:
    """Update individual creator state: accept, decline, submit, request_changes, approve, refund."""
    payload = payload or {}
    detail = await get_campaign_detail(campaign_id)
    if not detail:
        return {"error": f"Campaign '{campaign_id}' not found"}

    camp = dict(detail["campaign"])
    cc_rows = [dict(r) for r in detail["creators"]]
    target = next((r for r in cc_rows if r["id"] == creator_row_id or r["creator_id"] == creator_row_id), None)
    if not target:
        return {"error": f"Creator entry '{creator_row_id}' not found in campaign"}

    now = _now_utc()
    now_iso = now.isoformat()

    if action == "accept":
        target["status"] = "accepted"
        if camp["status"] == "funded":
            camp["status"] = "active"
        msg = f"{target['creator_name']} ({target['creator_handle']}) accepted the campaign brief."
    elif action == "decline":
        target["status"] = "declined"
        target["payout_status"] = "refunded"
        msg = f"{target['creator_name']} declined outreach; reserved creator fee refunded."
    elif action == "submit":
        sub_url = (payload.get("submission_url") or "").strip()
        if not sub_url:
            return {"error": "submission_url is required"}
        target["status"] = "submitted"
        target["submission_url"] = sub_url
        target["submission_notes"] = (payload.get("submission_notes") or "").strip()
        target["auto_approve_at"] = (now + timedelta(hours=AUTO_APPROVE_HOURS)).isoformat()
        msg = f"{target['creator_name']} submitted deliverable: {sub_url} (auto-approves in 48h)."
    elif action == "request_changes":
        notes = (payload.get("notes") or "Please align closer to Brand Voice guardrails.").strip()
        target["status"] = "changes_requested"
        target["submission_notes"] = notes
        msg = f"Brand requested changes for {target['creator_name']}: {notes}"
    elif action == "approve":
        target["status"] = "approved"
        target["payout_status"] = "paid"
        msg = f"Deliverable approved for {target['creator_name']}. Payout of {camp['currency']} {target['agreed_fee']:g} triggered."
    elif action == "refund":
        if target["status"] == "approved":
            return {"error": "Cannot refund after deliverable approval."}
        target["status"] = "refunded"
        target["payout_status"] = "refunded"
        msg = f"Refunded {camp['currency']} {target['agreed_fee']:g} for {target['creator_name']} (commission retained)."
    else:
        return {"error": f"Unknown creator action '{action}'"}

    target["updated_at"] = now_iso
    if cc_rows and all(r["status"] in ("approved", "declined", "expired", "refunded") for r in cc_rows):
        if any(r["status"] == "approved" for r in cc_rows):
            camp["status"] = "completed"
    camp["updated_at"] = now_iso

    await _persist_campaign_bundle(camp, cc_rows)
    await _append_message(campaign_id, "agent", msg, creator_id=target["creator_id"])
    return await get_campaign_detail(campaign_id)


async def post_campaign_message(campaign_id: str, sender_role: str, message: str, creator_id: str | None = None) -> dict:
    """Append a message to the 1:1 campaign messaging thread."""
    if not (message or "").strip():
        return {"error": "message is required"}
    detail = await get_campaign_detail(campaign_id)
    if not detail:
        return {"error": f"Campaign '{campaign_id}' not found"}
    msg = await _append_message(campaign_id, sender_role or "brand", message.strip(), creator_id=creator_id)
    return {"message": msg}
