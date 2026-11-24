import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
import aiohttp

logger = logging.getLogger("supabase_client")

BASE_DIR = Path(__file__).resolve().parent

def load_env():
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

def get_headers(key):
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }

async def check_supabase_health():
    url, key = get_supabase_config()
    rest_url = f"{url}/rest/v1"
    headers = get_headers(key)
    
    status = {
        "connected": False,
        "url": url,
        "tables": {},
        "missing_tables": [],
        "ready": False
    }

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            # Probe extractions table
            for tbl in ["extractions", "scraped_items", "competitors"]:
                check_url = f"{rest_url}/{tbl}?select=id&limit=1"
                try:
                    async with session.get(check_url, headers=headers) as resp:
                        if resp.status in (200, 206):
                            status["tables"][tbl] = "ready"
                        elif resp.status == 404:
                            status["tables"][tbl] = "missing"
                            status["missing_tables"].append(tbl)
                        else:
                            status["tables"][tbl] = f"error_{resp.status}"
                except Exception as e:
                    status["tables"][tbl] = str(e)

            status["connected"] = True
            status["ready"] = len(status["missing_tables"]) == 0
    except Exception as e:
        status["error"] = str(e)

    return status

async def save_extraction_to_supabase(
    job_id: str,
    url: str,
    overview: dict = None,
    items: list = None,
    competitors: list = None,
    status: str = "completed",
    max_pages: int = 1,
    competitor_query: str = None
):
    base_url, key = get_supabase_config()
    rest_url = f"{base_url}/rest/v1"
    headers = get_headers(key)

    overview = overview or {}
    items = items or []
    competitors = competitors or []

    result = {
        "success": False,
        "job_id": job_id,
        "stored_extractions": 0,
        "stored_items": 0,
        "stored_competitors": 0,
        "errors": []
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        # 1. Upsert extraction record
        now_iso = datetime.now(timezone.utc).isoformat()
        extraction_payload = [{
            "id": job_id,
            "url": url,
            "status": status,
            "item_count": len(items),
            "max_pages": max_pages,
            "overview": overview,
            "competitor_query": competitor_query or "",
            "created_at": now_iso
        }]

        try:
            upsert_headers = dict(headers)
            upsert_headers["Prefer"] = "resolution=merge-duplicates"
            async with session.post(
                f"{rest_url}/extractions",
                json=extraction_payload,
                headers=upsert_headers
            ) as resp:
                if resp.status in (200, 201, 204):
                    result["stored_extractions"] = 1
                else:
                    err_text = await resp.text()
                    result["errors"].append(f"extractions table: HTTP {resp.status} - {err_text}")
        except Exception as e:
            result["errors"].append(f"extractions insert exception: {e}")

        # 2. Insert scraped items (in chunks of 50)
        if items:
            items_payload = []
            for item in items:
                items_payload.append({
                    "extraction_id": job_id,
                    "title": item.get("title") or item.get("name") or "Item",
                    "price": str(item.get("price") or item.get("price_inr") or ""),
                    "category": item.get("category"),
                    "image": item.get("image") or item.get("image_url"),
                    "link": item.get("link"),
                    "rating": str(item.get("rating")) if item.get("rating") is not None else None,
                    "source_page": item.get("source_page") or url,
                    "raw_data": item,
                    "created_at": now_iso
                })

            chunk_size = 50
            for i in range(0, len(items_payload), chunk_size):
                chunk = items_payload[i:i + chunk_size]
                try:
                    async with session.post(
                        f"{rest_url}/scraped_items",
                        json=chunk,
                        headers=headers
                    ) as resp:
                        if resp.status in (200, 201, 204):
                            result["stored_items"] += len(chunk)
                        else:
                            err_text = await resp.text()
                            result["errors"].append(f"scraped_items table: HTTP {resp.status} - {err_text}")
                except Exception as e:
                    result["errors"].append(f"scraped_items insert exception: {e}")

        # 3. Insert competitors
        if competitors:
            comp_payload = []
            for c in competitors:
                comp_payload.append({
                    "extraction_id": job_id,
                    "name": c.get("name") or c.get("domain"),
                    "domain": c.get("domain"),
                    "url": c.get("url"),
                    "snippet": c.get("snippet"),
                    "badge": c.get("badge") or "Competitor",
                    "created_at": now_iso
                })

            try:
                async with session.post(
                    f"{rest_url}/competitors",
                    json=comp_payload,
                    headers=headers
                ) as resp:
                    if resp.status in (200, 201, 204):
                        result["stored_competitors"] = len(comp_payload)
                    else:
                        err_text = await resp.text()
                        result["errors"].append(f"competitors table: HTTP {resp.status} - {err_text}")
            except Exception as e:
                result["errors"].append(f"competitors insert exception: {e}")

    result["success"] = (
        result["stored_extractions"] > 0
        or result["stored_items"] > 0
        or result["stored_competitors"] > 0
    )
    return result
