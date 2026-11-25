import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "you", "are",
    "was", "were", "have", "has", "not", "all", "but", "our", "out", "into",
    "just", "more", "very", "can", "will", "about", "like", "what", "when",
    "which", "their", "they", "them", "these", "those", "every", "each",
}


def _attr(item: dict, key: str, default=None):
    """Read a field from top-level item or nested attributes/raw_data dict."""
    if not isinstance(item, dict):
        return default
    if item.get(key) not in (None, ""):
        return item.get(key)
    attrs = item.get("attributes") or item.get("raw_data") or {}
    if isinstance(attrs, dict) and attrs.get(key) not in (None, ""):
        return attrs.get(key)
    return default


def _to_float(val) -> float | None:
    if val is None:
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(val).replace(",", ""))
    return float(m.group(1)) if m else None


def _to_int(val) -> int | None:
    if val is None:
        return None
    m = re.search(r"([0-9]+)", str(val).replace(",", ""))
    return int(m.group(1)) if m else None


def _header_block(title: str, brand_name: str, domain: str, status: str, sources: list[str], gaps: list[str], now_iso: str) -> list[str]:
    lines = [
        "=" * 72,
        f"{title.upper()}",
        "=" * 72,
        f"Brand        : {brand_name} ({domain})",
        f"Generated At : {now_iso}",
        f"Doc Status   : {status}",
        f"Data Sources : {', '.join(sources) if sources else 'None'}",
    ]
    if gaps:
        lines.append("Known Gaps   :")
        for g in gaps:
            lines.append(f"  - {g}")
    lines.append("=" * 72)
    lines.append("")
    return lines


