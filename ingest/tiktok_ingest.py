#!/usr/bin/env python3
"""
Prowl overnight spot builder — TikTok hashtag edition (EXPERIMENTAL / FRAGILE).

WARNING, READ THIS FIRST:
  This is an experimental, ToS-gray, best-effort discovery path. TikTok has NO
  official public search API, so this leans on an unofficial method (yt-dlp
  against public hashtag pages). TikTok's anti-bot can block it at ANY time,
  and when it does this script simply exits with a clear message instead of
  crashing. Expect it to be flaky. The durable, clean path for real volume is
  the official TikTok Research API — this file is the scrappy stopgap.

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
from reddit_ingest import extract_place, geocode, in_region, classify, norm_name, dedupe_key

HERE = pathlib.Path(__file__).resolve().parent

_VIDEO_ID_RE = re.compile(r"/video/(\d+)")


# ------------------------------------------------------- tiktok oembed check ---
def validate_tiktok(url):
    """Public oEmbed check — confirms the video is live and returns author. No key.

    Returns author_name (str) if live, else None. This is the SAME public
    endpoint reddit_ingest.py uses; no login, no key, embed-safe.
    """
    try:
        r = requests.get(C.TIKTOK_OEMBED, params={"url": url},
                         headers={"User-Agent": C.USER_AGENT}, timeout=20)
        time.sleep(C.TIKTOK_VALIDATE_SLEEP)
        if r.status_code == 200 and r.json().get("html"):
            return r.json().get("author_name")
    except Exception:
        pass
    return None


# ----------------------------------------------------------- discovery layer ---
# TikTok has no public search API. Best-effort: shell out to yt-dlp against the
# public hashtag page and read the flat playlist as JSON. This is the fragile
# bit — wrapped defensively so any failure returns [] instead of crashing.
def _ytdlp_available():
    """True if yt-dlp can be invoked as a python module (most portable form)."""
    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def discover_hashtag(tag, limit):
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
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print(f"   discover #{tag}: timed out (TikTok likely throttling) — skipping")
        return []
    except Exception as e:
        print(f"   discover #{tag}: failed to launch yt-dlp ({e}) — skipping")
        return []

    if r.returncode != 0 and not r.stdout.strip():
        # This is the expected anti-bot / empty-page case. Not a crash.
        msg = (r.stderr or "").strip().splitlines()
        hint = msg[-1] if msg else "no output"
        print(f"   discover #{tag}: yt-dlp returned nothing (TikTok anti-bot?) — {hint[:120]}")
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
    args = ap.parse_args()

    print("TikTok ingest — EXPERIMENTAL, embed-only, no login. Blocks are expected.")
    if not _ytdlp_available():
        sys.exit(
            "yt-dlp is not available (tried: python -m yt_dlp).\n"
            "Discovery needs it. Install with:  pip install yt-dlp\n"
            "This path is fragile regardless; the durable option is the TikTok Research API."
        )

    seen_path = HERE / C.TIKTOK_SEEN_FILE
    seen = set(seen_path.read_text().split()) if seen_path.exists() else set()
    run_keys = set()

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
            items = discover_hashtag(tag, C.TIKTOK_PER_HASHTAG)
            total_found += len(items)
            time.sleep(C.TIKTOK_DISCOVER_SLEEP)
            for item in items:
                vid = item.get("id")
                if not vid or vid in seen:
                    continue
                seen.add(vid); stats["discovered"] += 1
                if item.get("likes", 0) < C.TIKTOK_MIN_LIKES:
                    stats["low_likes"] += 1; continue
                # Confirm the video is live via public oEmbed + grab author credit.
                author = validate_tiktok(item["url"])
                if author is None:
                    stats["dead"] += 1; continue
                # Reuse Reddit place/geocode/region/classify logic verbatim.
                place, conf = extract_place(item.get("title", ""), None)
                if not place:
                    stats["no_place"] += 1; continue
                coords = geocode(place)
                if not coords:
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
            seen_path.write_text("\n".join(sorted(seen)))

    if total_found == 0:
        print("\nDiscovery returned zero videos across all hashtags.")
        print("This is the EXPECTED failure mode: TikTok anti-bot is blocking the")
        print("unofficial path. Nothing was written. Try again later, or move to the")
        print("official TikTok Research API for a durable pipeline.")

    print("\n=== run summary ===")
    for k, v in stats.items():
        print(f"  {k:12}: {v}")
    if not args.dry_run and stats["kept"]:
        print(f"\nCandidates appended to {cand_path}")
        print("Next: review them, then run  promote.py  to publish the good ones.")


if __name__ == "__main__":
    main()
