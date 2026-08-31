#!/usr/bin/env python3
"""probe_geo_communities.py - build/refresh the DFW subreddit registry from live feeds.

Writes ingest/geo_communities.json. Every entry is measured, never assumed:
existence, which STATE the sub actually belongs to, and its real post rate
taken from the timespan its 25-item /new/.rss window covers.

Reddit rate-limits hard. 4s between requests 429s almost every call; 28s was
measured reliable on 2026-08-31. Slow is the price of keyless access.
"""
from __future__ import annotations

import json
import re
import sys
import time
import datetime as dt
from pathlib import Path

import requests

UA = {"User-Agent": "WingDigital-LeadScout/1.0 (DFW local lead research; contact wjackwing1@gmail.com)"}
THROTTLE_S = 28
OUT = Path(__file__).with_name("geo_communities.json")

# Texas signals. A sub is only VERIFIED_TEXAS if its own words say so; a name
# that merely looks like a DFW city is not evidence. r/Arlington is Virginia.
TX_SIGNAL = re.compile(
    r"\b(texas|tx|dfw|dallas|fort worth|ft worth|metroplex|north texas)\b", re.I)
NOT_TX_SIGNAL = re.compile(
    r"\b(virginia|,\s*va\b|massachusetts|,\s*ma\b|washington|illinois|"
    r"tennessee|nebraska|ohio|oregon|colorado|kentucky|missouri)\b", re.I)

# city = the sub IS that city. metro = poster could be anywhere in the metro,
# so the collector must record the city as unknown rather than guess one.
CANDIDATES = [
    # metro-wide
    ("Dallas", None, "metro", "Dallas"), ("FortWorth", None, "metro", "Fort Worth"),
    ("dfw", None, "metro", "DFW"),
    # Jackson Roofing service area
    ("plano", "Plano", "city", None), ("FriscoTX", "Frisco", "city", None),
    ("frisco", "Frisco", "city", None), ("mckinney", "McKinney", "city", None),
    ("AllenTX", "Allen", "city", None), ("allentx", "Allen", "city", None),
    ("Richardson", "Richardson", "city", None),
    # Hero's service area  (Arlington is the known trap)
    ("ArlingtonTX", "Arlington", "city", None), ("arlington", "Arlington", "city", None),
    # suburb ring
    ("garland", "Garland", "city", None), ("Irving", "Irving", "city", None),
    ("carrollton", "Carrollton", "city", None), ("Denton", "Denton", "city", None),
    ("lewisville", "Lewisville", "city", None), ("grapevine", "Grapevine", "city", None),
    ("Rowlett", "Rowlett", "city", None), ("wylie", "Wylie", "city", None),
    ("mesquite", "Mesquite", "city", None), ("Addison", "Addison", "city", None),
    ("coppell", "Coppell", "city", None), ("flowermound", "Flower Mound", "city", None),
    ("littleelm", "Little Elm", "city", None), ("prosper", "Prosper", "city", None),
    ("celina", "Celina", "city", None), ("sachse", "Sachse", "city", None),
    ("murphytx", "Murphy", "city", None), ("Rockwall", "Rockwall", "city", None),
    ("euless", "Euless", "city", None), ("mansfield", "Mansfield", "city", None),
    ("Keller", "Keller", "city", None), ("southlake", "Southlake", "city", None),
]


