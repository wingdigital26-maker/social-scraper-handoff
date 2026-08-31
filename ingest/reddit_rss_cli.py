#!/usr/bin/env python3
"""reddit_rss_cli.py - fresh local leads from Reddit's public per-subreddit RSS.

Why RSS and not search:

  A search index ranks by RELEVANCE and has no memory of what you already saw,
  so it hands back the same 2016 thread forever. That is exactly how the pool
  reached a 887-day median age while every row looked two days old. A /new/.rss
  feed is ordered by TIME and has an edge you can remember, so freshness stops
  being a filter applied afterwards and becomes a property of the source.

  It also needs no API key and no login. Measured 2026-08-31:
  www.reddit.com/r/<sub>/new/.rss returns 200 with real timestamps, while
  old.reddit.com/...json returns 403 and the HTML pages bot-wall with a 200
  plus a "Prove your humanity" body. Never trust a 200 alone.

The two failure modes this guards against, both silent by nature:

  WINDOW OVERFLOW. The feed holds only 25 items. If a sub posts faster than we
  poll, older posts fall off the end and are simply never seen. Nothing errors.
  So when a run finds 25 brand-new items we say so loudly: the window
  overflowed and posts were missed. Poll that sub more often.
  The rule is  poll_interval < 25 / posts_per_day.

  STALE PASSING AS FRESH. Anything past MAX_AGE_HOURS is dropped here, at the
  door, and the drop count is logged. A lead nobody can act on should never
  cost a judge call, a draft, or a place in the queue.

Location comes from the SUBREDDIT, not from a query, which is what makes these
rows geographically trustworthy. For a metro sub the poster could be anywhere
in the metro, so the city is recorded as unknown (null) rather than guessed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import time
from pathlib import Path

import requests

import geo_communities as geo

UA = {"User-Agent": "WingDigital-LeadScout/1.0 (DFW local lead research; contact wjackwing1@gmail.com)"}
# 4s between requests 429s almost every call. 28s worked on a cold start
# but still drew 429s under sustained use, so the default is 45s. Slow is the
# price of keyless access; a sweep is a background job, not an interactive one.
THROTTLE_S = 45
MAX_AGE_HOURS = 48
FEED_WINDOW = 25          # Reddit serves exactly this many items
STATE_PATH = Path(__file__).with_name("reddit_rss_state.json")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg: str) -> None:
    """Everything except records goes to stderr, so stdout stays clean JSONL."""
    print(msg, file=sys.stderr, flush=True)


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"[warn] state unreadable ({exc}); starting fresh")
    return {"subs": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def parse_feed(xml: str) -> list[dict]:
    """Pull entries out of Reddit's Atom feed. Text is kept verbatim."""
    out = []
    for blob in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        def grab(tag):
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", blob, re.S)
            return html.unescape(m.group(1)).strip() if m else None
        link = re.search(r'<link[^>]*href="([^"]+)"', blob)
        pub = grab("published")
        if not link or not pub:
            continue
        try:
            posted = dt.datetime.fromisoformat(pub)
        except ValueError:
            continue
        author = grab("name")
        out.append({
            "id": grab("id"),
            "url": link.group(1),
            "title": grab("title"),
            "body": grab("content"),
            # RSS gives us the author, which the search-index path never did.
            "author_handle": author if author and author.startswith("/u/") else None,
            "posted": posted,
        })
    return out


def strip_html(s: str | None) -> str | None:
    if not s:
        return None
    txt = re.sub(r"<[^>]+>", " ", s)
    txt = html.unescape(re.sub(r"\s+", " ", txt)).strip()
    return txt or None


