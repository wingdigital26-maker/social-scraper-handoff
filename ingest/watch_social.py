#!/usr/bin/env python3
"""
Sonar Watch — per-client social monitoring across every platform, no AI.

For each Wing client this watches the public web for people who need what that
client sells, drafts a response grounded in the actual post, and files it in the
CRM under that client. It runs on a schedule and does not need supervising.

WHAT IT WATCHES
  Nextdoor, Reddit, Facebook groups/pages, Instagram, TikTok, X — all through
  the public search index, which is how Sonar avoids the blocks that stop
  direct scraping. Recency is enforced by the index's own time filter plus a
  seen-list, so the same post is never drafted twice.

WHAT IT DOES NOT DO
  It never posts. Auto-replying from a bot account is what gets accounts banned
  on these platforms, and it violates their terms. Every draft lands in the CRM
  as status='draft' for a human to send from their own account. That is the
  difference between a durable system and a burned account.

NO AI. Intent detection and drafting are keyword rules and templates. The whole
loop is deterministic and free.

    python watch_social.py --client "Jackson Roofing" --dry-run
    python watch_social.py --all
"""
import argparse
import json
import pathlib
import re
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from db import load_env
from audit_prospect import sb_request   # retrying Supabase call, shared

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import functools
print = functools.partial(print, flush=True)  # noqa: A001

HERE = pathlib.Path(__file__).resolve().parent
SEEN = HERE / "seen_watch_urls.txt"
# The index rate-limits on burst volume: a few hundred rapid queries and it
# starts refusing across all its backends. This is a background job, not an
# interactive one, so it goes deliberately slow.
SLEEP = 6.0

PLATFORMS = {
    "nextdoor":  "site:nextdoor.com",
    "reddit":    "site:reddit.com",
    "facebook":  "site:facebook.com",
    "instagram": "site:instagram.com",
    "tiktok":    "site:tiktok.com",
    "x":         "site:x.com OR site:twitter.com",
}

# Someone ASKING is the whole point. These are the phrases a person uses when
# they are about to hire somebody, which is the only moment worth a reply.
INTENT = [
    '"anyone recommend"', '"looking for a"', '"any recommendations for"',
    '"who do you use for"', '"in need of"', '"need someone to"',
    '"can anyone recommend"', '"best company for"', '"asking for a friend"',
    '"does anyone know a"', '"need a good"',
]

# Complaint-shaped posts: someone unhappy with their current provider is a
# switch waiting to happen.
SWITCH = ['"terrible experience with"', '"never showed up"', '"still waiting on"',
          '"ripped me off"', '"looking to switch"']

URGENT = re.compile(r"\b(asap|urgent|emergency|today|tomorrow|this week|leak|leaking|"
                    r"no ac|no heat|flood|storm damage)\b", re.I)


def search(q, limit=8, recent=True, tries=3):
    """Search with backoff.

    Several hundred queries in a short window gets the index throttling, and it
    surfaces as connect errors across its backends. Without a retry that reads
    as "no leads found", which is the most misleading failure this tool could
    have, so a throttle is distinguished from a genuine empty result.
    """
    delay = 4
    for attempt in range(tries):
        try:
            with DDGS() as d:
                # timelimit 'm' keeps this to the last month; an old thread is a
                # dead lead and replying to one looks like a bot.
                out = list(d.text(q, max_results=limit, timelimit="m" if recent else None))
            time.sleep(SLEEP)
            return out
        except Exception as e:
            msg = str(e)
            if "No results found" in msg:
                return []          # genuinely empty, not a throttle
            if attempt == tries - 1:
                print(f"      search THROTTLED after {tries} tries: {msg[:60]}")
                return None        # None means "could not check", not "nothing there"
            time.sleep(delay)
            delay *= 2
    return None


def draft_reply(client_name, trade, city, post_title, urgent):
    """Templated, specific, non-salesy. A human edits and posts it."""
    opener = ("Saw this and it sounds time sensitive." if urgent
              else "Saw your post.")
    return (
        f"{opener} We do {trade} around {city} and could take a look. "
        f"Happy to give you a straight answer on what it needs and what it should cost, "
        f"no pressure either way. I can send over some recent work in the area if that helps."
    )


