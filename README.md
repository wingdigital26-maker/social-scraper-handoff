# Social Scraper Handoff

Python pipeline that discovers TikTok / Instagram / Reddit content about real places, with zero AI usage in the loop. Shared so we can develop it together into something that runs 24/7.

Start with [HANDOFF.md](HANDOFF.md) for the limitations and the roadmap. The code lives in [ingest/](ingest/) with its own README explaining the flow.

## Quick start

1. Python 3.10+, then `pip install requests yt-dlp`
2. Copy `.env.example` to `.env` and fill in free Reddit API keys (see HANDOFF.md step 1)
3. Edit `ENV_PATH` at the top of `ingest/reddit_ingest.py` to point at your `.env`
4. Test run:

```bash
python ingest/reddit_ingest.py --limit 30
```
