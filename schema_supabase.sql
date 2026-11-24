-- ====================================================================
-- SCRAPY STUDIO - SUPABASE DATABASE SCHEMA
-- Run this script in your Supabase SQL Editor:
-- https://supabase.com/dashboard/project/twwapvttxabmdvlzkuef/sql
-- ====================================================================

-- 1. Table: extractions (Crawl Jobs Overview & Metadata)
create table if not exists public.extractions (
    id text primary key,
    url text not null,
    status text default 'completed',
    item_count integer default 0,
    max_pages integer default 1,
    overview jsonb default '{}'::jsonb,
    competitor_query text,
    created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- 2. Table: scraped_items (Individual Product / Listing Cards)
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

-- 3. Table: competitors (Discovered Market Competitors)
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

-- ====================================================================
-- ROW LEVEL SECURITY (RLS) POLICIES
-- Allow anonymous / publishable key to insert and select records
-- ====================================================================

alter table public.extractions enable row level security;
alter table public.scraped_items enable row level security;
alter table public.competitors enable row level security;

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

-- Indexes for performance
create index if not exists idx_scraped_items_extraction on public.scraped_items(extraction_id);
create index if not exists idx_competitors_extraction on public.competitors(extraction_id);
create index if not exists idx_extractions_created on public.extractions(created_at desc);
