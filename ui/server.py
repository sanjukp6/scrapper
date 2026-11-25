import os
import sys
import json
import csv
import io
import uuid
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from aiohttp import web

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

try:
    from supabase_client import (
        save_extraction_to_supabase,
        check_supabase_health,
        get_supabase_config,
        list_brands,
        get_brand_dossier,
        list_brand_events,
        backfill_extractions_to_brands,
        get_brand_documents,
        regenerate_brand_documents,
    )
except ImportError:
    save_extraction_to_supabase = None
    check_supabase_health = None
    get_supabase_config = None
    list_brands = None
    get_brand_dossier = None
    list_brand_events = None
    backfill_extractions_to_brands = None
    get_brand_documents = None
    regenerate_brand_documents = None

try:
    from influencer_store import (
        list_creators,
        register_creator,
        set_creator_approval,
        discover_creators_for_brand,
        create_campaign,
        list_campaigns,
        get_campaign_detail,
        transition_campaign,
        transition_campaign_creator,
        post_campaign_message,
    )
except ImportError:
    list_creators = None
    register_creator = None
    set_creator_approval = None
    discover_creators_for_brand = None
    create_campaign = None
    list_campaigns = None
    get_campaign_detail = None
    transition_campaign = None
    transition_campaign_creator = None
    post_campaign_message = None

SCRAPY_EXE = BASE_DIR / "scrapy" / ".venv" / "Scripts" / "scrapy.exe"
RUNS_DIR = Path(__file__).resolve().parent / "runs"
STATIC_DIR = Path(__file__).resolve().parent

RUNS_DIR.mkdir(parents=True, exist_ok=True)

# In-memory storage for active and completed crawl jobs
jobs = {}

def get_now_iso():
    return datetime.now(timezone.utc).isoformat()

async def get_request_data(request):
    try:
        return await request.json()
    except Exception:
        try:
            text = await request.text()
            return json.loads(text)
        except Exception:
            return {}

