# Handoff: what this is, what breaks, and where we want to take it

Written for the dev picking this up. Jack (Wing Digital) built this with heavy AI assistance; the goal now is to harden it so it runs on pure Python, no AI in the loop, 24/7 on a cheap always-on box.

## What the pipeline does today

Three scripts in `ingest/`:

1. **reddit_ingest.py** (the solid path). Uses Reddit's official free API to search subreddits for posts about real places, extracts the place name, geocodes it with Nominatim (OpenStreetMap, free), filters to a region bounding box, dedupes, classifies by keyword, and harvests any TikTok/Instagram links inside the posts. TikTok links get validated through TikTok's public oEmbed endpoint (no key needed). Output: one JSON candidate per line in `candidates.jsonl`.
2. **tiktok_ingest.py** (the fragile path). Discovers TikTok videos directly by hashtag by shelling out to `yt-dlp` against public hashtag pages. Same output format.
3. **promote.py**. Turns reviewed candidates into the publishable JS data file.

All tunables (subreddits, hashtags, region box, quality gates, rate-limit sleeps) live in `config.py`. No AI anywhere: place extraction and classification are regex/keyword based on purpose.

## Setup

1. Free Reddit API key: https://www.reddit.com/prefs/apps, create a "script" type app, copy client id + secret into a `.env` (see `.env.example`).
2. `pip install requests yt-dlp`
3. Point `ENV_PATH` at the top of `reddit_ingest.py` to your `.env` (it currently points at a path on Jack's machine).
4. Change `USER_AGENT` in `config.py` to your own contact info.

## Known limitations (the honest list)

**TikTok**
- There is no official public TikTok search API. The hashtag discovery path rides on `yt-dlp` hitting public pages, and TikTok's anti-bot blocks it unpredictably. When blocked, the script exits gracefully; expect this to happen often from datacenter IPs (so a cheap VPS running it 24/7 will get blocked faster than a home connection).
- oEmbed validation is reliable and keyless, but it only tells you a public video exists; it gives no engagement data beyond what discovery scraped.
- The durable fix is the official TikTok Research API or Display API (application required), or a paid scraping provider (Apify, ScrapeCreators, etc.). Everything else is a cat-and-mouse game: it needs rotating residential proxies, realistic delays, and constant maintenance when TikTok changes markup.

**Instagram**
- Today we only capture IG links that other people posted on Reddit. There is no direct IG scraping at all. Direct IG scraping without login breaks quickly (IG walls off almost everything behind auth), and scraping while logged in risks the account. The official route is the Instagram Graph API, which only covers accounts you own/manage plus limited hashtag search for business accounts.

**Geocoding**
- Nominatim is free but hard-capped at 1 request/second and will ban abusers. Fine for overnight runs, a bottleneck at scale. Options: self-host Nominatim, or cache aggressively (we already sleep 1.1s between calls).

**Reddit**
- The free API tier is 100 queries/min, which we stay well under. Solid. Main weakness is place extraction: it is keyword-based and misses posts that describe a place without naming it cleanly.

**Running 24/7**
- Right now it writes local files and assumes Jack's PC. To go 24/7: a small VPS or a GitHub Actions cron, output to a hosted DB (Supabase/PocketBase) instead of local jsonl, and alerting when TikTok blocks kick in. The code is close to host-agnostic already; the file paths and ENV_PATH are the main things to abstract.

## The end goal, and what is actually buildable

The vision: monitor socials for relevant conversations, auto-draft replies, and market to people through DMs. Splitting that into lanes:

**Buildable and safe**
- 24/7 monitoring/scraping with the hardening above.
- Auto-drafting replies into a review queue (a human clicks send). Drafting can stay non-AI with templates, or use a free-tier LLM later if we relax the no-AI rule for that one step.
- Instagram DM automation through the official Messenger/Instagram Messaging API, but only for people who message the business account first (or comment-triggered opt-ins, the ManyChat model). This is the legit version of "automated DMs" and it works well.

**Not buildable without burning accounts**
- Mass unsolicited cold DMs on TikTok or Instagram. Both platforms detect and ban for this aggressively (device fingerprinting, send-rate limits, spam reports), and it violates their terms; commercial cold-DM spam can also create legal exposure (CAN-SPAM-adjacent state laws, TCPA analogies). Any tool selling this is selling banned accounts on a delay. We should not architect toward it.

**The realistic funnel** is: scrape publicly to find warm prospects and conversations, engage manually or via official APIs where people opted in, and push cold outreach through channels built for it (email). That keeps the 24/7 scraper valuable without putting accounts at risk.

## Suggested first tasks

1. Abstract `ENV_PATH` and output paths into `config.py` so it runs anywhere.
2. Add retry/backoff + a simple health log so an unattended run reports what happened.
3. Move output from `candidates.jsonl` to SQLite (or Supabase) so multiple runs merge cleanly.
4. Wrap the whole thing in one `run_all.py` entrypoint suitable for cron.
5. Experiment: how long does the yt-dlp TikTok path survive from a VPS vs home IP? That decides whether we need a paid data provider.

## Prospect mode (added 2026-08-21)

The pipeline now has a second, more valuable mode: finding CLIENTS instead of
places. Same plumbing, different input.

```
social_discover.py   niche + city -> public search index -> TikTok/IG/LinkedIn
                     profiles -> candidates.jsonl
audit_prospect.py    each prospect -> Google Maps (free gosom binary) + their
                     own website + SERP position -> need_score + gap list
enrich.py            score + intent + templated first-touch DM draft
db.py                -> Supabase
queue/serve.py       ranked review queue: approve / edit / skip / copy
queue/ghl_push.py    approved -> GoHighLevel contacts tagged social-lead
```

**Why search-index discovery instead of scraping the platforms.** TikTok blocks
direct scraping (measured: two runs, zero results), Instagram walls everything
behind login, and LinkedIn bans accounts and litigates over scraping. But all
three let Google index their public profiles. Querying the index gets the same
data with no blocks, no login, no keys, and no ToS problem. This is the single
most important design decision in the project.

**Data honesty rules baked into audit_prospect.py.** These exist because early
versions produced claims that would embarrass someone on a sales call:

- A website is only accepted when ownership is VERIFIED (domain matches the
  business name within a length ratio, or the name is in the page title).
  An early version matched a roofer to ultimate-guitar.com.
- Length guards on every fuzzy match. "roofingdallas" must not match
  "metalroofingdallas" — different company. Same guard stops a Dallas prospect
  matching a same-named Colorado business.
- Unreadable site (403/timeout) never counts as "no blog" or "thin site".
  Unknown is not missing; it emits "verify by hand" instead.
- Review numbers are only trusted from a snippet that names the business.
- "Not ranking" is only scored when a website was actually found, so the
  same weakness is never counted twice.

**Known limitations.** Website discovery is conservative and will return None
rather than guess, so some real sites are missed. Google Maps fast-mode returns
review_count = 0 (browser mode hits a consent gate), so review counts come from
search snippets and are best-effort. Maps results are cached per niche+city in
.maps_cache/ — delete that folder to refresh.

## Batch sweep (added 2026-08-22)

`sweep.py` runs the whole prospect pipeline across every DFW city x niche pair:

```bash
python ingest/sweep.py --niches roofing              # 45 DFW cities
python ingest/sweep.py --tier core                   # 10 biggest cities
python ingest/sweep.py --niches roofing,hvac --cities Dallas,Plano
python ingest/sweep.py --status                      # progress
python ingest/sweep.py --no-audit                    # discover now, audit later
```

Resumable: each finished pair is recorded in `sweep_state.json`, so Ctrl-C and
rerun continues where it stopped. Run it overnight — a full sweep is hours.

**Bugs found by actually running it, all fixed.** Worth knowing about, because
each one was invisible until real data went through:

- One transient Supabase connection blip killed an entire step. Every DB call
  now retries with backoff.
- The audit re-scanned the whole unaudited backlog each pair, so every city took
  longer than the last until it hit the timeout. Batches are now bounded.
- The audit ran serially at roughly a minute per prospect. It now runs
  concurrently (`--workers`, default 4).
- A failed audit used to discard a successful discovery. Prospects are already
  in Supabase by then, so the pair is kept and a later audit pass finishes it.

**Geography warnings.** A Plano sweep surfaced an Oklahoma "Litz Roofing" (405
area code) and an India-hosted "Sunaura Solar" (.in domain). Same-name
businesses in other states are a real failure mode for name-based discovery, so
prospects now carry an explicit WARNING gap when the area code is outside DFW or
the domain is a non-US TLD. They are flagged, never silently deleted — the human
decides.
