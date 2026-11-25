import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import aiohttp

from brand_aggregator import (
    normalize_domain,
    get_primary_url,
    clean_brand_name,
    normalize_item_row,
    deduplicate_item_rows,
    merge_brand_contacts,
    build_brand_knowledge,
    is_navigation_noise,
)
from brand_docs_generator import generate_five_brand_documents

logger = logging.getLogger("supabase_client")

BASE_DIR = Path(__file__).resolve().parent

ALL_TABLES = [
    "extractions",
    "scraped_items",
    "competitors",
    "brands",
    "brand_items",
    "brand_competitors",
    "brand_events",
    "brand_documents",
]


def load_env():
    """Load key=value pairs from .env into os.environ if not already defined."""
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v


load_env()


def get_supabase_config():
    """Return (supabase_url, supabase_key) from environment variables."""
    load_env()
    url = (
        os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
        or os.environ.get("SUPABASE_URL")
        or "https://twwapvttxabmdvlzkuef.supabase.co"
    ).rstrip("/")
    key = (
        os.environ.get("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or "sb_publishable_PH3ZPvEao55OGaR1ZQRUUw_7BpmGTfM"
    )
    return url, key


def get_headers(key, prefer="return=minimal"):
    """Return standard PostgREST headers."""
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


async def _rest_call(session: aiohttp.ClientSession, method: str, url: str, headers: dict, payload=None):
    """Execute a PostgREST call and return (ok, status_code, body_data_or_text)."""
    try:
        async with session.request(method, url, json=payload, headers=headers) as resp:
            text = await resp.text()
            ok = resp.status in (200, 201, 204, 206)
            if not text:
                return ok, resp.status, None
            try:
                return ok, resp.status, json.loads(text)
            except Exception:
                return ok, resp.status, text
    except Exception as e:
        return False, 0, str(e)


async def check_supabase_health():
    """Verify Supabase connectivity and readiness across raw and brand KB tables."""
    url, key = get_supabase_config()
    rest_url = f"{url}/rest/v1"
    headers = get_headers(key, prefer="return=representation")

    status = {
        "connected": False,
        "url": url,
        "tables": {},
        "missing_tables": [],
        "ready": False,
    }

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            for tbl in ALL_TABLES:
                check_url = f"{rest_url}/{tbl}?select=*&limit=1"
                ok, code, _ = await _rest_call(session, "GET", check_url, headers)
                if ok:
                    status["tables"][tbl] = "ready"
                elif code == 404:
                    status["tables"][tbl] = "missing"
                    status["missing_tables"].append(tbl)
                else:
                    status["tables"][tbl] = f"error_{code}"

            status["connected"] = True
            status["ready"] = len(status["missing_tables"]) == 0
    except Exception as e:
        status["error"] = str(e)

    return status


async def _dispatch_event_webhook(session: aiohttp.ClientSession, event_payload: dict) -> dict | None:
    """Optionally push brand event snapshot to EVENT_WEBHOOK_URL if set in .env."""
    load_env()
    webhook_url = (os.environ.get("EVENT_WEBHOOK_URL") or "").strip()
    if not webhook_url:
        return None
    try:
        async with session.post(webhook_url, json=event_payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            return {"url": webhook_url, "status": resp.status}
    except Exception as e:
        return {"url": webhook_url, "error": str(e)}


async def _aggregate_brand_knowledge(
    session: aiohttp.ClientSession,
    rest_url: str,
    key: str,
    job_id: str,
    url: str,
    overview: dict,
    items: list,
    competitors: list,
    competitor_query: str,
    now_iso: str,
) -> dict:
    """Merge crawl output into brands, brand_items, brand_competitors, and emit brand_events."""
    domain = normalize_domain(url)
    primary_url = get_primary_url(url) or url
    read_headers = get_headers(key, prefer="return=representation")
    upsert_headers = get_headers(key, prefer="resolution=merge-duplicates,return=minimal")
    insert_headers = get_headers(key, prefer="return=minimal")

    summary = {
        "brand_domain": domain,
        "brand_saved": False,
        "brand_items_upserted": 0,
        "brand_competitors_upserted": 0,
        "event_emitted": False,
        "event_type": None,
        "webhook": None,
        "errors": [],
    }

    encoded_domain = quote(domain, safe="")

    # 1. Fetch existing brand record (if any)
    existing_brand = None
    ok, code, data = await _rest_call(
        session, "GET", f"{rest_url}/brands?domain=eq.{encoded_domain}&limit=1", read_headers
    )
    if ok and isinstance(data, list) and data:
        existing_brand = data[0]
    elif code == 404:
        summary["errors"].append("brands table missing (run updated schema_supabase.sql)")
        return summary

    is_new_brand = existing_brand is None
    event_type = "brand_created" if is_new_brand else "brand_updated"
    summary["event_type"] = event_type

    merged_emails, merged_socials = merge_brand_contacts(existing_brand, overview)
    brand_name = clean_brand_name(overview, domain)
    if existing_brand and existing_brand.get("brand_name") and brand_name.lower() == domain.lower():
        brand_name = existing_brand["brand_name"]

    description = (overview.get("description") or "").strip() or (existing_brand or {}).get("description", "")
    favicon = overview.get("favicon") or (existing_brand or {}).get("favicon", "")
    niche_query = (competitor_query or "").strip() or (existing_brand or {}).get("niche_query", "")
    is_spa = bool(overview.get("is_spa") or (existing_brand or {}).get("is_spa", False))
    crawl_count = int((existing_brand or {}).get("crawl_count") or 0) + 1

    # 2. Ensure parent brand row exists before inserting child brand_items/competitors/events
    stub_payload = [{
        "domain": domain,
        "brand_name": brand_name,
        "primary_url": primary_url,
        "niche_query": niche_query,
        "description": description,
        "favicon": favicon,
        "is_spa": is_spa,
        "emails": merged_emails,
        "social_links": merged_socials,
        "crawl_count": crawl_count,
        "last_crawled_at": now_iso,
        "created_at": (existing_brand or {}).get("created_at") or now_iso,
    }]
    ok, code, err = await _rest_call(
        session, "POST", f"{rest_url}/brands?on_conflict=domain", upsert_headers, stub_payload
    )
    if not ok:
        summary["errors"].append(f"brands stub upsert: HTTP {code} - {err}")
        return summary

    # 3. Deduplicate & upsert brand_items (filtering out navigation noise)
    normalized_items = deduplicate_item_rows([
        normalize_item_row(domain, it, url, now_iso)
        for it in (items or [])
        if isinstance(it, dict) and not is_navigation_noise(it)
    ])
    for i in range(0, len(normalized_items), 50):
        chunk = normalized_items[i : i + 50]
        ok, code, err = await _rest_call(
            session,
            "POST",
            f"{rest_url}/brand_items?on_conflict=brand_domain,item_key",
            upsert_headers,
            chunk,
        )
        if ok:
            summary["brand_items_upserted"] += len(chunk)
        else:
            summary["errors"].append(f"brand_items upsert: HTTP {code} - {err}")

    # 4. Deduplicate & upsert brand_competitors
    comp_map = {}
    for c in (competitors or []):
        if not isinstance(c, dict):
            continue
        comp_domain = normalize_domain(c.get("domain") or c.get("url") or "")
        if not comp_domain or comp_domain == "unknown" or comp_domain == domain:
            continue
        comp_map[comp_domain] = {
            "brand_domain": domain,
            "competitor_domain": comp_domain,
            "name": c.get("name") or comp_domain,
            "url": c.get("url") or get_primary_url(comp_domain),
            "snippet": c.get("snippet"),
            "badge": c.get("badge") or "Competitor",
            "discovered_at": now_iso,
        }
    comp_rows = list(comp_map.values())
    if comp_rows:
        ok, code, err = await _rest_call(
            session,
            "POST",
            f"{rest_url}/brand_competitors?on_conflict=brand_domain,competitor_domain",
            upsert_headers,
            comp_rows,
        )
        if ok:
            summary["brand_competitors_upserted"] = len(comp_rows)
        else:
            summary["errors"].append(f"brand_competitors upsert: HTTP {code} - {err}")

    # 5. Fetch full consolidated items & competitors for this brand to rebuild collective knowledge
    all_items = normalized_items
    ok, _, fetched_items = await _rest_call(
        session,
        "GET",
        f"{rest_url}/brand_items?brand_domain=eq.{encoded_domain}&order=last_seen_at.desc&limit=500",
        read_headers,
    )
    if ok and isinstance(fetched_items, list):
        all_items = fetched_items

    all_comps = comp_rows
    ok, _, fetched_comps = await _rest_call(
        session,
        "GET",
        f"{rest_url}/brand_competitors?brand_domain=eq.{encoded_domain}&order=discovered_at.desc&limit=100",
        read_headers,
    )
    if ok and isinstance(fetched_comps, list):
        all_comps = fetched_comps

    # 6. Compile collective knowledge + event_brief + knowledge_markdown + 5 .txt docs
    collective_knowledge, knowledge_markdown, price_summary, competitors_summary = build_brand_knowledge(
        domain=domain,
        brand_name=brand_name,
        primary_url=primary_url,
        niche_query=niche_query,
        description=description,
        favicon=favicon,
        is_spa=is_spa,
        emails=merged_emails,
        social_links=merged_socials,
        brand_items=all_items,
        brand_competitors=all_comps,
        crawl_count=crawl_count,
        last_crawled_at=now_iso,
    )

    # Preserve H1/H2/page_copy in brand_identity for Brand Voice generation
    prev_identity = ((existing_brand or {}).get("collective_knowledge") or {}).get("brand_identity") or {}
    merged_overview = {
        "h1": overview.get("h1") or prev_identity.get("h1") or [],
        "h2": overview.get("h2") or prev_identity.get("h2") or [],
        "page_copy": overview.get("page_copy") or prev_identity.get("page_copy") or "",
    }
    collective_knowledge["brand_identity"].update(merged_overview)

    # Fetch analytics & cross-brand items for Marketing Strategy, Competitor Analysis & Content Strategy
    gsc_rows = []
    ok, _, fetched_gsc = await _rest_call(session, "GET", f"{rest_url}/gsc_performance?select=*&limit=200", read_headers)
    if ok and isinstance(fetched_gsc, list):
        gsc_rows = [
            r for r in fetched_gsc
            if not (r.get("brand_domain") or r.get("domain"))
            or normalize_domain(r.get("brand_domain") or r.get("domain") or "") == domain
        ]

    ga_rows = []
    ok, _, fetched_ga = await _rest_call(session, "GET", f"{rest_url}/ga_sessions?select=*&limit=200", read_headers)
    if ok and isinstance(fetched_ga, list):
        ga_rows = [
            r for r in fetched_ga
            if not (r.get("brand_domain") or r.get("domain"))
            or normalize_domain(r.get("brand_domain") or r.get("domain") or "") == domain
        ]

    other_brand_items = {}
    ok, _, fetched_other = await _rest_call(
        session,
        "GET",
        f"{rest_url}/brand_items?brand_domain=neq.{encoded_domain}&select=*&limit=500",
        read_headers,
    )
    if ok and isinstance(fetched_other, list):
        for oit in fetched_other:
            odom = oit.get("brand_domain")
            if odom:
                other_brand_items.setdefault(odom, []).append(oit)

    brand_ctx = {
        "domain": domain,
        "brand_name": brand_name,
        "primary_url": primary_url,
        "niche_query": niche_query,
        "description": description,
        "price_summary": price_summary,
    }
    five_docs = generate_five_brand_documents(
        brand=brand_ctx,
        overview=merged_overview,
        items=all_items,
        competitors=all_comps,
        other_brand_items=other_brand_items,
        gsc_rows=gsc_rows,
        ga_rows=ga_rows,
        now_iso=now_iso,
    )

    # Store all 5 .txt documents inside collective_knowledge["documents"] AND public.brand_documents
    collective_knowledge["documents"] = {
        d["filename"]: {
            "doc_key": d["doc_key"],
            "title": d["title"],
            "status": d["status"],
            "data_sources": d["data_sources"],
            "gaps": d["gaps"],
            "content_txt": d["content_txt"],
            "generated_at": d["generated_at"],
        }
        for d in five_docs
    }

    final_brand_payload = [{
        "domain": domain,
        "brand_name": brand_name,
        "primary_url": primary_url,
        "niche_query": niche_query,
        "description": description,
        "favicon": favicon,
        "is_spa": is_spa,
        "emails": merged_emails,
        "social_links": merged_socials,
        "total_items": len(all_items),
        "price_summary": price_summary,
        "competitors_summary": competitors_summary,
        "collective_knowledge": collective_knowledge,
        "knowledge_markdown": knowledge_markdown,
        "crawl_count": crawl_count,
        "last_crawled_at": now_iso,
        "created_at": (existing_brand or {}).get("created_at") or now_iso,
    }]
    ok, code, err = await _rest_call(
        session, "POST", f"{rest_url}/brands?on_conflict=domain", upsert_headers, final_brand_payload
    )
    if ok:
        summary["brand_saved"] = True
    else:
        summary["errors"].append(f"brands dossier upsert: HTTP {code} - {err}")

    # 6b. Upsert each of the 5 .txt docs into public.brand_documents
    doc_rows = [
        {
            "brand_domain": domain,
            "doc_key": d["doc_key"],
            "filename": d["filename"],
            "title": d["title"],
            "status": d["status"],
            "data_sources": d["data_sources"],
            "gaps": d["gaps"],
            "content_txt": d["content_txt"],
            "generated_at": d["generated_at"],
        }
        for d in five_docs
    ]
    ok, code, err = await _rest_call(
        session,
        "POST",
        f"{rest_url}/brand_documents?on_conflict=brand_domain,doc_key",
        upsert_headers,
        doc_rows,
    )
    if ok:
        summary["documents_saved"] = len(doc_rows)
    else:
        summary["documents_saved"] = 0
        summary["errors"].append(f"brand_documents table upsert (stored inside brands.collective_knowledge.documents instead): HTTP {code} - {err}")

    # 7. Emit event snapshot into brand_events for downstream Events consumers
    event_record = [{
        "brand_domain": domain,
        "extraction_id": job_id,
        "event_type": event_type,
        "payload": collective_knowledge,
        "created_at": now_iso,
    }]
    ok, code, err = await _rest_call(
        session, "POST", f"{rest_url}/brand_events", insert_headers, event_record
    )
    if ok:
        summary["event_emitted"] = True
    else:
        summary["errors"].append(f"brand_events insert: HTTP {code} - {err}")

    # 8. Dispatch optional webhook
    webhook_res = await _dispatch_event_webhook(session, {
        "event_type": event_type,
        "brand_domain": domain,
        "extraction_id": job_id,
        "timestamp": now_iso,
        "payload": collective_knowledge,
    })
    if webhook_res:
        summary["webhook"] = webhook_res

    return summary


async def save_extraction_to_supabase(
    job_id: str,
    url: str,
    overview: dict = None,
    items: list = None,
    competitors: list = None,
    status: str = "completed",
    max_pages: int = 1,
    competitor_query: str = None,
):
    """Persist raw extraction audit trail and merge into the Brand Collective Knowledge Base."""
    base_url, key = get_supabase_config()
    rest_url = f"{base_url}/rest/v1"
    headers = get_headers(key)
    upsert_headers = get_headers(key, prefer="resolution=merge-duplicates,return=minimal")

    overview = overview or {}
    items = items or []
    competitors = competitors or []
    brand_domain = normalize_domain(url)

    result = {
        "success": False,
        "job_id": job_id,
        "brand_domain": brand_domain,
        "stored_extractions": 0,
        "stored_items": 0,
        "stored_competitors": 0,
        "brand_aggregation": {},
        "errors": [],
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        now_iso = datetime.now(timezone.utc).isoformat()
        extraction_payload = [{
            "id": job_id,
            "url": url,
            "brand_domain": brand_domain,
            "status": status,
            "item_count": len(items),
            "max_pages": max_pages,
            "overview": overview,
            "competitor_query": competitor_query or "",
            "created_at": now_iso,
        }]

        ok, code, err = await _rest_call(
            session, "POST", f"{rest_url}/extractions?on_conflict=id", upsert_headers, extraction_payload
        )
        if ok:
            result["stored_extractions"] = 1
        else:
            result["errors"].append(f"extractions table: HTTP {code} - {err}")

        # Raw scraped_items audit rows
        if items:
            items_payload = [
                {
                    "extraction_id": job_id,
                    "title": item.get("title") or item.get("name") or "Item",
                    "price": str(item.get("price") or item.get("price_inr") or ""),
                    "category": item.get("category"),
                    "image": item.get("image") or item.get("image_url"),
                    "link": item.get("link"),
                    "rating": str(item.get("rating")) if item.get("rating") is not None else None,
                    "source_page": item.get("source_page") or url,
                    "raw_data": item,
                    "created_at": now_iso,
                }
                for item in items
            ]
            for i in range(0, len(items_payload), 50):
                chunk = items_payload[i : i + 50]
                ok, code, err = await _rest_call(
                    session, "POST", f"{rest_url}/scraped_items", headers, chunk
                )
                if ok:
                    result["stored_items"] += len(chunk)
                else:
                    result["errors"].append(f"scraped_items table: HTTP {code} - {err}")

        # Raw competitors audit rows
        if competitors:
            comp_payload = [
                {
                    "extraction_id": job_id,
                    "name": c.get("name") or c.get("domain"),
                    "domain": c.get("domain"),
                    "url": c.get("url"),
                    "snippet": c.get("snippet"),
                    "badge": c.get("badge") or "Competitor",
                    "created_at": now_iso,
                }
                for c in competitors
            ]
            ok, code, err = await _rest_call(
                session, "POST", f"{rest_url}/competitors", headers, comp_payload
            )
            if ok:
                result["stored_competitors"] = len(comp_payload)
            else:
                result["errors"].append(f"competitors table: HTTP {code} - {err}")

        # Aggregate into Collective Brand Knowledge Base & emit brand_events
        agg = await _aggregate_brand_knowledge(
            session=session,
            rest_url=rest_url,
            key=key,
            job_id=job_id,
            url=url,
            overview=overview,
            items=items,
            competitors=competitors,
            competitor_query=competitor_query or "",
            now_iso=now_iso,
        )
        result["brand_aggregation"] = agg
        result["errors"].extend(agg.get("errors", []))

    result["success"] = (
        result["stored_extractions"] > 0
        or result["brand_aggregation"].get("brand_saved", False)
    )
    return result


async def list_brands(search_query: str = "", limit: int = 50) -> dict:
    """List collective brand knowledge profiles from public.brands."""
    base_url, key = get_supabase_config()
    rest_url = f"{base_url}/rest/v1"
    headers = get_headers(key, prefer="return=representation")

    select_cols = (
        "domain,brand_name,primary_url,niche_query,description,favicon,is_spa,"
        "emails,social_links,total_items,price_summary,competitors_summary,"
        "crawl_count,last_crawled_at,created_at"
    )
    query_url = f"{rest_url}/brands?select={select_cols}&order=last_crawled_at.desc&limit={int(limit)}"
    if search_query:
        q = quote(f"*{search_query.strip()}*", safe="")
        query_url += f"&or=(brand_name.ilike.{q},domain.ilike.{q},niche_query.ilike.{q})"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        ok, code, data = await _rest_call(session, "GET", query_url, headers)
        if ok and isinstance(data, list):
            return {"brands": data, "count": len(data)}
        return {"brands": [], "count": 0, "error": f"HTTP {code}: {data}"}


async def get_brand_dossier(domain: str) -> dict | None:
    """Fetch complete collective brand dossier, items, competitors, and recent events."""
    clean_domain = normalize_domain(domain)
    encoded = quote(clean_domain, safe="")
    base_url, key = get_supabase_config()
    rest_url = f"{base_url}/rest/v1"
    headers = get_headers(key, prefer="return=representation")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
        ok, code, brand_rows = await _rest_call(
            session, "GET", f"{rest_url}/brands?domain=eq.{encoded}&limit=1", headers
        )
        if not ok or not isinstance(brand_rows, list) or not brand_rows:
            return None

        brand = brand_rows[0]
        _, _, items = await _rest_call(
            session,
            "GET",
            f"{rest_url}/brand_items?brand_domain=eq.{encoded}&order=last_seen_at.desc&limit=500",
            headers,
        )
        _, _, comps = await _rest_call(
            session,
            "GET",
            f"{rest_url}/brand_competitors?brand_domain=eq.{encoded}&order=discovered_at.desc&limit=100",
            headers,
        )
        _, _, events = await _rest_call(
            session,
            "GET",
            f"{rest_url}/brand_events?brand_domain=eq.{encoded}&order=created_at.desc&limit=20",
            headers,
        )
        ok_docs, _, docs = await _rest_call(
            session,
            "GET",
            f"{rest_url}/brand_documents?brand_domain=eq.{encoded}&order=id.asc",
            headers,
        )
        if not ok_docs or not isinstance(docs, list) or not docs:
            ck_docs = (brand.get("collective_knowledge") or {}).get("documents") or {}
            docs = [
                {"filename": fname, **dmeta}
                for fname, dmeta in ck_docs.items()
            ]

        doc_order = {
            "product_info": 1,
            "brand_voice": 2,
            "marketing_strategy": 3,
            "competitor_analysis": 4,
            "content_strategy": 5,
        }
        docs = sorted(
            docs,
            key=lambda d: doc_order.get(
                d.get("doc_key") or (d.get("filename") or "").replace(".txt", ""),
                99,
            ),
        )

        return {
            "brand": brand,
            "items": items if isinstance(items, list) else [],
            "competitors": comps if isinstance(comps, list) else [],
            "events": events if isinstance(events, list) else [],
            "documents": docs,
        }


async def list_brand_events(domain: str = "", limit: int = 50) -> dict:
    """Fetch recent emitted brand knowledge events from public.brand_events."""
    base_url, key = get_supabase_config()
    rest_url = f"{base_url}/rest/v1"
    headers = get_headers(key, prefer="return=representation")

    url = f"{rest_url}/brand_events?select=*&order=created_at.desc&limit={int(limit)}"
    if domain:
        encoded = quote(normalize_domain(domain), safe="")
        url += f"&brand_domain=eq.{encoded}"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        ok, code, data = await _rest_call(session, "GET", url, headers)
        if ok and isinstance(data, list):
            return {"events": data, "count": len(data)}
        return {"events": [], "count": 0, "error": f"HTTP {code}: {data}"}


async def backfill_extractions_to_brands() -> dict:
    """Aggregate existing historical extractions in Supabase into the brands knowledge base."""
    base_url, key = get_supabase_config()
    rest_url = f"{base_url}/rest/v1"
    headers = get_headers(key, prefer="return=representation")

    summary = {"processed_extractions": 0, "brands_updated": set(), "errors": []}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        ok, code, extractions = await _rest_call(
            session, "GET", f"{rest_url}/extractions?order=created_at.asc&limit=100", headers
        )
        if not ok or not isinstance(extractions, list):
            return {"processed_extractions": 0, "brands_updated": [], "error": f"HTTP {code}: {extractions}"}

        for ext in extractions:
            ext_id = ext.get("id")
            ext_url = ext.get("url") or ""
            if not ext_id or not ext_url:
                continue

            enc_id = quote(str(ext_id), safe="")
            _, _, raw_items = await _rest_call(
                session, "GET", f"{rest_url}/scraped_items?extraction_id=eq.{enc_id}&limit=500", headers
            )
            _, _, raw_comps = await _rest_call(
                session, "GET", f"{rest_url}/competitors?extraction_id=eq.{enc_id}&limit=100", headers
            )

            items = []
            for it in (raw_items if isinstance(raw_items, list) else []):
                merged_it = dict(it.get("raw_data") or {})
                for col in ("title", "price", "category", "image", "link", "rating", "source_page", "inspired_by", "review_count", "stock_status"):
                    if it.get(col) not in (None, ""):
                        merged_it[col] = it[col]
                items.append(merged_it)
            comps = raw_comps if isinstance(raw_comps, list) else []

            agg = await _aggregate_brand_knowledge(
                session=session,
                rest_url=rest_url,
                key=key,
                job_id=str(ext_id),
                url=ext_url,
                overview=ext.get("overview") or {},
                items=items,
                competitors=comps,
                competitor_query=ext.get("competitor_query") or "",
                now_iso=ext.get("created_at") or datetime.now(timezone.utc).isoformat(),
            )
            summary["processed_extractions"] += 1
            if agg.get("brand_saved"):
                summary["brands_updated"].add(agg["brand_domain"])
            summary["errors"].extend(agg.get("errors", []))

    summary["brands_updated"] = sorted(summary["brands_updated"])
    return summary


async def get_brand_documents(domain: str) -> list[dict]:
    """Return the 5 .txt documents for a brand from brand_documents or collective_knowledge."""
    dossier = await get_brand_dossier(domain)
    if not dossier:
        return []
    return dossier.get("documents") or []


async def regenerate_brand_documents(domain: str) -> dict:
    """Recompute and save all 5 .txt documents in Supabase for an existing brand."""
    clean_domain = normalize_domain(domain)
    encoded = quote(clean_domain, safe="")
    base_url, key = get_supabase_config()
    rest_url = f"{base_url}/rest/v1"
    read_headers = get_headers(key, prefer="return=representation")
    upsert_headers = get_headers(key, prefer="resolution=merge-duplicates,return=minimal")
    insert_headers = get_headers(key, prefer="return=minimal")
    now_iso = datetime.now(timezone.utc).isoformat()

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        ok, code, brand_rows = await _rest_call(
            session, "GET", f"{rest_url}/brands?domain=eq.{encoded}&limit=1", read_headers
        )
        if not ok or not isinstance(brand_rows, list) or not brand_rows:
            return {"success": False, "error": f"Brand '{clean_domain}' not found (HTTP {code})"}

        brand = brand_rows[0]
        _, _, items = await _rest_call(
            session, "GET", f"{rest_url}/brand_items?brand_domain=eq.{encoded}&limit=500", read_headers
        )
        _, _, comps = await _rest_call(
            session, "GET", f"{rest_url}/brand_competitors?brand_domain=eq.{encoded}&limit=100", read_headers
        )
        _, _, other_items_raw = await _rest_call(
            session, "GET", f"{rest_url}/brand_items?brand_domain=neq.{encoded}&limit=500", read_headers
        )
        _, _, gsc_raw = await _rest_call(session, "GET", f"{rest_url}/gsc_performance?select=*&limit=200", read_headers)
        _, _, ga_raw = await _rest_call(session, "GET", f"{rest_url}/ga_sessions?select=*&limit=200", read_headers)

        other_brand_items = {}
        for oit in (other_items_raw if isinstance(other_items_raw, list) else []):
            odom = oit.get("brand_domain")
            if odom:
                other_brand_items.setdefault(odom, []).append(oit)

        gsc_rows = [
            r for r in (gsc_raw if isinstance(gsc_raw, list) else [])
            if not (r.get("brand_domain") or r.get("domain"))
            or normalize_domain(r.get("brand_domain") or r.get("domain") or "") == clean_domain
        ]
        ga_rows = [
            r for r in (ga_raw if isinstance(ga_raw, list) else [])
            if not (r.get("brand_domain") or r.get("domain"))
            or normalize_domain(r.get("brand_domain") or r.get("domain") or "") == clean_domain
        ]

        ck = dict(brand.get("collective_knowledge") or {})
        identity = ck.get("brand_identity") or {}
        overview = {
            "h1": identity.get("h1") or [],
            "h2": identity.get("h2") or [],
            "page_copy": identity.get("page_copy") or "",
        }
        five_docs = generate_five_brand_documents(
            brand=brand,
            overview=overview,
            items=items if isinstance(items, list) else [],
            competitors=comps if isinstance(comps, list) else [],
            other_brand_items=other_brand_items,
            gsc_rows=gsc_rows,
            ga_rows=ga_rows,
            now_iso=now_iso,
        )

        ck["documents"] = {
            d["filename"]: {
                "doc_key": d["doc_key"],
                "title": d["title"],
                "status": d["status"],
                "data_sources": d["data_sources"],
                "gaps": d["gaps"],
                "content_txt": d["content_txt"],
                "generated_at": d["generated_at"],
            }
            for d in five_docs
        }
        await _rest_call(
            session,
            "PATCH",
            f"{rest_url}/brands?domain=eq.{encoded}",
            insert_headers,
            {"collective_knowledge": ck, "last_crawled_at": now_iso},
        )

        doc_rows = [
            {
                "brand_domain": clean_domain,
                "doc_key": d["doc_key"],
                "filename": d["filename"],
                "title": d["title"],
                "status": d["status"],
                "data_sources": d["data_sources"],
                "gaps": d["gaps"],
                "content_txt": d["content_txt"],
                "generated_at": d["generated_at"],
            }
            for d in five_docs
        ]
        ok_docs, code_docs, err_docs = await _rest_call(
            session,
            "POST",
            f"{rest_url}/brand_documents?on_conflict=brand_domain,doc_key",
            upsert_headers,
            doc_rows,
        )
        return {
            "success": True,
            "brand_domain": clean_domain,
            "documents_count": len(five_docs),
            "brand_documents_table_saved": ok_docs,
            "brand_documents_note": None if ok_docs else f"HTTP {code_docs}: {err_docs}",
            "documents": five_docs,
        }
