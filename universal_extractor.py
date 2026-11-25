import re
import json
import urllib.parse
from urllib.parse import urljoin, urlparse
import scrapy

class UniversalSpider(scrapy.Spider):
    name = "universal"

    custom_settings = {
        "USER_AGENT": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "ROBOTSTXT_OBEY": False,
        "LOG_LEVEL": "INFO",
        "TELNETCONSOLE_ENABLED": False,
        "REMOTE_CONTROL_ENABLED": False,
        "REQUEST_FINGERPRINTER_IMPLEMENTATION": "2.7",
    }

    def __init__(self, target_url=None, max_pages=5, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if target_url:
            target_url = target_url.strip()
            if not target_url.startswith(("http://", "https://")):
                target_url = "https://" + target_url
            self.start_urls = [target_url]
        self.target_url = target_url
        self.max_pages = int(max_pages) if max_pages else 5
        self.visited_urls = set()
        self.scheduled_count = 1
        self.primary_overview = None
        self.competitors_searched = False

    def is_valid_internal_link(self, url, target_netloc):
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        base = target_netloc[4:] if target_netloc.startswith("www.") else target_netloc
        if netloc != base:
            return False

        path = parsed.path.lower()
        ignored_exts = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.pdf', '.zip', '.css', '.js', '.xml', '.ico')
        if any(path.endswith(ext) for ext in ignored_exts):
            return False
        if any(bad in path for bad in ['/login', '/cart', '/checkout', '/account', '/privacy', '/terms', '/logout', '/signup']):
            return False
        return True

    def find_next_links(self, response):
        target_netloc = urlparse(self.target_url).netloc.lower()
        links = []

        # 1. High priority: pagination
        pagination_selectors = [
            "a[rel='next']::attr(href)",
            "li.next a::attr(href)",
            ".next a::attr(href)",
            ".pagination a::attr(href)",
            "a:contains('Next')::attr(href)",
            "a:contains('next')::attr(href)",
            "a:contains('»')::attr(href)",
            "a[href*='page=']::attr(href)",
            "a[href*='/page/']::attr(href)",
        ]
        for sel in pagination_selectors:
            for href in response.css(sel).getall():
                full_url = urljoin(response.url, href)
                if self.is_valid_internal_link(full_url, target_netloc):
                    links.append(full_url)

        # 2. Medium priority: Catalog / Collection / Category / Products
        catalog_selectors = [
            "a[href*='/product']::attr(href)",
            "a[href*='/shop']::attr(href)",
            "a[href*='/collection']::attr(href)",
            "a[href*='/catalog']::attr(href)",
            "a[href*='/category']::attr(href)",
        ]
        for sel in catalog_selectors:
            for href in response.css(sel).getall():
                full_url = urljoin(response.url, href)
                if self.is_valid_internal_link(full_url, target_netloc):
                    links.append(full_url)

        # Deduplicate while preserving order
        seen = set()
        deduped = []
        for l in links:
            if l not in seen and l not in self.visited_urls:
                seen.add(l)
                deduped.append(l)
        return deduped

    def extract_page_items(self, response):
        candidate_selectors = [
            ".product", "[class*='product']", ".card", "[class*='card']",
            "article", ".quote", "div.quote", "[class*='item']",
            "li.product", "article.product_pod", "tr.athing", ".post", "[class*='post']"
        ]

        extracted = []
        for sel in candidate_selectors:
            raw_matches = response.css(sel)
            matches = []
            for m in raw_matches:
                has_matched_ancestor = False
                for p in m.xpath("ancestor::*"):
                    cls = p.attrib.get("class", "")
                    tag = p.root.tag
                    if sel.startswith(".") and sel[1:] in cls:
                        has_matched_ancestor = True
                        break
                    elif sel.startswith("[class*=") and sel.split("'")[1] in cls:
                        has_matched_ancestor = True
                        break
                    elif sel == tag:
                        has_matched_ancestor = True
                        break
                if not has_matched_ancestor:
                    matches.append(m)

            if 2 <= len(matches) <= 120:
                self.logger.info(f"Page {response.url}: Detected {len(matches)} items matching container '{sel}'")
                for el in matches:
                    primary_text = el.css("span.text::text, p.text::text, [class*='quote-text']::text").get()
                    author_text = el.css("small.author::text, [class*='author']::text, [class*='byline']::text").get()

                    candidate_titles = el.css("h1::text, h2::text, h3::text, h4::text, h3 a::attr(title), h2 a::attr(title), [class*='title']::text, [class*='name']::text").getall()
                    cleaned_titles = [t.strip() for t in candidate_titles if t.strip() and t.strip().lower() not in ["(about)", "read more", "view", "details", "buy now", "learn more"]]

                    item_title = ""
                    if primary_text:
                        item_title = primary_text.strip()
                    elif cleaned_titles:
                        item_title = cleaned_titles[0]
                    else:
                        anchor_texts = [a.strip() for a in el.css("a::text").getall() if a.strip() and a.strip().lower() not in ["(about)", "read more", "view", "details", "buy now", "learn more"]]
                        if anchor_texts:
                            item_title = anchor_texts[0]

                    price_match = None
                    text_content = " ".join(el.css("::text").getall())
                    price_search = re.search(r'(?:[$€£₹]|Rs\.?|INR|USD|EUR)\s*[0-9,]+(?:\.[0-9]{2})?|[0-9,]+(?:\.[0-9]{2})?\s*(?:[$€£₹]|INR|USD|EUR)', text_content)
                    if price_search:
                        price_match = price_search.group().strip()

                    item_img = (
                        el.css("img::attr(src)").get()
                        or el.css("img::attr(data-src)").get()
                        or el.css("img::attr(srcset)").get()
                    )
                    if item_img:
                        item_img = urljoin(response.url, item_img.split()[0])

                    item_link = el.css("a::attr(href)").get()
                    if item_link:
                        item_link = urljoin(response.url, item_link)

                    rating = el.css("[class*='star']::attr(class), [class*='rating']::text").get()
                    tags = el.css("[class*='tag']::text, [class*='badge']::text").getall()

                    if item_title or price_match or item_img:
                        extracted.append({
                            "type": "card_item",
                            "title": item_title or "Item",
                            "subtitle": author_text.strip() if author_text else None,
                            "price": price_match,
                            "image": item_img,
                            "link": item_link,
                            "rating": rating,
                            "tags": [t.strip() for t in tags if t.strip() and t.strip() != "Tags:"],
                            "text_snippet": " ".join([t.strip() for t in el.css("::text").getall() if t.strip()])[:250],
                            "source_page": response.url
                        })
                break
        return extracted

    def parse(self, response):
        self.visited_urls.add(response.url)
        page_index = len(self.visited_urls)
        self.logger.info(f"Crawling page [{page_index}/{self.max_pages}]: {response.url}")

        # Extract primary overview if first page
        if not self.primary_overview:
            title = (
                response.css("meta[property='og:title']::attr(content)").get()
                or response.css("title::text").get()
                or ""
            ).strip()

            description = (
                response.css("meta[property='og:description']::attr(content)").get()
                or response.css("meta[name='description']::attr(content)").get()
                or ""
            ).strip()

            site_name = (
                response.css("meta[property='og:site_name']::attr(content)").get()
                or urlparse(response.url).netloc
            )

            favicon = response.css("link[rel*='icon']::attr(href)").get()
            favicon = urljoin(response.url, favicon) if favicon else urljoin(response.url, "/favicon.ico")

            og_image = response.css("meta[property='og:image']::attr(content)").get()
            if og_image:
                og_image = urljoin(response.url, og_image)

            canonical = response.css("link[rel='canonical']::attr(href)").get() or response.url
            h1s = [h.strip() for h in response.css("h1::text, h1 *::text").getall() if h.strip()]
            h2s = [h.strip() for h in response.css("h2::text, h2 *::text").getall() if h.strip()]

            # Extract visible body copy for Brand Voice analysis
            body_chunks = [
                re.sub(r"\s+", " ", t).strip()
                for t in response.xpath("//body//p//text() | //body//li//text() | //body//blockquote//text()").getall()
                if t and t.strip() and len(t.strip()) > 20
            ]
            seen_copy = []
            for chunk in body_chunks:
                if chunk not in seen_copy:
                    seen_copy.append(chunk)
            page_copy = " ".join(seen_copy)[:4000]

            html_text = response.text
            emails = list(set(re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:\.[a-zA-Z]{2,})*', html_text)))
            emails = [
                e for e in emails
                if not any(e.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp', '.svg', '.gif', '.js', '.css', '.woff', '.ttf'])
                and 'example.com' not in e.lower()
                and 'sentry.io' not in e.lower()
            ]

            social_links = {}
            for a in response.css("a::attr(href)").getall():
                href = urljoin(response.url, a)
                parsed_href = urlparse(href)
                if not parsed_href.path.strip("/"):
                    continue
                for platform in ["twitter.com", "x.com", "linkedin.com", "github.com", "facebook.com", "instagram.com", "youtube.com"]:
                    if platform in parsed_href.netloc.lower():
                        key = platform.split(".")[0]
                        if key not in social_links:
                            social_links[key] = href

            is_spa = bool(response.css("#root, #__next, #app, [id*='root']"))

            self.primary_overview = {
                "url": response.url,
                "title": title,
                "description": description,
                "site_name": site_name,
                "favicon": favicon,
                "og_image": og_image,
                "canonical": canonical,
                "h1": h1s,
                "h2": h2s,
                "page_copy": page_copy,
                "emails": emails,
                "social_links": social_links,
                "is_spa": is_spa,
            }

            self.logger.info(f"SITE_OVERVIEW_FOUND::{json.dumps(self.primary_overview)}")
            yield {"type": "site_overview", "overview": self.primary_overview}

        # Extract items from this page
        page_items = self.extract_page_items(response)
        for item in page_items:
            yield item

        # Check for SPA script bundles on first page if no items
        if page_index == 1 and not page_items and self.primary_overview.get("is_spa"):
            spa_js_bundles = response.css("script[src*='assets/']::attr(src), script[src*='static/']::attr(src), script[src*='main']::attr(src), script[src*='app']::attr(src)").getall()
            if spa_js_bundles:
                bundle_url = urljoin(response.url, spa_js_bundles[0])
                self.logger.info(f"Following SPA script bundle: {bundle_url}")
                yield scrapy.Request(
                    bundle_url,
                    callback=self.parse_spa_bundle,
                    meta={"page_overview": self.primary_overview}
                )

        # Multi-page link discovery (pagination & subpages)
        if self.scheduled_count < self.max_pages:
            next_links = self.find_next_links(response)
            for link in next_links:
                if link not in self.visited_urls and self.scheduled_count < self.max_pages:
                    self.scheduled_count += 1
                    self.logger.info(f"Scheduling subpage [{self.scheduled_count}/{self.max_pages}]: {link}")
                    yield scrapy.Request(link, callback=self.parse)

        # Competitor discovery (runs once for primary page)
        if not self.competitors_searched and self.primary_overview:
            self.competitors_searched = True
            yield from self.fetch_competitors(self.primary_overview, response.url, page_items)

    def parse_spa_bundle(self, response):
        text = response.text
        self.logger.info("Scanning SPA bundle for data records...")

        pattern = re.compile(
            r'\{id:"(?P<id>[^"]+)",name:"(?P<name>[^"]+)",brand:"(?P<brand>[^"]*)",category:"(?P<category>[^"]*)",variant:"(?P<variant>[^"]*)",(?:originalPrice:(?P<original_price>\d+),)?price:(?P<price>\d+)(?:,discountPercent:(?P<discount>\d+))?,(?:isOffer:[!0-9a-zA-Z]+,offerTag:"(?P<offer_tag>[^"]*)",)?description:"(?P<desc>[^"]*)",image:"(?P<image>[^"]*)",stock:(?P<stock>\d+),topNotes:\[(?P<top>[^\]]*)\],heartNotes:\[(?P<heart>[^\]]*)\],baseNotes:\[(?P<base>[^\]]*)\],mood:"(?P<mood>[^"]*)",rating:(?P<rating>[0-9\.]+),reviewCount:(?P<reviews>\d+)'
        )

        spa_items = []
        for m in pattern.finditer(text):
            d = m.groupdict()
            name = d.get("name")
            if name:
                def _split_notes(raw_notes):
                    return [n.strip('" \t') for n in raw_notes.split(",") if n.strip('" \t')] if raw_notes else []

                stock_num = int(d["stock"]) if d.get("stock") and d["stock"].isdigit() else None
                it = {
                    "type": "product_item",
                    "title": name,
                    "brand": d.get("brand") or "Brand",
                    "category": d.get("category"),
                    "variant": d.get("variant"),
                    "price": f"₹{d.get('price')}" if d.get("price") else None,
                    "original_price": f"₹{d.get('original_price')}" if d.get("original_price") else None,
                    "offer_tag": d.get("offer_tag") or None,
                    "stock": stock_num,
                    "stock_status": "out_of_stock" if stock_num == 0 else ("in_stock" if stock_num is not None else None),
                    "rating": d.get("rating"),
                    "review_count": int(d["reviews"]) if d.get("reviews") and d["reviews"].isdigit() else None,
                    "top_notes": _split_notes(d.get("top")),
                    "heart_notes": _split_notes(d.get("heart")),
                    "base_notes": _split_notes(d.get("base")),
                    "mood": d.get("mood"),
                    "image": d.get("image"),
                    "description": d.get("desc"),
                    "source_page": self.target_url
                }
                spa_items.append(it)
                yield it

        if not spa_items:
            generic_pattern = re.compile(r'\{(?=[^{}]*name:"(?P<name>[^"]+)")(?=[^{}]*price:(?P<price>[0-9.]+))(?=[^{}]*(?:image:"(?P<image>[^"]+)")?)[^{}]*\}')
            for m in generic_pattern.finditer(text):
                d = m.groupdict()
                it = {
                    "type": "spa_item",
                    "title": d.get("name"),
                    "price": f"₹{d.get('price')}" if d.get("price") else None,
                    "image": d.get("image"),
                    "source_page": self.target_url
                }
                yield it

    def generate_competitor_query(self, overview, items, site_url):
        """Derive a product/category competitor search query (never searching the brand's own name)."""
        from collections import Counter

        title = (overview.get("title") or "").strip()
        description = (overview.get("description") or "").strip()
        page_copy = (overview.get("page_copy") or "").strip()
        h1_h2 = " ".join((overview.get("h1") or []) + (overview.get("h2") or []))

        parsed_site = urlparse(site_url)
        netloc = parsed_site.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        domain_root = netloc.split(".")[0] if netloc else ""

        stop_words = {
            "home", "welcome", "official", "site", "website", "page", "store", "shop",
            "online", "the", "and", "for", "with", "your", "our", "from", "this", "that",
            "you", "are", "all", "new", "best", "buy", "top", "india", "global", "pvt",
            "ltd", "inc", "llc", "rights", "reserved", "enter", "world", "explore",
            "exclusive", "collection", "try", "surprised", "free", "shipping", "exchange",
            "returns", "customer", "service", "stunningly", "comfortable", "certified",
            "item", "items", "products", "product", "category", "categories", "perfect",
            "now", "more", "view", "details", "cart", "checkout", "login", "policy",
        }
        if domain_root:
            stop_words.add(domain_root)
            for part in re.split(r"[-_]+", domain_root):
                if len(part) >= 3 and part not in {"scents", "perfume", "shoes", "sandals", "bakery", "foods"}:
                    stop_words.add(part)

        # Split title only on spaced separators or | • — :, preserving hyphenated words like Sugar-Free
        title_segments = [
            seg.strip() for seg in re.split(r"\s*[|•—–:]\s*|\s+-\s+", title)
            if seg.strip() and seg.strip().lower() != domain_root
        ]
        clean_title_str = " ".join(title_segments)

        product_nouns = {
            "sandals", "sandal", "shoes", "shoe", "footwear", "sneakers", "heels", "boots",
            "slippers", "clogs", "loafers", "perfume", "perfumes", "fragrance", "fragrances",
            "scent", "scents", "cologne", "attar", "sweets", "cakes", "cake", "cookies",
            "sweeteners", "keto", "skincare", "serum", "cleanser", "moisturizer", "makeup",
            "lipstick", "clothing", "apparel", "tshirts", "jeans", "dresses", "watches",
            "jewelry", "jewellery", "bags", "backpacks", "wallets", "furniture", "coffee",
            "tea", "chocolate", "chocolates", "supplements", "protein", "eyewear", "sunglasses",
        }

        weighted_tokens = Counter()

        def _add_tokens(text_blob, weight):
            for tok in re.findall(r"[a-zA-Z]{3,}", (text_blob or "").lower()):
                if tok in stop_words:
                    continue
                boost = 3 if tok in product_nouns else 1
                weighted_tokens[tok] += weight * boost

        # 1. Item categories, titles, and category URL slugs
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            _add_tokens(it.get("category") or "", 4)
            _add_tokens(it.get("title") or it.get("name") or "", 2)
            link = it.get("link") or ""
            if "/product-category/" in link or "/collections/" in link:
                slug = link.rstrip("/").split("/")[-1].replace("-", " ")
                _add_tokens(slug, 3)

        # 2. Clean title segments, meta description, headings, and page copy
        _add_tokens(clean_title_str, 3)
        _add_tokens(description, 2)
        _add_tokens(h1_h2, 2)
        _add_tokens(page_copy[:2500], 1)

        top_keywords = [w for w, _ in weighted_tokens.most_common(6)]
        # Prefer explicit product nouns at the front of the query
        niche_first = [w for w in top_keywords if w in product_nouns] + [w for w in top_keywords if w not in product_nouns]
        chosen = niche_first[:4] if niche_first else [domain_root or "ecommerce"]

        base_kw = " ".join(chosen)
        query = f"{base_kw} brands online store"
        return query, chosen

    def fetch_competitors(self, overview, target_url, items):
        query, niche_keywords = self.generate_competitor_query(overview, items, target_url)
        self.logger.info(f"Searching competitor websites for niche: '{query}'...")
        ddg_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}"
        yield scrapy.Request(
            ddg_url,
            callback=self.parse_competitors,
            meta={
                "target_domain": urlparse(target_url).netloc,
                "query": query,
                "niche_keywords": niche_keywords,
            },
            priority=100,
            dont_filter=True,
            errback=lambda failure: self.logger.warning("Competitor search query failed.")
        )

    def parse_competitors(self, response):
        target_domain = response.meta.get("target_domain", "").lower()
        if target_domain.startswith("www."):
            target_domain = target_domain[4:]
        brand_root = target_domain.split(".")[0] if target_domain else ""
        niche_keywords = [k.lower() for k in (response.meta.get("niche_keywords") or [])]

        candidates = []
        seen_domains = {target_domain, "duckduckgo.com", "google.com"}
        ignored_domains = {
            "wikipedia.org", "facebook.com", "instagram.com", "twitter.com", "x.com",
            "youtube.com", "linkedin.com", "pinterest.com", "reddit.com", "amazon.com",
            "amazon.in", "flipkart.com", "walmart.com", "ebay.com", "etsy.com", "quora.com",
            "medium.com", "esquireindia.co.in", "highsnobiety.com", "fragrantica.com",
            "tiktok.com", "myntra.com", "ajio.com", "nykaa.com", "meesho.com",
        }
        off_topic_noise = {"whiskey", "whisky", "bourbon", "vodka", "tequila", "liquor", "winery", "casino", "betting"}
        allowed_noise = off_topic_noise & set(niche_keywords)
        blocked_noise = off_topic_noise - allowed_noise

        for res in response.css(".result"):
            href = res.css(".result__url::attr(href)").get() or ""
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            if "uddg" not in qs:
                continue
            clean_url = qs["uddg"][0]
            parsed_comp = urlparse(clean_url)
            domain = parsed_comp.netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]

            if not domain or domain in seen_domains or any(ign in domain for ign in ignored_domains):
                continue

            # Exclude homonymous domains sharing the target's exact brand name (e.g. happenstancewhiskey.com)
            if len(brand_root) >= 5 and brand_root in domain:
                continue

            seen_domains.add(domain)

            comp_title = (
                res.css(".result__title a::text").get()
                or res.css(".result__title::text").get()
                or domain
            ).strip()
            comp_snippet = " ".join([t.strip() for t in res.css(".result__snippet::text").getall() if t.strip()]).strip()
            combined_text = f"{domain} {comp_title} {comp_snippet}".lower()

            if any(bad in combined_text for bad in blocked_noise):
                continue

            relevance_score = sum(1 for kw in niche_keywords if kw in combined_text)
            root_url = f"{parsed_comp.scheme}://{parsed_comp.netloc}"

            candidates.append((relevance_score, {
                "name": comp_title,
                "domain": domain,
                "url": root_url,
                "snippet": comp_snippet[:220],
                "badge": "Similar Brand"
            }))

        # Rank by niche keyword overlap, preserving DuckDuckGo rank on ties
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        competitors = [c for _, c in candidates[:5]]

        query_str = response.meta.get("query", "")
        self.logger.info(f"Discovered {len(competitors)} competitor brands: {[c['domain'] for c in competitors]}")
        self.logger.info(f"COMPETITORS_FOUND::{json.dumps({'competitors': competitors, 'query': query_str})}")
        yield {
            "type": "discovered_competitors",
            "competitors": competitors,
            "query": query_str
        }