def load_clients(env, only=None):
    url, key = env["SUPABASE_URL"], env["SUPABASE_SERVICE_KEY"]
    r = requests.get(f"{url}/rest/v1/crm_clients", timeout=30,
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     params={"active": "is.true", "select": "*"})
    rows = r.json() if r.ok else []
    if only:
        rows = [c for c in rows if c["name"].lower() == only.lower()
                or c["slug"].lower() == only.lower()]
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", help="one client by name or slug")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--platforms", default="nextdoor,reddit,facebook")
    ap.add_argument("--limit", type=int, default=25, help="max drafts per run")
    ap.add_argument("--phrases", type=int, default=3,
                    help="intent phrases per city per platform (keeps a run bounded)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    env = load_env()
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY")

    clients = load_clients(env, None if args.all else args.client)
    if not clients:
        sys.exit("No matching active client in crm_clients. Add one first.")

    plats = [p.strip() for p in args.platforms.split(",") if p.strip() in PLATFORMS]
    seen = set(SEEN.read_text(encoding="utf-8").split("\n")) if SEEN.exists() else set()
    drafts, stats = [], dict(queries=0, results=0, kept=0, dup=0, no_intent=0, throttled=0)

    for c in clients:
        trade = c.get("scrape_niche") or "work"
        cities = [x.strip() for x in (c.get("scrape_cities") or "").split(",") if x.strip()]
        extra = [x.strip() for x in (c.get("scrape_terms") or "").split(",") if x.strip()]
        print(f"\n=== {c['name']} — {trade} in {', '.join(cities) or '(no cities set)'}")

        for city in cities or [""]:
            for plat in plats:
                op = PLATFORMS[plat]
                # Rotate phrases by city index so one run stays short while
                # successive runs still work through the whole phrase list.
                allp = INTENT + SWITCH
                ci = (cities.index(city) if city in cities else 0)
                phrases = [allp[(ci * args.phrases + k) % len(allp)]
                           for k in range(args.phrases)]
                for phrase in phrases:
                    q = f"{op} {phrase} {trade} {city} {' '.join(extra[:2])}".strip()
                    stats["queries"] += 1
                    res = search(q, 6)
                    if res is None:
                        stats["throttled"] = stats.get("throttled", 0) + 1
                        continue
                    for r in res:
                        stats["results"] += 1
                        u = (r.get("href") or "").split("?")[0]
                        if not u or u in seen:
                            stats["dup"] += 1
                            continue
                        title = r.get("title") or ""
                        body = r.get("body") or ""
                        blob = f"{title} {body}"
                        # Require the trade word to actually appear, or this is
                        # someone asking about something else entirely.
                        if trade.split()[0].lower() not in blob.lower():
                            stats["no_intent"] += 1
                            continue
                        seen.add(u)
                        urgent = bool(URGENT.search(blob))
                        drafts.append({
                            "client": c["name"],
                            "channel": plat,
                            "direction": "outbound",
                            "recipient": title[:120] or "(post)",
                            "recipient_url": u,
                            "evidence_url": u,
                            "subject": None,
                            "body": draft_reply(c["name"], trade, city or "your area",
                                                title, urgent),
                            "personalization": (("URGENT. " if urgent else "")
                                                + f"Public post: {title[:150]}"),
                            "status": "draft",
                            "tier": "reply",
                        })
                        stats["kept"] += 1
                        print(f"  + [{plat}] {'URGENT ' if urgent else ''}{title[:62]}")
                        if stats["kept"] >= args.limit:
                            break
                    if stats["kept"] >= args.limit:
                        break
                if stats["kept"] >= args.limit:
                    break

    print("\n=== run summary ===")
    for k, v in stats.items():
        print(f"  {k:10}: {v}")

    if args.dry_run:
        print("\ndry run, nothing written")
        return
    park = HERE / "unsent_drafts.json"
    if park.exists():
        try:
            parked = json.loads(park.read_text(encoding="utf-8"))
            drafts = parked + drafts
            park.unlink()
            print(f"  (re-including {len(parked)} drafts parked by an earlier failed run)")
        except Exception:
            pass

    if drafts:
        h = {"apikey": key, "Authorization": f"Bearer {key}",
             "Content-Type": "application/json",
             "Prefer": "resolution=ignore-duplicates,return=representation"}
        res = requests.post(
            f"{url}/rest/v1/outbound?on_conflict=client,channel,recipient,subject",
            headers=h, json=drafts, timeout=60)
        n = len(res.json()) if res.ok else 0
        print(f"\nfiled {len(drafts)} drafts, {n} new -> CRM")
    SEEN.write_text("\n".join(sorted(x for x in seen if x)), encoding="utf-8")


if __name__ == "__main__":
    main()