def generate_product_info_txt(brand: dict, items: list[dict], now_iso: str) -> dict:
    """Build 1. product_info.txt from brand_items/scraped_items."""
    domain = brand.get("domain", "unknown")
    brand_name = brand.get("brand_name") or domain
    ps = brand.get("price_summary") or {}

    inspired_count = sum(1 for it in items if _attr(it, "inspired_by"))
    review_count_present = sum(1 for it in items if _to_int(_attr(it, "review_count")) is not None)
    oos_items = [
        it for it in items
        if str(_attr(it, "stock_status") or "").lower() == "out_of_stock" or _attr(it, "stock") == 0
    ]
    zero_review_items = [
        it for it in items
        if (_to_int(_attr(it, "review_count")) in (None, 0)) and _to_float(it.get("rating")) is None
    ]

    gaps = []
    if not items:
        gaps.append("No catalog items available in brand_items / scraped_items yet.")
    else:
        if inspired_count == 0:
            gaps.append("inspired_by is empty on all items (requires backfill or LLM/pattern tagging).")
        elif inspired_count < len(items):
            gaps.append(f"{len(items) - inspired_count}/{len(items)} items lack an inspired_by mapping.")
        if review_count_present == 0:
            gaps.append("review_count not captured on scraped items.")

    status = "ready" if items and inspired_count > 0 else ("partial" if items else "blocked")

    lines = _header_block(
        "1. Product Info Document",
        brand_name,
        domain,
        status.upper(),
        ["brand_items", "scraped_items"],
        gaps,
        now_iso,
    )

    # Business Overview
    categories = [c.get("name") for c in (ps.get("categories") or []) if c.get("name")]
    lines.extend([
        "1. BUSINESS OVERVIEW",
        "-" * 40,
        f"Brand Name      : {brand_name}",
        f"Domain          : {domain}",
        f"Primary URL     : {brand.get('primary_url') or ''}",
        f"Niche / Query   : {brand.get('niche_query') or 'Not classified'}",
        f"Total SKUs      : {len(items)}",
        f"Categories      : {', '.join(categories) if categories else 'Uncategorized'}",
        f"Description     : {brand.get('description') or 'No site meta description extracted.'}",
        "",
    ])

    # Pricing Structure & Tiers
    cur = ps.get("currency") or ""
    variant_prices = defaultdict(list)
    for it in items:
        var = _attr(it, "variant")
        num = it.get("numeric_price") or _to_float(it.get("price"))
        if var and num is not None:
            variant_prices[str(var)].append(num)

    lines.extend([
        "2. PRICING STRUCTURE & TIERS",
        "-" * 40,
    ])
    if ps.get("min") is not None and ps.get("max") is not None:
        lines.append(f"Overall Range   : {cur} {ps['min']:g} - {ps['max']:g} (Avg: {cur} {ps['avg']:g}, Priced SKUs: {ps.get('priced_count', 0)})".strip())
    else:
        lines.append("Overall Range   : No numeric prices extracted.")

    if variant_prices:
        lines.append("By Variant      :")
        for var, plist in sorted(variant_prices.items()):
            lines.append(f"  - {var}: {cur} {min(plist):g} - {max(plist):g} ({len(plist)} SKUs)".strip())
    lines.append("")

    # Bestsellers / Most-Reviewed
    ranked = sorted(
        items,
        key=lambda x: (_to_int(_attr(x, "review_count")) or 0, _to_float(x.get("rating")) or 0.0),
        reverse=True,
    )
    lines.extend([
        "3. BESTSELLERS & HIGHEST-RATED PRODUCTS",
        "-" * 40,
    ])
    top_rated = [it for it in ranked if _to_float(it.get("rating")) is not None or _to_int(_attr(it, "review_count"))]
    if top_rated:
        for idx, it in enumerate(top_rated[:10], start=1):
            r = it.get("rating") or "N/A"
            rc = _to_int(_attr(it, "review_count"))
            rc_txt = f", {rc} reviews" if rc is not None else ""
            lines.append(f"  {idx}. {it.get('title')} | {it.get('price') or 'N/A'} | Rating: {r}{rc_txt}")
    else:
        lines.append("  No rating or review metrics found on extracted items.")
    lines.append("")

    # Scent / Category Taxonomy
    moods = Counter(str(_attr(it, "mood")).strip() for it in items if _attr(it, "mood"))
    notes = Counter()
    for it in items:
        for nk in ("top_notes", "heart_notes", "base_notes", "tags"):
            vals = _attr(it, nk) or []
            if isinstance(vals, list):
                for v in vals:
                    if v:
                        notes[str(v).strip()] += 1

    lines.extend([
        "4. SCENT / CATEGORY TAXONOMY",
        "-" * 40,
        f"Categories      : {', '.join(f'{c['name']} ({c['count']})' for c in (ps.get('categories') or [])) or 'None'}",
        f"Mood Groupings  : {', '.join(f'{k} ({v})' for k, v in moods.most_common(10)) or 'None extracted'}",
        f"Top Notes/Tags  : {', '.join(f'{k} ({v})' for k, v in notes.most_common(15)) or 'None extracted'}",
        "",
    ])

    # Representative Product Catalog
    lines.extend([
        f"5. PRODUCT CATALOG ({len(items)} ITEMS)",
        "-" * 40,
    ])
    for idx, it in enumerate(items[:80], start=1):
        insp = _attr(it, "inspired_by") or "Not tagged"
        var = _attr(it, "variant") or "-"
        stk = _attr(it, "stock_status") or (_attr(it, "stock") if _attr(it, "stock") is not None else "unknown")
        lines.append(
            f"  [{idx}] {it.get('title')} | Price: {it.get('price') or 'N/A'} | "
            f"Variant: {var} | Inspired By: {insp} | Stock: {stk}"
        )
    if len(items) > 80:
        lines.append(f"  ... ({len(items) - 80} additional items stored in brand_items)")
    lines.append("")

    # Gaps
    lines.extend([
        "6. CATALOG GAPS & OBSERVATIONS",
        "-" * 40,
        f"Out-of-Stock SKUs : {len(oos_items)}" + (f" ({', '.join(i.get('title','') for i in oos_items[:5])})" if oos_items else ""),
        f"Unreviewed SKUs   : {len(zero_review_items)} of {len(items)}",
        f"Missing inspired_by: {len(items) - inspired_count} of {len(items)}",
        "",
    ])

    return {
        "doc_key": "product_info",
        "filename": "product_info.txt",
        "title": "Product Info Document",
        "status": status,
        "data_sources": ["brand_items", "scraped_items"],
        "gaps": gaps,
        "content_txt": "\n".join(lines),
        "generated_at": now_iso,
    }


