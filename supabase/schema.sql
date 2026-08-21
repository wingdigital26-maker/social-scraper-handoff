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