class CrawlJob:
    def __init__(self, job_id, spider_path, job_type="custom", spider_args=None):
        self.id = job_id
        self.job_type = job_type
        self.spider_path = spider_path
        self.spider_args = spider_args or []
        self.output_json = RUNS_DIR / f"output_{job_id}.json"
        self.status = "queued"
        self.started_at = None
        self.finished_at = None
        self.logs = []
        self.items = []
        self.overview = {}
        self.competitors = []
        self.competitor_query = ""
        self.json_ld = []
        self.listeners = []
        self.error = None

    def add_log(self, line):
        self.logs.append(line)
        for queue in list(self.listeners):
            try:
                queue.put_nowait({"type": "log", "line": line})
            except Exception:
                pass

    def finish(self, status, error=None):
        self.status = status
        self.error = error
        self.finished_at = get_now_iso()
        if self.output_json.exists():
            try:
                with open(self.output_json, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        raw_data = json.loads(content)
                        if isinstance(raw_data, list):
                            for obj in raw_data:
                                if isinstance(obj, dict):
                                    t = obj.get("type")
                                    if t == "site_overview":
                                        self.overview = obj.get("overview", {})
                                    elif t == "discovered_competitors":
                                        self.competitors = obj.get("competitors", [])
                                        if obj.get("query"):
                                            self.competitor_query = obj.get("query", "")
                                    elif t == "website_extraction":
                                        self.overview = obj.get("overview", {})
                                        self.items.extend(obj.get("items", []))
                                        self.competitors = obj.get("competitors", [])
                                    else:
                                        self.items.append(obj)
                        elif isinstance(raw_data, dict):
                            self.items = [raw_data]
            except Exception as e:
                self.add_log(f"[UI ERROR] Failed to parse output JSON: {e}")

        for queue in list(self.listeners):
            try:
                queue.put_nowait({
                    "type": "done",
                    "status": self.status,
                    "item_count": len(self.items),
                    "error": self.error
                })
            except Exception:
                pass

async def run_spider_process(job: CrawlJob):
    job.status = "running"
    job.started_at = get_now_iso()
    job.add_log(f"[*] Starting crawl job {job.id}...")
    job.add_log(f"[*] Executable: {SCRAPY_EXE}")
    job.add_log(f"[*] Spider File: {job.spider_path}")
    job.add_log(f"[*] Output File: {job.output_json}")

    cmd = [
        str(SCRAPY_EXE),
        "runspider",
        str(job.spider_path),
        "-o",
        str(job.output_json),
        "-s", "TELNETCONSOLE_ENABLED=0",
        "-s", "REMOTE_CONTROL_ENABLED=0",
    ]
    if job.spider_args:
        cmd.extend(job.spider_args)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL
        )

        async def read_stream():
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                if decoded:
                    if "COMPETITORS_FOUND::" in decoded:
                        try:
                            payload_str = decoded.split("COMPETITORS_FOUND::", 1)[1].strip()
                            comp_data = json.loads(payload_str)
                            job.competitors = comp_data.get("competitors", [])
                            query_str = comp_data.get("query", "")
                            if query_str:
                                job.competitor_query = query_str
                            for queue in list(job.listeners):
                                try:
                                    queue.put_nowait({
                                        "type": "competitors",
                                        "competitors": job.competitors,
                                        "query": query_str
                                    })
                                except Exception:
                                    pass
                        except Exception as e:
                            job.add_log(f"[UI ERROR] Live competitor parse failed: {e}")
                    elif "SITE_OVERVIEW_FOUND::" in decoded:
                        try:
                            payload_str = decoded.split("SITE_OVERVIEW_FOUND::", 1)[1].strip()
                            job.overview = json.loads(payload_str)
                            for queue in list(job.listeners):
                                try:
                                    queue.put_nowait({
                                        "type": "overview",
                                        "overview": job.overview
                                    })
                                except Exception:
                                    pass
                        except Exception as e:
                            job.add_log(f"[UI ERROR] Live overview parse failed: {e}")
                    job.add_log(decoded)

        reader_task = asyncio.create_task(read_stream())
        try:
            returncode = await asyncio.wait_for(proc.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            proc.kill()
            returncode = -1
            job.add_log("[!] Crawl timed out after 60 seconds.")

        await reader_task

        if returncode == 0:
            job.finish("completed")
            job.add_log(f"[✓] Crawl finished successfully! Found {len(job.items)} items.")
            if save_extraction_to_supabase:
                asyncio.create_task(sync_job_to_supabase(job))
        else:
            job.finish("failed", error=f"Process exited with code {returncode}")
            job.add_log(f"[!] Crawl failed with exit code {returncode}")
    except Exception as e:
        job.finish("failed", error=str(e))
        job.add_log(f"[!] Exception running crawler: {e}")

async def sync_job_to_supabase(job):
    if not save_extraction_to_supabase:
        return

    target_url = ""
    max_pages = 1
    competitor_query = getattr(job, "competitor_query", "") or ""
    for arg in job.spider_args:
        if arg.startswith("target_url="):
            target_url = arg.split("target_url=", 1)[1]
        elif arg.startswith("max_pages="):
            try:
                max_pages = int(arg.split("max_pages=", 1)[1])
            except Exception:
                pass

    if not target_url and job.overview:
        target_url = job.overview.get("url", "")
    if not target_url and job.items:
        target_url = job.items[0].get("source_page", "")
    if not target_url:
        target_url = "https://unknown"

    try:
        job.add_log("[*] Storing extraction & building collective brand knowledge in Supabase...")
        res = await save_extraction_to_supabase(
            job_id=job.id,
            url=target_url,
            overview=job.overview,
            items=job.items,
            competitors=job.competitors,
            status=job.status,
            max_pages=max_pages,
            competitor_query=competitor_query
        )
        if res.get("success"):
            agg = res.get("brand_aggregation") or {}
            if agg.get("brand_saved"):
                job.add_log(
                    f"[✓] Collective Brand KB updated for {agg.get('brand_domain')} "
                    f"({agg.get('brand_items_upserted')} items, event: {agg.get('event_type')})"
                )
            else:
                job.add_log(
                    f"[✓] Stored raw extraction to Supabase: {res.get('stored_items')} items, "
                    f"{res.get('stored_competitors')} competitors."
                )
        else:
            errs = "; ".join(res.get("errors", []))
            job.add_log(f"[!] Supabase note: {errs}")
            if "Could not find the table" in errs or "missing" in errs:
                job.add_log("[!] Please execute schema_supabase.sql in Supabase SQL Editor to initialize tables.")
    except Exception as e:
        job.add_log(f"[!] Supabase sync exception: {e}")

async def handle_index(request):
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return web.Response(text="index.html not found", status=404)
    return web.FileResponse(index_file)

async def handle_status(request):
    return web.json_response({
        "status": "online",
        "scrapy_version": "2.20.0",
        "scrapy_path": str(SCRAPY_EXE),
        "exists": SCRAPY_EXE.exists(),
        "active_jobs": sum(1 for j in jobs.values() if j.status == "running"),
        "total_jobs": len(jobs)
    })

async def handle_list_spiders(request):
    files = []
    for ext in ("*.py",):
        for p in BASE_DIR.glob(ext):
            if p.name != "sample_spider.py" and not p.name.endswith("_spider.py"):
                continue
            files.append({
                "name": p.name,
                "path": str(p),
                "size": p.stat().st_size
            })
    return web.json_response({"spiders": files})

async def handle_run_existing(request):
    data = await get_request_data(request)
    file_name = data.get("file_name", "sample_spider.py")
    spider_path = BASE_DIR / file_name
    if not spider_path.exists():
        return web.json_response({"error": f"File {file_name} not found"}, status=404)

    job_id = str(uuid.uuid4())[:8]
    job = CrawlJob(job_id, spider_path, job_type="existing")
    jobs[job_id] = job
    asyncio.create_task(run_spider_process(job))
    return web.json_response({"job_id": job_id})

async def handle_run_custom_code(request):
    data = await get_request_data(request)
    code = data.get("code", "").strip()
    if not code:
        return web.json_response({"error": "No spider code provided"}, status=400)

    job_id = str(uuid.uuid4())[:8]
    temp_spider = RUNS_DIR / f"spider_{job_id}.py"
    with open(temp_spider, "w", encoding="utf-8") as f:
        f.write(code)

    job = CrawlJob(job_id, temp_spider, job_type="custom_code")
    jobs[job_id] = job
    asyncio.create_task(run_spider_process(job))
    return web.json_response({"job_id": job_id})

async def handle_quick_scrape(request):
    data = await get_request_data(request)
    url = data.get("url", "").strip()
    container = data.get("container", "").strip()
    fields = data.get("fields", [])

    if not url:
        return web.json_response({"error": "Target URL is required"}, status=400)

    job_id = str(uuid.uuid4())[:8]
    temp_spider = RUNS_DIR / f"quick_spider_{job_id}.py"

    field_lines = []
    for f in fields:
        fname = f.get("name", "field").replace(" ", "_").replace("-", "_")
        fsel = f.get("selector", "")
        ftype = f.get("type", "text")
        if ftype == "all":
            field_lines.append(f'                "{fname}": item.css("{fsel}").getall(),')
        elif ftype == "attr":
            attr_name = f.get("attr", "href")
            field_lines.append(f'                "{fname}": item.css("{fsel}::attr({attr_name})").get(),')
        else:
            if "::" in fsel:
                field_lines.append(f'                "{fname}": item.css("{fsel}").get(),')
            else:
                field_lines.append(f'                "{fname}": item.css("{fsel}::text").get(),')

    fields_code = "\n".join(field_lines) if field_lines else '                "raw_text": item.css("::text").get(),'

    if "luma-scents" in url:
        spider_code = f"""import re
from urllib.parse import urljoin
import scrapy

class QuickSpider(scrapy.Spider):
    name = "quick_{job_id}"
    start_urls = ["{url}"]
    custom_settings = {{
        "USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "ROBOTSTXT_OBEY": False,
        "LOG_LEVEL": "INFO",
    }}

    def parse(self, response):
        self.logger.info("Detected Luma Scents React SPA landing page...")
        js_scripts = response.css("script[src*='assets/index']::attr(src)").getall()
        if not js_scripts:
            js_scripts = response.css("script::attr(src)").re(r".*assets/index.*\\.js")

        for s in js_scripts:
            full_url = urljoin(response.url, s)
            self.logger.info(f"Following React asset bundle: {{full_url}}")
            yield scrapy.Request(full_url, callback=self.parse_bundle)

    def parse_bundle(self, response):
        pattern = re.compile(
            r'\\{{id:"(?P<id>[^"]+)",name:"(?P<name>[^"]+)",brand:"(?P<brand>[^"]*)",category:"(?P<category>[^"]*)",variant:"(?P<variant>[^"]*)",(?:originalPrice:(?P<original_price>\\d+),)?price:(?P<price>\\d+)(?:,discountPercent:(?P<discount>\\d+))?,(?:isOffer:[!0-9a-zA-Z]+,offerTag:"(?P<offer_tag>[^"]*)",)?description:"(?P<desc>[^"]*)",image:"(?P<image>[^"]*)",stock:(?P<stock>\\d+),topNotes:\\[(?P<top>[^\\]]*)\\],heartNotes:\\[(?P<heart>[^\\]]*)\\],baseNotes:\\[(?P<base>[^\\]]*)\\],mood:"(?P<mood>[^"]*)",rating:(?P<rating>[0-9\\.]+),reviewCount:(?P<reviews>\\d+)'
        )
        count = 0
        for m in pattern.finditer(response.text):
            count += 1
            d = m.groupdict()
            def clean_notes(s):
                return [n.strip('" \\t') for n in s.split(",") if n.strip('" \\t')] if s else []

            yield {{
                "id": d.get("id"),
                "name": d.get("name"),
                "brand": d.get("brand") or "Luma Scents",
                "category": d.get("category"),
                "variant": d.get("variant"),
                "price_inr": int(d["price"]) if d.get("price") else None,
                "rating": float(d["rating"]) if d.get("rating") else None,
                "mood": d.get("mood"),
                "description": d.get("desc"),
                "top_notes": clean_notes(d.get("top")),
                "heart_notes": clean_notes(d.get("heart")),
                "base_notes": clean_notes(d.get("base")),
                "image_url": d.get("image"),
            }}
        self.logger.info(f"Successfully scraped {{count}} items from Luma Scents.")
"""
    elif container:
        parse_code = f"""
    def parse(self, response):
        items = response.css("{container}")
        if items:
            for item in items:
                yield {{
{fields_code}
                }}
        else:
            self.logger.warning("No elements matched container: '{container}'. Checking page metadata...")
            title = response.css("title::text").get()
            desc = response.css("meta[name='description']::attr(content)").get()
            yield {{"page_title": title, "meta_description": desc, "note": "No elements matched CSS selector '{container}'. Check if site is a JavaScript/React SPA."}}
"""
        spider_code = f"""import scrapy

class QuickSpider(scrapy.Spider):
    name = "quick_{job_id}"
    start_urls = ["{url}"]
    custom_settings = {{
        "USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "ROBOTSTXT_OBEY": False,
        "LOG_LEVEL": "INFO",
    }}
{parse_code}
"""
    else:
        parse_code = f"""
    def parse(self, response):
        yield {{
{fields_code}
        }}
"""
        spider_code = f"""import scrapy

class QuickSpider(scrapy.Spider):
    name = "quick_{job_id}"
    start_urls = ["{url}"]
    custom_settings = {{
        "USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "ROBOTSTXT_OBEY": False,
        "LOG_LEVEL": "INFO",
    }}
{parse_code}
"""
    with open(temp_spider, "w", encoding="utf-8") as f:
        f.write(spider_code)

    job = CrawlJob(job_id, temp_spider, job_type="quick")
    jobs[job_id] = job
    asyncio.create_task(run_spider_process(job))
    return web.json_response({"job_id": job_id})

async def handle_stream(request):
    job_id = request.match_info.get("job_id")
    job = jobs.get(job_id)
    if not job:
        return web.Response(text="Job not found", status=404)

    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        }
    )
    await response.prepare(request)

    queue = asyncio.Queue()
    job.listeners.append(queue)

    try:
        # First send historical logs
        for log in job.logs:
            data = json.dumps({"type": "log", "line": log})
            await response.write(f"data: {data}\n\n".encode("utf-8"))

        if job.competitors:
            comp_data = json.dumps({"type": "competitors", "competitors": job.competitors})
            await response.write(f"data: {comp_data}\n\n".encode("utf-8"))

        if job.overview:
            ov_data = json.dumps({"type": "overview", "overview": job.overview})
            await response.write(f"data: {ov_data}\n\n".encode("utf-8"))

        if job.status in ("completed", "failed"):
            done_data = json.dumps({
                "type": "done",
                "status": job.status,
                "item_count": len(job.items),
                "error": job.error
            })
            await response.write(f"data: {done_data}\n\n".encode("utf-8"))
            return response

        while True:
            msg = await queue.get()
            data = json.dumps(msg)
            await response.write(f"data: {data}\n\n".encode("utf-8"))
            if msg.get("type") == "done":
                break
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        if queue in job.listeners:
            job.listeners.remove(queue)

    return response