def generate_brand_voice_txt(brand: dict, overview: dict, items: list[dict], now_iso: str) -> dict:
    """Build 2. brand_voice.txt from overview, page_copy, and item descriptions."""
    domain = brand.get("domain", "unknown")
    brand_name = brand.get("brand_name") or domain
    overview = overview or {}

    h1s = overview.get("h1") or []
    h2s = overview.get("h2") or []
    page_copy = (overview.get("page_copy") or "").strip()
    desc = (brand.get("description") or "").strip()
    item_descs = [str(it.get("description") or "").strip() for it in items if it.get("description")]

    corpus_parts = [desc] + h1s + h2s + ([page_copy] if page_copy else []) + item_descs
    corpus = " ".join(p for p in corpus_parts if p).strip()
    lower_corpus = corpus.lower()

    gaps = []
    if not page_copy:
        gaps.append("Homepage/About body copy (page_copy) not captured yet; re-crawl site to enrich voice samples.")
    if len(item_descs) < 3:
        gaps.append("Limited product description copy extracted.")

    status = "ready" if (page_copy and len(item_descs) >= 3) else ("partial" if corpus else "blocked")

    # Tone signal detection from actual corpus
    tone_descriptors = []
    if any(w in lower_corpus for w in ("luxury", "artisanal", "crafted", "bespoke", "elixir", "opulent", "velvet")):
        tone_descriptors.append("Artisanal / Aspirational Luxury")
    if any(w in lower_corpus for w in ("affordable", "steal", "value", "dupe", "inspired by", "save", "off")):
        tone_descriptors.append("Value-Driven / Accessible")
    if any(w in lower_corpus for w in ("bold", "confidence", "unleash", "command", "magnetic", "seductive")):
        tone_descriptors.append("Confident & Evocative")
    if any(w in lower_corpus for w in ("keto", "sugar-free", "healthy", "natural", "clean", "guilt-free")):
        tone_descriptors.append("Health-Conscious / Solution-Oriented")
    if not tone_descriptors:
        tone_descriptors.append("Direct & Descriptive (insufficient narrative copy for deeper nuance)")

    # Audience inference
    ps = brand.get("price_summary") or {}
    avg_p = ps.get("avg")
    audience_signals = []
    if avg_p is not None:
        cur = ps.get("currency") or ""
        if (cur == "INR" and avg_p < 1800) or (cur == "USD" and avg_p < 45):
            audience_signals.append(f"Price-conscious urban shoppers (avg price {cur} {avg_p:g}).")
        else:
            audience_signals.append(f"Mid-to-premium segment buyers (avg price {cur} {avg_p:g}).")
    if any(w in lower_corpus for w in ("gift", "box", "combo", "hamper")):
        audience_signals.append("Gifting & bundle buyers explicitly targeted.")
    if any(w in lower_corpus for w in ("office", "date", "night", "daily", "party", "everyday")):
        audience_signals.append("Occasion-driven lifestyle buyers (daily wear, office, date night).")
    if not audience_signals:
        audience_signals.append("General category shoppers (crawl About/FAQ pages for stronger demographic signals).")

    # Recurring phrases mined from real text
    words = [w for w in re.findall(r"[a-zA-Z]{4,}", lower_corpus) if w not in _STOPWORDS]
    top_terms = Counter(words).most_common(12)
    bigrams = Counter(
        f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)
    ).most_common(8)

    # Verbatim example sentences
    sentences = []
    for text_block in [desc] + h2s[:4] + item_descs[:6]:
        for s in re.split(r"(?<=[.!?])\s+", text_block):
            s = s.strip()
            if 25 <= len(s) <= 220 and s not in sentences:
                sentences.append(s)
            if len(sentences) >= 6:
                break
        if len(sentences) >= 6:
            break

    avoid_rules = [
        "Avoid generic AI filler ('elevate your journey', 'unleash the power of') not present in site copy.",
        "Maintain the exact price/quality framing used on the storefront (do not invent unverified claims or discounts).",
    ]
    if "Value-Driven / Accessible" in tone_descriptors:
        avoid_rules.append("Avoid overly academic or intimidating niche-perfumery jargon; keep copy accessible.")

    lines = _header_block(
        "2. Brand Voice Document",
        brand_name,
        domain,
        status.upper(),
        ["brands.description", "overview.h1/h2/page_copy", "brand_items.description"],
        gaps,
        now_iso,
    )

    lines.extend([
        "1. TONE DESCRIPTORS",
        "-" * 40,
        *[f"  - {t}" for t in tone_descriptors],
        "",
        "2. TARGET AUDIENCE INFERENCE",
        "-" * 40,
        *[f"  - {a}" for a in audience_signals],
        "",
        "3. RECURRING PHRASES & KEYWORDS (EXTRACTED FROM SITE)",
        "-" * 40,
        f"Top Keywords    : {', '.join(f'{w} ({c})' for w, c in top_terms) or 'Insufficient copy'}",
        f"Common Phrases  : {', '.join(f'{b} ({c})' for b, c in bigrams) or 'Insufficient copy'}",
        f"Headings (H1/H2): {', '.join((h1s + h2s)[:8]) or 'None extracted'}",
        "",
        "4. VERBATIM EXAMPLE SENTENCES FROM SITE",
        "-" * 40,
    ])
    if sentences:
        for i, s in enumerate(sentences, start=1):
            lines.append(f'  {i}. "{s}"')
    else:
        lines.append("  No full descriptive sentences found in crawled metadata.")

    lines.extend([
        "",
        "5. WHAT NOT TO SOUND LIKE (GUARDRAILS)",
        "-" * 40,
        *[f"  - {r}" for r in avoid_rules],
        "",
    ])

    return {
        "doc_key": "brand_voice",
        "filename": "brand_voice.txt",
        "title": "Brand Voice Document",
        "status": status,
        "data_sources": ["brands.overview", "brand_items.description"],
        "gaps": gaps,
        "content_txt": "\n".join(lines),
        "generated_at": now_iso,
    }


