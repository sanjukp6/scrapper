import hashlib
import re
from collections import Counter
from urllib.parse import urlparse

_CURRENCY_MAP = {
    "₹": "INR",
    "rs": "INR",
    "inr": "INR",
    "$": "USD",
    "usd": "USD",
    "£": "GBP",
    "gbp": "GBP",
    "€": "EUR",
    "eur": "EUR",
}


def normalize_domain(url: str) -> str:
    """Return canonical lowercase domain without www. prefix or port."""
    if not url:
        return "unknown"
    raw = url.strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    netloc = urlparse(raw).netloc.lower()
    if ":" in netloc:
        netloc = netloc.split(":", 1)[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or "unknown"


def get_primary_url(url: str) -> str:
    """Return scheme + host root URL for a brand."""
    if not url:
        return ""
    raw = url.strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if not parsed.netloc:
        return raw
    return f"{parsed.scheme}://{parsed.netloc}"


def clean_brand_name(overview: dict, domain: str) -> str:
    """Derive a clean human-readable brand name from site metadata or domain."""
    overview = overview or {}
    site_name = (overview.get("site_name") or "").strip()
    title = (overview.get("title") or "").strip()

    if site_name and site_name.lower() != domain.lower():
        return site_name

    if title:
        # Take first meaningful segment before common title separators
        parts = [p.strip() for p in re.split(r"\s*[|•—–:]\s*|\s+-\s+", title) if p.strip()]
        if parts:
            candidate = parts[0]
            if candidate.lower() in {"home", "welcome", "official site", "shop"} and len(parts) > 1:
                candidate = parts[1]
            if len(candidate) <= 60:
                return candidate

    root = domain.split(".")[0] if domain else "Brand"
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", root) if w) or domain


def parse_price(raw_price) -> tuple[float | None, str | None]:
    """Extract numeric price and ISO-like currency code from a formatted price string."""
    if raw_price is None:
        return None, None
    text = str(raw_price).strip()
    if not text:
        return None, None

    currency = None
    lower = text.lower()
    for token, code in _CURRENCY_MAP.items():
        if token in lower:
            currency = code
            break

    match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)", text)
    if not match:
        return None, currency

    num_str = match.group(1).replace(",", "")
    try:
        return float(num_str), currency
    except ValueError:
        return None, currency


def make_item_key(item: dict) -> str:
    """Build deterministic short SHA-256 key to deduplicate brand items across crawls."""
    title = (item.get("title") or item.get("name") or "").strip().lower()
    title = re.sub(r"\s+", " ", title)
    link = (item.get("link") or "").strip().lower().rstrip("/")
    brand = (item.get("brand") or "").strip().lower()
    category = (item.get("category") or "").strip().lower()

    identity = f"{title}|{link}" if (title or link) else f"{brand}|{category}|{item.get('description', '')[:80]}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


_NAV_NOISE_TITLES = {
    "home", "your cart", "cart", "faq", "faq & policies", "policies",
    "login", "log in", "sign in", "register", "menu", "search",
    "account", "wishlist", "checkout", "shop all", "view all", "about us", "contact us",
}

_INSPIRED_PATTERNS = [
    re.compile(r"(?:inspired\s+by|dupe\s+(?:of|for)|impression\s+of|similar\s+to|reminiscent\s+of|clone\s+of)\s+([A-Z0-9][^.,;!\n]{2,60})", re.IGNORECASE),
]


def extract_inspired_by(item: dict) -> str | None:
    """Return explicit inspired_by value or infer it from product title/description."""
    if not isinstance(item, dict):
        return None
    direct = item.get("inspired_by") or (item.get("attributes") or {}).get("inspired_by")
    if direct and str(direct).strip():
        return str(direct).strip()

    text = f"{item.get('title') or ''} {item.get('description') or ''} {item.get('text_snippet') or ''}"
    for pat in _INSPIRED_PATTERNS:
        m = pat.search(text)
        if m:
            candidate = m.group(1).strip(" -:.,;\"'")
            if len(candidate) >= 3:
                return candidate[:80]
    return None


def is_navigation_noise(item: dict) -> bool:
    """Detect non-product navbar/footer link items that have no price or image."""
    title = (item.get("title") or item.get("name") or "").strip().lower()
    has_price = bool(item.get("price") or item.get("price_inr"))
    has_image = bool(item.get("image") or item.get("image_url"))
    if not has_price and not has_image and title in _NAV_NOISE_TITLES:
        return True
    return False


