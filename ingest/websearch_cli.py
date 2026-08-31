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
import re
import sys
import time
from datetime import datetime, timezone

import requests

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

# ---------------------------------------------------------------------------
# Publication-date resolution
#
# A search index hands back a URL and a snippet and NO trustworthy post date.
# That single gap is why leads_raw.posted_at was null on every websearch row,
# why display and sorting silently fell back to collected_at, and therefore why
# a 2017 Reddit thread read as a lead collected two days ago. Emitting null was
# the honest thing to do; it was never the finished thing to do.
#
# So the date is fetched from the post's OWN page at collect time, per platform,
# and only where a route has actually been verified to work:
#
#   reddit  -- old.reddit.com HTML carries the post's real epoch in a
#              data-timestamp attribute on the post's own container div.
#              MEASURED 2026-08-30:
#                old.reddit.com/<permalink>        -> HTTP 200, date present
#                www.reddit.com/<permalink>/.json  -> HTTP 403
#                old.reddit.com/<permalink>/.json  -> HTTP 403
#              The timestamp is read ONLY from the div anchored to this post's
#              own t3_ id. Every comment on the page carries a data-timestamp
#              too, so an unanchored regex would happily return the date of a
#              reply and call it the post date.
#
#   everything else -- no verified route, so the date stays null and the reason
#              is reported. A null that is counted and named is recoverable. An
#              invented date is not.
#
# NOTHING here ever falls back to "now". A date is either read off the post or
# it is null.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# COLLECT-TIME AGE FILTER
#
# Dating a post is only half the fix. Without this, the pipeline still spends
# every run collecting, storing, categorizing and AI-qualifying posts from 2016
# and only discards them at the drafting stage -- wasting the whole run and
# inflating the pool so it looks far healthier than it is. Measured on the live
# table 2026-08-30: 61 of 107 actionable leads (57%) were a YEAR or older.
#
# WHY 365 DAYS AND NOT THE 45 THE DRAFTER USES
#   Category is not known yet at collect time. categorize_raw.py decides
#   consumer_lead vs partner LATER, and those have very different shelf lives
#   (a homeowner's "who do you recommend" dies in weeks; an estate-sale operator
#   is a durable partner). Filtering to 45 days here would silently destroy
#   every partner lead before anything had a chance to recognize it as one.
#
#   So this is deliberately a COARSE pre-filter with a very different job from
#   the drafter's gate. It removes only what is waste under ANY interpretation:
#   past a year, a consumer request is long dead and a partner is better found
#   from a current listing. The precise, category-aware cut stays in
#   draft_from_leads.py where the category is actually known.
#
#   The asymmetry is deliberate. Dropping at collect time is IRREVERSIBLE -- the
#   row never exists. Skipping at draft time is reversible, because the row is
#   still in leads_raw and becomes draftable the moment its date resolves. So
#   the collect-time cut is the conservative one and the draft-time cut is the
#   strict one.
#
# UNKNOWN DATE IS KEPT HERE, NOT DROPPED
#   The opposite of the drafter's rule, for the same reason: dropping is
#   permanent. An undated row cannot be shown to be stale, and the drafter will
#   refuse it anyway until backfill_posted_at.py resolves it. Kept, counted, and
#   reported -- never silently.
# ---------------------------------------------------------------------------

MAX_AGE_DAYS_AT_COLLECT = 365

DATE_FETCH_SLEEP = 2.0  # polite pacing between post-page fetches
DATE_UA = ("WingDigitalResearch/1.0 (by /u/wingdigital, "
           "contact: wjackwing1@gmail.com)")

_REDDIT_POST_ID = re.compile(r"/comments/([a-z0-9]+)", re.I)
_REDDIT_HOST = re.compile(r"^https?://(?:www\.|old\.|new\.|np\.)?reddit\.com", re.I)

# A soft block answers HTTP 200 with a wall page. Treating that as "no date"
# would be wrong in a quiet, permanent way, so it gets its own outcome.
_BOTWALL_MARKERS = ("prove your humanity", "whoa there, pardner",
                    "your request has been blocked")


