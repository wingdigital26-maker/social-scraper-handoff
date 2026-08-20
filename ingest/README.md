# Prowl spot ingestion pipeline

Auto-builds the spot database from **real Reddit posts** via Reddit's official
API. Nothing is scraped, nothing is copied — each spot links back to the
original post as an embed. Built to scale from Texas to the whole US.

## The flow

```
reddit_ingest.py   Reddit API -> extract place -> geocode -> region gate
                   -> dedupe -> classify -> harvest TikTok/IG links
                   -> candidates.jsonl
        (you review candidates.jsonl — delete junk lines)
promote.py         candidates.jsonl -> ingested-spots.js  (window.INGESTED_SPOTS)
        (we wire that file into the app together — one <script> + one spread)
```

**Social harvest:** Reddit posts often link out to TikTok / Instagram. The
engine pulls those links, validates TikTok ones through the public oEmbed
endpoint (dead/private links are dropped), and attaches them as extra embeds on
the spot — alongside the Reddit post. All legal: public URLs, embedded the
sanctioned way, never scraped or downloaded. Toggle via `HARVEST_SOCIAL` in
`config.py`.

Every auto-collected spot carries `needs_review: true` and
`legal_status: "unverified"`, so nothing pretends to be hand-vetted, and
private/trespassing spots stay flagged just like the hand-curated ones.

## Setup (once)

1. Get a Reddit API key — see `../REDDIT-KEY-GUIDE.md` (10 min, free).
   Puts `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` in `C:\Users\wjack\ghl-cli\.env`.
2. `pip install requests` (already present on this machine).

## Run

```bash
python reddit_ingest.py --limit 30     # small test run
python reddit_ingest.py                # full sweep (overnight)
python reddit_ingest.py --dry-run      # search + geocode, write nothing

python promote.py --min-conf 0.9       # publish only exactly-located spots
python promote.py                      # publish everything past the gates
```

## Tuning

Everything lives in `config.py`: which subreddits, which search terms, the
region bounding box (Texas by default; a commented USA box is right there),
quality gates (min upvotes, min title length), and politeness/rate-limit sleeps.

- **Go nationwide:** swap `REGION_BBOX` to the USA box in `config.py` and add
  more city subreddits to `SUBREDDITS`.
- **Location accuracy:** urbex posts often hide exact spots on purpose. Those
  come through at `location_confidence 0.2–0.5` and get a `loc-approx` tag.
  Use `promote.py --min-conf 0.9` to keep only exactly-located ones.

## TikTok hashtag discovery (experimental, fragile)

`tiktok_ingest.py` is a second, **best-effort** ingestion path that discovers
TikTok videos by hashtag (`#abandoneddallas`, `#urbextexas`, `#dallasabandoned`,
etc.) and writes candidates in the **same** `candidates.jsonl` format, so
`promote.py` consumes them unchanged (the TikTok like count fills the `upvotes`
field). It reuses the Reddit place-extraction, geocoding, region gate, and
classifier by import, so behavior matches the Reddit pipeline.

**This path is ToS-gray and fragile by design.** TikTok has no official public
search API, so discovery shells out to `yt-dlp` against public hashtag pages.
TikTok's anti-bot can block it at any time; when it does, the script prints a
clear message and exits gracefully instead of crashing. For durable volume the
clean answer is the official **TikTok Research API** — this is the scrappy
stopgap.

Hard rules baked in:

- **Embed-only.** Never downloads a video or image. The only thing stored is the
  public video URL as `{"type":"tiktok","url":"https://www.tiktok.com/@user/video/ID"}`.
- **No login / no credentials.** Public unauthenticated access only.
- Every discovered video is confirmed live via TikTok's **public oEmbed**
  endpoint (no key), which also supplies the author name for credit.

```bash
python tiktok_ingest.py --limit 20     # small test run
python tiktok_ingest.py                # full sweep (may be blocked — that's expected)
python tiktok_ingest.py --dry-run      # discover + geocode, write nothing
```

Needs `yt-dlp` (`pip install yt-dlp`; already present on this machine). Knobs
live in `config.py` under the TikTok section: `TIKTOK_HASHTAGS`,
`TIKTOK_PER_HASHTAG`, the sleeps, and `TIKTOK_MIN_LIKES`. Processed video ids
are remembered in `seen_tiktok_ids.txt` so reruns skip them.

## Cloud runner (the "PC-off" version)

To run overnight without your PC on, this script goes on a small always-on host
(same idea as the OS phone-access setup) on a nightly schedule, writing to the
**hosted** PocketBase instead of a local file. That needs the backend hosted
first — the next infrastructure step. The engine itself is already host-agnostic:
point it at a hosted DB and it just works.

## Files

| file | what |
|------|------|
| `config.py` | all the knobs — subreddits, region, gates |
| `reddit_ingest.py` | the engine: Reddit -> candidates.jsonl |
| `tiktok_ingest.py` | experimental TikTok hashtag discovery -> candidates.jsonl |
| `seen_tiktok_ids.txt` | tiktok video ids already processed, so reruns skip them |
| `promote.py` | candidates -> publishable ingested-spots.js |
| `candidates.jsonl` | staging output (git-ignored; regenerated each run) |
| `seen_ids.txt` | posts already processed, so reruns skip them |
