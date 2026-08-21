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
                           dedupe_key, load_env)

HERE = pathlib.Path(__file__).resolve().parent
API = "https://www.googleapis.com/youtube/v3"


def yt_search(key, query, limit):
    """One search.list call (100 quota units). Returns raw video items."""
    r = requests.get(f"{API}/search", timeout=30, params={
        "key": key, "q": query, "part": "snippet", "type": "video",
        "maxResults": min(limit, 50), "relevanceLanguage": "en",
        "safeSearch": "none", "order": C.YOUTUBE_ORDER,
    })
    time.sleep(C.YOUTUBE_SLEEP)
    if r.status_code == 403:
        sys.exit("YouTube API 403 — key invalid or daily quota exhausted:\n"
                 + r.text[:300])
    if r.status_code != 200:
        print(f"   search '{query}' -> {r.status_code}"); return []
    return r.json().get("items", [])


def yt_stats(key, video_ids):
    """Batch like/view counts for up to 50 ids (1 quota unit)."""
    if not video_ids:
        return {}
    r = requests.get(f"{API}/videos", timeout=30, params={
        "key": key, "id": ",".join(video_ids[:50]), "part": "statistics"})
    time.sleep(C.YOUTUBE_SLEEP)
    if r.status_code != 200:
        return {}
    return {v["id"]: v.get("statistics", {}) for v in r.json().get("items", [])}


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

    key = load_env().get("YOUTUBE_API_KEY")
    if not key:
        sys.exit("Missing YOUTUBE_API_KEY.\n"
                 "Free key: console.cloud.google.com -> enable 'YouTube Data API v3'"
                 " -> Credentials -> API key.")

    seen_path = HERE / C.YOUTUBE_SEEN_FILE
    seen = set(seen_path.read_text().split()) if seen_path.exists() else set()
    run_keys = set()

    cand_path = HERE / C.CANDIDATES_FILE
    out = None if args.dry_run else cand_path.open("a", encoding="utf-8")
    stats_ct = dict(discovered=0, no_place=0, geo_fail=0, out_region=0,
                    low_likes=0, dup=0, kept=0)

    print(f"YouTube ingest (official API) — region={C.REGION_NAME} "
          f"queries={len(C.YOUTUBE_QUERIES)}")
    try:
        for q in C.YOUTUBE_QUERIES:
            print(f"searching '{q}' ...")
            items = yt_search(key, q, C.YOUTUBE_PER_QUERY)
            ids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]
            stat_map = yt_stats(key, ids)
            for item in items:
                vid = item.get("id", {}).get("videoId")
                if not vid or vid in seen:
                    continue
                seen.add(vid); stats_ct["discovered"] += 1
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
            seen_path.write_text("\n".join(sorted(seen)))

    print("\n=== run summary ===")
    for k, v in stats_ct.items():
        print(f"  {k:12}: {v}")


if __name__ == "__main__":
    main()