def extract_item_attributes(item: dict) -> dict:
    """Collect extra non-column attributes (inspired_by, stock, reviews, notes, mood, etc.)."""
    excluded = {
        "type", "title", "name", "price", "price_inr", "category",
        "description", "desc", "image", "image_url", "link", "rating", "source_page"
    }
    attrs = {}
    for k, v in item.items():
        if k not in excluded and v not in (None, "", [], {}):
            attrs[k] = v

    inspired = extract_inspired_by(item)
    if inspired and "inspired_by" not in attrs:
        attrs["inspired_by"] = inspired
    return attrs


def normalize_item_row(brand_domain: str, item: dict, fallback_url: str, timestamp_iso: str) -> dict:
    """Convert raw extracted item dict into a normalized public.brand_items row."""
    raw_price = item.get("price") if item.get("price") is not None else item.get("price_inr")
    price_text = str(raw_price).strip() if raw_price not in (None, "") else ""
    numeric_price, currency = parse_price(raw_price)

    return {
        "brand_domain": brand_domain,
        "item_key": make_item_key(item),
        "title": (item.get("title") or item.get("name") or "Item").strip(),
        "price": price_text or None,
        "numeric_price": numeric_price,
        "currency": currency,
        "category": item.get("category"),
        "description": item.get("description") or item.get("text_snippet"),
        "image": item.get("image") or item.get("image_url"),
        "link": item.get("link"),
        "rating": str(item.get("rating")) if item.get("rating") is not None else None,
        "source_page": item.get("source_page") or fallback_url,
        "attributes": extract_item_attributes(item),
        "last_seen_at": timestamp_iso,
    }


def deduplicate_item_rows(rows: list[dict]) -> list[dict]:
    """Deduplicate normalized item rows within a single batch by item_key."""
    merged = {}
    for row in rows:
        merged[row["item_key"]] = row
    return list(merged.values())


def merge_brand_contacts(existing_brand: dict | None, overview: dict) -> tuple[list[str], dict]:
    """Union emails and merge social_links without losing previously discovered channels."""
    existing_brand = existing_brand or {}
    overview = overview or {}

    seen_emails = {}
    for e in (existing_brand.get("emails") or []) + (overview.get("emails") or []):
        if isinstance(e, str) and e.strip():
            clean = e.strip()
            seen_emails.setdefault(clean.lower(), clean)

    merged_socials = dict(existing_brand.get("social_links") or {})
    for platform, link in (overview.get("social_links") or {}).items():
        if platform and link:
            merged_socials[platform] = link

    return list(seen_emails.values()), merged_socials


def compute_price_summary(brand_items: list[dict]) -> dict:
    """Compute pricing stats, primary currency, and category counts from brand items."""
    prices = []
    currencies = Counter()
    categories = Counter()
    tags = Counter()

    for it in brand_items:
        num = it.get("numeric_price")
        if num is not None:
            try:
                prices.append(float(num))
            except (TypeError, ValueError):
                pass
        cur = it.get("currency")
        if cur:
            currencies[cur] += 1
        cat = it.get("category")
        if cat:
            categories[str(cat).strip()] += 1
        attrs = it.get("attributes") or {}
        for t in attrs.get("tags") or []:
            if t:
                tags[str(t).strip()] += 1
        if attrs.get("mood"):
            tags[str(attrs["mood"]).strip()] += 1

    primary_currency = currencies.most_common(1)[0][0] if currencies else None
    summary = {
        "currency": primary_currency,
        "priced_count": len(prices),
        "min": round(min(prices), 2) if prices else None,
        "max": round(max(prices), 2) if prices else None,
        "avg": round(sum(prices) / len(prices), 2) if prices else None,
        "categories": [{"name": name, "count": cnt} for name, cnt in categories.most_common(10)],
        "top_tags": [name for name, _ in tags.most_common(10)],
    }
    return summary