def generate_marketing_strategy_txt(brand: dict, items: list[dict], gsc_rows: list[dict], ga_rows: list[dict], now_iso: str) -> dict:
    """Build 3. marketing_strategy.txt from gsc_performance & ga_sessions (or flagged mock baseline)."""
    domain = brand.get("domain", "unknown")
    brand_name = brand.get("brand_name") or domain
    has_gsc = bool(gsc_rows)
    has_ga = bool(ga_rows)

    gaps = []
    if not has_gsc:
        gaps.append("gsc_performance table has 0 rows for this brand (GSC OAuth/sync not wired yet).")
    if not has_ga:
        gaps.append("ga_sessions table has 0 rows for this brand (GA4 auth/sync not wired yet).")

    if has_gsc and has_ga:
        status = "ready"
    elif has_gsc or has_ga:
        status = "partial"
    else:
        status = "mock"

    lines = _header_block(
        "3. Marketing Strategy Document",
        brand_name,
        domain,
        status.upper(),
        ["gsc_performance", "ga_sessions"],
        gaps,
        now_iso,
    )

    if not has_gsc and not has_ga:
        sample_products = [it.get("title") for it in items[:3] if it.get("title")] or ["Flagship Product"]
        lines.extend([
            "NOTE: GSC and GA4 tables are currently empty for this brand.",
            "The sections below show MOCK / PLACEHOLDER metrics mapped to your catalog",
            "until Google Search Console & Google Analytics sync is connected.",
            "",
            "1. TOP-PERFORMING PAGES (TRAFFIC & REVENUE) [MOCK]",
            "-" * 40,
            f"  - {brand.get('primary_url') or domain} | Sessions: [awaiting GA4] | Revenue: [awaiting GA4]",
            f"  - Featured SKU focus: {', '.join(sample_products)}",
            "",
            "2. TOP SEARCH QUERIES (IMPRESSIONS / CLICKS / RANK) [MOCK]",
            "-" * 40,
            f"  - \"{brand_name.lower()} reviews\" | Impressions: [awaiting GSC] | Position: [awaiting GSC]",
            f"  - \"{brand.get('niche_query') or domain}\" | Impressions: [awaiting GSC]",
            "",
            "3. UNDERPERFORMING PAGES (CTR & CONVERSION GAPS) [MOCK]",
            "-" * 40,
            "  - Requires gsc_performance (impressions, ctr, position) + ga_sessions (conversions, revenue).",
            "",
            "4. TRAFFIC SOURCE BREAKDOWN (SOURCE / MEDIUM) [MOCK]",
            "-" * 40,
            "  - Pending ga_sessions.source_medium ingestion.",
            "",
            "5. FUNNEL DROP-OFF PATTERNS [MOCK]",
            "-" * 40,
            "  - Pending ga_sessions.drop_off_step instrumentation.",
            "",
        ])
    else:
        # Real GSC & GA aggregation
        top_pages = sorted(ga_rows, key=lambda r: _to_float(r.get("revenue") or r.get("sessions")) or 0, reverse=True)[:10]
        top_queries = sorted(gsc_rows, key=lambda r: _to_int(r.get("clicks") or r.get("impressions")) or 0, reverse=True)[:15]
        low_ctr = [
            r for r in gsc_rows
            if (_to_int(r.get("impressions")) or 0) >= 100 and (_to_float(r.get("ctr")) or 1.0) < 0.02
        ][:10]

        sources = Counter()
        for r in ga_rows:
            sm = r.get("source_medium") or r.get("source") or "direct"
            sources[str(sm)] += _to_int(r.get("sessions")) or 1

        drop_offs = Counter(
            str(r.get("drop_off_step")) for r in ga_rows if r.get("drop_off_step")
        ).most_common(10)

        lines.extend(["1. TOP-PERFORMING PAGES (TRAFFIC & REVENUE)", "-" * 40])
        for p in top_pages:
            lines.append(f"  - {p.get('page_path') or p.get('url')} | Sessions: {p.get('sessions', 'N/A')} | Revenue: {p.get('revenue', 'N/A')}")
        lines.extend(["", "2. TOP SEARCH QUERIES (GSC)", "-" * 40])
        for q in top_queries:
            lines.append(f"  - \"{q.get('query')}\" | Imp: {q.get('impressions', 0)} | Clicks: {q.get('clicks', 0)} | Pos: {q.get('position', 'N/A')}")
        lines.extend(["", "3. UNDERPERFORMING PAGES (HIGH IMPRESSIONS / LOW CTR)", "-" * 40])
        for q in low_ctr:
            lines.append(f"  - {q.get('page') or q.get('query')} | Imp: {q.get('impressions')} | CTR: {q.get('ctr')}")
        if not low_ctr:
            lines.append("  None flagged at current thresholds.")
        lines.extend(["", "4. TRAFFIC SOURCE BREAKDOWN", "-" * 40])
        for sm, cnt in sources.most_common(10):
            lines.append(f"  - {sm}: {cnt} sessions")
        lines.extend(["", "5. FUNNEL DROP-OFF PATTERNS", "-" * 40])
        for step, cnt in drop_offs:
            lines.append(f"  - {step}: {cnt} sessions")
        if not drop_offs:
            lines.append("  No drop_off_step data recorded in ga_sessions yet.")
        lines.append("")

    return {
        "doc_key": "marketing_strategy",
        "filename": "marketing_strategy.txt",
        "title": "Marketing Strategy Document",
        "status": status,
        "data_sources": ["gsc_performance", "ga_sessions"],
        "gaps": gaps,
        "content_txt": "\n".join(lines),
        "generated_at": now_iso,
    }


