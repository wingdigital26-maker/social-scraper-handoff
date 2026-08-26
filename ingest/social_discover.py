#!/usr/bin/env python3
"""
Social prospect discovery — TikTok + Instagram + LinkedIn, ZERO API COST.

THE KEY IDEA: we do not scrape the platforms directly. TikTok, Instagram and
LinkedIn all block direct scraping hard (TikTok anti-bot, IG login wall,
LinkedIn bans + litigates). Instead we query the SEARCH INDEX for public
profiles and posts those platforms already let Google index. Same data,
no blocks, no login, no keys, no cost.

What it does:
    niche + city  ->  search index  ->  public profiles/posts on 3 platforms
                  ->  extract handle/name  ->  trade + geography gates
                  ->  dedupe  ->  candidates.jsonl
                  ->  (enrich.py scores + drafts)  ->  (db.py -> queue)

HARD RULES (same as the rest of the pipeline):
  1. No login, no cookies, no credentials on any platform. Public index only.
  2. No media downloaded. We store public URLs only.
  3. Nothing auto-sends. Everything lands in the review queue for a human.
  4. LinkedIn: public profile URLs from the search index only. We never log in
     to LinkedIn and never bulk-scrape profile pages — that violates their
     User Agreement and gets accounts permanently banned.

THREE THINGS THIS FILE LEARNED THE HARD WAY
-------------------------------------------
1. A ZERO IS NOT A SUCCESS. The old `search()` caught every exception, printed
   one line and returned []. A run where the index refused every single query
   finished with "kept: 0" and exit code 0 — indistinguishable from a quiet
   day. The index soft-blocks datacenter IPs by answering HTTP 200 with an
   empty page, which ddgs surfaces as DDGSException("No results found."), so on
   a GitHub runner this was the NORMAL outcome (measured 0.13 results/query on
   a hosted runner vs ~3.1 from a workstation). One empty query is not
   diagnosable; a whole run of them is. Hence IndexHealth + the yield floor
   below, and a non-zero exit when the run produced nothing.

2. THE CITY IN THE QUERY IS NOT THE CITY OF THE BUSINESS. Searching "roofing
   Addison" matched Addison ILLINOIS; "Ivan Murphy Murphy TX" matched a painter
   in Halifax, Nova Scotia. Every row used to be stamped location_confidence
   0.6 regardless of evidence. Now geography is graded from what the result
   actually says, the grade is recorded alongside the evidence for it, and only
   POSITIVE evidence of somewhere else rejects a row. No evidence means low
   confidence — unknown, not wrong (see identity_gate.py's docstring).
   Note carefully: a DFW city name inside a BUSINESS NAME ("Prosper Roofing",
   "Cedar Roofing", "Addison Roof") is not location evidence. Those three are
   real rows in the DB and all three carry out-of-state phone numbers.

3. LINKEDIN PERSON PROFILES ARE NOT PROSPECTS. Of 469 linkedin.com/in rows
   discovered historically, 454 were classified not_a_business downstream and
   ZERO were ever verified. They are skipped by default now, and the person
   query is not even issued unless --include-people is passed.

Usage:
    python social_discover.py --niche roofing --city Dallas
    python social_discover.py --niche "warehouse" --city "Fort Worth" --platforms linkedin
    python social_discover.py --niche hvac --city Plano --limit 20 --dry-run

Exit codes:
    0  the run worked (prospects kept, or honest zero with a healthy index)
    2  HARD FAILURE — the index was not answering, so a zero here means
       nothing about the market. Never report this run as success.
"""
import argparse
import json
import pathlib
import re
import sys
import time

# Windows consoles default to cp1252 and business names routinely contain
# symbols and emoji it cannot encode. Without this, printing a single
# prospect name raises UnicodeEncodeError and kills the whole run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from ddgs import DDGS
except ImportError:  # older package name
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        sys.exit("Needs the search client:  pip install ddgs")

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import trade_vocab  # noqa: E402  (shared trade vocabulary; read-only use)

SEEN_FILE = HERE / "seen_social_profiles.txt"
OUT_FILE = HERE / "candidates.jsonl"
REJECT_FILE = HERE / "discover_rejects.jsonl"

SLEEP = 2.0          # between searches, be polite to the index
MAX_PER_QUERY = 25

