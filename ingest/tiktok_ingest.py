#!/usr/bin/env python3
"""
Prowl overnight spot builder — TikTok hashtag edition.  *** RETIRED 2026-08-26 ***

READ THIS FIRST:
  This path is RETIRED and running it normally is a no-op that exits 3.
  TikTok blocks this host at the transport layer — the TCP connection is reset
  (ConnectionResetError 10054) before any HTTP response exists, on every
  hashtag page, every time. There is no header or rate to tune around that.
  Hosted runners (datacenter IPs) fare worse, not better.

  It is retired rather than left running because a blocked run and a quiet
  night used to be byte-identical: it exited 0 with 0 rows four times and
  nothing noticed. Full reasoning in the RETIRED block below.

  `--force-attempt` re-runs the discovery to re-measure the block by hand.
  Even then it cannot exit 0 without producing real rows.

What it does (mirrors reddit_ingest.py so promote.py consumes it unchanged):
  discover by hashtag -> validate via PUBLIC oEmbed -> extract place ->
  geocode -> region gate -> dedupe -> classify -> candidates.jsonl

HARD LEGAL CONSTRAINTS baked into this script:
  1. EMBED-ONLY. It NEVER downloads a video or image file. The only thing it
     ever stores is the public video URL as an embed dict:
     {"type": "tiktok", "url": "https://www.tiktok.com/@user/video/ID"}.
     No media is ever written to disk.
  2. NO LOGIN / NO CREDENTIALS. No TikTok account, session cookie, or password
     is used or requested. Public unauthenticated access only.
  3. Each discovered video is confirmed live via TikTok's PUBLIC oEmbed endpoint
     (https://www.tiktok.com/oembed?url=...), which needs no key and returns
     author_name + html. That gives us author credit and proof the video is up.

The location/geocode/region/classify logic is REUSED from reddit_ingest.py by
import, so behavior stays identical to the Reddit pipeline.

Usage:
    python tiktok_ingest.py                 # full run, appends to candidates.jsonl
    python tiktok_ingest.py --limit 20      # stop after ~20 new candidates (test)
    python tiktok_ingest.py --dry-run       # discover + geocode, write nothing
"""
import os, sys, re, json, time, subprocess, argparse, pathlib

try:
    import requests
except ImportError:
    sys.exit("This needs the 'requests' package.  Run:  pip install requests")

import config as C
# Reuse the exact Reddit logic — do not duplicate it here.
from reddit_ingest import (extract_place, geocode, in_region, classify, norm_name,
                           dedupe_key, SourceHealth, GEO_HEALTH, validate_tiktok,
                           fail_blocked, fail_zero, EXIT_OK, EXIT_BLOCKED, _banner)

HERE = pathlib.Path(__file__).resolve().parent

_VIDEO_ID_RE = re.compile(r"/video/(\d+)")

# ------------------------------------------------------------- RETIRED STATE ---
# This path is retired from automated scheduling as of 2026-08-26.
#
# WHY, with the measurements:
#   * TikTok blocks this host at the TRANSPORT layer. Re-measured 2026-08-26:
#     every hashtag page returns ConnectionResetError(10054) before any HTTP
#     response exists. There is no request to tune, no header to change, no
#     rate to slow down. The connection is severed.
#   * From a GitHub hosted runner it is worse, not better: datacenter IPs are
#     blocked harder than residential ones.
#   * The damage this caused: tiktok_ingest exited 0 with 0 rows FOUR times and
#     no health check caught it, because a total block and a quiet night
#     produced byte-identical output. That incident is the origin of this
#     project's rule that zero yield is a hard failure.
#
# A permanently blocked platform must never be left quietly returning zero.
# Since this file cannot edit the workflow that schedules it, it takes
# responsibility for its own state: by default it now REFUSES TO RUN and exits
# EXIT_BLOCKED (3) immediately. It cannot be the silent zero again.
#
# --force-attempt still runs it, for the day someone wants to re-measure the
# block by hand. Even then it can no longer exit 0 without producing rows.
RETIRED = True
RETIRED_ON = "2026-08-26"
RETIRE_REASON = [
    "tiktok_ingest.py is RETIRED. It is not a working data source.",
    "",
    f"Retired {RETIRED_ON}. Measured cause: TikTok severs the TCP connection",
    "to this host (ConnectionResetError 10054) before returning any HTTP",
    "response, for every hashtag page, every time. Datacenter IPs such as",
    "GitHub hosted runners are blocked harder still.",
    "",
    "History: this script exited 0 with 0 rows four separate times without any",
    "health check noticing, because 'blocked' and 'quiet night' looked the",
    "same. It will not do that again — it now exits 3 (BLOCKED) instead.",
    "",
    "The durable replacement is the official TikTok Research API",
    "(https://developers.tiktok.com/products/research-api/), which requires an",
    "application and approval. Until that exists, TikTok media reaches the",
    "pipeline only via reddit_ingest.py's harvest_social(), which picks up",
    "TikTok links that people already posted to Reddit.",
    "",
    "To re-measure the block by hand:  python tiktok_ingest.py --force-attempt",
    "That still cannot exit 0 unless it genuinely produces rows.",
]


