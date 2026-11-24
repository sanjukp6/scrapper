from scrapy import Selector

with open("luma_test.html", "r", encoding="utf-8") as f:
    html = f.read()

sel = Selector(text=html)
print("Title:", sel.css("title::text").get())
print("H1s:", [h.strip() for h in sel.css("h1::text").getall() if h.strip()])
print("H2s:", [h.strip() for h in sel.css("h2::text").getall() if h.strip()])
print("H3s:", [h.strip() for h in sel.css("h3::text").getall() if h.strip()])
print("Nav links:", sel.css("nav a::text, header a::text").getall())
print("All links (count):", len(sel.css("a").getall()))
print("All links sample:", sel.css("a::attr(href)").getall()[:15])

# Check for product names, prices, images
print("\n--- Potential Product / Content Items ---")
headings = sel.css("h1, h2, h3, h4, h5, p")
print(f"Total text blocks: {len(headings)}")
for tag in sel.css("div[class]"):
    cls = tag.attrib.get("class", "")
    if any(k in cls.lower() for k in ["product", "card", "scent", "candle", "collection", "grid"]):
        print(f"Match: <{tag.root.tag} class='{cls}'> text: {tag.css('::text').get()[:50] if tag.css('::text').get() else ''}")