# --- index health thresholds ------------------------------------------------
# EMPTY_STREAK_FAIL mirrors watch_social.py's rule: real queries do not all come
# back empty, so a long dead streak means the host is being refused.
# A single discovery run only issues 5-6 queries, so the streak limit is small
# on purpose: five dead queries in a row IS the whole run.
EMPTY_STREAK_FAIL = 5
MIN_YIELD = 0.5          # results/query; a healthy workstation run measures ~3,
                         # a soft-blocked hosted runner measured 0.13
YIELD_MIN_QUERIES = 6    # below this, too few samples for the ratio to mean much

# Per-platform search recipes. Each yields public, indexed URLs.
# "person": this query only ever returns individual people, not companies.
PLATFORMS = {
    "tiktok": {
        "queries": [
            {"q": 'site:tiktok.com "{niche}" {city}'},
            {"q": 'site:tiktok.com/@ {niche} {city}'},
        ],
        "profile_re": re.compile(r"tiktok\.com/@([A-Za-z0-9._-]+)"),
    },
    "instagram": {
        "queries": [
            {"q": 'site:instagram.com {niche} {city}'},
            {"q": 'site:instagram.com "{niche}" {city} contractor'},
        ],
        "profile_re": re.compile(r"instagram\.com/([A-Za-z0-9._]+)/?$"),
    },
    "linkedin": {
        "queries": [
            {"q": 'site:linkedin.com/in {niche} {city}', "person": True},
            {"q": 'site:linkedin.com/company {niche} {city}'},
        ],
        "profile_re": re.compile(r"linkedin\.com/(?:in|company)/([A-Za-z0-9._-]+)"),
    },
}

# URL fragments that are never a prospect (platform chrome, help pages, etc.).
JUNK = ("/explore", "/directory", "/legal", "/help", "/about", "/privacy",
        "/tags/", "/pulse/", "/jobs/", "/p/", "/reel/")

# A TikTok video URL still names its account, and the account is the prospect.
# The old code threw every /video/ hit away via JUNK, which is most of what
# TikTok returns — 28 TikTok rows exist in the DB against 272 Instagram ones.
TIKTOK_VIDEO = re.compile(r"tiktok\.com/@([A-Za-z0-9._-]+)/video/")


class IndexDown(RuntimeError):
    """The search index stopped answering this host. Not an empty market."""


class LimitReached(Exception):
    """--limit satisfied. A clean stop, not an interruption or a failure."""