def collect_sub(entry: dict, client: str, state: dict, max_age_h: int) -> tuple[list[dict], dict]:
    sub = entry["subreddit"]
    url = f"https://www.reddit.com/r/{sub}/new/.rss"
    stats = {"http": None, "items": 0, "new": 0, "too_old": 0,
             "kept": 0, "overflow": False, "error": None}
    try:
        r = requests.get(url, headers=UA, timeout=30)
    except Exception as exc:
        stats["error"] = f"{type(exc).__name__}: {exc}"[:160]
        return [], stats
    stats["http"] = r.status_code
    if r.status_code != 200:
        # A non-200 is a real, visible failure for this ONE source. Nothing
        # else in the sweep changes, which is the point of per-source tools.
        stats["error"] = f"HTTP {r.status_code}"
        return [], stats

    posts = parse_feed(r.text)
    stats["items"] = len(posts)
    seen = set(state["subs"].get(sub, {}).get("seen_ids", []))
    now = dt.datetime.now(dt.timezone.utc)

    rows, fresh_ids, newest = [], [], None
    for p in posts:
        if p["id"] in seen:
            continue
        stats["new"] += 1
        age_h = (now - p["posted"]).total_seconds() / 3600
        if age_h > max_age_h:
            stats["too_old"] += 1
            continue
        fresh_ids.append(p["id"])
        newest = max(newest or p["posted"], p["posted"])
        # Metro subs cover many cities, so the city is genuinely unknown.
        # Contract: anything unknown is null, never a placeholder.
        city = entry.get("city")
        rows.append({
            "source": "reddit_rss",
            "platform": "reddit",
            "url": p["url"],
            "title": p["title"],
            "body": strip_html(p["body"]),
            "author_handle": p["author_handle"],
            "location_text": city,
            "posted_at": p["posted"].astimezone(dt.timezone.utc)
                          .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event_date": None,
            "query": f"r/{sub}",
            "client": client,
            # Extra, non-contract keys. A later reader must be able to tell a
            # source-derived location from a query-derived guess.
            "location_provenance": ("subreddit" if city
                                    else f"metro_unknown_city:{entry.get('metro_label')}"),
            "subreddit": sub,
        })

    stats["kept"] = len(rows)
    # Every item in the window was new => older posts fell off the end unseen.
    # But on the FIRST run of a sub there is no watermark, so everything is new
    # by definition and that is not overflow. Only claim overflow when we had a
    # prior position to fall behind. Crying wolf on every new sub would train
    # Jack to ignore the one alarm that matters.
    stats["first_run"] = not seen
    stats["overflow"] = (bool(seen) and stats["new"] >= FEED_WINDOW
                         and stats["items"] >= FEED_WINDOW)

    keep_ids = ([p["id"] for p in posts] + list(seen))[:400]
    state["subs"][sub] = {
        "seen_ids": keep_ids,
        "last_run": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_newest_post": newest.strftime("%Y-%m-%dT%H:%M:%SZ") if newest else
                            state["subs"].get(sub, {}).get("last_newest_post"),
        "posts_per_day_hint": entry.get("posts_per_day"),
    }
    return rows, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client", required=True, help="client name, written onto every row")
    ap.add_argument("--cities", help="comma separated; default is every verified sub")
    ap.add_argument("--max-age-hours", type=int, default=MAX_AGE_HOURS)
    ap.add_argument("--throttle", type=int, default=THROTTLE_S)
    ap.add_argument("--limit-subs", type=int, help="cap subs polled, for a quick test")
    ap.add_argument("--dry-run", action="store_true",
                    help="poll and report, write no records and no watermark")
    args = ap.parse_args()

    try:
        reg = geo.load_registry()
    except FileNotFoundError:
        log(f"[fatal] no registry at {geo.REGISTRY_PATH}. "
            f"Run: python ingest/probe_geo_communities.py")
        return 2

    if args.cities:
        picked, unmatched = geo.select([c for c in args.cities.split(",")], reg)
        for u in unmatched:
            # Silently substituting a metro sub for a city nobody covers is the
            # failure this refuses to make.
            log(f"[warn] no verified subreddit for {u!r}; nothing collected for it")
    else:
        picked = geo.pollable(reg)

    picked = sorted(picked, key=lambda e: -(e.get("posts_per_day") or 0))
    if args.limit_subs:
        picked = picked[: args.limit_subs]
    if not picked:
        log("[fatal] no pollable subreddits selected")
        return 2

    state = load_state()
    log(f"[reddit_rss] {len(picked)} subs, {args.throttle}s apart, "
        f"max age {args.max_age_hours}h"
        f"{' (DRY RUN)' if args.dry_run else ''}")

    total, overflow, errors = 0, [], []
    for i, entry in enumerate(picked):
        if i:
            time.sleep(args.throttle)
        rows, st = collect_sub(entry, args.client, state, args.max_age_hours)
        if st["error"]:
            errors.append((entry["subreddit"], st["error"]))
        if st["overflow"]:
            overflow.append(entry["subreddit"])
        if not args.dry_run:
            for row in rows:
                print(json.dumps(row, ensure_ascii=False))
        total += len(rows)
        log(f"  r/{entry['subreddit']:<16} http={st['http']} items={st['items']:<3} "
            f"new={st['new']:<3} dropped_old={st['too_old']:<3} kept={st['kept']}"
            + ("  ** WINDOW OVERFLOW **" if st["overflow"] else "")
            + ("  (first run, no watermark yet)" if st.get("first_run") and st["items"] else "")
            + (f"  ERROR {st['error']}" if st["error"] else ""))

    if not args.dry_run:
        save_state(state)

    log(f"[reddit_rss] kept {total} rows within {args.max_age_hours}h "
        f"across {len(picked)} subs")
    for sub, err in errors:
        log(f"[error] r/{sub}: {err}")
    for sub in overflow:
        hint = next((e.get("posts_per_day") for e in picked
                     if e["subreddit"] == sub), None)
        every = f"{FEED_WINDOW / hint:.1f} days" if hint else "unknown"
        log(f"[OVERFLOW] r/{sub} returned a full window of {FEED_WINDOW} new "
            f"posts, so older ones fell off before we saw them. At its measured "
            f"{hint or '?'} posts/day the window covers about {every}; poll more "
            f"often than that.")
    if total == 0 and not errors:
        # A real zero and a broken run must never look the same.
        log("[reddit_rss] zero rows, and every feed answered. Nothing new was "
            "posted inside the age window. This is an empty result, not a failure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