def generate_competitor_analysis_txt(
    brand: dict,
    items: list[dict],
    competitors: list[dict],
    other_brand_items: dict[str, list[dict]],
    now_iso: str,
) -> dict:
    """Build 4. competitor_analysis.txt from brand_competitors + cross-brand items via inspired_by."""
    domain = brand.get("domain", "unknown")
    brand_name = brand.get("brand_name") or domain

    own_by_inspired = {}
    for it in items:
        insp = _attr(it, "inspired_by")
        if insp:
            own_by_inspired[insp.strip().lower()] = it

    overlaps = []
    pricing_gaps = []
    promos_found = []

    for comp in competitors:
        snip = f"{comp.get('name') or ''} {comp.get('snippet') or ''}"
        if re.search(r"(buy\s+\d+\s+get|\d+%\s*off|free\s+shipping|combo|bundle|discount|sale)", snip, re.IGNORECASE):
            promos_found.append(f"{comp.get('competitor_domain') or comp.get('domain')}: {comp.get('snippet') or comp.get('name')}")

    for comp_dom, comp_items in other_brand_items.items():
        if comp_dom == domain:
            continue
        for cit in comp_items:
            tag = _attr(cit, "offer_tag")
            if tag:
                promos_found.append(f"{comp_dom} ({cit.get('title')}): {tag}")
            cinsp = _attr(cit, "inspired_by")
            if not cinsp:
                continue
            key = cinsp.strip().lower()
            if key in own_by_inspired:
                oit = own_by_inspired[key]
                op = oit.get("numeric_price") or _to_float(oit.get("price"))
                cp = cit.get("numeric_price") or _to_float(cit.get("price"))
                overlaps.append({
                    "inspired_by": cinsp,
                    "own_title": oit.get("title"),
                    "own_price": op,
                    "own_rating": oit.get("rating"),
                    "comp_domain": comp_dom,
                    "comp_title": cit.get("title"),
                    "comp_price": cp,
                    "comp_rating": cit.get("rating"),
                })
                if op and cp and cp > 0:
                    pct = ((op - cp) / cp) * 100.0
                    if abs(pct) >= 20.0:
                        direction = "ABOVE" if pct > 0 else "BELOW"
                        pricing_gaps.append(
                            f"{cinsp}: {domain} ({op:g}) is {abs(pct):.1f}% {direction} {comp_dom} ({cp:g})"
                        )

    scraped_comp_domains = [d for d, lst in other_brand_items.items() if d != domain and lst]
    gaps = []
    if not competitors:
        gaps.append("No competitor domains recorded in brand_competitors yet.")
    if not own_by_inspired:
        gaps.append("Own catalog lacks inspired_by tags needed for 1:1 fragrance matching.")
    if not scraped_comp_domains:
        gaps.append("Competitor websites have been discovered only, not yet crawled into brand_items.")
    elif not overlaps:
        gaps.append("No overlapping inspired_by values found across scraped brands yet.")

    status = "ready" if overlaps else ("partial" if (competitors or scraped_comp_domains) else "blocked")

    lines = _header_block(
        "4. Competitor Analysis Document",
        brand_name,
        domain,
        status.upper(),
        ["brand_competitors", "brand_items (cross-brand inspired_by)"],
        gaps,
        now_iso,
    )

    lines.extend([
        f"1. DISCOVERED COMPETITOR LANDSCAPE ({len(competitors)} DOMAINS)",
        "-" * 40,
    ])
    for c in competitors:
        cdom = c.get("competitor_domain") or c.get("domain")
        scraped_flag = "SCRAPED" if cdom in other_brand_items else "DISCOVERED ONLY (NOT SCRAPED)"
        lines.append(f"  - {cdom} [{scraped_flag}] | {c.get('name') or cdom} | {c.get('url')}")
        if c.get("snippet"):
            lines.append(f"      Snippet: {c['snippet']}")
    if not competitors:
        lines.append("  None discovered yet.")
    lines.append("")

    lines.extend([
        "2. CATALOG BREADTH COMPARISON",
        "-" * 40,
        f"  - {domain} (You): {len(items)} unique items",
    ])
    for cdom, clist in sorted(other_brand_items.items()):
        if cdom != domain:
            lines.append(f"  - {cdom}: {len(clist)} unique items")
    lines.append("")

    lines.extend([
        f"3. PER-FRAGRANCE (INSPIRED_BY) PRICE & RATING COMPARISON ({len(overlaps)} MATCHES)",
        "-" * 40,
    ])
    if overlaps:
        for ov in overlaps[:25]:
            lines.append(
                f"  - {ov['inspired_by']}: You ({ov['own_title']}, price={ov['own_price']}, rating={ov['own_rating']}) "
                f"vs {ov['comp_domain']} ({ov['comp_title']}, price={ov['comp_price']}, rating={ov['comp_rating']})"
            )
    else:
        lines.append("  Blocked/Pending: Backfill inspired_by and scrape competitor websites to enable SKU-level price comparison.")
    lines.append("")

    lines.extend([
        "4. COMPETITOR PROMOS & BUNDLES DETECTED",
        "-" * 40,
    ])
    if promos_found:
        for p in promos_found[:15]:
            lines.append(f"  - {p}")
    else:
        lines.append("  No promo/bundle signals detected in current competitor snippets or tags.")
    lines.append("")

    lines.extend([
        "5. FLAGGED PRICING GAPS (>=20% DELTA)",
        "-" * 40,
    ])
    if pricing_gaps:
        for pg in pricing_gaps:
            lines.append(f"  - {pg}")
    else:
        lines.append("  No >=20% cross-brand pricing gaps computed yet.")
    lines.append("")

    return {
        "doc_key": "competitor_analysis",
        "filename": "competitor_analysis.txt",
        "title": "Competitor Analysis Document",
        "status": status,
        "data_sources": ["brand_competitors", "brand_items"],
        "gaps": gaps,
        "content_txt": "\n".join(lines),
        "generated_at": now_iso,
    }


