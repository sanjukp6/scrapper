import math
import re
import uuid
from datetime import datetime, timedelta, timezone

SUPPORTED_DELIVERABLES = {
    "twitter_x": ["tweet", "retweet", "quote_tweet", "thread"],
    "linkedin": ["post", "repost", "carousel", "video"],
    "youtube": ["review_video", "short", "mention"],
    "instagram": ["reel", "story", "post"],
}

COMMISSION_PCT = 10.0
MIN_LEAD_DAYS = 2
OUTREACH_TTL_HOURS = 48
AUTO_APPROVE_HOURS = 48
SOURCE_URL_LOCK_MINUTES = 60

SEED_CREATORS = [
    # Real verified fragrance industry & creator profiles (public LinkedIn / X)
    {
        "id": "cr_real_mona_li",
        "name": "Mona Kattan",
        "handle": "in/monakattan",
        "profile_url": "https://www.linkedin.com/in/monakattan",
        "is_sample": False,
        "platform": "linkedin",
        "verticals": ["perfume", "fragrance", "scents", "luxury", "beauty", "d2c"],
        "tone_tags": ["aspirational", "storytelling", "artisanal", "product-focused"],
        "followers_count": 250000,
        "engagement_rate": 4.5,
        "min_rate": 1500,
        "currency": "USD",
        "acceptance_rate": 0.85,
        "on_time_rate": 0.92,
        "bio": "Founder of Kayali Fragrances & co-founder of Huda Beauty; shares perfume formulations & scent launches.",
        "email": "",
        "approval_status": "approved",
    },
    {
        "id": "cr_real_renaud_li",
        "name": "Renaud Salmon",
        "handle": "in/renaud-salmon-a7605912",
        "profile_url": "https://www.linkedin.com/in/renaud-salmon-a7605912",
        "is_sample": False,
        "platform": "linkedin",
        "verticals": ["perfume", "fragrance", "luxury", "niche", "artisanal"],
        "tone_tags": ["artisanal", "aspirational", "storytelling", "evocative"],
        "followers_count": 45000,
        "engagement_rate": 3.9,
        "min_rate": 900,
        "currency": "USD",
        "acceptance_rate": 0.86,
        "on_time_rate": 0.94,
        "bio": "Chief Creative Officer at Amouage sharing niche luxury perfumery, ingredient sourcing, and creative direction.",
        "email": "",
        "approval_status": "approved",
    },
    {
        "id": "cr_real_valerie_li",
        "name": "Valerie Lovisa",
        "handle": "in/valerielovisa",
        "profile_url": "https://www.linkedin.com/in/valerielovisa",
        "is_sample": False,
        "platform": "linkedin",
        "verticals": ["perfume", "fragrance", "beauty", "sustainability", "cosmetics"],
        "tone_tags": ["educational", "analytical", "artisanal", "product-focused"],
        "followers_count": 28000,
        "engagement_rate": 4.1,
        "min_rate": 600,
        "currency": "USD",
        "acceptance_rate": 0.9,
        "on_time_rate": 0.95,
        "bio": "ISIPCA-trained global perfumery educator & founder of ABTYS consultancy.",
        "email": "",
        "approval_status": "approved",
    },
    {
        "id": "cr_real_mauricio_x",
        "name": "Mauricio Abrego",
        "handle": "@mauricioabrego",
        "profile_url": "https://twitter.com/mauricioabrego",
        "is_sample": False,
        "platform": "twitter_x",
        "verticals": ["perfume", "fragrance", "fashion", "lifestyle", "ugc"],
        "tone_tags": ["storytelling", "evocative", "confident", "product-focused"],
        "followers_count": 32000,
        "engagement_rate": 4.2,
        "min_rate": 450,
        "currency": "USD",
        "acceptance_rate": 0.92,
        "on_time_rate": 0.95,
        "bio": "Fashion & fragrance creator sharing lifestyle scent reviews, unboxings, and UGC.",
        "email": "",
        "approval_status": "approved",
    },
    {
        "id": "cr_real_mikaria_x",
        "name": "Mikaria Janae",
        "handle": "@MikariaJ",
        "profile_url": "https://twitter.com/MikariaJ",
        "is_sample": False,
        "platform": "twitter_x",
        "verticals": ["perfume", "fragrance", "beauty", "niche", "vintage"],
        "tone_tags": ["artisanal", "evocative", "educational", "confident"],
        "followers_count": 24000,
        "engagement_rate": 4.6,
        "min_rate": 380,
        "currency": "USD",
        "acceptance_rate": 0.93,
        "on_time_rate": 0.96,
        "bio": "Beauty & fragrance content creator publishing fragrance breakdowns and scent reviews on X.",
        "email": "",
        "approval_status": "approved",
    },
]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def parse_campaign_prompt(prompt: str, overrides: dict | None = None) -> dict:
    """Extract structured platforms, budget, creator count, timeline, tone, and deliverables."""
    overrides = overrides or {}
    text = (prompt or "").strip()
    lower = text.lower()

    # Platforms
    platforms = list(overrides.get("platforms") or [])
    if not platforms:
        if any(tok in lower for tok in ("twitter", " x ", "on x", "tweet", "thread")):
            platforms.append("twitter_x")
        if "linkedin" in lower or "carousel" in lower:
            platforms.append("linkedin")
        if "youtube" in lower or "yt " in lower:
            platforms.append("youtube")
        if "instagram" in lower or "ig " in lower or "reel" in lower:
            platforms.append("instagram")
        if not platforms:
            platforms = ["twitter_x", "linkedin"]

    # Budget & currency
    budget = overrides.get("budget")
    currency = overrides.get("currency")
    if budget is None:
        m_explicit = re.search(
            r"(?:budget(?:\s+is)?\s*[:=]?\s*(?:[$₹€£]|usd|inr)?\s*|([$₹€£]|usd|inr)\s*)([0-9][0-9,]*(?:\.[0-9]+)?)\s*(k|thousand)?",
            lower,
        )
        m_suffix = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(k|thousand)?\s*budget", lower)
        match = m_explicit or m_suffix
        if match:
            if m_explicit:
                sym = (m_explicit.group(1) or ("₹" if "₹" in m_explicit.group(0) or "inr" in m_explicit.group(0) else "$")).lower()
                num_raw = m_explicit.group(2)
                k_tok = m_explicit.group(3)
            else:
                sym = "inr" if ("₹" in lower or "inr" in lower) else "$"
                num_raw = m_suffix.group(1)
                k_tok = m_suffix.group(2)
            num = float(num_raw.replace(",", ""))
            if k_tok:
                num *= 1000.0
            budget = round(num, 2)
            currency = "INR" if ("₹" in sym or "inr" in sym) else "USD"
    if not budget or float(budget) <= 0:
        budget = 2500.0
    if not currency:
        currency = "USD"

    # Creator count
    creator_count = overrides.get("creator_count")
    if not creator_count:
        mc = re.search(r"([0-9]+)\s*(?:creators?|influencers?)", lower)
        creator_count = int(mc.group(1)) if mc else 5
    creator_count = max(1, min(int(creator_count), 12))

    # Timeline days
    timeline_days = overrides.get("timeline_days")
    if not timeline_days:
        if "two-week" in lower or "two week" in lower or "2 week" in lower or "2-week" in lower:
            timeline_days = 14
        elif "one week" in lower or "1 week" in lower or "weekly" in lower:
            timeline_days = 7
        elif "month" in lower:
            timeline_days = 30
        else:
            md = re.search(r"([0-9]+)\s*(?:days?|weeks?)", lower)
            if md:
                val = int(md.group(1))
                timeline_days = val * 7 if "week" in md.group(0) else val
            else:
                timeline_days = 14

    # Deliverables per platform
    deliverables = dict(overrides.get("deliverables") or {})
    if "twitter_x" in platforms and not deliverables.get("twitter_x"):
        if "quote" in lower:
            deliverables["twitter_x"] = "quote_tweet"
        elif "retweet" in lower or "repost" in lower:
            deliverables["twitter_x"] = "retweet"
        elif "tweet" in lower and "thread" not in lower:
            deliverables["twitter_x"] = "tweet"
        else:
            deliverables["twitter_x"] = "thread"

    if "linkedin" in platforms and not deliverables.get("linkedin"):
        if "video" in lower:
            deliverables["linkedin"] = "video"
        elif "repost" in lower:
            deliverables["linkedin"] = "repost"
        elif "standalone" in lower or ("post" in lower and "carousel" not in lower):
            deliverables["linkedin"] = "post"
        else:
            deliverables["linkedin"] = "carousel"

    if "youtube" in platforms and not deliverables.get("youtube"):
        deliverables["youtube"] = "short" if "short" in lower else "review_video"

    if "instagram" in platforms and not deliverables.get("instagram"):
        deliverables["instagram"] = "story" if "story" in lower else "reel"

    # Tone
    tone = (overrides.get("tone") or "").strip()
    if not tone:
        tone_match = re.search(r"([a-zA-Z-]+)\s+tone", lower)
        tone = tone_match.group(1) if tone_match else "product-focused"

    start_date = overrides.get("start_date") or (_now_utc() + timedelta(days=MIN_LEAD_DAYS)).date().isoformat()
    goal = (overrides.get("goal") or text or f"Promote flagship offerings across {', '.join(platforms)}").strip()

    return {
        "platforms": platforms,
        "deliverables": deliverables,
        "budget": float(budget),
        "currency": currency,
        "creator_count": creator_count,
        "timeline_days": int(timeline_days),
        "start_date": start_date,
        "tone": tone,
        "goal": goal,
    }


