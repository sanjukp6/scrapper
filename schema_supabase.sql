-- ====================================================================
-- SCRAPY STUDIO - SUPABASE DATABASE SCHEMA (COLLECTIVE BRAND KB)
-- Run this script in your Supabase SQL Editor:
-- https://supabase.com/dashboard/project/twwapvttxabmdvlzkuef/sql
-- Safe to re-run on both fresh and existing databases.
-- ====================================================================

-- 1. Table: extractions (Raw Crawl Jobs Overview & Audit Trail)
create table if not exists public.extractions (
    id text primary key,
    url text not null,
    brand_domain text,
    status text default 'completed',
    item_count integer default 0,
    max_pages integer default 1,
    overview jsonb default '{}'::jsonb,
    competitor_query text,
    created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

alter table public.extractions add column if not exists brand_domain text;

-- 2. Table: scraped_items (Raw Per-Crawl Item Rows)
create table if not exists public.scraped_items (
    id bigserial primary key,
    extraction_id text references public.extractions(id) on delete cascade,
    title text,
    price text,
    category text,
    image text,
    link text,
    rating text,
    source_page text,
    raw_data jsonb default '{}'::jsonb,
    created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- 3. Table: competitors (Raw Per-Crawl Competitor Rows)
create table if not exists public.competitors (
    id bigserial primary key,
    extraction_id text references public.extractions(id) on delete cascade,
    name text,
    domain text,
    url text,
    snippet text,
    badge text default 'Competitor',
    created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- 4. Table: brands (Master Collective Brand Knowledge Base)
create table if not exists public.brands (
    domain text primary key,
    brand_name text not null,
    primary_url text not null,
    niche_query text default '',
    description text default '',
    favicon text default '',
    is_spa boolean default false,
    emails jsonb default '[]'::jsonb,
    social_links jsonb default '{}'::jsonb,
    total_items integer default 0,
    price_summary jsonb default '{}'::jsonb,
    competitors_summary jsonb default '[]'::jsonb,
    collective_knowledge jsonb default '{}'::jsonb,
    knowledge_markdown text default '',
    search_vector tsvector generated always as (
        to_tsvector(
            'english',
            coalesce(brand_name, '') || ' ' ||
            coalesce(domain, '') || ' ' ||
            coalesce(niche_query, '') || ' ' ||
            coalesce(knowledge_markdown, '')
        )
    ) stored,
    crawl_count integer default 1,
    last_crawled_at timestamp with time zone default timezone('utc'::text, now()) not null,
    created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- 5. Table: brand_items (Deduplicated Per-Brand Catalog)
create table if not exists public.brand_items (
    id bigserial primary key,
    brand_domain text not null references public.brands(domain) on delete cascade,
    item_key text not null,
    title text,
    price text,
    numeric_price numeric,
    currency text,
    category text,
    description text,
    image text,
    link text,
    rating text,
    source_page text,
    attributes jsonb default '{}'::jsonb,
    first_seen_at timestamp with time zone default timezone('utc'::text, now()) not null,
    last_seen_at timestamp with time zone default timezone('utc'::text, now()) not null,
    unique (brand_domain, item_key)
);

-- 6. Table: brand_competitors (Deduplicated Brand-to-Competitor Graph)
create table if not exists public.brand_competitors (
    id bigserial primary key,
    brand_domain text not null references public.brands(domain) on delete cascade,
    competitor_domain text not null,
    name text,
    url text,
    snippet text,
    badge text default 'Competitor',
    discovered_at timestamp with time zone default timezone('utc'::text, now()) not null,
    unique (brand_domain, competitor_domain)
);

-- 7. Table: brand_events (Event Snapshots for Events Apps, Realtime & Webhooks)
create table if not exists public.brand_events (
    id bigserial primary key,
    brand_domain text not null references public.brands(domain) on delete cascade,
    extraction_id text,
    event_type text not null,
    payload jsonb not null default '{}'::jsonb,
    created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- 8. Table: brand_documents (The 5 Generated .txt Brand & Strategy Documents)
create table if not exists public.brand_documents (
    id bigserial primary key,
    brand_domain text not null references public.brands(domain) on delete cascade,
    doc_key text not null,
    filename text not null,
    title text not null,
    status text not null default 'partial',
    data_sources jsonb default '[]'::jsonb,
    gaps jsonb default '[]'::jsonb,
    content_txt text not null default '',
    generated_at timestamp with time zone default timezone('utc'::text, now()) not null,
    unique (brand_domain, doc_key)
);

-- 9. Table: influencer_creators (Active Creator Pool & /creator/new Onboarding)
create table if not exists public.influencer_creators (
    id text primary key,
    name text not null,
    handle text not null,
    platform text not null,
    verticals jsonb default '[]'::jsonb,
    tone_tags jsonb default '[]'::jsonb,
    followers_count integer default 0,
    engagement_rate numeric default 0,
    min_rate numeric default 0,
    currency text default 'USD',
    acceptance_rate numeric default 0.9,
    on_time_rate numeric default 0.95,
    bio text default '',
    email text default '',
    profile_url text default '',
    is_sample boolean default false,
    approval_status text default 'approved',
    created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

alter table public.influencer_creators add column if not exists profile_url text default '';
alter table public.influencer_creators add column if not exists is_sample boolean default false;

-- 10. Table: influencer_campaigns (Campaign Briefs, Funding & Lifecycle State)
create table if not exists public.influencer_campaigns (
    id text primary key,
    brand_domain text not null,
    title text not null,
    goal text not null,
    platforms jsonb default '[]'::jsonb,
    deliverables jsonb default '{}'::jsonb,
    budget numeric not null default 0,
    currency text default 'USD',
    commission_pct numeric default 10,
    timeline_days integer default 14,
    start_date text,
    tone text default '',
    source_post_url text default '',
    source_post_locked_at timestamp with time zone,
    status text not null default 'shortlist_ready',
    payment_status text default 'unpaid',
    wallet_applied numeric default 0,
    brief_text text not null default '',
    kb_snapshot jsonb default '{}'::jsonb,
    created_at timestamp with time zone default timezone('utc'::text, now()) not null,
    updated_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- 11. Table: influencer_campaign_creators (Ranked Shortlist, Outreach & Submissions)
create table if not exists public.influencer_campaign_creators (
    id text primary key,
    campaign_id text not null references public.influencer_campaigns(id) on delete cascade,
    creator_id text not null,
    creator_name text,
    creator_handle text,
    creator_profile_url text default '',
    platform text,
    deliverable_type text,
    match_score numeric default 0,
    score_breakdown jsonb default '{}'::jsonb,
    agreed_fee numeric default 0,
    status text not null default 'shortlisted',
    outreach_email text default '',
    outreach_expires_at timestamp with time zone,
    submission_url text default '',
    submission_notes text default '',
    auto_approve_at timestamp with time zone,
    payout_status text default 'pending',
    created_at timestamp with time zone default timezone('utc'::text, now()) not null,
    updated_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- 12. Table: influencer_messages (1:1 Messaging Thread per Campaign)
create table if not exists public.influencer_messages (
    id bigserial primary key,
    campaign_id text not null references public.influencer_campaigns(id) on delete cascade,
    creator_id text,
    sender_role text not null,
    message text not null,
    created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- Safe analytics columns on brand_items & scraped_items
alter table public.brand_items add column if not exists inspired_by text;
alter table public.brand_items add column if not exists review_count integer;
alter table public.brand_items add column if not exists stock_status text;
alter table public.brand_items add column if not exists variant text;

-- ====================================================================
-- ROW LEVEL SECURITY (RLS) POLICIES
-- Allow anonymous / publishable key to read, insert, and upsert records
-- ====================================================================

alter table public.extractions enable row level security;
alter table public.scraped_items enable row level security;
alter table public.competitors enable row level security;
alter table public.brands enable row level security;
alter table public.brand_items enable row level security;
alter table public.brand_competitors enable row level security;
alter table public.brand_events enable row level security;
alter table public.brand_documents enable row level security;
alter table public.influencer_creators enable row level security;
alter table public.influencer_campaigns enable row level security;
alter table public.influencer_campaign_creators enable row level security;
alter table public.influencer_messages enable row level security;

-- Policies for extractions
drop policy if exists "Allow anon read extractions" on public.extractions;
create policy "Allow anon read extractions" on public.extractions for select using (true);
drop policy if exists "Allow anon insert extractions" on public.extractions;
create policy "Allow anon insert extractions" on public.extractions for insert with check (true);
drop policy if exists "Allow anon update extractions" on public.extractions;
create policy "Allow anon update extractions" on public.extractions for update using (true);

-- Policies for scraped_items
drop policy if exists "Allow anon read scraped_items" on public.scraped_items;
create policy "Allow anon read scraped_items" on public.scraped_items for select using (true);
drop policy if exists "Allow anon insert scraped_items" on public.scraped_items;
create policy "Allow anon insert scraped_items" on public.scraped_items for insert with check (true);

-- Policies for competitors
drop policy if exists "Allow anon read competitors" on public.competitors;
create policy "Allow anon read competitors" on public.competitors for select using (true);
drop policy if exists "Allow anon insert competitors" on public.competitors;
create policy "Allow anon insert competitors" on public.competitors for insert with check (true);

-- Policies for brands
drop policy if exists "Allow anon read brands" on public.brands;
create policy "Allow anon read brands" on public.brands for select using (true);
drop policy if exists "Allow anon insert brands" on public.brands;
create policy "Allow anon insert brands" on public.brands for insert with check (true);
drop policy if exists "Allow anon update brands" on public.brands;
create policy "Allow anon update brands" on public.brands for update using (true);

-- Policies for brand_items
drop policy if exists "Allow anon read brand_items" on public.brand_items;
create policy "Allow anon read brand_items" on public.brand_items for select using (true);
drop policy if exists "Allow anon insert brand_items" on public.brand_items;
create policy "Allow anon insert brand_items" on public.brand_items for insert with check (true);
drop policy if exists "Allow anon update brand_items" on public.brand_items;
create policy "Allow anon update brand_items" on public.brand_items for update using (true);

-- Policies for brand_competitors
drop policy if exists "Allow anon read brand_competitors" on public.brand_competitors;
create policy "Allow anon read brand_competitors" on public.brand_competitors for select using (true);
drop policy if exists "Allow anon insert brand_competitors" on public.brand_competitors;
create policy "Allow anon insert brand_competitors" on public.brand_competitors for insert with check (true);
drop policy if exists "Allow anon update brand_competitors" on public.brand_competitors;
create policy "Allow anon update brand_competitors" on public.brand_competitors for update using (true);

-- Policies for brand_events
drop policy if exists "Allow anon read brand_events" on public.brand_events;
create policy "Allow anon read brand_events" on public.brand_events for select using (true);
drop policy if exists "Allow anon insert brand_events" on public.brand_events;
create policy "Allow anon insert brand_events" on public.brand_events for insert with check (true);

-- Policies for brand_documents
drop policy if exists "Allow anon read brand_documents" on public.brand_documents;
create policy "Allow anon read brand_documents" on public.brand_documents for select using (true);
drop policy if exists "Allow anon insert brand_documents" on public.brand_documents;
create policy "Allow anon insert brand_documents" on public.brand_documents for insert with check (true);
drop policy if exists "Allow anon update brand_documents" on public.brand_documents;
create policy "Allow anon update brand_documents" on public.brand_documents for update using (true);

-- Policies for influencer_creators
drop policy if exists "Allow anon read influencer_creators" on public.influencer_creators;
create policy "Allow anon read influencer_creators" on public.influencer_creators for select using (true);
drop policy if exists "Allow anon insert influencer_creators" on public.influencer_creators;
create policy "Allow anon insert influencer_creators" on public.influencer_creators for insert with check (true);
drop policy if exists "Allow anon update influencer_creators" on public.influencer_creators;
create policy "Allow anon update influencer_creators" on public.influencer_creators for update using (true);
drop policy if exists "Allow anon delete influencer_creators" on public.influencer_creators;
create policy "Allow anon delete influencer_creators" on public.influencer_creators for delete using (true);

-- Policies for influencer_campaigns
drop policy if exists "Allow anon read influencer_campaigns" on public.influencer_campaigns;
create policy "Allow anon read influencer_campaigns" on public.influencer_campaigns for select using (true);
drop policy if exists "Allow anon insert influencer_campaigns" on public.influencer_campaigns;
create policy "Allow anon insert influencer_campaigns" on public.influencer_campaigns for insert with check (true);
drop policy if exists "Allow anon update influencer_campaigns" on public.influencer_campaigns;
create policy "Allow anon update influencer_campaigns" on public.influencer_campaigns for update using (true);

-- Policies for influencer_campaign_creators
drop policy if exists "Allow anon read influencer_campaign_creators" on public.influencer_campaign_creators;
create policy "Allow anon read influencer_campaign_creators" on public.influencer_campaign_creators for select using (true);
drop policy if exists "Allow anon insert influencer_campaign_creators" on public.influencer_campaign_creators;
create policy "Allow anon insert influencer_campaign_creators" on public.influencer_campaign_creators for insert with check (true);
drop policy if exists "Allow anon update influencer_campaign_creators" on public.influencer_campaign_creators;
create policy "Allow anon update influencer_campaign_creators" on public.influencer_campaign_creators for update using (true);
drop policy if exists "Allow anon delete influencer_campaign_creators" on public.influencer_campaign_creators;
create policy "Allow anon delete influencer_campaign_creators" on public.influencer_campaign_creators for delete using (true);

-- Policies for influencer_messages
drop policy if exists "Allow anon read influencer_messages" on public.influencer_messages;
create policy "Allow anon read influencer_messages" on public.influencer_messages for select using (true);
drop policy if exists "Allow anon insert influencer_messages" on public.influencer_messages;
create policy "Allow anon insert influencer_messages" on public.influencer_messages for insert with check (true);

-- Indexes for fast queries & event lookups
create index if not exists idx_scraped_items_extraction on public.scraped_items(extraction_id);
create index if not exists idx_competitors_extraction on public.competitors(extraction_id);
create index if not exists idx_extractions_created on public.extractions(created_at desc);
create index if not exists idx_extractions_brand_domain on public.extractions(brand_domain);
create index if not exists idx_brands_last_crawled on public.brands(last_crawled_at desc);
create index if not exists idx_brands_search_vector on public.brands using gin(search_vector);
create index if not exists idx_brand_items_domain on public.brand_items(brand_domain);
create index if not exists idx_brand_competitors_domain on public.brand_competitors(brand_domain);
create index if not exists idx_brand_events_domain_created on public.brand_events(brand_domain, created_at desc);
create index if not exists idx_brand_documents_domain on public.brand_documents(brand_domain);
create index if not exists idx_influencer_campaigns_brand on public.influencer_campaigns(brand_domain, created_at desc);
create index if not exists idx_influencer_cc_campaign on public.influencer_campaign_creators(campaign_id);
create index if not exists idx_influencer_msgs_campaign on public.influencer_messages(campaign_id, created_at asc);
