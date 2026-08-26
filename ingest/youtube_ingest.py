#!/usr/bin/env python3
"""
YouTube discovery leg — OFFICIAL API, the durable counterpart to the TikTok path.

Uses the YouTube Data API v3 (free key, 10k quota units/day; each search costs
100 units so the defaults below use ~1,600/day). Finds videos (incl. Shorts)
matching the region queries, then reuses the exact Reddit place/geocode/region/
classify logic so output lands in the SAME candidates.jsonl shape.

Embed-only, same hard rules as the rest of the pipeline: only the public video
URL is stored, no media ever downloaded, no login.

Env: YOUTUBE_API_KEY (real env var, or in the .env at repo root / $ENV_FILE).
Get one free: console.cloud.google.com -> APIs & Services -> enable
"YouTube Data API v3" -> Credentials -> API key.

Usage:
    python youtube_ingest.py                # full run, appends to candidates.jsonl
    python youtube_ingest.py --limit 20     # stop after N new candidates
    python youtube_ingest.py --dry-run      # discover + geocode, write nothing
"""
import argparse
import json
import pathlib
import sys
import time

import requests

import config as C
from reddit_ingest import (extract_place, geocode, in_region, classify,
                           dedupe_key, load_env, SourceHealth, GEO_HEALTH,
                           fail_config, fail_blocked, fail_zero,
                           EXIT_OK)

HERE = pathlib.Path(__file__).resolve().parent
API = "https://www.googleapis.com/youtube/v3"

YOUTUBE_KEY_HOWTO = """\
  1. Open  https://console.cloud.google.com/  and sign in.
  2. Create (or pick) a project from the project dropdown at the top.
  3. APIs & Services -> Library -> search "YouTube Data API v3" -> ENABLE.
     (This step is the one people skip; a key without the API enabled
      returns 403 accessNotConfigured, not a helpful message.)
  4. APIs & Services -> Credentials -> Create credentials -> API key. Copy it.
  5. Store it. Add to C:\\Users\\wjack\\ghl-cli\\.env :
       YOUTUBE_API_KEY=<the key>
     ...and/or for the cloud lane, from the repo root:
       gh secret set YOUTUBE_API_KEY
  6. Verify:  cd ingest && python youtube_ingest.py --dry-run --limit 5
  Cost: free tier, 10,000 quota units/day. Each search costs 100 units, and
  config.py currently defines 16 queries = ~1,600 units per run."""


def _yt_error_reason(r):
    try:
        errs = r.json().get("error", {}).get("errors", [])
        return (errs[0].get("reason") or "") if errs else ""
    except (ValueError, IndexError, AttributeError):
        return ""


def yt_search(key, query, limit, health=None):
    """One search.list call (100 quota units). Returns raw video items."""
    try:
        r = requests.get(f"{API}/search", timeout=30, params={
            "key": key, "q": query, "part": "snippet", "type": "video",
            "maxResults": min(limit, 50), "relevanceLanguage": "en",
            "safeSearch": "none", "order": C.YOUTUBE_ORDER,
        })
    except requests.RequestException as e:
        print(f"   search '{query}' -> transport error: {e}")
        if health:
            health.note("blocked", f"'{query}': transport error {type(e).__name__}: {e}")
        return []
    time.sleep(C.YOUTUBE_SLEEP)
    if r.status_code in (400, 403):
        reason = _yt_error_reason(r)
        # A bad/unconfigured key is a CONFIG problem Jack can fix in minutes.
        # An exhausted quota is the platform refusing us for today. Different
        # things; they used to share one sys.exit and one exit code.
        if reason in ("keyInvalid", "accessNotConfigured", "forbidden",
                      "ipRefererBlocked", "badRequest"):
            fail_config(
                f"a WORKING YOUTUBE_API_KEY (the key present was rejected: "
                f"HTTP {r.status_code} reason={reason or 'unspecified'})\n"
                f"  {r.text[:200]}",
                YOUTUBE_KEY_HOWTO)
        fail_blocked("youtube",
                     f"search.list -> HTTP {r.status_code} reason="
                     f"{reason or 'unspecified'}\n{r.text[:300]}\n"
                     f"quotaExceeded means the 10k/day budget is spent; the run "
                     f"is not 'empty', it is cut off.")
    if r.status_code != 200:
        print(f"   search '{query}' -> {r.status_code}")
        if health:
            health.note("errors", f"'{query}': HTTP {r.status_code}")
        return []
    try:
        items = r.json().get("items", [])
    except ValueError:
        if health:
            health.note("errors", f"'{query}': 200 with non-JSON body")
        return []
    if health:
        health.note("ok" if items else "empty",
                    "" if items else f"'{query}': HTTP 200 with zero items")
    return items


def yt_stats(key, video_ids):
    """Batch like/view counts for up to 50 ids (1 quota unit)."""
    if not video_ids:
        return {}
    try:
        r = requests.get(f"{API}/videos", timeout=30, params={
            "key": key, "id": ",".join(video_ids[:50]), "part": "statistics"})
    except requests.RequestException as e:
        print(f"   !! stats lookup failed ({type(e).__name__}: {e}) — "
              f"like counts unavailable for {len(video_ids[:50])} videos")
        return {}
    time.sleep(C.YOUTUBE_SLEEP)
    if r.status_code != 200:
        # Was a bare `return {}`. That reads downstream as "every video has 0
        # likes", which silently deletes everything the moment YOUTUBE_MIN_LIKES
        # is raised above 0. Say it out loud instead.
        print(f"   !! stats lookup -> HTTP {r.status_code}; like counts "
              f"unavailable for {len(video_ids[:50])} videos "
              f"(they will read as 0 likes)")
        return {}
    try:
        return {v["id"]: v.get("statistics", {}) for v in r.json().get("items", [])}
    except ValueError:
        print("   !! stats lookup returned 200 with a non-JSON body")
        return {}