def validate_start_date(start_date_str: str) -> tuple[bool, str]:
    """Enforce >=2 days lead time so creators have time to accept outreach."""
    try:
        target = datetime.fromisoformat(start_date_str).date()
        min_date = (_now_utc() + timedelta(days=MIN_LEAD_DAYS)).date()
        if target < min_date:
            return False, f"Campaign start_date must be at least {MIN_LEAD_DAYS} days ahead ({min_date.isoformat()} or later)."
        return True, ""
    except Exception:
        return False, "Invalid start_date format; expected YYYY-MM-DD."


def _extract_section_excerpt(txt: str, max_chars: int = 700) -> str:
    if not txt:
        return ""
    cleaned_lines = [
        line.strip() for line in txt.splitlines()
        if line.strip() and not line.strip().startswith(("===", "---"))
    ]
    return "\n".join(cleaned_lines)[:max_chars]


def build_kb_campaign_brief(dossier: dict | None, spec: dict) -> tuple[str, dict]:
    """Synthesize a grounded Influencer Campaign Brief from the brand's 5 .txt Knowledge Base docs."""
    dossier = dossier or {}
    brand = dossier.get("brand") or {}
    domain = brand.get("domain") or "brand"
    brand_name = brand.get("brand_name") or domain
    ck = brand.get("collective_knowledge") or {}

    docs_list = dossier.get("documents") or []
    docs_by_key = {}
    for d in docs_list:
        key = d.get("doc_key") or (d.get("filename") or "").replace(".txt", "")
        if key:
            docs_by_key[key] = d
    if not docs_by_key and isinstance(ck.get("documents"), dict):
        for fname, dmeta in ck["documents"].items():
            docs_by_key[fname.replace(".txt", "")] = dmeta

    product_txt = (docs_by_key.get("product_info") or {}).get("content_txt", "")
    voice_txt = (docs_by_key.get("brand_voice") or {}).get("content_txt", "")
    comp_txt = (docs_by_key.get("competitor_analysis") or {}).get("content_txt", "")
    content_txt = (docs_by_key.get("content_strategy") or {}).get("content_txt", "")

    event_brief = ck.get("event_brief") or brand.get("description") or ""
    featured_items = (ck.get("catalog_showcase") or {}).get("featured_items") or []
    price_summary = brand.get("price_summary") or {}

    deliverable_summary = ", ".join(
        f"{p}: {d}" for p, d in (spec.get("deliverables") or {}).items()
    )

    brief_lines = [
        f"INFLUENCER CAMPAIGN BRIEF — {brand_name} ({domain})",
        f"Goal & Angle    : {spec.get('goal')}",
        f"Platforms       : {', '.join(spec.get('platforms') or [])}",
        f"Deliverables    : {deliverable_summary}",
        f"Budget & Window : {spec.get('currency')} {spec.get('budget'):g} | {spec.get('timeline_days')} days (Start: {spec.get('start_date')})",
        f"Requested Tone  : {spec.get('tone')}",
        "",
        "1. BRAND & PRODUCT GROUND TRUTH (from product_info.txt)",
        event_brief or _extract_section_excerpt(product_txt, 500) or f"Target site: {brand.get('primary_url', domain)}",
    ]

    if featured_items:
        brief_lines.append("Key SKUs to Spotlight:")
        for fi in featured_items[:5]:
            p_str = f" ({fi['price']})" if fi.get("price") else ""
            brief_lines.append(f"  - {fi.get('title')}{p_str}")

    if price_summary.get("min") is not None:
        cur = price_summary.get("currency") or ""
        brief_lines.append(f"Price Tiering: {cur} {price_summary['min']:g} - {price_summary['max']:g} (avg {price_summary['avg']:g})")

    brief_lines.extend([
        "",
        "2. BRAND VOICE & MESSAGING GUARDRAILS (from brand_voice.txt)",
        _extract_section_excerpt(voice_txt, 550) or f"Keep tone {spec.get('tone')} and authentic to {brand_name}.",
        "",
        "3. COMPETITIVE DIFFERENTIATION & CONTENT HOOKS (from competitor_analysis.txt + content_strategy.txt)",
        _extract_section_excerpt(content_txt or comp_txt, 550) or "Highlight unique quality, pricing transparency, and real customer experience.",
    ])

    kb_snapshot = {
        "brand_domain": domain,
        "brand_name": brand_name,
        "docs_used": sorted(docs_by_key.keys()),
        "featured_count": len(featured_items),
        "niche_query": brand.get("niche_query") or "",
    }
    return "\n".join(brief_lines), kb_snapshot


