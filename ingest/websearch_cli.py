#!/usr/bin/env python3
"""
Wide-gate web-search index CLI.

Reddit, Nextdoor, Facebook, TikTok, Instagram and friends all refuse direct
scraping (measured 2026-08-27: 403s, connection resets, login walls, even
through a reader proxy -- see SOURCE-CLI-CONTRACT.md). Their CONTENT is still
reachable sideways through a web search index using site: queries. That is how
watch_social.py reaches Reddit today. This tool extracts exactly that
capability into one standalone CLI that goes as wide as possible across every
platform the index has crawled, and judges nothing.

Collect wide, filter later. This tool cross-products queries x sites x cities
and emits every result verbatim. No relevance scoring, no geo gate, no intent
gate. The only dedup is by URL.

Exit codes (per SOURCE-CLI-CONTRACT.md):
  0  ran, whatever the yield, including zero results
  2  the source (the search index) refused us
  1  bad input or a real crash

    python websearch_cli.py --query "need a roofer" --cities Plano,Frisco
    python websearch_cli.py --query "junk removal,haul away" --sites reddit.com,tiktok.com --max-queries 20
"""
import argparse
import json
import sys
import time

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Polite pacing between queries, matching watch_social.py's SLEEP. This is a
# background job, not an interactive one; hammering the index is how the whole
# host gets soft-blocked.
SLEEP = 6.0

# Broad default site list. Reddit/Nextdoor/Facebook/TikTok/Instagram are the
# platforms named in the task; x.com and quora.com are added because they are
# public-indexed forums with real local-service demand threads, same as
# Reddit. This list is deliberately wide -- narrowing it is a later decision,
# not this tool's job.
DEFAULT_SITES = [
    "reddit.com",
    "nextdoor.com",
    "facebook.com",
    "tiktok.com",
    "instagram.com",
    "x.com",
    "quora.com",
]

CLIENT_LABEL = None  # set from --client in main()


def _csv(value):
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def _collect_repeatable_or_csv(values):
    """--query can be passed multiple times AND/OR as a comma separated list."""
    out = []
    for v in values or []:
        out.extend(_csv(v))
    return out


def platform_from_domain(domain):
    """The bare platform name from a site: domain, e.g. 'x.com' -> 'x'."""
    d = (domain or "").lower().strip()
    if d.endswith(".com"):
        d = d[:-4]
    if d.endswith(".net"):
        d = d[:-4]
    return d or None


def build_query(site, query, city):
    parts = [f"site:{site}", query]
    if city:
        parts.append(city)
    return " ".join(p for p in parts if p).strip()


def search(q, limit=8, tries=3):
    """Run one query. Returns (status, results).

    status is one of "ok", "empty", "throttled", "error" -- same three-way
    distinction as watch_social.py's search(), because a soft block answers
    HTTP 200 with an empty page and must not be read as a genuine empty
    result. NOTE: timelimit is deliberately never passed here. Measured
    2026-08-26/27: the index's timelimit="m" recency parameter is INERT on
    site: queries (reproduced twice) and its Reddit corpus is stale regardless
    of what timelimit claims, so passing it would buy false confidence, not
    real freshness.
    """
    delay = 4
    for attempt in range(tries):
        try:
            with DDGS() as d:
                out = list(d.text(q, max_results=limit, timelimit=None))
            time.sleep(SLEEP)
            if out:
                return "ok", out
            if attempt == 0:
                time.sleep(delay)
                continue
            return "empty", []
        except Exception as e:
            msg = str(e)
            if "No results found" in msg:
                if attempt == 0:
                    time.sleep(delay)
                    continue
                return "empty", []
            if attempt == tries - 1:
                print(f"    search THROTTLED after {tries} tries: {msg[:120]}", file=sys.stderr)
                return "throttled", []
            time.sleep(delay)
            delay *= 2
    return "error", []