async def handle_universal_scrape(request):
    data = await get_request_data(request)
    url = data.get("url", "").strip()
    if not url:
        return web.json_response({"error": "Website URL is required"}, status=400)

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    universal_spider_file = BASE_DIR / "universal_extractor.py"
    if not universal_spider_file.exists():
        return web.json_response({"error": "universal_extractor.py not found"}, status=500)

    max_pages = data.get("max_pages", 5)
    job_id = str(uuid.uuid4())[:8]
    job = CrawlJob(
        job_id,
        universal_spider_file,
        job_type="universal",
        spider_args=["-a", f"target_url={url}", "-a", f"max_pages={max_pages}"]
    )
    jobs[job_id] = job
    asyncio.create_task(run_spider_process(job))
    return web.json_response({"job_id": job_id})

async def handle_get_results(request):
    job_id = request.match_info.get("job_id")
    job = jobs.get(job_id)
    if not job:
        return web.json_response({"error": "Job not found"}, status=404)

    return web.json_response({
        "job_id": job.id,
        "status": job.status,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "item_count": len(job.items),
        "overview": job.overview,
        "items": job.items,
        "competitors": job.competitors,
        "json_ld": job.json_ld,
        "logs": job.logs,
        "error": job.error
    })

async def handle_export(request):
    job_id = request.match_info.get("job_id")
    fmt = request.query.get("format", "json").lower()
    job = jobs.get(job_id)
    if not job:
        return web.Response(text="Job not found", status=404)

    export_items = job.items if job.items else ([job.overview] if job.overview else [])
    if fmt == "csv":
        output = io.StringIO()
        if export_items:
            # Flatten keys
            all_keys = []
            for item in export_items:
                for k in item.keys():
                    if k not in all_keys:
                        all_keys.append(k)
            writer = csv.DictWriter(output, fieldnames=all_keys)
            writer.writeheader()
            for item in export_items:
                row = {}
                for k in all_keys:
                    val = item.get(k, "")
                    if isinstance(val, (list, dict)):
                        val = json.dumps(val, ensure_ascii=False)
                    row[k] = val
                writer.writerow(row)
        csv_data = output.getvalue()
        return web.Response(
            text=csv_data,
            content_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="scraped_{job_id}.csv"'}
        )
    else:
        payload = {
            "overview": job.overview,
            "items": job.items,
            "item_count": len(job.items)
        } if job.overview else job.items
        json_data = json.dumps(payload, indent=2, ensure_ascii=False)
        return web.Response(
            text=json_data,
            content_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="scraped_{job_id}.json"'}
        )