class IndexHealth:
    """Tracks whether the search index is actually answering.

    Same shape as watch_social.IndexHealth, deliberately re-implemented rather
    than imported so discovery keeps no runtime dependency on the reply engine.

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
                f"reporting a genuinely empty city. Treating as a hard failure "
                f"rather than filing zero prospects as success.")


def search(query, limit, tries=3):
    """Run one query. Returns (status, results).

    status is one of:
      "ok"        results came back
      "empty"     the index answered, nothing matched
      "throttled" a backend refused or timed out, retries exhausted
      "error"     something else went wrong; results is []

    The old version collapsed all four into `return []`, which is precisely how
    a datacenter soft block disguised itself as a quiet market.
    """
    delay = 4
    for attempt in range(tries):
        try:
            with DDGS() as d:
                out = list(d.text(query, max_results=limit))
            if out:
                return "ok", out
            # Defensive: current ddgs raises rather than returning []. If a
            # future version returns empty, give it one more chance before
            # believing it.
            if attempt == 0:
                time.sleep(delay)
                continue
            return "empty", []
        except Exception as e:
            msg = str(e)
            if "No results found" in msg:
                # ddgs only phrases it this way when NO backend errored, i.e.
                # the index really did answer with nothing. Retry once anyway;
                # the index is flaky enough that a single zero is not proof.
                if attempt == 0:
                    time.sleep(delay)
                    continue
                return "empty", []
            if attempt == tries - 1:
                print(f"   search THROTTLED after {tries} tries: {msg[:90]}")
                return "throttled", []
            time.sleep(delay)
            delay *= 2
    return "error", []


# --- geography ---------------------------------------------------------------
# Everything below grades the geographic evidence CARRIED BY THE RESULT. It
# never upgrades a row on the strength of the query that found it.

DFW_AREA_CODES = {"214", "430", "469", "682", "817", "903", "940", "945", "972"}

# The DFW metro city list, matched only in address-shaped context (see below).
DFW_CITY_WORDS = (
    "dallas|fort worth|ft worth|arlington|plano|irving|garland|frisco|mckinney|"
    "denton|grand prairie|mesquite|carrollton|richardson|lewisville|allen|"
    "flower mound|coppell|farmers branch|grapevine|euless|bedford|hurst|"
    "north richland hills|haltom city|keller|southlake|mansfield|cedar hill|"
    "desoto|lancaster|duncanville|rockwall|addison|wylie|murphy|sachse|"
    "the colony|little elm|prosper|celina|anna|forney|waxahachie|midlothian|"
    "cleburne")

# Texas, stated outright. "Location: Plano, Texas", "Dallas, TX", "DFW".
# A bare "TX" is not matched on its own — it appears as an initialism often
# enough to be noise. It counts when a comma, a ZIP or a DFW city sits next to
# it, which is how a real address is written ("Plano TX", "Dallas, TX 75201").
TEXAS_RE = re.compile(
    r"(?:\btexas\b|,\s*tx\b|\btx\s+\d{5}\b|\bdfw\b|\bmetroplex\b|"
    r"\bnorth texas\b|\bdallas[-/ ]?fort worth\b|"
    rf"(?:{DFW_CITY_WORDS})[,\s]+tx\b)", re.I)

# A phone number whose area code is DFW is strong, self-carried evidence.
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]*)?\(?([2-9]\d{2})\)?[\s.-]*\d{3}[\s.-]*\d{4}(?!\d)")

_STATE_ABBR = ("AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|"
               "MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|"
               "UT|VT|VA|WA|WV|WI|WY")
_STATE_NAME = ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|"
               "Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|"
               "Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|"
               "Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|"
               "New Hampshire|New Jersey|New Mexico|New York|North Carolina|"
               "North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|"
               "South Carolina|South Dakota|Tennessee|Utah|Vermont|Virginia|"
               "Washington|West Virginia|Wisconsin|Wyoming")

# Address shape only: a comma then a state. "Addison, IL" is evidence.
# "Washington Avenue" and "Virginia Beach Rd" are not, and must not be.
# Case-SENSITIVE on purpose: ", IN" is Indiana, ", in" is the English word.
OTHER_STATE = re.compile(rf",\s*(?:{_STATE_ABBR})\b|,\s*(?:{_STATE_NAME})\b")

# Outside the US entirely. Every one of these was seen in the live data.
FOREIGN = re.compile(
    r"\b(nova scotia|ontario|british columbia|alberta|quebec|manitoba|"
    r"saskatchewan|canada|united kingdom|england|scotland|wales|ireland|"
    r"australia|new zealand|india|pakistan|philippines|nigeria|kenya|"
    r"south africa|ghana|tanzania|uganda|zimbabwe|egypt|morocco|jamaica|"
    r"brazil|brasil|mexico|germany|deutschland|france|spain|"
    r"portugal|italy|netherlands|poland|romania|ukraine|singapore|malaysia|"
    r"indonesia|vietnam|thailand|bangladesh|sri lanka|nepal|japan|"
    r"turkey|greece|sweden|norway|denmark|switzerland|austria|"
    r"argentina|chile|colombia|peru|guatemala|costa rica|"
    r"dubai|united arab emirates|saudi arabia|qatar)\b", re.I)


# LinkedIn serves a company on its own country subdomain — uk.linkedin.com,
# ae.linkedin.com, in.linkedin.com. That prefix is free, self-carried evidence
# of being outside the US, and it was being thrown away: "M&A Air Conditioning
# Ltd" (uk.) and "Airstron, LLC" (ae.) both landed in a Grapevine HVAC sweep.
# "us", "www" and the bare domain are all fine.
COUNTRY_SUBDOMAIN = re.compile(r"^https?://(?!www\.|us\.)([a-z]{2})\.linkedin\.com/", re.I)


def geo_evidence(city, title, body, url=""):
    """Grade the geography a RESULT carries. Returns (verdict, confidence, why).

    verdict:
      "in_region"     the text names Texas / DFW / a DFW area code
      "city_only"     a DFW city appears in address shape but no state
      "elsewhere"     positive evidence of another state or country, and no
                      Texas evidence at all — the only verdict that rejects
      "conflicting"   both are present; unproven either way, kept at low weight
      "none"          the text says nothing about location. UNKNOWN, NOT WRONG.
                      The city on this row came from the query, nothing more.

    Confidence feeds enrich.py's location weight and db.py's loc_confidence.
    """
    text = f"{title or ''} {body or ''}"
    tex = TEXAS_RE.search(text)
    area = next((m.group(1) for m in PHONE_RE.finditer(text)
                 if m.group(1) in DFW_AREA_CODES), None)
    other = OTHER_STATE.search(text)
    foreign = FOREIGN.search(text)

    here = []
    if tex:
        here.append(f"'{tex.group(0).strip()}'")
    if area:
        here.append(f"DFW area code {area}")
    away = []
    if other:
        away.append(f"'{other.group(0).strip()}'")
    if foreign:
        away.append(f"'{foreign.group(0)}'")
    cc = COUNTRY_SUBDOMAIN.match(url or "")
    if cc:
        away.append(f"served from the '{cc.group(1).lower()}.' LinkedIn country domain")

    if here and away:
        return ("conflicting", 0.35,
                f"names both {', '.join(here)} and {', '.join(away)} — unproven")
    if here:
        return "in_region", 0.85, f"result text names {', '.join(here)}"
    if away:
        return "elsewhere", 0.0, f"result text names {', '.join(away)}, no Texas anywhere"

    # A DFW city in address shape ("· Plano ·", "in Plano,") without a state.
    # Deliberately NOT matched inside a business name, because "Prosper
    # Roofing" / "Cedar Roofing" / "Addison Roof" are all real DB rows that
    # turned out to have out-of-state phone numbers.
    city_ctx = re.compile(
        rf"(?:location|based in|serving|located in|area)\s*[:\-]?\s*(?:{DFW_CITY_WORDS})\b"
        rf"|\bin\s+(?:{DFW_CITY_WORDS})\b", re.I)
    m = city_ctx.search(body or "")
    if m:
        return "city_only", 0.5, f"result text says {m.group(0).strip()!r}, but never names the state"
    return ("none", 0.2,
            f"result text carries no location at all; '{city}' came from the "
            f"search query only")


_BOILERPLATE = re.compile(
    r"\s*(?:[•·|\-]\s*)?(?:Instagram photos and videos?|Instagram|TikTok|LinkedIn)"
    r"(?:\s*photos and videos?)?\s*$", re.I)


def clean_name(title, platform):
    """Pull a human/business name out of the search result title."""
    t = (title or "").replace("�", " ")
    # Cut everything from the platform's own boilerplate onward, truncated or not
    # ("Infinite Roofing · Instagram photos and ..." -> "Infinite Roofing").
    t = re.split(r"\s*(?:Instagram photos|Instagram Photos|on TikTok|on Instagram|on LinkedIn)",
                 t)[0]
    t = re.split(r"\s*[|·—]\s*|\s+-\s+", t)[0].strip(" •·-|,")
    t = re.sub(r"\s*\(@[^)]*\)?[\s•·|,-]*$", " ", t)  # "Name (@handle)" / truncated "(@hand"
    for _ in range(3):                              # peel repeated platform tails
        new = _BOILERPLATE.sub("", t).strip(" •·-|")
        if new == t:
            break
        t = new
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t[:120] or None


_JUNK_TITLE = re.compile(r"^(link to |https?://|www\.)|^(instagram|tiktok|linkedin)\.?com?$", re.I)


def prettify_handle(handle):
    """roofingdallas -> Roofingdallas; new_view_roofing -> New View Roofing."""
    return " ".join(w.capitalize() for w in re.split(r"[._-]+", handle) if w) or handle


def best_name(title, handle, platform):
    n = clean_name(title, platform)
    if not n or _JUNK_TITLE.match(n) or len(n) < 3:
        return prettify_handle(handle)
    return n


def build(platform, handle, url, title, snippet, niche, city, geo):
    verdict, confidence, why = geo
    is_person = "/in/" in url
    name = best_name(title, handle, platform)
    return {
        "source": platform,
        "id": f"{platform}:{handle}",
        "name": name,
        "title": name,
        "author": handle,
        "place": city,
        "category": niche,
        "cat": niche,
        # Graded from what the RESULT said, never from the query. See
        # geo_evidence(); "none" is unknown, not wrong.
        "location_confidence": confidence,
        "location_verdict": verdict,
        "location_evidence": why,
        "lat": None, "lng": None,
        "upvotes": 0,                        # no public engagement number from the index
        "prospect_type": "person" if is_person else "business",
        "embeds": [{"type": platform, "url": url}],
        "desc": (snippet or "")[:400],
        "needs_review": True,
        "legal_status": "public-index",
        "tags": ["auto-discovered", platform, f"niche-{niche.replace(' ', '-')}",
                 f"city-{city.replace(' ', '-')}", f"geo-{verdict}"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", required=True, help='e.g. roofing, hvac, "warehouse operations"')
    ap.add_argument("--city", required=True, help='e.g. Dallas, "Fort Worth"')
    ap.add_argument("--platforms", default="tiktok,instagram,linkedin")
    ap.add_argument("--limit", type=int, default=0, help="stop after N new prospects")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-people", action="store_true",
                    help="also keep LinkedIn person profiles. Off by default: of "
                         "469 discovered historically, 454 were later classified "
                         "not_a_business and none were ever verified.")
    ap.add_argument("--no-trade-gate", action="store_true",
                    help="keep results whose text never mentions the trade "
                         "(diagnostic; they are almost always not prospects)")
    args = ap.parse_args()

    wanted = [p.strip() for p in args.platforms.split(",") if p.strip() in PLATFORMS]
    if not wanted:
        sys.exit(f"No valid platforms. Choose from: {', '.join(PLATFORMS)}")

    seen = (set(SEEN_FILE.read_text(encoding="utf-8", errors="replace").split())
            if SEEN_FILE.exists() else set())
    out = None if args.dry_run else OUT_FILE.open("a", encoding="utf-8")
    rej = None if args.dry_run else REJECT_FILE.open("a", encoding="utf-8")
    stats = dict(queries=0, results=0, junk=0, dup=0, off_trade=0, person=0,
                 out_of_region=0, kept=0)
    health = IndexHealth()
    aborted = ""

    print(f"Discovering '{args.niche}' in {args.city} across: {', '.join(wanted)}")
    print("(search-index only — no platform login, no scraping of walled pages)\n")

    try:
        for platform in wanted:
            spec = PLATFORMS[platform]
            for tmpl in spec["queries"]:
                if tmpl.get("person") and not args.include_people:
                    print(f"[{platform}] skipping person-profile query "
                          f"(--include-people to enable)")
                    continue
                q = tmpl["q"].format(niche=args.niche, city=args.city)
                print(f"[{platform}] {q}")
                stats["queries"] += 1
                status, results = search(q, MAX_PER_QUERY)
                health.note(status)
                if status != "ok":
                    print(f"   index returned {status.upper()} for this query")
                for r in results:
                    stats["results"] += 1
                    url = (r.get("href") or "").split("?")[0]
                    vid = TIKTOK_VIDEO.search(url) if platform == "tiktok" else None
                    if vid:
                        # The account behind the video is the prospect. Keep the
                        # canonical profile URL and remember where we saw it.
                        handle = vid.group(1)
                        found_url, url = url, f"https://www.tiktok.com/@{handle}"
                    else:
                        m = spec["profile_re"].search(url)
                        if not m or any(j in url for j in JUNK) or "/video/" in url:
                            stats["junk"] += 1
                            continue
                        handle, found_url = m.group(1), url

                    key = f"{platform}:{handle}"
                    if key in seen:
                        stats["dup"] += 1
                        continue

                    title, body = r.get("title", ""), r.get("body", "")

                    if "/in/" in url and not args.include_people:
                        stats["person"] += 1
                        continue

                    # Keyword args on purpose: is_relevant(trade, title, body,
                    # url) was once called positionally as (title, body, url,
                    # trade), which derived the vocabulary from the result's own
                    # title and matched it against itself — the gate passed
                    # everything for as long as it existed. Keywords make that
                    # class of bug impossible here.
                    if not args.no_trade_gate and not trade_vocab.is_relevant(
                            trade=args.niche, title=title, body=body, url=found_url):
                        stats["off_trade"] += 1
                        # Logged, not deleted. Most of these are profiles whose
                        # description the index could not read, so we have no
                        # evidence either way — unknown, not wrong.
                        if rej:
                            rej.write(json.dumps(
                                {"id": key, "url": found_url, "title": title,
                                 "niche": args.niche, "city": args.city,
                                 "rejected": "off_trade",
                                 "why": "nothing in the title, snippet or URL "
                                        f"mentions {args.niche}"},
                                ensure_ascii=False) + "\n")
                        continue

                    geo = geo_evidence(args.city, title, body, found_url)
                    if geo[0] == "elsewhere":
                        stats["out_of_region"] += 1
                        print(f"  - [{platform}] @{handle}: {geo[2]}")
                        if rej:
                            rej.write(json.dumps(
                                {"id": key, "url": found_url, "title": title,
                                 "niche": args.niche, "city": args.city,
                                 "rejected": "out_of_region", "why": geo[2]},
                                ensure_ascii=False) + "\n")
                        continue

                    seen.add(key)
                    cand = build(platform, handle, url, title, body,
                                 args.niche, args.city, geo)
                    cand["found_url"] = found_url
                    stats["kept"] += 1
                    print(f"  + [{platform}] {cand['name'][:42]}  @{handle}  "
                          f"[geo:{geo[0]} {geo[1]}]")
                    if out:
                        out.write(json.dumps(cand, ensure_ascii=False) + "\n")
                        out.flush()
                    if args.limit and stats["kept"] >= args.limit:
                        raise LimitReached
                time.sleep(SLEEP)
    except LimitReached:
        print("\nStopping early — --limit reached.")
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        aborted = "interrupted before the run finished"
    except IndexDown as e:
        aborted = str(e)
        print(f"\n!! INDEX DOWN — aborting run\n   {aborted}")
    finally:
        if out:
            out.close()
        if rej:
            rej.close()
        if not args.dry_run:
            SEEN_FILE.write_text("\n".join(sorted(seen)), encoding="utf-8")

    print("\n=== run summary ===")
    for k, v in stats.items():
        print(f"  {k:14}: {v}")
    answered = health.ok + health.empty + health.throttled + health.errors
    if answered:
        print(f"  {'yield':14}: {stats['results'] / answered:.2f} results/query "
              f"(ok={health.ok} empty={health.empty} throttled={health.throttled} "
              f"errors={health.errors}, longest dead streak={health.max_empty_streak})")

    # Every result must land in exactly one bucket. If it does not, a filter is
    # silently eating rows and the counts above are fiction.
    bucketed = sum(stats[k] for k in ("junk", "dup", "off_trade", "person",
                                      "out_of_region", "kept"))
    reconciled = bucketed == stats["results"]
    print(f"  {'reconciled':14}: " + ("OK" if reconciled else
          f"MISMATCH buckets={bucketed} results={stats['results']}"))

    # --- health verdict ------------------------------------------------------
    # Zero yield with non-zero attempts is a HARD FAILURE. This project adopted
    # that rule after tiktok_ingest exited 0 with 0 rows four times running and
    # no health check could see it.
    problems = []
    if aborted:
        problems.append(aborted)
    if not reconciled:
        problems.append(f"counters do not reconcile: buckets={bucketed} "
                        f"results={stats['results']}")
    if stats["queries"] and stats["results"] == 0:
        problems.append(
            f"{stats['queries']} queries returned zero results between them. The "
            f"index is not answering this host — that is a failure, not an empty "
            f"city.")
    elif (stats["queries"] >= YIELD_MIN_QUERIES
          and stats["results"] / stats["queries"] < MIN_YIELD):
        problems.append(
            f"yield {stats['results'] / stats['queries']:.2f} results/query over "
            f"{stats['queries']} queries is below the {MIN_YIELD} floor (a healthy "
            f"run measures ~3). The index is probably soft-blocking this host.")

    if stats["kept"] and not args.dry_run:
        print(f"\nAppended to {OUT_FILE}")
        print("Next:  python enrich.py  &&  python db.py --source social")
    if stats["out_of_region"] and not args.dry_run:
        print(f"{stats['out_of_region']} out-of-region results logged (not deleted) "
              f"to {REJECT_FILE.name}")

    if problems:
        print("\n=== HARD FAILURE ===")
        for p in problems:
            print(f"  ! {p}")
        sys.exit(2)


if __name__ == "__main__":
    main()
