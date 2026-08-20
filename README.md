# Social Scraper Handoff

Python pipeline that discovers TikTok / Instagram / Reddit content about real places, with zero AI usage in the loop. Shared so we can develop it together into something that runs 24/7.

Start with [HANDOFF.md](HANDOFF.md) for the limitations and the roadmap. The code lives in [ingest/](ingest/) with its own README explaining the flow. Build targets are specced in [specs/](specs/): the 24/7 cloud runner and the scored review queue.

Pipeline: `reddit_ingest.py` (or `tiktok_ingest.py`) -> `enrich.py` (score + intent + templated reply draft, no AI) -> `db.py` (push to Supabase, dedupe in DB). The GitHub Actions workflow in `.github/workflows/nightly-ingest.yml` runs the Reddit leg nightly once the repo secrets are set; run `supabase/schema.sql` in your Supabase project first.

## Quick start

1. Python 3.10+, then `pip install requests yt-dlp`
2. Copy `.env.example` to `.env` and fill in free Reddit API keys (see HANDOFF.md step 1)
3. Edit `ENV_PATH` at the top of `ingest/reddit_ingest.py` to point at your `.env`
4. Test run:

```bash
python ingest/reddit_ingest.py --limit 30
```