async def handle_latest_results(request):
    # Check for the latest successful job with items or overview
    for job in reversed(list(jobs.values())):
        if job.status == "completed" and (job.items or job.overview):
            return web.json_response({
                "job_id": job.id,
                "status": job.status,
                "item_count": len(job.items),
                "overview": job.overview,
                "items": job.items,
                "competitors": job.competitors
            })
    
    # Fallback to luma_products.json in BASE_DIR if exists
    luma_file = BASE_DIR / "luma_products.json"
    if luma_file.exists():
        try:
            with open(luma_file, "r", encoding="utf-8") as f:
                items = json.load(f)
                return web.json_response({
                    "job_id": "cached_luma",
                    "status": "completed",
                    "item_count": len(items),
                    "items": items
                })
        except Exception:
            pass

    return web.json_response({"items": [], "item_count": 0})

async def handle_supabase_status(request):
    if not check_supabase_health:
        return web.json_response({"connected": False, "error": "supabase_client not available"})
    status = await check_supabase_health()
    return web.json_response(status)

async def handle_supabase_sync(request):
    job_id = request.match_info.get("job_id")
    job = jobs.get(job_id)
    if not job:
        for j in reversed(list(jobs.values())):
            if j.items or j.overview:
                job = j
                break
    if not job:
        return web.json_response({"error": "Job not found to sync"}, status=404)

    target_url = job.overview.get("url") if job.overview else ""
    if not target_url and job.items:
        target_url = job.items[0].get("source_page", "")
    if not target_url:
        target_url = "https://unknown"

    res = await save_extraction_to_supabase(
        job_id=job.id,
        url=target_url,
        overview=job.overview,
        items=job.items,
        competitors=job.competitors,
        status=job.status,
        competitor_query=getattr(job, "competitor_query", "") or ""
    )
    return web.json_response(res)

