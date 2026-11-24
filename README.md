# Scrapy Studio - Web Intelligence & Universal Scraper Platform

An automated web crawler and intelligence platform built on top of Scrapy. Features zero-config universal content extraction, client-side SPA support, multi-page pagination following, real-time market competitor discovery, a modern web dashboard, and Supabase cloud database synchronization.

---

## Key Features

1. **Zero-Config Universal Extraction**:
   - Provide any URL. Scrapy automatically identifies product cards, listings, articles, metadata, contact emails, and social links without writing custom CSS selectors.
2. **React / Vite Single Page Application (SPA) Support**:
   - Automatically detects client-side SPAs (such as Vite/React storefronts) and extracts structured product databases directly from compiled JavaScript asset bundles.
3. **Multi-Page Pagination Traversal**:
   - Discovers and follows pagination links (`Next`, `?page=X`, `/page/X`) and catalog directories (`/products`, `/shop`, `/category`) with selectable depth limits (1, 5, 10, or 25 pages).
4. **Real-Time Competitor Intelligence**:
   - Derives the website's industry niche and queries DuckDuckGo in real time with high priority (`priority=100`) to surface competitor brand websites within ~1 second.
5. **Modern Dashboard & Sidebar Navigation**:
   - Built with a clean enterprise light theme (zero emojis), featuring dedicated tabs for **Scraper**, **Competitors**, **Extracted Records**, **Execution Logs**, and **Spider Code**.
6. **Supabase Cloud Database Integration**:
   - Synchronizes every completed extraction (metadata, individual items, and discovered competitors) directly into your Supabase PostgreSQL database. Includes ready-to-run SQL schema with RLS policies.

---

## Project Structure

```
├── universal_extractor.py      # Core universal Scrapy spider (SPA + pagination + competitors)
├── supabase_client.py          # Supabase REST client for automated cloud syncing
├── schema_supabase.sql         # SQL schema migration for Supabase database tables
├── sample_spider.py            # Sample standalone Scrapy spider
├── luma_spider.py              # Specialized spider example for React SPA storefronts
├── ui/
│   ├── index.html              # Modern dashboard with left sidebar and competitors workspace
│   └── server.py               # aiohttp backend with SSE log streaming & Supabase routes
├── .env.example                # Template for environment variables
└── requirements.txt            # Python dependencies
```

---

## Quickstart

### 1. Installation
Clone the repository and install dependencies:
```bash
git clone https://github.com/sanjukp6/scrapper.git
cd scrapper

python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Fill in your Supabase project URL and Publishable API Key:
```env
NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co
NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=your-supabase-publishable-key
```

### 3. Initialize Supabase Database Tables
Run the SQL script in [schema_supabase.sql](schema_supabase.sql) in your **Supabase Dashboard SQL Editor** to create the tables (`extractions`, `scraped_items`, `competitors`) and configure Row Level Security (RLS).

### 4. Start the Web Dashboard
```bash
python ui/server.py
```
Open **http://127.0.0.1:5000** in your browser.

---

## Command Line Usage

Run the universal crawler directly via Scrapy CLI:
```bash
scrapy runspider universal_extractor.py -a target_url="https://luma-scents.vercel.app/" -a max_pages=5 -o output.json
```

---

## License
MIT License.