def _collect_brand_signals(dossier: dict) -> tuple[set[str], set[str]]:
    """Collect normalized category keywords and tone keywords from a brand dossier."""
    brand = (dossier or {}).get("brand") or {}
    ck = brand.get("collective_knowledge") or {}
    ps = brand.get("price_summary") or {}
    items = (dossier or {}).get("items") or []

    text_blobs = [
        brand.get("domain", ""),
        brand.get("brand_name", ""),
        brand.get("niche_query", ""),
        brand.get("description", ""),
        ck.get("event_brief", ""),
    ]
    for cat in ps.get("categories") or []:
        if isinstance(cat, dict) and cat.get("name"):
            text_blobs.append(cat["name"])
    for it in items[:25]:
        text_blobs.append(it.get("category") or "")
        text_blobs.append(it.get("title") or "")

    vertical_tokens = set(re.findall(r"[a-zA-Z]{3,}", " ".join(text_blobs).lower()))

    # Extract tone tags from brand_voice.txt if available
    docs = dossier.get("documents") or []
    voice_txt = ""
    for d in docs:
        if d.get("doc_key") == "brand_voice" or d.get("filename") == "brand_voice.txt":
            voice_txt = d.get("content_txt") or ""
            break
    tone_tokens = set(re.findall(r"[a-zA-Z-]{4,}", voice_txt.lower()))
    return vertical_tokens, tone_tokens