def build_candidate(vid, sn, stats, place, conf, lat, lng, cat):
    title = sn.get("title", "").strip()
    url = f"https://www.youtube.com/watch?v={vid}"
    return {
        "source": "youtube",
        "id": vid,
        "name": (title or f"YouTube {cat} spot")[:80],
        "title": title,
        "cat": cat,
        "category": cat,
        "lat": round(lat, 5),
        "lng": round(lng, 5),
        "place": place,
        "location_confidence": conf,
        "upvotes": int(stats.get("likeCount") or 0),
        "views": int(stats.get("viewCount") or 0),
        "author": sn.get("channelTitle"),
        "created_utc": _to_epoch(sn.get("publishedAt")),
        "embeds": [{"type": "youtube", "url": url}],
        "social_embeds": 1,
        "desc": (sn.get("description") or title)[:400],
        "needs_review": True,
        "legal_status": "unverified",
        "tags": ["auto-ingested", "youtube",
                 f"loc-{'exact' if conf >= 0.9 else 'approx'}", "has-video"],
    }


def _to_epoch(iso):
    if not iso:
        return None
    import datetime
    try:
        return int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    key = (load_env().get("YOUTUBE_API_KEY") or "").strip()
    if not key:
        fail_config(
            "YOUTUBE_API_KEY\n"
            "  Looked in environment variables and in the .env file.\n"
            "  This key has never been created, so the YouTube leg of\n"
            "  nightly-ingest has never actually run even once — the workflow\n"
            "  step silently skips it when the secret is empty.",
            YOUTUBE_KEY_HOWTO)

    seen_path = HERE / C.YOUTUBE_SEEN_FILE
    seen = (set(seen_path.read_text(encoding="utf-8", errors="ignore").split())
            if seen_path.exists() else set())
    newly_seen, transient = set(), set()
    run_keys = set()
    health = SourceHealth("youtube")

    cand_path = HERE / C.CANDIDATES_FILE
    out = None if args.dry_run else cand_path.open("a", encoding="utf-8")
    stats_ct = dict(discovered=0, no_place=0, geo_fail=0, out_region=0,
                    low_likes=0, dup=0, kept=0)

    print(f"YouTube ingest (official API) — region={C.REGION_NAME} "
          f"queries={len(C.YOUTUBE_QUERIES)}")
    try:
        for q in C.YOUTUBE_QUERIES:
            print(f"searching '{q}' ...")
            items = yt_search(key, q, C.YOUTUBE_PER_QUERY, health)
            ids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]
            stat_map = yt_stats(key, ids)
            for item in items:
                vid = item.get("id", {}).get("videoId")
                if not vid or vid in seen or vid in newly_seen:
                    continue
                newly_seen.add(vid); stats_ct["discovered"] += 1
                sn = item.get("snippet", {})
                st = stat_map.get(vid, {})
                if int(st.get("likeCount") or 0) < C.YOUTUBE_MIN_LIKES:
                    stats_ct["low_likes"] += 1; continue
                text = f"{sn.get('title', '')} {sn.get('description', '')}"
                place, conf = extract_place(text, None)
                if not place:
                    stats_ct["no_place"] += 1; continue
                coords = geocode(place)
                if not coords:
                    transient.add(vid)          # geocoder down = retry later
                    stats_ct["geo_fail"] += 1; continue
                lat, lng = coords
                if not in_region(lat, lng):
                    stats_ct["out_region"] += 1; continue
                cat = classify(text, None)
                k = dedupe_key(sn.get("title", "") or vid, lat, lng)
                if k in run_keys:
                    stats_ct["dup"] += 1; continue
                run_keys.add(k)
                cand = build_candidate(vid, sn, st, place, conf, lat, lng, cat)
                stats_ct["kept"] += 1
                print(f"  + [{cat}] {cand['name'][:50]}  ({place}, conf {conf})")
                if out:
                    out.write(json.dumps(cand, ensure_ascii=False) + "\n"); out.flush()
                if args.limit and stats_ct["kept"] >= args.limit:
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        print("\nStopping early (limit reached or interrupted).")
    finally:
        if out:
            out.close()
        if not args.dry_run:
            seen_path.write_text("\n".join(sorted(seen | (newly_seen - transient))),
                                 encoding="utf-8")

    print("\n=== run summary ===")
    for k, v in stats_ct.items():
        print(f"  {k:12}: {v}")
    print(f"  {'retryable':12}: {len(transient)} (geocoder down — not marked seen)")
    print("\n=== source health ===")
    print(health.detail())
    if GEO_HEALTH.attempts:
        print(GEO_HEALTH.detail())

    if stats_ct["kept"]:
        if not args.dry_run:
            print(f"\nCandidates appended to {cand_path}")
        sys.exit(EXIT_OK)

    if health.verdict() == "blocked":
        fail_blocked("youtube", health.detail())
    if GEO_HEALTH.attempts and GEO_HEALTH.verdict() == "blocked":
        fail_blocked("nominatim (geocoder)",
                     "YouTube answered fine, but the GEOCODER refused us, so every\n"
                     "candidate died at the geocode step:\n" + GEO_HEALTH.detail())
    fail_zero("youtube",
              health.detail() +
              f"\n{stats_ct['discovered']} new videos examined; none survived "
              f"(no_place={stats_ct['no_place']} geo_fail={stats_ct['geo_fail']} "
              f"out_region={stats_ct['out_region']} low_likes={stats_ct['low_likes']} "
              f"dup={stats_ct['dup']}).")


if __name__ == "__main__":
    main()