# ----------------------------------------------------------- discovery layer ---
# TikTok has no public search API. Best-effort: shell out to yt-dlp against the
# public hashtag page and read the flat playlist as JSON. This is the fragile
# bit — wrapped defensively so any failure returns [] instead of crashing.
def _ytdlp_available():
    """True if yt-dlp can be invoked as a python module (most portable form)."""
    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def discover_hashtag(tag, limit, health=None):
    """Return a list of {id, url, title, likes, author} for one hashtag.

    Uses:  yt-dlp "https://www.tiktok.com/tag/TAG" --flat-playlist --dump-json
    Defensive by design — TikTok anti-bot commonly blocks this. On ANY failure
    (non-zero exit, no output, timeout, bad JSON) it prints a clear note and
    returns an empty list so the run continues gracefully.
    """
    tag_url = f"https://www.tiktok.com/tag/{tag}"
    cmd = [
        sys.executable, "-m", "yt_dlp",
        tag_url,
        "--flat-playlist",
        "--dump-json",
        "--playlist-end", str(limit),
        "--no-warnings",
        "--ignore-errors",
    ]
    try:
        # encoding pinned: on Windows text=True decodes with the ANSI codepage,
        # and yt-dlp's UTF-8 JSON (TikTok titles are full of emoji) raises
        # UnicodeDecodeError, killing the whole hashtag.
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
    except subprocess.TimeoutExpired:
        print(f"   discover #{tag}: timed out (TikTok likely throttling) — skipping")
        if health:
            health.note("blocked", f"#{tag}: yt-dlp timed out after 180s")
        return []
    except Exception as e:
        print(f"   discover #{tag}: failed to launch yt-dlp ({e}) — skipping")
        if health:
            health.note("errors", f"#{tag}: could not launch yt-dlp: {e}")
        return []

    if r.returncode != 0 and not r.stdout.strip():
        # Anti-bot / severed connection. This is a REFUSAL and is now recorded
        # as one; it used to vanish into an empty list.
        msg = (r.stderr or "").strip().splitlines()
        hint = msg[-1] if msg else "no output"
        print(f"   discover #{tag}: yt-dlp returned nothing (TikTok anti-bot?) — {hint[:120]}")
        if health:
            health.note("blocked", f"#{tag}: {hint[:160]}")
        return []

    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue
        url = j.get("url") or j.get("webpage_url") or ""
        vid = j.get("id") or ""
        if not vid:
            m = _VIDEO_ID_RE.search(url)
            vid = m.group(1) if m else ""
        # Normalize to a canonical public video URL when we can.
        if "/video/" not in url and vid and j.get("uploader"):
            url = f"https://www.tiktok.com/@{j['uploader']}/video/{vid}"
        if not url or not vid:
            continue
        out.append({
            "id": str(vid),
            "url": url.split("?")[0],
            "title": (j.get("title") or j.get("description") or "").strip(),
            "likes": int(j.get("like_count") or 0),
            "author": j.get("uploader") or j.get("channel") or "",
        })
    if health:
        health.note("ok" if out else "empty",
                    "" if out else f"#{tag}: yt-dlp exited cleanly with zero videos")
    return out


# ---------------------------------------------------------- classify/build ---
def build_candidate(item, place, conf, lat, lng, cat, author):
    """Emit a candidate in the SAME shape reddit_ingest.py writes, so promote.py
    reads it with zero changes. 'upvotes' is populated from the TikTok like
    count, per spec. The only embed is the public video URL — never media."""
    title = item.get("title", "").strip()
    name = (title or f"TikTok #{cat} spot")[:80]
    embeds = [{"type": "tiktok", "url": item["url"]}][:C.MAX_EMBEDS]
    return {
        "source": "tiktok",
        "tiktok_id": item.get("id"),
        "name": name,
        "cat": cat,
        "lat": round(lat, 5),
        "lng": round(lng, 5),
        "place": place,
        "location_confidence": conf,
        "upvotes": item.get("likes", 0),     # like count reused as the signal
        "author": author or item.get("author"),
        "embeds": embeds,
        "social_embeds": 1,                  # the tiktok video itself
        "desc": (title or name)[:400],
        # Auto-collected -> always needs a human before going live.
        "needs_review": True,
        "legal_status": "unverified",
        "tags": ["auto-ingested", "tiktok", f"loc-{'exact' if conf >= 0.9 else 'approx'}",
                 "has-video"],
    }


