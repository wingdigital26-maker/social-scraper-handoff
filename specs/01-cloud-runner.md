# Spec 1: 24/7 cloud runner (Supabase + GitHub Actions)

Goal: the Reddit leg of the pipeline runs every night with no PC on, writing to a hosted database instead of local files. The TikTok leg stays on a home IP (residential IPs survive TikTok's anti-bot far longer than datacenter IPs) but writes to the same database.

## Architecture

```
GitHub Actions (nightly cron)          Home machine (nightly, optional)
  reddit_ingest.py  ----\                tiktok_ingest.py ----\
                         \-->  Supabase (Postgres)  <---------/
                                   |
                          review queue UI (spec 2)
```

## Database schema (Supabase / Postgres)

```sql
create table candidates (
  id            bigint generated always as identity primary key,
  source        text not null,              -- 'reddit' | 'tiktok'
  source_id     text not null,              -- reddit post id / tiktok video id
  url           text not null,
  title         text,
  body          text,
  author        text,
  place_name    text,
  lat           double precision,
  lng           double precision,
  loc_confidence real,
  category      text,
  upvotes       int,
  embeds        jsonb default '[]',         -- [{type, url}, ...]
  score         real,                       -- filled by spec 2 scorer
  status        text not null default 'new',-- new | queued | approved | rejected | sent
  discovered_at timestamptz default now(),
  posted_at     timestamptz,
  unique (source, source_id)                -- dedupe across runs, replaces seen_ids.txt
);

create table run_log (
  id          bigint generated always as identity primary key,
  ran_at      timestamptz default now(),
  source      text,
  found       int,
  inserted    int,
  blocked     bool default false,           -- tiktok anti-bot tripped
  error       text
);
```

The `unique (source, source_id)` constraint + `insert ... on conflict do nothing` replaces both `seen_ids.txt` and `seen_tiktok_ids.txt` entirely.

## Code changes

1. New `ingest/db.py`: thin wrapper over Supabase REST (`requests` only, no SDK needed).
   - `insert_candidates(rows)` -> POST to `/rest/v1/candidates?on_conflict=source,source_id` with `Prefer: resolution=ignore-duplicates`
   - `log_run(source, found, inserted, blocked, error)`
2. `reddit_ingest.py` / `tiktok_ingest.py`: add `--sink db` flag. Default stays `jsonl` so local dev is unchanged. When `db`, skip the seen-file logic.
3. Config from env vars, not hardcoded paths: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, plus the existing Reddit keys. Read via `os.environ` with the `.env` file as local fallback.

## GitHub Actions workflow

`.github/workflows/nightly-ingest.yml`:

```yaml
name: nightly-ingest
on:
  schedule:
    - cron: "0 8 * * *"   # 3am Central
  workflow_dispatch: {}     # manual run button
jobs:
  reddit:
    runs-on: ubuntu-latest
    timeout-minutes: 120
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install requests
      - run: python ingest/reddit_ingest.py --sink db
        env:
          REDDIT_CLIENT_ID: ${{ secrets.REDDIT_CLIENT_ID }}
          REDDIT_CLIENT_SECRET: ${{ secrets.REDDIT_CLIENT_SECRET }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
```

Secrets go in repo Settings -> Secrets and variables -> Actions. Never in code.

Note: do NOT run tiktok_ingest.py from Actions. GitHub runner IPs are datacenter IPs and will be blocked almost immediately. Home machine + Task Scheduler/cron for that leg.

## Health / alerting

- Every run writes a `run_log` row, success or fail.
- Failure alert without new infra: add a final workflow step `if: failure()` that hits a webhook (Discord webhook is free and takes 5 minutes) or just rely on GitHub's built-in "workflow failed" email.
- Staleness check: if no `run_log` row in 48h, something silently died. A second tiny scheduled workflow can query Supabase and fail loudly if stale.

## Definition of done

- [ ] `db.py` written, candidates land in Supabase from a local `--sink db` run
- [ ] Dedupe proven: run twice, second run inserts 0
- [ ] Actions workflow green on manual dispatch, then on schedule
- [ ] run_log populating; failure email confirmed by forcing a bad key once
- [ ] TikTok leg scheduled on home machine writing to the same tables
