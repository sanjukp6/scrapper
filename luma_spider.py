import re
import json
from urllib.parse import urljoin
import scrapy

class LumaScentsSpider(scrapy.Spider):
    name = "luma_scents"
    start_urls = ["https://luma-scents.vercel.app/"]

    custom_settings = {
        "USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "ROBOTSTXT_OBEY": False,
        "LOG_LEVEL": "INFO",
    }

    def parse(self, response):
        self.logger.info(f"Crawled landing page: {response.url}")
        
        # Scrape page title and meta description
        title = response.css("title::text").get()
        description = response.css("meta[name='description']::attr(content)").get()
        self.logger.info(f"Page Title: {title}")

        # Find the compiled React/Vite bundle script
        js_scripts = response.css("script[src*='assets/index']::attr(src)").getall()
        if not js_scripts:
            # Fallback to any index JS in assets
            js_scripts = response.css("script::attr(src)").re(r".*assets/index.*\.js")

        for script_url in js_scripts:
            full_url = urljoin(response.url, script_url)
            self.logger.info(f"Found frontend bundle: {full_url}")
            yield scrapy.Request(
                full_url,
                callback=self.parse_bundle,
                meta={"site_title": title, "site_desc": description}
            )

    def parse_bundle(self, response):
        self.logger.info("Extracting product catalog from React application bundle...")
        text = response.text

        # Regex to extract structured product definitions
        # Pattern captures: id, name, category, price, description, etc.
        pattern = re.compile(
            r'\{id:"(?P<id>[^"]+)",name:"(?P<name>[^"]+)",brand:"(?P<brand>[^"]*)",category:"(?P<category>[^"]*)",variant:"(?P<variant>[^"]*)",(?:originalPrice:(?P<original_price>\d+),)?price:(?P<price>\d+)(?:,discountPercent:(?P<discount>\d+))?,(?:isOffer:[!0-9a-zA-Z]+,offerTag:"(?P<offer_tag>[^"]*)",)?description:"(?P<desc>[^"]*)",image:"(?P<image>[^"]*)",stock:(?P<stock>\d+),topNotes:\[(?P<top>[^\]]*)\],heartNotes:\[(?P<heart>[^\]]*)\],baseNotes:\[(?P<base>[^\]]*)\],mood:"(?P<mood>[^"]*)",rating:(?P<rating>[0-9\.]+),reviewCount:(?P<reviews>\d+)'
        )

        found_count = 0
        for m in pattern.finditer(text):
            found_count += 1
            d = m.groupdict()

            def clean_notes(s):
                if not s:
                    return []
                return [n.strip('" \t') for n in s.split(",") if n.strip('" \t')]

            yield {
                "id": d.get("id"),
                "name": d.get("name"),
                "brand": d.get("brand") or "Luma Scents",
                "category": d.get("category"),
                "variant": d.get("variant"),
                "price_inr": int(d["price"]) if d.get("price") else None,
                "original_price": int(d["original_price"]) if d.get("original_price") else None,
                "rating": float(d["rating"]) if d.get("rating") else None,
                "review_count": int(d["reviews"]) if d.get("reviews") else None,
                "mood": d.get("mood"),
                "description": d.get("desc"),
                "top_notes": clean_notes(d.get("top")),
                "heart_notes": clean_notes(d.get("heart")),
                "base_notes": clean_notes(d.get("base")),
                "image_url": d.get("image"),
            }

        self.logger.info(f"Extracted {found_count} artisanal perfume products successfully.")