# ------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="stop after N new candidates")
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    ap.add_argument("--force-attempt", action="store_true",
                    help="run the retired discovery path anyway, to re-measure the block")
    args = ap.parse_args()

    if RETIRED and not args.force_attempt:
        _banner("RETIRED: TIKTOK INGEST IS NOT A WORKING SOURCE", RETIRE_REASON)
        sys.exit(EXIT_BLOCKED)

    print("TikTok ingest — RETIRED path, forced attempt. Blocks are expected.")
    if not _ytdlp_available():
        sys.exit(
            "yt-dlp is not available (tried: python -m yt_dlp).\n"
            "Discovery needs it. Install with:  pip install yt-dlp\n"
            "This path is fragile regardless; the durable option is the TikTok Research API."
        )

    seen_path = HERE / C.TIKTOK_SEEN_FILE
    seen = (set(seen_path.read_text(encoding="utf-8", errors="ignore").split())
            if seen_path.exists() else set())
    newly_seen, transient = set(), set()
    run_keys = set()
    health = SourceHealth("tiktok")

    print(f"region={C.REGION_NAME}  hashtags={len(C.TIKTOK_HASHTAGS)}  "
          f"per_hashtag={C.TIKTOK_PER_HASHTAG}")

    cand_path = HERE / C.CANDIDATES_FILE
    out = None if args.dry_run else cand_path.open("a", encoding="utf-8")
    stats = dict(discovered=0, dead=0, no_place=0, geo_fail=0,
                 out_region=0, low_likes=0, dup=0, kept=0)
    total_found = 0

    try:
        for tag in C.TIKTOK_HASHTAGS:
            print(f"discovering #{tag} ...")
            items = discover_hashtag(tag, C.TIKTOK_PER_HASHTAG, health)
            total_found += len(items)
            time.sleep(C.TIKTOK_DISCOVER_SLEEP)
            for item in items:
                vid = item.get("id")
                if not vid or vid in seen or vid in newly_seen:
                    continue
                newly_seen.add(vid); stats["discovered"] += 1
                if item.get("likes", 0) < C.TIKTOK_MIN_LIKES:
                    stats["low_likes"] += 1; continue
                # Confirm the video is live via public oEmbed + grab author credit.
                # Three states now: live / dead / blocked. "blocked" is transient
                # and must not be filed as "dead", nor burn the id forever.
                status, author = validate_tiktok(item["url"])
                if status == "blocked":
                    transient.add(vid)
                    health.note("blocked", f"oEmbed unreachable for {item['url']}")
                    stats["dead"] += 1; continue
                if status != "live":
                    stats["dead"] += 1; continue
                # Reuse Reddit place/geocode/region/classify logic verbatim.
                place, conf = extract_place(item.get("title", ""), None)
                if not place:
                    stats["no_place"] += 1; continue
                coords = geocode(place)
                if not coords:
                    transient.add(vid)
                    stats["geo_fail"] += 1; continue
                lat, lng = coords
                if not in_region(lat, lng):
                    stats["out_region"] += 1; continue
                cat = classify(item.get("title", ""), None)
                k = dedupe_key(item.get("title", "") or vid, lat, lng)
                if k in run_keys:
                    stats["dup"] += 1; continue
                run_keys.add(k)
                cand = build_candidate(item, place, conf, lat, lng, cat, author)
                stats["kept"] += 1
                print(f"  + [{cat}] {cand['name'][:50]}  ({place}, conf {conf})  📹by @{author}")
                if out:
                    out.write(json.dumps(cand, ensure_ascii=False) + "\n"); out.flush()
                if args.limit and stats["kept"] >= args.limit:
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
    for k, v in stats.items():
        print(f"  {k:12}: {v}")
    print(f"  {'retryable':12}: {len(transient)} (unreachable, not marked seen)")
    print("\n=== source health ===")
    print(health.detail())
    if GEO_HEALTH.attempts:
        print(GEO_HEALTH.detail())

    if stats["kept"]:
        if not args.dry_run:
            print(f"\nCandidates appended to {cand_path}")
            print("Next: review them, then run  promote.py  to publish the good ones.")
        sys.exit(EXIT_OK)

    # The four silent exit-0-with-zero-rows runs died right here. Never again.
    if total_found == 0 or health.verdict() == "blocked":
        fail_blocked("tiktok",
                     health.detail() +
                     "\n\nDiscovery returned zero videos across all "
                     f"{len(C.TIKTOK_HASHTAGS)} hashtags. This is the block, not a "
                     "quiet night.\nSee the RETIRED notice at the top of this file.")
    if GEO_HEALTH.attempts and GEO_HEALTH.verdict() == "blocked":
        fail_blocked("nominatim (geocoder)", GEO_HEALTH.detail())
    fail_zero("tiktok", health.detail() +
              f"\n{stats['discovered']} videos examined, none usable.")


if __name__ == "__main__":
    main()