def score_and_match_creators(
    creators: list[dict],
    dossier: dict | None,
    spec: dict,
    exclude_ids: set[str] | None = None,
) -> dict:
    """Rank creators using Reach, Vertical fit, Tone match, Reliability, and Budget fit."""
    exclude_ids = exclude_ids or set()
    platforms = spec.get("platforms") or ["twitter_x"]
    deliverables = spec.get("deliverables") or {}
    total_budget = float(spec.get("budget") or 2500.0)
    creator_count = int(spec.get("creator_count") or 5)
    per_creator_budget = total_budget / max(1, creator_count)
    campaign_tone = (spec.get("tone") or "").lower()
    _generic_goal_words = {
        "launch", "campaign", "creators", "creator", "budget", "timeline", "week",
        "weeks", "days", "tone", "twitter", "linkedin", "youtube", "instagram",
        "thread", "tweet", "carousel", "post", "video", "reel", "review", "reviews",
        "promote", "product", "focused", "brand", "with", "for", "and", "the",
    }
    goal_tokens = {
        w for w in re.findall(r"[a-zA-Z]{3,}", (spec.get("goal") or "").lower())
        if w not in _generic_goal_words
    }

    vertical_tokens, voice_tokens = _collect_brand_signals(dossier or {})
    target_verticals = (vertical_tokens | goal_tokens) - _generic_goal_words

    scored = []
    for cr in creators:
        if cr.get("approval_status", "approved") != "approved":
            continue
        if cr.get("id") in exclude_ids:
            continue
        plat = cr.get("platform")
        if plat not in platforms:
            continue

        # 1. Reach (0 - 25)
        followers = int(cr.get("followers_count") or 0)
        reach_score = min(25.0, round(math.log10(max(followers, 100)) * 5.0, 2))

        # 2. Vertical fit (0 - 30)
        cr_verts = {v.lower() for v in (cr.get("verticals") or [])}
        cr_bio_tokens = set(re.findall(r"[a-zA-Z]{3,}", (cr.get("bio") or "").lower()))
        overlap = len((cr_verts | cr_bio_tokens) & target_verticals)
        vertical_score = min(30.0, round(overlap * 7.5, 2))

        # 3. Tone match (0 - 20)
        cr_tones = {t.lower() for t in (cr.get("tone_tags") or [])}
        tone_hits = sum(1 for t in cr_tones if t in campaign_tone or t in voice_tokens)
        tone_score = min(20.0, round(tone_hits * 7.0 + (6.0 if campaign_tone in cr_tones else 0.0), 2))

        # 4. Reliability (0 - 15)
        acc = float(cr.get("acceptance_rate") or 0.85)
        ont = float(cr.get("on_time_rate") or 0.9)
        reliability_score = round(((acc + ont) / 2.0) * 15.0, 2)

        # 5. Budget fit (0 - 10; penalize rather than exclude when min_rate exceeds slice)
        min_rate = float(cr.get("min_rate") or 0)
        if min_rate <= per_creator_budget:
            budget_score = 10.0
        else:
            over_ratio = (min_rate - per_creator_budget) / max(per_creator_budget, 1.0)
            budget_score = max(0.0, round(10.0 - over_ratio * 10.0, 2))

        total_score = round(reach_score + vertical_score + tone_score + reliability_score + budget_score, 2)
        fee = round(max(min_rate, per_creator_budget) if min_rate <= per_creator_budget * 1.25 else min_rate, 2)

        scored.append({
            "creator": cr,
            "deliverable_type": deliverables.get(plat) or SUPPORTED_DELIVERABLES[plat][0],
            "match_score": total_score,
            "score_breakdown": {
                "reach": reach_score,
                "vertical_fit": vertical_score,
                "tone_match": tone_score,
                "reliability": reliability_score,
                "budget_fit": budget_score,
            },
            "agreed_fee": fee,
        })

    scored.sort(key=lambda x: x["match_score"], reverse=True)
    shortlist = scored[:creator_count]

    # Compute platform budget split & creator count estimates
    platform_split = {}
    for plat in platforms:
        plat_items = [s for s in shortlist if s["creator"]["platform"] == plat]
        platform_split[plat] = {
            "creators_estimated": len(plat_items),
            "estimated_spend": round(sum(s["agreed_fee"] for s in plat_items), 2),
            "deliverable": deliverables.get(plat) or SUPPORTED_DELIVERABLES[plat][0],
        }

    return {
        "shortlist": shortlist,
        "platform_split": platform_split,
        "per_creator_budget": round(per_creator_budget, 2),
    }