def make_record(site, query, city, client, hit):
    url = (hit.get("href") or "").split("?")[0]
    title = hit.get("title") or None
    body = hit.get("body") or None
    return {
        "source": "websearch",
        "platform": platform_from_domain(site),
        "url": url or None,
        "title": title,
        "body": body,
        "author_handle": None,
        "location_text": city or None,
        # HARD RULE (SOURCE-CLI-CONTRACT.md): a search index does not give a
        # reliable post date. Never substitute collection time. This is the
        # exact bug that made an 18 day old post look like a live lead.
        "posted_at": None,
        "event_date": None,
        "query": query,
        "client": client,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query", action="append", default=[],
                    help="repeatable, or comma separated")
    ap.add_argument("--sites", default=",".join(DEFAULT_SITES),
                    help="comma separated domains, defaults to a broad list")
    ap.add_argument("--cities", default="", help="comma separated, optional")
    ap.add_argument("--client", default=None, help="label written onto each record")
    ap.add_argument("--limit", type=int, default=None, help="max records emitted")
    ap.add_argument("--max-queries", type=int, default=None,
                    help="bound the run; truncation is reported, never silent")
    ap.add_argument("--since", type=int, default=None,
                    help="freshness window in days -- THIS SOURCE CANNOT FILTER BY "
                         "DATE, see stderr warning")
    ap.add_argument("--json", action="store_true", help="emit JSONL to stdout (default, always on)")
    ap.add_argument("--dry-run", action="store_true",
                    help="do the fetches, emit records, write nothing anywhere "
                         "(this tool never writes anywhere regardless)")
    ap.add_argument("--per-query-results", type=int, default=8,
                    help="max results requested per individual query")
    args = ap.parse_args()

    queries = _collect_repeatable_or_csv(args.query)
    if not queries:
        print("no --query given, nothing to search", file=sys.stderr)
        sys.exit(1)

    sites = _csv(args.sites) or DEFAULT_SITES
    cities = _csv(args.cities)
    city_list = cities if cities else [None]

    if args.since is not None:
        print(f"--since {args.since} ignored: this source (a web search index) "
              f"cannot filter by date. Its timelimit parameter is inert on "
              f"site: queries (measured 2026-08-26/27) and its corpus is stale "
              f"regardless. posted_at will be null on every record.",
              file=sys.stderr)

    # Cross product: queries x sites x cities. Deliberately wide.
    all_combos = [(site, q, city) for site in sites for q in queries for city in city_list]
    total_combos = len(all_combos)
    run_combos = all_combos[:args.max_queries] if args.max_queries else all_combos
    truncated = total_combos - len(run_combos)

    print(f"query combinations: {total_combos} possible, {len(run_combos)} will run"
          + (f", {truncated} truncated by --max-queries {args.max_queries}" if truncated else ""),
          file=sys.stderr)

    seen_urls = set()
    emitted = 0
    ok = empty = throttled = errors = 0
    per_site = {}

    for site, q, city in run_combos:
        query_str = build_query(site, q, city)
        status, results = search(query_str, limit=args.per_query_results)
        per_site.setdefault(site, dict(queries=0, results=0, kept=0))
        per_site[site]["queries"] += 1
        if status == "ok":
            ok += 1
        elif status == "empty":
            empty += 1
        elif status == "throttled":
            throttled += 1
        else:
            errors += 1
        print(f"  q[{status}:{len(results)}] {query_str}", file=sys.stderr)

        for hit in results:
            rec = make_record(site, q, city, args.client, hit)
            url = rec["url"]
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            per_site[site]["results"] += 1
            per_site[site]["kept"] += 1
            print(json.dumps(rec, ensure_ascii=False))
            emitted += 1
            if args.limit and emitted >= args.limit:
                break
        if args.limit and emitted >= args.limit:
            print(f"CAPPED at --limit {args.limit} records; remaining query "
                  f"combinations were not run.", file=sys.stderr)
            break

    answered = ok + empty + throttled + errors
    print(f"\n=== run summary ===", file=sys.stderr)
    print(f"  combinations possible : {total_combos}", file=sys.stderr)
    print(f"  combinations run      : {len(run_combos) if not (args.limit and emitted >= args.limit) else '(stopped early by --limit)'}", file=sys.stderr)
    print(f"  queries truncated     : {truncated}", file=sys.stderr)
    print(f"  queries ok/empty/throttled/errors : {ok}/{empty}/{throttled}/{errors}", file=sys.stderr)
    print(f"  records emitted       : {emitted} (deduped by URL)", file=sys.stderr)
    for site in sites:
        s = per_site.get(site, dict(queries=0, results=0, kept=0))
        note = ""
        if s["queries"] and s["results"] == 0:
            note = "  -- NOTHING came back for this site through the index"
        print(f"  [{site}] {s['queries']}q -> {s['results']} results, {s['kept']} kept{note}",
              file=sys.stderr)

    if answered == 0:
        print("no queries were answered at all", file=sys.stderr)
        sys.exit(1)

    # A block/rate-limit must never be reported as an empty result. If every
    # single answered query came back throttled/error (none ok, none even
    # a genuine empty), that is the index refusing this host, not a quiet day.
    if ok == 0 and empty == 0 and (throttled + errors) > 0:
        print(f"BLOCKED: every one of {answered} queries was throttled or errored, "
              f"none came back ok or even a clean empty. The index is refusing "
              f"this host.", file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
