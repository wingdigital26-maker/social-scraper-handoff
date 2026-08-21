-- Run once in the Supabase SQL editor. See specs/01-cloud-runner.md.

create table if not exists candidates (
  id            bigint generated always as identity primary key,
  source        text not null,               -- 'reddit' | 'tiktok'
  source_id     text not null,               -- reddit post id / tiktok video id
  url           text,
  title         text,
  body          text,
  author        text,
  place_name    text,
  lat           double precision,
  lng           double precision,
  loc_confidence real,
  category      text,
  upvotes       int,
  embeds        jsonb default '[]',
  score         real,
  intent        text,                        -- question | showcase | complaint
  draft_reply   text,
  ghl_pushed    bool not null default false,
  status        text not null default 'new', -- new | queued | approved | rejected | sent

  -- prospect audit (filled by audit_prospect.py)
  website           text,
  phone             text,
  email             text,
  gmb_rating        real,
  gmb_reviews       int,
  bad_review_themes text,
  seo_rank          int,      -- position for "{niche} {city}", null = not found
  has_blog          bool,
  has_service_pages bool,
  page_count        int,
  ssl_ok            bool,
  audit_gaps        jsonb default '[]',  -- ["no blog", "1.9 star rating", ...]
  need_score        real,     -- how badly they need Wing Digital, 0-1
  audited_at        timestamptz,
  discovered_at timestamptz default now(),
  posted_at     timestamptz,
  unique (source, source_id)
);

create index if not exists candidates_queue_idx on candidates (status, score desc);

create table if not exists run_log (
  id       bigint generated always as identity primary key,
  ran_at   timestamptz default now(),
  source   text,
  found    int,
  inserted int,
  blocked  bool default false,
  error    text
);

alter table candidates enable row level security;
alter table run_log enable row level security;
-- Service key bypasses RLS; add anon policies only if/when the queue UI reads directly.