def _epoch_to_iso(seconds) -> str | None:
    try:
        return (datetime.fromtimestamp(float(seconds), tz=timezone.utc)
                .isoformat().replace("+00:00", "Z"))
    except (ValueError, OSError, OverflowError, TypeError):
        return None


def reddit_posted_at(url: str, session: requests.Session | None = None
                     ) -> tuple[str | None, str]:
    """Real publication date for one Reddit permalink.

    Returns (iso8601_or_None, outcome). Outcome is always a specific,
    reportable string -- never a bare success/failure boolean -- because
    "we were blocked" and "this page genuinely has no date" demand different
    responses from the caller.
    """
    post_id = _REDDIT_POST_ID.search(url or "")
    if not post_id:
        return None, "reddit:no-post-id-in-url"
    pid = post_id.group(1)

    old_url = _REDDIT_HOST.sub("https://old.reddit.com", url)
    if not old_url.startswith("https://old.reddit.com"):
        return None, "reddit:not-a-reddit-url"

    get = (session or requests).get
    try:
        resp = get(old_url, headers={"User-Agent": DATE_UA}, timeout=25)
    except requests.RequestException as exc:
        return None, f"reddit:fetch-error:{type(exc).__name__}"

    if resp.status_code != 200:
        return None, f"reddit:http-{resp.status_code}"

    body = resp.text
    low = body.lower()
    if any(m in low for m in _BOTWALL_MARKERS):
        # HTTP 200 with a wall body. Explicitly NOT "no date found".
        return None, "reddit:botwalled"

    # Anchor strictly to THIS post's own container, never a comment's.
    anchored = re.search(
        r'<div[^>]*\bid="thing_t3_%s"[^>]*>' % re.escape(pid), body)
    if anchored:
        ts = re.search(r'data-timestamp="(\d+)"', anchored.group(0))
        if ts:
            iso = _epoch_to_iso(int(ts.group(1)) // 1000)
            if iso:
                return iso, "reddit:ok"

    fullname = re.search(
        r'data-fullname="t3_%s"[^>]*?data-timestamp="(\d+)"' % re.escape(pid),
        body)
    if fullname:
        iso = _epoch_to_iso(int(fullname.group(1)) // 1000)
        if iso:
            return iso, "reddit:ok"

    return None, "reddit:no-date-in-page"


def resolve_posted_at(url: str, platform: str | None,
                      session: requests.Session | None = None
                      ) -> tuple[str | None, str]:
    """Dispatch to whatever verified route exists for this platform.

    Unsupported platforms return (None, reason). That is a real answer and it
    gets counted and printed, so a source that can never date its own records
    is visible in the run summary instead of quietly producing undateable rows
    forever.
    """
    if not url:
        return None, "no-url"
    if _REDDIT_HOST.match(url):
        return reddit_posted_at(url, session=session)
    return None, f"unsupported-platform:{platform or 'unknown'}"


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
    result.

    NOTE: timelimit is deliberately never passed here. The index's recency
    parameter is INERT on site: queries.

    RE-MEASURED 2026-08-30, this time by resolving the TRUE date of every
    returned result rather than trusting the parameter:
        timelimit=None  -> 8 results, median true age 1144d, max 2301d
        timelimit="m"   -> 8 results, median true age  748d, max  852d
        timelimit="y"   -> 8 results, median true age  880d, max 2301d
    A one-MONTH restriction returning a median 748-day-old post, and a one-YEAR
    restriction returning a 2301-day-old post, settles it: the parameter shifts
    which results come back but does not constrain them by date at all. Passing
    it would buy false confidence, not real freshness.

    Recency for this source is therefore enforced the only way that actually
    works: fetch each result's real date (resolve_posted_at) and filter on it
    after the fact. See MAX_AGE_DAYS_AT_COLLECT.
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
        #
        # The index still cannot date a result -- so the date is fetched from
        # the post's own page instead (see resolve_posted_at). Filled in by the
        # caller, which owns the pacing; null here remains null unless a real
        # date was actually read off the post.
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
    ap.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS_AT_COLLECT,
                    help=f"drop results older than this at collect time "
                         f"(default {MAX_AGE_DAYS_AT_COLLECT}). 0 disables the "
                         f"filter and emits everything, dated or not.")
    ap.add_argument("--no-resolve-dates", action="store_true",
                    help="skip fetching each post's real publication date. "
                         "Faster, but every record is emitted with a null "
                         "posted_at and is undateable downstream.")
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
    date_outcomes = {}
    dated = undated = 0
    seen_before_filter = 0
    dropped_stale = 0
    kept_ages: list[float] = []
    dropped_ages: list[float] = []
    date_session = requests.Session()
    now = datetime.now(timezone.utc)

    if args.no_resolve_dates and args.max_age_days:
        print("--no-resolve-dates disables date lookup, so the age filter has "
              "nothing to filter on and is INACTIVE this run. Every result will "
              "be emitted with a null posted_at.", file=sys.stderr)

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

            if args.no_resolve_dates:
                outcome = "skipped:--no-resolve-dates"
            else:
                rec["posted_at"], outcome = resolve_posted_at(
                    url, rec.get("platform"), session=date_session)
                time.sleep(DATE_FETCH_SLEEP)
            date_outcomes[outcome] = date_outcomes.get(outcome, 0) + 1
            seen_before_filter += 1

            age = None
            if rec["posted_at"]:
                dated += 1
                try:
                    age = (now - datetime.fromisoformat(
                        rec["posted_at"].replace("Z", "+00:00"))
                    ).total_seconds() / 86400.0
                except ValueError:
                    age = None
            else:
                undated += 1

            # Drop only what is PROVABLY too old. Unknown age is kept, per the
            # asymmetry documented at MAX_AGE_DAYS_AT_COLLECT.
            if args.max_age_days and age is not None and age > args.max_age_days:
                dropped_stale += 1
                dropped_ages.append(age)
                print(f"      drop[{age:.0f}d > {args.max_age_days}d] {url[:88]}",
                      file=sys.stderr)
                continue

            if age is not None:
                kept_ages.append(age)

            # The date is carried on the record itself, so a reader of the
            # JSONL can see exactly which rows are undateable and why.
            rec["posted_at_source"] = outcome

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

    print(f"  publication dates     : {dated} resolved, {undated} still null",
          file=sys.stderr)
    for outcome, n in sorted(date_outcomes.items(), key=lambda kv: -kv[1]):
        print(f"      {n:>4}  {outcome}", file=sys.stderr)
    if undated:
        print(f"  {undated} record(s) carry NO publication date. Downstream must "
              f"treat them as unknown age, never as fresh.", file=sys.stderr)

    def _span(label, ages):
        if not ages:
            print(f"      {label}: (none dated)", file=sys.stderr)
            return
        a = sorted(ages)
        print(f"      {label}: n={len(a)} min={a[0]:.0f}d "
              f"median={a[len(a) // 2]:.0f}d max={a[-1]:.0f}d", file=sys.stderr)

    print(f"\n  === collect-time age filter "
          f"(max {args.max_age_days or 'OFF'} days) ===", file=sys.stderr)
    print(f"      results before filter : {seen_before_filter}", file=sys.stderr)
    print(f"      dropped as too old    : {dropped_stale}", file=sys.stderr)
    print(f"      emitted after filter  : {emitted}", file=sys.stderr)
    _span("age of KEPT   ", kept_ages)
    _span("age of DROPPED", dropped_ages)
    if seen_before_filter and emitted == 0:
        print(f"      WARNING: the filter removed EVERYTHING. That is a starved "
              f"pipeline, not a clean run. Loosen --max-age-days or widen the "
              f"queries.", file=sys.stderr)
    elif seen_before_filter and dropped_stale / max(seen_before_filter, 1) > 0.8:
        print(f"      NOTE: over 80% of results were stale. This query set is "
              f"mostly mining old threads; the yield per run is much smaller "
              f"than the raw result count suggests.", file=sys.stderr)

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
