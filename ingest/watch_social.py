#!/usr/bin/env python3
"""
Sonar Watch — per-client social monitoring across every platform, no AI.

For each Wing client this watches the public web for people who need what that
client sells, drafts a response grounded in the actual post, and files it in the
CRM under that client. It runs on a schedule and does not need supervising.

WHAT IT WATCHES
  Whatever each client's crm_clients.channels says, intersected with the
  platforms below. Nextdoor and Reddit are where local-service demand actually
  lives and is publicly indexed — Nextdoor's /ask-neighbors/ threads are the
  gold surface and its robots.txt whitelists the search crawlers; Reddit has
  been Google-indexed since Jul 2024. Facebook groups hold demand too but are
  walled (Groups API killed Apr 2024), so site: searches only reach public
  pages. TikTok, X and Instagram carry little local-service demand.

  There is deliberately NO fallback default. A client whose channels are empty,
  or resolve to nothing this watcher supports, is SKIPPED and says so. Silently
  searching nextdoor+reddit for a client configured as 'email' is how a run
  looks busy while doing work nobody asked for.

WHAT IT DOES NOT DO
  It never posts. Auto-replying from a bot account is what gets accounts banned
  on these platforms, and it violates their terms. Every draft lands in the CRM
  as status='draft' for a human to send from their own account. That is the
  difference between a durable system and a burned account.

NO AI. Intent detection and drafting are keyword rules and templates. The whole
loop is deterministic and free.

WHY THE INDEX HEALTH CODE EXISTS
  Run 32976099694 (2026-08-26) reported "success": 60 queries, 8 results, 1
  kept, 0 throttled. Measured against the same queries run from a residential
  IP the same day: 30 queries, 93 results. The index was not empty; it was
  refusing the GitHub runner. It refuses POLITELY — the backends answer HTTP
  200 with an empty result page, no error — so ddgs aggregates zero rows and
  raises DDGSException("No results found."), which the old code read as a
  genuine empty result. `throttled: 0` was therefore true of the signal and a
  lie about reality.

  There is no way to tell a soft block from a real empty answer on ONE query.
  There is at the run level: real queries do not all come back empty. So a
  streak of consecutive empties is treated as the index being down — a HARD
  FAILURE with a non-zero exit — exactly the rule this project already adopted
  after TikTok exited 0 with 0 rows four times running.

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
import trade_vocab                        # per-trade search phrasing + on-topic terms
import relevance                          # scores/rejects a hit before it becomes a draft
import client_voice                       # per-client reply voice, gated
import watch_telemetry                    # last_scraped_at + watch_runs
import craigslist_source                  # read-only Craigslist household/free/labor scrape

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

# Below this, a hit is noise rather than a lead. Tuned against real Nextdoor
# and Reddit results: a genuine "anyone recommend a roofer in Plano" scores
# ~0.8, while chrome and off-topic chatter land far lower.
MIN_RELEVANCE = 0.35

# How many consecutive EMPTY answers before the index is declared down.
# A healthy run measures ~3 results per query, so the probability of a dozen
# genuine consecutive zeroes across different phrasings and platforms is
# negligible; a soft block produces them immediately and forever.
EMPTY_STREAK_FAIL = 12

# Yield floor for the whole run, checked only once enough queries have been
# spent for the number to mean anything. The failing run scored 0.13.
MIN_YIELD = 0.5
YIELD_MIN_QUERIES = 20

# The index's own time filter, or None for no filter.
#
# MEASURED 2026-08-26 on a residential IP, same query, same minute:
#     site:reddit.com/r/Dallas roofer          timelimit='m' -> 0   'y' -> 6
#     site:reddit.com/r/plano roofer           timelimit='m' -> 0   'y' -> 6
#     site:reddit.com/r/Dallas junk removal... timelimit='m' -> 0   'y' -> 6
# Every result those queries returned was a genuine DFW demand post. The filter
# was not removing stale leads, it was removing the entire answer — the watcher
# was running with 'm' hardcoded, which is a large part of why "kept 0" was the
# normal outcome. The index's timelimit is also not trustworthy in the other
# direction ('y' happily returns 2023 threads), so recency is NOT delegated to
# it. relevance.py parses a real date when one exists and hard-rejects a dead
# thread, and _reddit_is_legacy below drops pre-2023 Reddit posts by post-id
# ordering. Those two are the honest recency gates; this one is off.
TIMELIMIT = None

# Reddit assigns post ids as monotonically increasing base36, so id LENGTH is a
# strict ordering fact, not an estimate: every 7-character id was issued after
# every 6-character id, because the 6-character space had to be exhausted first.
# Observed cleanly in the 2026-08-26 sample — 6-char ids (37hfy1, 4btqj3, gii4lg,
# o1gqel, y22qx5) are all old threads, 7-char ids (12n98e5, 1boc5tc, 1ckxcso) are
# the recent ones. Search results carry no date for us to read, so this ordering
# is the only recency signal available on a Reddit hit. It is used ONLY to drop
# clearly-ancient threads and is never reported as a date.
_REDDIT_ID_RE = re.compile(r"reddit\.com/r/[^/]+/comments/([a-z0-9]+)", re.I)

# Which subreddit a hit actually landed in. site:reddit.com/r/plano in the
# query does not guarantee the index only answers from r/plano -- measured
# 2026-08-27, a Dallas junk-removal run got back an r/okc post and a Dallas
# roofing run got back r/PPC, neither of which the query text should have
# been able to reach. This is the hard backstop: whatever subreddit the
# result URL actually names has to be one this CLIENT's own cities resolve
# to, or it is rejected before it can ever become a candidate.
_SUBREDDIT_RE = re.compile(r"reddit\.com/r/([^/]+)/", re.I)


def allowed_subreddits(cities):
    """The union of subreddits that count as 'in this client's metro'.

    Built from the client's own scrape_cities via trade_vocab.local_subreddits,
    never hardcoded to one client -- a Dallas client and an Oklahoma City
    client would each get their own set from their own configured cities.
    """
    subs = set()
    for city in cities:
        subs.update(s.lower() for s in trade_vocab.local_subreddits(city)[0])
    return subs


def geo_gate_reddit(url, allowed_subs):
    """(ok, subreddit, reason). Reddit-only geographic gate on the result URL
    itself, independent of what the query asked for."""
    m = _SUBREDDIT_RE.search(url or "")
    sub = m.group(1).lower() if m else ""
    if not sub:
        return False, "", "no subreddit could be read from the result URL"
    if sub not in allowed_subs:
        return False, sub, (
            f"subreddit r/{sub} is not in this client's configured metro "
            f"(allowed: {', '.join(sorted(allowed_subs)) or 'none configured'})"
        )
    return True, sub, ""

PLATFORMS = {
    "nextdoor":  "site:nextdoor.com",
    "reddit":    "site:reddit.com",
    "facebook":  "site:facebook.com",
    "instagram": "site:instagram.com",
    "tiktok":    "site:tiktok.com",
    "x":         "site:x.com OR site:twitter.com",
    # craigslist is handled entirely outside the DDGS index (see
    # craigslist_source.py) -- this entry exists only so resolve_platforms()
    # accepts "craigslist" in a client's crm_clients.channels.
    "craigslist": None,
}

# Craigslist sections searched per query, in priority order. household/free
# is the measured winner for junk-type demand; labor/gigs catches "need
# someone to haul/help move" posts that never touch the for-sale board.
CRAIGSLIST_SECTIONS = ["free", "household", "labor"]

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


class IndexDown(RuntimeError):
    """The search index stopped answering. Raised so the run aborts loudly
    instead of quietly reporting that the whole internet had nothing."""


class IndexHealth:
    """Tracks whether the search index is actually answering.

    ddgs never returns an empty list — it raises DDGSException, and the message
    is "No results found." only when no backend errored. So:
      EMPTY     the index answered and matched nothing        (message says so)
      THROTTLED a backend errored / timed out, retries spent  (message is the error)
    Both are honest per-query readings. Neither is trustworthy in bulk, which is
    what the streak counter is for.
    """

    def __init__(self, streak_limit=EMPTY_STREAK_FAIL):
        self.streak_limit = streak_limit
        self.empty_streak = 0
        self.max_empty_streak = 0
        self.ok = 0
        self.empty = 0
        self.throttled = 0
        self.errors = 0

    def note(self, status):
        if status == "ok":
            self.ok += 1
            self.empty_streak = 0
            return
        if status == "empty":
            self.empty += 1
        elif status == "throttled":
            self.throttled += 1
        else:
            self.errors += 1
        # A throttle counts toward the streak too: a refusal is a refusal
        # whether it arrives as an error or as a blank page.
        self.empty_streak += 1
        self.max_empty_streak = max(self.max_empty_streak, self.empty_streak)
        if self.empty_streak >= self.streak_limit:
            raise IndexDown(
                f"{self.empty_streak} consecutive queries returned nothing "
                f"(empty={self.empty} throttled={self.throttled} errors={self.errors}, "
                f"only {self.ok} answered). The index is refusing this host, not "
                f"reporting a genuinely quiet week. Treating as a hard failure "
                f"rather than filing zero leads as success."
            )


def search(q, limit=8, recent=True, tries=3):
    """Run one query. Returns (status, results).

    status is one of:
      "ok"        results came back
      "empty"     the index answered, nothing matched
      "throttled" a backend refused or timed out, retries exhausted
      "error"     something else went wrong; results is []

    The distinction matters because the old code collapsed all three into
    "return [] or None", and a datacenter-IP soft block reads as "empty" — the
    most misleading failure this tool could have.
    """
    delay = 4
    last = ""
    for attempt in range(tries):
        try:
            with DDGS() as d:
                # See TIMELIMIT above for why the index's time filter is off.
                out = list(d.text(q, max_results=limit,
                                  timelimit=TIMELIMIT if recent else None))
            time.sleep(SLEEP)
            if out:
                return "ok", out
            # Defensive: current ddgs raises rather than returning []. If a
            # future version returns empty, give it one more chance before
            # believing it — the same proven query has returned 7, 7, then 0.
            if attempt == 0:
                time.sleep(delay)
                continue
            return "empty", []
        except Exception as e:
            msg = str(e)
            last = msg
            if "No results found" in msg:
                # ddgs only phrases it this way when NO backend errored, i.e.
                # the index really did answer with nothing. Retry once anyway;
                # the index is flaky enough that a single zero is not proof.
                if attempt == 0:
                    time.sleep(delay)
                    continue
                return "empty", []
            if attempt == tries - 1:
                print(f"      search THROTTLED after {tries} tries: {msg[:80]}")
                return "throttled", []
            time.sleep(delay)
            delay *= 2
    return "error", []


def reddit_slug_title(url):
    """The post title Reddit puts in its own URL, or "".

    Sub-scoped queries usually return a real title, but a minority still come
    back as "Link to reddit.com" with an empty snippet. The slug carries the
    title verbatim, so recovering it is the difference between a scoreable hit
    and a blank one — and it stops "Link to reddit.com" being written into the
    CRM as the name of the lead.
    """
    u = url or ""
    if "/comments/" not in u:
        return ""
    parts = [x for x in u.split("/comments/")[-1].split("/") if x]
    if len(parts) < 2:
        return ""
    return re.sub(r"\s+", " ", parts[1].replace("_", " ")).strip()


def _reddit_is_legacy(url):
    """True for a Reddit post whose id predates the 7-character era.

    Ordering fact, not a date. See _REDDIT_ID_RE above.
    """
    m = _REDDIT_ID_RE.search(url or "")
    return bool(m) and len(m.group(1)) < 7


def build_queries(plat, phrase, city):
    """The literal query strings for one (platform, phrase, city).

    Platform shape is not cosmetic. Measured 2026-08-26:
      site:nextdoor.com/ask-neighbors "roof leak" Plano   -> 0 results, always.
          That path is not in the index. It was consuming HALF of every
          Nextdoor query budget and returning nothing, every run.
      site:reddit.com "roof leak" Plano                   -> 6 results, none in
          Texas (r/HousingUK, r/centuryhomes, r/memes), all titled
          "Link to reddit.com". The index ignores a bare city word.
      site:reddit.com/r/plano roofer                      -> 6 results, all real
          Plano homeowners asking for a roofer, titles intact.
    """
    phrase = (phrase or "").strip()
    city = (city or "").strip()
    if plat == "reddit":
        subs, sub_names_city = trade_vocab.local_subreddits(city)
        qs = []
        for sub in subs:
            # When the sub IS the city, repeating the city word only narrows the
            # index against itself. When it is the metro sub, the city word is
            # the only thing separating Frisco from Fort Worth.
            tail = "" if sub_names_city else f" {city}"
            qs.append(f"site:reddit.com/r/{sub} {phrase}{tail}".strip())
        return qs
    return [f"{PLATFORMS[plat]} {phrase} {city}".strip()]


# Which phrase of the trade vocabulary a run starts from. Without this the
# rotation is a pure function of (city index, phrase count), so every run
# forever sends the SAME handful of queries out of a 56-to-63 phrase
# vocabulary — and after the first run every URL they can reach is already in
# seen_watch_urls.txt, which makes kept=0 structurally guaranteed. The offset
# advances each run so the tail of the vocabulary actually gets used.
ROTATION = HERE / "watch_rotation.json"


def rotation_offset(bump=0):
    try:
        n = int(json.loads(ROTATION.read_text(encoding="utf-8")).get("offset", 0))
    except Exception:
        n = 0
    if bump:
        try:
            ROTATION.write_text(json.dumps({"offset": n + bump}), encoding="utf-8")
        except Exception:
            pass
    return n


def draft_reply(client_slug, client_name, trade, city, post_title, snippet, urgent):
    """Per-client voice. Every client used to share one template, so a roofer,
    a junk hauler and a 3PL all sounded identical — which reads as a bot.
    client_voice guarantees the returned text passes its own voice gate."""
    return client_voice.draft_reply(client_slug, client_name, trade, city,
                                    post_title, snippet, urgent)


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


def _csv(value):
    return [x.strip() for x in (value or "").split(",") if x.strip()]


# channels='none' is Jack's deliberate off-switch for a client that is a real,
# active, paying account but simply has no social lead-watching (Northcomm,
# 2026-08-26: "northcomm doesnt need a scrapper take it off"). It is NOT the
# same as a missing config, and it must never be reported as one — otherwise
# the health board nags forever about a decision that has already been made.
# This is a string sentinel because a proper scrape_enabled boolean has not
# been migrated yet; see the report notes on the DDL path.
OFF_SWITCH = {"none", "off", "disabled"}


def resolve_platforms(client, override=None):
    """Which platforms to search for THIS client.

    Source of truth is crm_clients.channels. `override` is the --platforms flag,
    for manual debugging only; when it is set it replaces the client's channels
    and says so.

    Returns (platforms, state, reason).
      state "ok"      -> platforms is non-empty, go
            "off"     -> deliberately disabled, spend zero queries, not a defect
            "broken"  -> misconfigured; somebody needs to fix it
    """
    if override:
        wanted = _csv(override)
        source = "--platforms flag"
    else:
        wanted = _csv(client.get("channels"))
        source = "crm_clients.channels"
    if wanted and all(w.lower() in OFF_SWITCH for w in wanted):
        return [], "off", f"{source}={','.join(wanted)}"
    if not wanted:
        return [], "broken", f"{source} is empty — set it to e.g. 'nextdoor,reddit'"
    good = [p for p in wanted if p in PLATFORMS]
    bad = [p for p in wanted if p not in PLATFORMS and p.lower() not in OFF_SWITCH]
    if not good:
        return [], "broken", (f"{source}={','.join(wanted)} — none are searchable "
                              f"here (supported: {','.join(sorted(PLATFORMS))})")
    if bad:
        print(f"    note: ignoring unsupported channel(s) {','.join(bad)} "
              f"from {source}")
    return good, "ok", ""


def config_gap(client):
    """Why this client cannot be searched, or "" if it can.

    NEVER invents a niche or a city. Northcomm has scrape_niche=None and
    scrape_cities=None; the old code substituted the literal string "work" and
    searched with no location, producing nationwide noise like a Boynton Beach
    lanai-rescreening thread filed against a Texas IT company.
    """
    missing = []
    if not (client.get("scrape_niche") or "").strip():
        missing.append("scrape_niche")
    if not (client.get("scrape_cities") or "").strip():
        missing.append("scrape_cities")
    if missing:
        return f"no scraper config, needs niche+cities (missing: {', '.join(missing)})"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", help="one client by name or slug")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--platforms", default="",
                    help="DEBUG override; normally each client's crm_clients.channels is used")
    ap.add_argument("--limit", type=int, default=25, help="max drafts per run")
    ap.add_argument("--phrases", type=int, default=6,
                    help="intent phrases per city per platform (keeps a run bounded). "
                         "Raised from 2-3 to 6 to cover more of the 22-30 phrase "
                         "vocabulary per run without loosening any gate; the "
                         "rotation offset still advances by this amount each run "
                         "so a full cycle completes in roughly vocab_size/phrases "
                         "runs (printed per client at the top of its section).")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show-queries", action="store_true",
                    help="print every query string and its raw result count")
    args = ap.parse_args()

    env = load_env()
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY")

    clients = load_clients(env, None if args.all else args.client)
    if not clients:
        sys.exit("No matching active client in crm_clients. Add one first.")

    seen = set(SEEN.read_text(encoding="utf-8").split("\n")) if SEEN.exists() else set()
    drafts, parked_location = [], []
    stats = dict(queries=0, results=0, kept=0, dup=0, no_intent=0, throttled=0,
                 rejected=0, low_score=0, unresolved_location=0, empty_queries=0,
                 errors=0, off_by_choice=0, misconfigured=0, supply_side=0, no_draft=0)
    # Per-client counters are summed at the end and checked against these
    # totals. A telemetry number that disagrees with the run summary is worse
    # than no number, because the OS renders it to Jack as a health signal.
    ledger = []
    health = IndexHealth()
    # Craigslist is a direct HTML scrape, not the DDGS index, so a bad streak
    # there says nothing about whether the search index is down. Tracked
    # separately (streak_limit effectively disabled) so a Craigslist block
    # is reported honestly without aborting the DDGS-backed platforms for
    # every other client in the run.
    cl_health = IndexHealth(streak_limit=10_000)
    # Advance the phrase window so consecutive runs ask DIFFERENT questions.
    # A dry run must not move it, or a debug run silently steals the next real
    # run's queries.
    rot = rotation_offset(bump=0 if args.dry_run else max(1, args.phrases))
    print(f"phrase rotation offset {rot} (each run advances by --phrases)")
    ran_at = watch_telemetry.utcnow()
    aborted = ""

    def flush_client(c, per, status, skip_reason="", plats_used=""):
        """Write telemetry for one client. Runs for skipped clients too — a
        skipped client that leaves no row is indistinguishable from one that
        was never looked at."""
        ledger.append(dict(per))
        if args.dry_run:
            return
        watch_telemetry.record_run(url, key, {
            "ran_at": ran_at,
            "client": c["name"],
            "client_slug": c.get("slug"),
            "status": status,
            "skip_reason": skip_reason or None,
            "platforms": plats_used or None,
            "queries": per["queries"], "results": per["results"],
            "kept": per["kept"], "rejected": per["rejected"],
            "throttled": per["throttled"], "empty_queries": per["empty_queries"],
            "errors": per["errors"],
            "dup": per["dup"], "no_intent": per["no_intent"],
            "low_score": per["low_score"],
            "unresolved_location": per["unresolved_location"],
        })
        watch_telemetry.mark_scraped(url, key, c.get("slug"), ran_at)

    try:
        for c in clients:
            per = dict(queries=0, results=0, kept=0, rejected=0,
                       throttled=0, empty_queries=0, errors=0,
                       unresolved_location=0, supply_side=0, no_draft=0,
                       dup=0, no_intent=0, low_score=0)
            # WHY nothing was kept. "kept 0" with no reason is the failure that
            # hid every one of the problems this run was written to find: a
            # channel whose whole indexed corpus is a marketplace, a query shape
            # the index answers with 0, and a dedupe file that had already eaten
            # every URL the fixed rotation could reach all read as the same
            # silent zero. These counters are per-client and per-channel so the
            # next zero says which of those it is.
            why_drop = {}
            by_channel = {}

            def note(reason, plat, n=1):
                why_drop[reason] = why_drop.get(reason, 0) + n
                ch = by_channel.setdefault(plat, dict(queries=0, results=0,
                                                      kept=0, reasons={}))
                ch["reasons"][reason] = ch["reasons"].get(reason, 0) + n

            # The deliberate off-switch is checked FIRST and wins outright.
            # Northcomm has channels='none' AND null niche/cities; reporting it
            # as misconfigured would be nagging Jack about a client he has
            # explicitly taken off the scraper.
            plats, state, why = resolve_platforms(c, args.platforms)
            if state == "off":
                print(f"\n=== {c['name']}")
                print(f"  SKIPPED - scraping off by choice ({why})")
                stats["off_by_choice"] += 1
                flush_client(c, per, "off", f"scraping off by choice ({why})")
                continue

            gap = config_gap(c)
            if gap:
                print(f"\n=== {c['name']}")
                print(f"  SKIPPED - MISCONFIGURED: {gap}")
                stats["misconfigured"] += 1
                flush_client(c, per, "skipped", gap)
                continue

            if state == "broken":
                print(f"\n=== {c['name']}")
                print(f"  SKIPPED - MISCONFIGURED: no searchable channels: {why}")
                stats["misconfigured"] += 1
                flush_client(c, per, "skipped", f"no searchable channels: {why}")
                continue

            trade = c["scrape_niche"].strip()
            cities = _csv(c.get("scrape_cities"))
            extra = _csv(c.get("scrape_terms"))
            allowed_subs = allowed_subreddits(cities)
            allowed_cl_subs = craigslist_source.subdomains_for_cities(cities)
            print(f"\n=== {c['name']} — {trade} in {', '.join(cities)} "
                  f"[{','.join(plats)}]")
            if "reddit" in plats:
                print(f"    reddit geo gate: allowed subs = "
                      f"{', '.join(sorted(allowed_subs)) or '(none, misconfigured)'}")
            if "craigslist" in plats:
                print(f"    craigslist geo gate: allowed metro subdomains = "
                      f"{', '.join(sorted(allowed_cl_subs)) or '(none mapped for these cities)'}")
            # Vocabulary coverage honesty: how much of this trade's phrase
            # vocabulary a run actually touches, and how many runs the
            # rotation takes to cycle through all of it once.
            _voc_len = len(trade_vocab.intent_queries(trade, "", extra))
            _runs_to_cover = -(-_voc_len // max(1, args.phrases))  # ceil div
            print(f"    phrase vocabulary: {_voc_len} phrases, {args.phrases} used "
                  f"per city per run -> full rotation every {_runs_to_cover} runs")

            for city in cities:
                for plat in plats:
                    # Trade-specific phrasing. The old generic list searched for
                    # words customers do not use — junk-removal demand reads "need
                    # to get rid of" / "haul away", almost never "junk removal",
                    # which is why that client had produced zero drafts ever.
                    #
                    # NOTE the phrases are built WITHOUT the city. build_queries
                    # decides where the city belongs per platform: as a keyword
                    # for Nextdoor, but as a SUBREDDIT for Reddit, where a bare
                    # city word is ignored by the index entirely.
                    allp = trade_vocab.intent_queries(trade, "", extra)
                    ci = cities.index(city)
                    base = rot + ci * args.phrases
                    picked = [allp[(base + k) % len(allp)]
                              for k in range(min(args.phrases, len(allp)))]
                    cl_sub = craigslist_source.subdomain_for_city(city) if plat == "craigslist" else ""
                    if plat == "craigslist":
                        if not cl_sub:
                            note("craigslist has no metro subdomain mapped for "
                                 "this city (not skipped as an error, just not "
                                 "searchable here)", plat)
                            queries = []
                        else:
                            queries = [(section, phrase) for phrase in picked
                                      for section in CRAIGSLIST_SECTIONS]
                    else:
                        queries = []
                        for phrase in picked:
                            queries.extend(build_queries(plat, phrase, city))
                    for q in queries:
                        stats["queries"] += 1
                        per["queries"] += 1
                        by_channel.setdefault(plat, dict(queries=0, results=0,
                                                         kept=0, reasons={}))
                        by_channel[plat]["queries"] += 1
                        if plat == "craigslist":
                            section, phrase = q
                            status, res = craigslist_source.search(cl_sub, section, phrase, limit=15)
                            q_display = f"craigslist/{section} \"{phrase}\" ({cl_sub})"
                            cl_health.note(status)
                        else:
                            status, res = search(q, 6)
                            q_display = q
                            health.note(status)
                        if args.show_queries:
                            print(f"    q[{status}:{len(res)}] {q_display}")
                        if status == "throttled":
                            stats["throttled"] += 1
                            per["throttled"] += 1
                            note("query throttled by the index", plat)
                            continue
                        if status == "error":
                            stats["errors"] += 1
                            per["errors"] += 1
                            note("query errored", plat)
                            continue
                        if status == "empty":
                            stats["empty_queries"] += 1
                            per["empty_queries"] += 1
                            note("query answered with nothing", plat)
                            continue
                        for r in res:
                            stats["results"] += 1
                            per["results"] += 1
                            by_channel[plat]["results"] += 1
                            u = (r.get("href") or "").split("?")[0]
                            if not u or u in seen:
                                stats["dup"] += 1
                                per["dup"] += 1
                                note("already seen in an earlier run", plat)
                                continue
                            title = r.get("title") or ""
                            body = r.get("body") or ""
                            # Recover the title Reddit puts in its own URL when
                            # the index hands back "Link to reddit.com" and an
                            # empty snippet. Without this the hit is unscoreable
                            # AND "Link to reddit.com" gets written into the CRM
                            # as the name of the lead.
                            slug_text = reddit_slug_title(u)
                            if slug_text and (not title
                                              or title.strip().lower().startswith("link to reddit")):
                                title = slug_text
                            blob = f"{title} {body}"
                            if plat == "reddit":
                                geo_ok, sub, geo_reason = geo_gate_reddit(u, allowed_subs)
                                if not geo_ok:
                                    stats["rejected"] += 1
                                    per["rejected"] += 1
                                    note(f"GEO REJECT: {geo_reason}", plat)
                                    continue
                            if plat == "craigslist":
                                geo_ok, sub, geo_reason = craigslist_source.geo_gate(
                                    cl_sub, allowed_cl_subs)
                                if not geo_ok:
                                    stats["rejected"] += 1
                                    per["rejected"] += 1
                                    note(f"GEO REJECT: {geo_reason}", plat)
                                    continue
                            if _reddit_is_legacy(u):
                                note("reddit thread predates the 7-char post-id era "
                                     "(too old to reply to)", plat)
                                continue
                            # Score before drafting. The old check just required the
                            # trade's first word somewhere in the text, which let
                            # through roofing companies' own business pages — the
                            # single most common false positive on Nextdoor.
                            # Reddit hits arrive as "Link to reddit.com" with the
                            # description hidden, so title+body is empty and every
                            # gate rejected them — the platform was structurally
                            # unreachable. The post title is in the URL slug.
                            #
                            # ARGUMENT ORDER IS LOAD-BEARING. The signature is
                            # is_relevant(trade, title, body, url). This was being
                            # called as (title, body, url, trade), which made the
                            # function derive the trade vocabulary from the RESULT'S
                            # OWN TITLE — so it matched itself and waved almost
                            # everything through. That is how a "Hello! - San Marcos,
                            # TX" post got kept for a health & beauty DTC brand.
                            if not trade_vocab.is_relevant(trade, title, body, u):
                                stats["no_intent"] += 1
                                per["no_intent"] += 1
                                note("not about this trade, or marketplace/"
                                     "platform-marketing noise", plat)
                                continue
                            rel = relevance.score_hit(title or slug_text, body or slug_text,
                                                      u, trade, city,
                                                      trade_vocab.relevance_terms(trade))
                            if rel["reject"] or rel.get("verdict") == "reject":
                                stats["rejected"] += 1
                                per["rejected"] += 1
                                # A competitor's ad and a post from the wrong
                                # state are both rejections, but they mean very
                                # different things about a channel's health. A
                                # channel full of supply_side is one where our
                                # own trade advertises, not one where customers
                                # ask. Counted as a SUBSET of rejected, never
                                # in addition to it.
                                if rel.get("verdict") == "supply_side":
                                    stats["supply_side"] += 1
                                    per["supply_side"] += 1
                                    note("supply side: a competitor advertising, "
                                         "not a customer asking", plat)
                                else:
                                    note(f"relevance rejected: "
                                         f"{(rel.get('reject_reason') or 'no reason given')[:90]}",
                                         plat)
                                continue
                            # Strong demand we cannot PLACE. relevance.py caps
                            # these at 0.30, which sits under MIN_RELEVANCE, so
                            # without this branch they vanish into low_score —
                            # burying a good lead AND making the counters lie
                            # about why it was dropped. The real case: r/Roofing
                            # "Hail Storm came through town 2 days ago", intent
                            # 1.0 / trade 1.0 / recency 1.0, geo unresolvable,
                            # 0.910 -> 0.300. Six of these were auto-filed as
                            # ready-to-send replies offering work "around Plano"
                            # to posts that might have been in Ohio.
                            if rel.get("verdict") == "unresolved_location":
                                stats["unresolved_location"] += 1
                                per["unresolved_location"] += 1
                                if u not in seen:
                                    seen.add(u)
                                    parked_location.append({
                                        "client": c["name"],
                                        "channel": plat,
                                        "direction": "outbound",
                                        "recipient": title[:120] or "(post)",
                                        "recipient_url": u,
                                        "evidence_url": u,
                                        "subject": None,
                                        # Deliberately NOT a drafted reply. A
                                        # human decides where this post is
                                        # before a single word is written.
                                        "body": None,
                                        "personalization": (
                                            "NEEDS LOCATION CHECK — demand looks real "
                                            f"(score_if_located "
                                            f"{rel.get('components', {}).get('score_if_located', 0):.2f}) "
                                            f"but no city is resolvable from the post. "
                                            f"Confirm it is in {c['name']}'s service area "
                                            f"before replying. Post: {title[:120]}"),
                                        "status": "needs_location_check",
                                        "tier": "reply",
                                    })
                                note("real demand but no resolvable location "
                                     "(parked for a human to place)", plat)
                                continue
                            if rel["score"] < MIN_RELEVANCE:
                                stats["low_score"] += 1
                                per["low_score"] += 1
                                note(f"scored {rel['score']:.2f}, under the "
                                     f"{MIN_RELEVANCE} bar", plat)
                                continue
                            urgent = bool(URGENT.search(blob))
                            _body, _voice = draft_reply(
                                c.get("slug") or "", c["name"], trade,
                                city, u or title, body, urgent)
                            if _body is None:
                                # client_voice refused: no real detail to
                                # reference, or the post fails its own
                                # buying-intent gate. A legitimate funnel
                                # stage, not an error -- log and skip, do NOT
                                # write an empty draft row to Supabase.
                                stats["no_draft"] += 1
                                per["no_draft"] += 1
                                seen.add(u)
                                note(f"NO DRAFT WRITTEN: {(_voice or 'no reason given')[:100]}",
                                     plat)
                                continue
                            seen.add(u)
                            drafts.append({
                                "client": c["name"],
                                "channel": plat,
                                "direction": "outbound",
                                "recipient": title[:120] or "(post)",
                                "recipient_url": u,
                                "evidence_url": u,
                                "subject": None,
                                "body": _body,
                                "personalization": (("URGENT. " if urgent else "")
                                                    + f"[relevance {rel['score']:.2f}] "
                                                    + f"Public post: {title[:130]}"),
                                "status": "draft",
                                "tier": "reply",
                            })
                            stats["kept"] += 1
                            per["kept"] += 1
                            by_channel[plat]["kept"] += 1
                            print(f"  + [{plat}] {'URGENT ' if urgent else ''}{title[:62]}")
                            if stats["kept"] >= args.limit:
                                print(f"  CAPPED at --limit {args.limit} drafts for this "
                                      f"run; remaining queries/results this run were not "
                                      f"evaluated (not rejected, just not reached).")
                                break
                        if stats["kept"] >= args.limit:
                            break
                    if stats["kept"] >= args.limit:
                        break
                if stats["kept"] >= args.limit:
                    break

            print(f"  -- {c['name']}: queries={per['queries']} results={per['results']} "
                  f"kept={per['kept']} rejected={per['rejected']} "
                  f"no_draft={per['no_draft']} "
                  f"empty={per['empty_queries']} throttled={per['throttled']}")

            # Per-channel readout. A client can be healthy on one channel and
            # structurally dead on another, and one blended number hides it.
            for plat in plats:
                ch = by_channel.get(plat)
                if not ch:
                    continue
                top = sorted(ch["reasons"].items(), key=lambda kv: -kv[1])[:3]
                print(f"     [{plat}] {ch['queries']}q -> {ch['results']} results, "
                      f"{ch['kept']} kept")
                for reason, n in top:
                    print(f"        {n:3d} x {reason}")
                if ch["queries"] and ch["results"] == 0:
                    print(f"        NOTE: {plat} returned nothing at all for this "
                          f"client. Either the query shape does not match what "
                          f"this platform exposes to the index, or the platform "
                          f"is not publicly indexed.")

            if per["kept"] == 0:
                # The honest zero. "no demand found" is a valid, useful result,
                # but only when it says which of the several very different
                # zeroes it actually is.
                print(f"  WHY NOTHING WAS KEPT for {c['name']}:")
                if not why_drop:
                    print("        nothing came back to judge at all")
                for reason, n in sorted(why_drop.items(), key=lambda kv: -kv[1]):
                    print(f"        {n:3d} x {reason}")

            flush_client(c, per, "ok", "", ",".join(plats))
    except IndexDown as e:
        aborted = str(e)
        print(f"\n!! INDEX DOWN — aborting run\n   {aborted}")

    print("\n=== run summary ===")
    for k, v in stats.items():
        print(f"  {k:20}: {v}")
    answered = health.ok + health.empty + health.throttled + health.errors
    if answered:
        print(f"  {'yield':20}: {stats['results'] / answered:.2f} results/query "
              f"(ok={health.ok} empty={health.empty} throttled={health.throttled} "
              f"errors={health.errors}, longest dead streak={health.max_empty_streak})")
    cl_answered = cl_health.ok + cl_health.empty + cl_health.throttled + cl_health.errors
    if cl_answered:
        cl_status = ("BLOCKED" if cl_health.ok == 0 and cl_health.throttled > 0
                     else "ok" if cl_health.ok else "no listings matched")
        print(f"  {'craigslist':20}: {cl_health.ok} ok, {cl_health.empty} empty, "
              f"{cl_health.throttled} throttled, {cl_health.errors} errors "
              f"({cl_answered} queries) -- {cl_status}")
        if cl_health.ok == 0 and cl_health.throttled > 0:
            print(f"        Craigslist is refusing every request from this host "
                  f"(403/429/503 on all {cl_health.throttled} attempts). This is a "
                  f"real block, not a quiet channel -- reporting it rather than "
                  f"silently filing zero Craigslist leads.")

    # --- reconciliation -----------------------------------------------------
    # Every per-client row written to watch_runs must add up to the run summary
    # above. A skipped client legitimately shows queries=0; what would NOT be
    # legitimate is the parts failing to sum to the whole, and the OS is now
    # rendering these per-client numbers to Jack as a health signal.
    mismatch = []
    for field in ("queries", "results", "kept", "rejected", "throttled",
                  "empty_queries", "errors", "unresolved_location",
                  "supply_side", "dup", "no_intent", "low_score"):
        summed = sum(p.get(field, 0) for p in ledger)
        if summed != stats.get(field, 0):
            mismatch.append(f"{field}: per-client sum {summed} != run total "
                            f"{stats.get(field, 0)}")
    print(f"  {'reconciled':20}: {len(ledger)} client rows, "
          + ("OK — per-client counts sum to the run totals"
             if not mismatch else "MISMATCH " + "; ".join(mismatch)))

    # --- health verdict -----------------------------------------------------
    # Zero-yield is a HARD FAILURE, not a quiet zero. Same rule this project
    # adopted after TikTok exited 0 with 0 rows four runs in a row and the
    # health-check could never see it.
    problems = []
    if mismatch:
        problems.append("telemetry does not reconcile — " + "; ".join(mismatch))
    if aborted:
        problems.append(aborted)
    if stats["queries"] and stats["results"] == 0:
        problems.append(
            f"{stats['queries']} queries returned zero results between them. "
            f"The index is not answering this host — this is a failure, not an "
            f"empty week.")
    elif (stats["queries"] >= YIELD_MIN_QUERIES
          and stats["results"] / stats["queries"] < MIN_YIELD):
        problems.append(
            f"yield {stats['results'] / stats['queries']:.2f} results/query over "
            f"{stats['queries']} queries is below the {MIN_YIELD} floor "
            f"(a healthy run measures ~3). The index is probably soft-blocking "
            f"this host.")
    searchable = len(clients) - stats["off_by_choice"]
    if searchable and stats["misconfigured"] == searchable:
        problems.append(
            f"every client that was supposed to be searched ({searchable}) was "
            f"skipped as misconfigured — nothing was searched.")

    if args.dry_run:
        print("\ndry run, nothing written")
        if problems:
            print("\n=== HARD FAILURE ===")
            for p in problems:
                print(f"  ! {p}")
            sys.exit(2)
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

    if parked_location:
        # Parked, never drafted. status='needs_location_check' keeps these out
        # of anything that treats status='draft' as send-ready.
        h = {"apikey": key, "Authorization": f"Bearer {key}",
             "Content-Type": "application/json",
             "Prefer": "resolution=ignore-duplicates,return=representation"}
        res = requests.post(
            f"{url}/rest/v1/outbound?on_conflict=client,channel,recipient,subject",
            headers=h, json=parked_location, timeout=60)
        n = len(res.json()) if res.ok else 0
        if not res.ok:
            print(f"\nparking FAILED {res.status_code} {res.text[:200]}")
        else:
            print(f"parked {len(parked_location)} unplaceable posts, {n} new "
                  f"-> CRM status='needs_location_check' (human locates before replying)")
    SEEN.write_text("\n".join(sorted(x for x in seen if x)), encoding="utf-8")

    if problems:
        print("\n=== HARD FAILURE ===")
        for p in problems:
            print(f"  ! {p}")
        sys.exit(2)


if __name__ == "__main__":
    main()