def generate_content_strategy_txt(
    brand: dict,
    items: list[dict],
    other_brand_items: dict[str, list[dict]],
    marketing_doc: dict,
    competitor_doc: dict,
    gsc_rows: list[dict],
    now_iso: str,
) -> dict:
    """Build 5. content_strategy.txt synthesizing product_info + marketing_strategy + competitor_analysis."""
    domain = brand.get("domain", "unknown")
    brand_name = brand.get("brand_name") or domain
    ps = brand.get("price_summary") or {}

    upstream_ready = (marketing_doc.get("status") == "ready") and (competitor_doc.get("status") == "ready")
    gaps = []
    if marketing_doc.get("status") != "ready":
        gaps.append("Blocked/Provisional on Marketing Strategy (real GSC/GA data missing).")
    if competitor_doc.get("status") != "ready":
        gaps.append("Blocked/Provisional on Competitor Analysis (competitor catalogs + inspired_by overlap missing).")

    status = "ready" if upstream_ready else "blocked"

    # Find competitor inspired_by items absent from own catalog
    own_inspired = {str(_attr(it, "inspired_by")).strip().lower() for it in items if _attr(it, "inspired_by")}
    comp_only_inspired = Counter()
    for cdom, clist in other_brand_items.items():
        if cdom == domain:
            continue
        for cit in clist:
            ci = _attr(cit, "inspired_by")
            if ci and ci.strip().lower() not in own_inspired:
                comp_only_inspired[ci.strip()] += 1

    # Zero-review / low-visibility own SKUs needing promotion
    low_vis_skus = [
        it.get("title") for it in items
        if _to_int(_attr(it, "review_count")) in (None, 0) and it.get("title")
    ][:8]

    moods = sorted({str(_attr(it, "mood")).strip() for it in items if _attr(it, "mood")})
    categories = [c.get("name") for c in (ps.get("categories") or []) if c.get("name")]

    lines = _header_block(
        "5. Content Strategy Document",
        brand_name,
        domain,
        status.upper(),
        ["product_info", "brand_voice", "marketing_strategy", "competitor_analysis"],
        gaps,
        now_iso,
    )

    lines.extend([
        "1. CONTENT GAPS (GSC QUERY COVERAGE)",
        "-" * 40,
    ])
    if gsc_rows:
        weak = [r for r in gsc_rows if (_to_float(r.get("position")) or 99) > 10][:8]
        for r in weak:
            lines.append(f"  - Query \"{r.get('query')}\" (pos {r.get('position')}, imp {r.get('impressions')}) -> Needs dedicated landing page.")
        if not weak:
            lines.append("  No weak-ranking GSC queries (>pos 10) found.")
    else:
        lines.append("  [PROVISIONAL] Connect GSC to surface queries with impressions lacking dedicated landing pages.")
    lines.append("")

    lines.extend([
        "2. PRODUCT / INSPIRED-BY GAPS VS COMPETITORS",
        "-" * 40,
    ])
    if comp_only_inspired:
        for insp_name, cnt in comp_only_inspired.most_common(10):
            lines.append(f"  - \"{insp_name}\" covered by {cnt} competitor SKU(s), missing from {domain}.")
    else:
        lines.append("  [PROVISIONAL] Scrape competitor sites and tag inspired_by to reveal catalog gaps.")
    lines.append("")

    lines.extend([
        "3. RECOMMENDED ARTICLE & LANDING-PAGE TOPICS",
        "-" * 40,
    ])
    topics = []
    for insp in list(own_inspired)[:4]:
        topics.append(f"Best {insp.title()} Inspired Alternatives Guide — featuring {brand_name} SKUs")
    for cat in categories[:3]:
        topics.append(f"{brand_name} {cat} Buying Guide & Comparison")
    for sku in low_vis_skus[:3]:
        topics.append(f"Spotlight Landing Page / Social Reel for low-visibility SKU: {sku}")
    if not topics:
        topics.append(f"{brand_name} Flagship Collection & Buyer Guide (awaiting richer catalog/GSC data)")
    for i, t in enumerate(topics, start=1):
        lines.append(f"  {i}. {t}")
    lines.append("")

    lines.extend([
        "4. SEASONAL & OCCASION / MOOD OPPORTUNITIES",
        "-" * 40,
    ])
    if moods:
        for m in moods[:8]:
            lines.append(f"  - Mood Collection Campaign: \"{m}\" (curated bundle + occasion blog)")
    else:
        lines.append("  No mood tags extracted yet; infer occasion hubs (Office, Date Night, Gifting) from catalog.")
    lines.append("")

    lines.extend([
        "5. PRIORITY RANKING (WHAT TO ACT ON FIRST)",
        "-" * 40,
        f"  [P1] Promote {len(low_vis_skus)} unreviewed/low-visibility SKUs via brand-voice social & product copy.",
        "  [P2] Crawl top competitor domains from brand_competitors & backfill inspired_by to unlock real price-gap content.",
        "  [P3] Wire GSC + GA4 sync into gsc_performance & ga_sessions to convert provisional topics into traffic-ranked priorities.",
        "",
    ])

    return {
        "doc_key": "content_strategy",
        "filename": "content_strategy.txt",
        "title": "Content Strategy Document",
        "status": status,
        "data_sources": ["product_info", "brand_voice", "marketing_strategy", "competitor_analysis"],
        "gaps": gaps,
        "content_txt": "\n".join(lines),
        "generated_at": now_iso,
    }


def generate_five_brand_documents(
    brand: dict,
    overview: dict,
    items: list[dict],
    competitors: list[dict],
    other_brand_items: dict[str, list[dict]] | None = None,
    gsc_rows: list[dict] | None = None,
    ga_rows: list[dict] | None = None,
    now_iso: str | None = None,
) -> list[dict]:
    """Generate all 5 .txt brand & strategy documents for storage in Supabase."""
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    other_brand_items = other_brand_items or {}
    gsc_rows = gsc_rows or []
    ga_rows = ga_rows or []

    doc1 = generate_product_info_txt(brand, items, now_iso)
    doc2 = generate_brand_voice_txt(brand, overview, items, now_iso)
    doc3 = generate_marketing_strategy_txt(brand, items, gsc_rows, ga_rows, now_iso)
    doc4 = generate_competitor_analysis_txt(brand, items, competitors, other_brand_items, now_iso)
    doc5 = generate_content_strategy_txt(brand, items, other_brand_items, doc3, doc4, gsc_rows, now_iso)
    return [doc1, doc2, doc3, doc4, doc5]