def generate_outreach_email(creator: dict, campaign: dict, deliverable: str, fee: float) -> str:
    """Create personalized creator outreach message referencing brand brief & 48h window."""
    return (
        f"Subject: Creator Partnership: {campaign.get('title')} ({creator.get('platform')})\n\n"
        f"Hi {creator.get('name')},\n\n"
        f"We selected you ({creator.get('handle')}) for {campaign.get('brand_domain')}'s upcoming campaign.\n"
        f"- Deliverable: {deliverable} on {creator.get('platform')}\n"
        f"- Reserved Fee: {campaign.get('currency', 'USD')} {fee:g}\n"
        f"- Timeline: {campaign.get('timeline_days', 14)} days (Starts {campaign.get('start_date')})\n"
        f"- Response Window: Please accept or decline within 48 hours.\n\n"
        f"Campaign Brief & Voice Guardrails:\n"
        f"{campaign.get('brief_text', '')[:1200]}\n"
    )


def build_campaign_entities(brand_domain: str, prompt: str, dossier: dict | None, creators: list[dict], overrides: dict | None = None) -> tuple[dict, list[dict], list[dict]]:
    """Construct campaign row, shortlisted campaign_creator rows, and initial agent log message."""
    spec = parse_campaign_prompt(prompt, overrides)
    brief_text, kb_snapshot = build_kb_campaign_brief(dossier, spec)
    brand_name = kb_snapshot.get("brand_name") or brand_domain
    matched = score_and_match_creators(creators, dossier, spec)
    now_iso = _now_iso()
    campaign_id = f"camp_{uuid.uuid4().hex[:8]}"

    campaign = {
        "id": campaign_id,
        "brand_domain": brand_domain,
        "title": (overrides or {}).get("title") or f"{brand_name} Influencer Launch",
        "goal": spec["goal"],
        "platforms": spec["platforms"],
        "deliverables": spec["deliverables"],
        "budget": spec["budget"],
        "currency": spec["currency"],
        "commission_pct": COMMISSION_PCT,
        "timeline_days": spec["timeline_days"],
        "start_date": spec["start_date"],
        "tone": spec["tone"],
        "source_post_url": "",
        "source_post_locked_at": None,
        "status": "shortlist_ready",
        "payment_status": "unpaid",
        "wallet_applied": 0.0,
        "brief_text": brief_text,
        "kb_snapshot": {**kb_snapshot, "platform_split": matched["platform_split"]},
        "created_at": now_iso,
        "updated_at": now_iso,
    }

    cc_rows = []
    for item in matched["shortlist"]:
        cr = item["creator"]
        cc_rows.append({
            "id": f"cc_{uuid.uuid4().hex[:8]}",
            "campaign_id": campaign_id,
            "creator_id": cr["id"],
            "creator_name": cr["name"],
            "creator_handle": cr["handle"],
            "creator_profile_url": cr.get("profile_url") or "",
            "platform": cr["platform"],
            "deliverable_type": item["deliverable_type"],
            "match_score": item["match_score"],
            "score_breakdown": {
                **item["score_breakdown"],
                "profile_url": cr.get("profile_url") or "",
                "is_sample": bool(cr.get("is_sample", not bool(cr.get("profile_url")))),
            },
            "agreed_fee": item["agreed_fee"],
            "status": "shortlisted",
            "outreach_email": generate_outreach_email(cr, campaign, item["deliverable_type"], item["agreed_fee"]),
            "outreach_expires_at": None,
            "submission_url": "",
            "submission_notes": "",
            "auto_approve_at": None,
            "payout_status": "pending",
            "created_at": now_iso,
            "updated_at": now_iso,
        })

    initial_messages = [{
        "campaign_id": campaign_id,
        "creator_id": None,
        "sender_role": "agent",
        "message": (
            f"Campaign brief compiled from Knowledge Base docs ({', '.join(kb_snapshot.get('docs_used') or ['overview'])}). "
            f"Shortlisted {len(cc_rows)} creator(s) across {', '.join(spec['platforms'])}. Approve shortlist to unlock Funding."
        ),
        "created_at": now_iso,
    }]
    return campaign, cc_rows, initial_messages