def build_brand_knowledge(
    domain: str,
    brand_name: str,
    primary_url: str,
    niche_query: str,
    description: str,
    favicon: str,
    is_spa: bool,
    emails: list[str],
    social_links: dict,
    brand_items: list[dict],
    brand_competitors: list[dict],
    crawl_count: int,
    last_crawled_at: str,
) -> tuple[dict, str, dict, list[dict]]:
    """Compile collective_knowledge JSONB and event-ready knowledge_markdown."""
    price_summary = compute_price_summary(brand_items)

    featured_items = []
    for it in brand_items[:12]:
        featured_items.append({
            "title": it.get("title"),
            "price": it.get("price"),
            "category": it.get("category"),
            "rating": it.get("rating"),
            "link": it.get("link"),
            "image": it.get("image"),
            "description": (it.get("description") or "")[:180],
        })

    competitors_summary = [
        {
            "name": c.get("name") or c.get("competitor_domain"),
            "domain": c.get("competitor_domain"),
            "url": c.get("url"),
            "snippet": c.get("snippet"),
            "badge": c.get("badge") or "Competitor",
        }
        for c in brand_competitors
    ]

    cat_names = [c["name"] for c in price_summary.get("categories", [])[:5]]
    price_range_str = "N/A"
    if price_summary.get("min") is not None and price_summary.get("max") is not None:
        cur = price_summary.get("currency") or ""
        price_range_str = f"{cur} {price_summary['min']:g} - {price_summary['max']:g} (avg {price_summary['avg']:g})".strip()

    brief_parts = [
        f"{brand_name} ({domain}) operates in {niche_query or 'digital commerce/content'}.",
    ]
    if description:
        brief_parts.append(description)
    if brand_items:
        cat_txt = f" across {', '.join(cat_names)}" if cat_names else ""
        brief_parts.append(f"Catalog includes {len(brand_items)} tracked offerings{cat_txt} (pricing: {price_range_str}).")
    if competitors_summary:
        comp_names = ", ".join(c["domain"] for c in competitors_summary[:5] if c.get("domain"))
        brief_parts.append(f"Key identified competitors: {comp_names}.")

    event_brief = " ".join(brief_parts)

    collective_knowledge = {
        "brand_identity": {
            "domain": domain,
            "name": brand_name,
            "primary_url": primary_url,
            "description": description,
            "favicon": favicon,
            "niche": niche_query,
            "is_spa": bool(is_spa),
        },
        "outreach_contacts": {
            "emails": emails,
            "social_links": social_links,
        },
        "catalog_showcase": {
            "total_items": len(brand_items),
            "price_summary": price_summary,
            "featured_items": featured_items,
        },
        "competitor_landscape": competitors_summary,
        "event_brief": event_brief,
        "metadata": {
            "crawl_count": crawl_count,
            "last_crawled_at": last_crawled_at,
        },
    }

    md_lines = [
        f"# {brand_name} ({domain})",
        "",
        f"**Website:** {primary_url}",
        f"**Niche:** {niche_query or 'General'}",
        f"**Crawls Merged:** {crawl_count} | **Last Updated:** {last_crawled_at}",
        "",
        "## Event Brief",
        event_brief,
        "",
        "## Outreach & Social Channels",
        f"- **Emails:** {', '.join(emails) if emails else 'None discovered'}",
    ]
    if social_links:
        for plat, link in social_links.items():
            md_lines.append(f"- **{plat.capitalize()}:** {link}")
    else:
        md_lines.append("- **Social Links:** None discovered")

    md_lines.extend([
        "",
        f"## Catalog Summary ({len(brand_items)} Unique Items)",
        f"- **Price Range:** {price_range_str}",
        f"- **Top Categories:** {', '.join(cat_names) if cat_names else 'Uncategorized'}",
    ])
    if featured_items:
        md_lines.append("")
        md_lines.append("### Featured Offerings")
        for fi in featured_items[:10]:
            price_tag = f" — {fi['price']}" if fi.get("price") else ""
            cat_tag = f" [{fi['category']}]" if fi.get("category") else ""
            md_lines.append(f"- **{fi['title']}**{cat_tag}{price_tag}")

    md_lines.extend(["", f"## Competitor Landscape ({len(competitors_summary)})"])
    if competitors_summary:
        for comp in competitors_summary:
            md_lines.append(f"- **{comp['name']}** ({comp['url']}) — {comp.get('snippet') or 'Similar brand'}")
    else:
        md_lines.append("- No competitors recorded yet.")

    return collective_knowledge, "\n".join(md_lines), price_summary, competitors_summary