async def handle_supabase_schema(request):
    schema_file = BASE_DIR / "schema_supabase.sql"
    if schema_file.exists():
        with open(schema_file, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/plain")
    return web.Response(text="schema_supabase.sql not found", status=404)

async def handle_list_brands(request):
    if not list_brands:
        return web.json_response({"brands": [], "count": 0, "error": "supabase_client unavailable"})
    q = request.query.get("q", "").strip()
    data = await list_brands(search_query=q)
    return web.json_response(data)

async def handle_get_brand_dossier(request):
    if not get_brand_dossier:
        return web.json_response({"error": "supabase_client unavailable"}, status=500)
    domain = request.match_info.get("domain", "")
    dossier = await get_brand_dossier(domain)
    if not dossier:
        return web.json_response({"error": f"Brand '{domain}' not found"}, status=404)
    return web.json_response(dossier)

async def handle_export_brand_dossier(request):
    if not get_brand_dossier:
        return web.Response(text="supabase_client unavailable", status=500)
    domain = request.match_info.get("domain", "")
    fmt = request.query.get("format", "json").lower()
    dossier = await get_brand_dossier(domain)
    if not dossier:
        return web.Response(text="Brand not found", status=404)

    brand = dossier["brand"]
    safe_domain = brand.get("domain", domain).replace(".", "_")
    if fmt == "md":
        md_text = brand.get("knowledge_markdown") or ""
        return web.Response(
            text=md_text,
            content_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{safe_domain}_knowledge.md"'}
        )
    else:
        payload = {
            "collective_knowledge": brand.get("collective_knowledge") or {},
            "items": dossier.get("items", []),
            "competitors": dossier.get("competitors", []),
        }
        return web.Response(
            text=json.dumps(payload, indent=2, ensure_ascii=False),
            content_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{safe_domain}_knowledge.json"'}
        )

async def handle_list_brand_events(request):
    if not list_brand_events:
        return web.json_response({"events": [], "count": 0, "error": "supabase_client unavailable"})
    domain = request.query.get("domain", "").strip()
    data = await list_brand_events(domain=domain)
    return web.json_response(data)

async def handle_backfill_brands(request):
    if not backfill_extractions_to_brands:
        return web.json_response({"error": "supabase_client unavailable"}, status=500)
    res = await backfill_extractions_to_brands()
    return web.json_response(res)

async def handle_list_brand_documents(request):
    if not get_brand_documents:
        return web.json_response({"documents": [], "error": "supabase_client unavailable"}, status=500)
    domain = request.match_info.get("domain", "")
    docs = await get_brand_documents(domain)
    return web.json_response({"domain": domain, "documents": docs, "count": len(docs)})

async def handle_get_brand_document_txt(request):
    if not get_brand_documents:
        return web.Response(text="supabase_client unavailable", status=500)
    domain = request.match_info.get("domain", "")
    doc_key = request.match_info.get("doc_key", "").replace(".txt", "").strip().lower()
    docs = await get_brand_documents(domain)
    for d in docs:
        if d.get("doc_key") == doc_key or (d.get("filename") or "").replace(".txt", "").lower() == doc_key:
            fname = d.get("filename") or f"{doc_key}.txt"
            return web.Response(
                text=d.get("content_txt") or "",
                content_type="text/plain",
                charset="utf-8",
                headers={"Content-Disposition": f'inline; filename="{fname}"'}
            )
    return web.Response(text=f"Document '{doc_key}.txt' not found for '{domain}'", status=404)

async def handle_regenerate_brand_documents(request):
    if not regenerate_brand_documents:
        return web.json_response({"error": "supabase_client unavailable"}, status=500)
    domain = request.match_info.get("domain", "")
    res = await regenerate_brand_documents(domain)
    return web.json_response(res)

# ================= INFLUENCER AGENT HANDLERS =================
async def handle_influencer_list_creators(request):
    if not list_creators:
        return web.json_response({"creators": [], "error": "influencer_store unavailable"}, status=500)
    plat = request.query.get("platform", "").strip()
    status = request.query.get("approval_status", "").strip()
    return web.json_response(await list_creators(platform=plat, approval_status=status))

async def handle_influencer_register_creator(request):
    if not register_creator:
        return web.json_response({"error": "influencer_store unavailable"}, status=500)
    data = await get_request_data(request)
    res = await register_creator(data)
    status_code = 400 if res.get("error") else 200
    return web.json_response(res, status=status_code)

async def handle_influencer_approve_creator(request):
    if not set_creator_approval:
        return web.json_response({"error": "influencer_store unavailable"}, status=500)
    cid = request.match_info.get("creator_id", "")
    data = await get_request_data(request)
    st = (data.get("approval_status") or "approved").strip()
    creator = await set_creator_approval(cid, st)
    if not creator:
        return web.json_response({"error": "Creator not found or invalid status"}, status=404)
    return web.json_response({"creator": creator})

async def handle_influencer_discover(request):
    if not discover_creators_for_brand:
        return web.json_response({"error": "influencer_store unavailable"}, status=500)
    data = await get_request_data(request)
    domain = (data.get("brand_domain") or "").strip()
    query = (data.get("query") or "").strip()
    platforms = data.get("platforms") or None
    if not domain and not query:
        return web.json_response({"error": "Provide brand_domain or niche query"}, status=400)
    res = await discover_creators_for_brand(brand_domain=domain, custom_query=query, platforms=platforms)
    return web.json_response(res)

async def handle_influencer_list_campaigns(request):
    if not list_campaigns:
        return web.json_response({"campaigns": [], "error": "influencer_store unavailable"}, status=500)
    domain = request.query.get("brand_domain", "").strip()
    return web.json_response(await list_campaigns(brand_domain=domain))

async def handle_influencer_create_campaign(request):
    if not create_campaign:
        return web.json_response({"error": "influencer_store unavailable"}, status=500)
    data = await get_request_data(request)
    domain = (data.get("brand_domain") or "").strip()
    prompt = (data.get("prompt") or "").strip()
    overrides = data.get("overrides") or {
        k: data[k] for k in ("platforms", "deliverables", "budget", "currency", "creator_count", "timeline_days", "start_date", "tone", "goal", "title")
        if data.get(k) is not None
    }
    if not domain:
        return web.json_response({"error": "brand_domain is required"}, status=400)
    if data.get("auto_discover"):
        overrides["auto_discover"] = True
    res = await create_campaign(domain, prompt, overrides=overrides)
    if res.get("error"):
        return web.json_response(res, status=400)
    return web.json_response(res)

async def handle_influencer_get_campaign(request):
    if not get_campaign_detail:
        return web.json_response({"error": "influencer_store unavailable"}, status=500)
    cid = request.match_info.get("campaign_id", "")
    detail = await get_campaign_detail(cid)
    if not detail:
        return web.json_response({"error": "Campaign not found"}, status=404)
    return web.json_response(detail)

async def handle_influencer_campaign_action(request, action_name: str):
    if not transition_campaign:
        return web.json_response({"error": "influencer_store unavailable"}, status=500)
    cid = request.match_info.get("campaign_id", "")
    data = await get_request_data(request)
    res = await transition_campaign(cid, action_name, data)
    if res.get("error"):
        return web.json_response(res, status=400)
    return web.json_response(res)

async def handle_influencer_creator_action(request):
    if not transition_campaign_creator:
        return web.json_response({"error": "influencer_store unavailable"}, status=500)
    cid = request.match_info.get("campaign_id", "")
    cr_id = request.match_info.get("creator_id", "")
    data = await get_request_data(request)
    action = (data.get("action") or "").strip()
    res = await transition_campaign_creator(cid, cr_id, action, data)
    if res.get("error"):
        return web.json_response(res, status=400)
    return web.json_response(res)

async def handle_influencer_post_message(request):
    if not post_campaign_message:
        return web.json_response({"error": "influencer_store unavailable"}, status=500)
    cid = request.match_info.get("campaign_id", "")
    data = await get_request_data(request)
    res = await post_campaign_message(
        cid,
        sender_role=data.get("sender_role") or "brand",
        message=data.get("message") or "",
        creator_id=data.get("creator_id"),
    )
    if res.get("error"):
        return web.json_response(res, status=400)
    return web.json_response(res)

def create_app():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/spiders", handle_list_spiders)
    app.router.add_get("/api/latest-results", handle_latest_results)
    app.router.add_get("/api/supabase/status", handle_supabase_status)
    app.router.add_post("/api/supabase/sync/{job_id}", handle_supabase_sync)
    app.router.add_get("/api/supabase/schema", handle_supabase_schema)
    app.router.add_get("/api/brands", handle_list_brands)
    app.router.add_post("/api/brands/backfill", handle_backfill_brands)
    app.router.add_get("/api/brands/{domain}", handle_get_brand_dossier)
    app.router.add_get("/api/brands/{domain}/export", handle_export_brand_dossier)
    app.router.add_get("/api/brands/{domain}/documents", handle_list_brand_documents)
    app.router.add_post("/api/brands/{domain}/documents/generate", handle_regenerate_brand_documents)
    app.router.add_get("/api/brands/{domain}/documents/{doc_key}", handle_get_brand_document_txt)
    app.router.add_get("/api/events", handle_list_brand_events)
    app.router.add_get("/api/influencer/creators", handle_influencer_list_creators)
    app.router.add_post("/api/influencer/creators", handle_influencer_register_creator)
    app.router.add_post("/api/influencer/creators/{creator_id}/approve", handle_influencer_approve_creator)
    app.router.add_post("/api/influencer/discover", handle_influencer_discover)
    app.router.add_get("/api/influencer/campaigns", handle_influencer_list_campaigns)
    app.router.add_post("/api/influencer/campaigns", handle_influencer_create_campaign)
    app.router.add_get("/api/influencer/campaigns/{campaign_id}", handle_influencer_get_campaign)
    for act in ("approve", "fund", "rematch", "resend", "cancel"):
        app.router.add_post(f"/api/influencer/campaigns/{{campaign_id}}/{act}", lambda r, _a=act: handle_influencer_campaign_action(r, _a))
    app.router.add_post("/api/influencer/campaigns/{campaign_id}/source-url", lambda r: handle_influencer_campaign_action(r, "source_url"))
    app.router.add_post("/api/influencer/campaigns/{campaign_id}/creators/{creator_id}/action", handle_influencer_creator_action)
    app.router.add_post("/api/influencer/campaigns/{campaign_id}/messages", handle_influencer_post_message)
    app.router.add_post("/api/scrape/universal", handle_universal_scrape)
    app.router.add_post("/api/scrape/quick", handle_quick_scrape)
    app.router.add_post("/api/scrape/file", handle_run_existing)
    app.router.add_post("/api/scrape/code", handle_run_custom_code)
    app.router.add_get("/api/jobs/{job_id}/stream", handle_stream)
    app.router.add_get("/api/jobs/{job_id}/results", handle_get_results)
    app.router.add_get("/api/jobs/{job_id}/export", handle_export)
    return app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app = create_app()
    print(f"[*] Starting Scrapy UI on http://127.0.0.1:{port}")
    web.run_app(app, host="127.0.0.1", port=port)