def probe(sub: str) -> dict:
    """Fetch one sub's RSS and report what it actually is. Never guesses."""
    url = f"https://www.reddit.com/r/{sub}/new/.rss"
    try:
        r = requests.get(url, headers=UA, timeout=30)
    except Exception as exc:
        return {"status": "probe_error", "reason": f"{type(exc).__name__}: {exc}"[:160]}

    if r.status_code == 404:
        return {"status": "not_found", "reason": "no such subreddit (HTTP 404)"}
    if r.status_code == 403:
        return {"status": "forbidden", "reason": "HTTP 403, private or quarantined"}
    if r.status_code == 429:
        return {"status": "rate_limited", "reason": "HTTP 429, re-run to resolve"}
    if r.status_code != 200:
        return {"status": "probe_error", "reason": f"HTTP {r.status_code}"}

    body = r.text
    title = (re.search(r"<title>(.*?)</title>", body, re.S) or [None, ""])[1].strip()
    subtitle = (re.search(r"<subtitle>(.*?)</subtitle>", body, re.S) or [None, ""])[1].strip()
    posts = re.findall(r"<title>(.*?)</title>", body, re.S)[1:]
    stamps = sorted(dt.datetime.fromisoformat(x)
                    for x in re.findall(r"<published>([^<]+)</published>", body))

    # Judge the state from the feed's OWN words plus its post titles.
    corpus = " ".join([title, subtitle] + posts[:25])
    tx, not_tx = bool(TX_SIGNAL.search(corpus)), bool(NOT_TX_SIGNAL.search(corpus))

    if not stamps:
        return {"status": "empty", "reason": "feed returned no posts",
                "feed_title": title}

    span_days = (stamps[-1] - stamps[0]).total_seconds() / 86400
    ppd = round(len(stamps) / span_days, 1) if span_days > 0.02 else None
    now = dt.datetime.now(dt.timezone.utc)
    out = {
        "feed_title": title, "feed_subtitle": subtitle[:160],
        "items": len(stamps), "posts_per_day": ppd,
        "window_hours": round(span_days * 24, 1),
        "newest_age_hours": round((now - stamps[-1]).total_seconds() / 3600, 1),
        "within_48h": sum(1 for s in stamps if (now - s).total_seconds() <= 48 * 3600),
    }
    if not_tx and not tx:
        out.update(status="wrong_state",
                   reason=f"feed says another state, not Texas: {title!r}")
    elif not tx:
        out.update(status="unconfirmed",
                   reason=f"no Texas signal in feed title/subtitle/posts: {title!r}")
    else:
        out.update(status="verified_texas", reason=None)
    return out


def main() -> int:
    ap = __import__("argparse").ArgumentParser()
    ap.add_argument("--retry-unresolved", action="store_true",
                    help="re-probe only entries a previous run could not resolve "
                         "(rate_limited / probe_error), keeping verified ones")
    ap.add_argument("--throttle", type=int, default=THROTTLE_S)
    args = ap.parse_args()

    # A 429 means "ask me later", never "this sub does not exist". Keeping the
    # two apart is why unresolved entries are retried rather than written off.
    RETRYABLE = {"rate_limited", "probe_error", "empty"}
    prior = {}
    if args.retry_unresolved and OUT.exists():
        old = json.loads(OUT.read_text(encoding="utf-8"))
        prior = {e["subreddit"]: e for e in old["communities"]}

    today = dt.date.today().isoformat()
    comms, seen, probed = [], set(), 0
    for i, (sub, city, scope, metro) in enumerate(CANDIDATES):
        if sub.lower() in seen:
            continue
        seen.add(sub.lower())
        keep = prior.get(sub)
        if keep and keep.get("status") not in RETRYABLE:
            comms.append(keep)
            continue
        # Throttle between actual network calls only. Entries carried over from
        # a previous run cost nothing, so they must not buy a sleep.
        if probed:
            time.sleep(args.throttle)
        probed += 1
        res = probe(sub)
        entry = {"subreddit": sub, "city": city, "scope": scope,
                 "metro_label": metro, "aliases": [], "verified_on": today}
        entry.update(res)
        # A metro sub must never hand a city to the collector.
        if entry.get("scope") == "metro":
            entry["city"] = None
        comms.append(entry)
        print(f"r/{sub:<16} {entry['status']:<16} "
              f"ppd={entry.get('posts_per_day')} "
              f"{('- ' + entry['reason']) if entry.get('reason') else ''}",
              file=sys.stderr, flush=True)

    reg = {
        "generated_on": today,
        "method": ("Live /new/.rss probe. State confirmed from the feed's own "
                   "title, subtitle and post titles, never from the sub name. "
                   "posts_per_day is the observed rate across the 25-item "
                   "window, so it moves; re-run to refresh."),
        "throttle_seconds": args.throttle,
        "communities": comms,
    }
    OUT.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    ok = [c for c in comms if c["status"] == "verified_texas"]
    print(f"\nwrote {OUT}  ({len(ok)} verified / {len(comms)} probed)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
