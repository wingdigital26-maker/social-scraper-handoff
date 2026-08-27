#!/usr/bin/env python3
"""
source_junk — a demand-side lead source for haul-away / cleanout trades.

WHY THIS EXISTS
  The search-index watcher produced zero usable leads for a DFW junk-removal
  client. A full run on 2026-08-26 was 48 queries -> 161 results -> 9 "kept",
  and all 9 kept rows were Facebook group names and an out-of-state business
  page. Across a 128-query sweep of every client, 22 of 23 filed rows were
  Facebook garbage. The diagnosis is not a bad relevance gate. It is that the
  channels being searched do not contain the demand:

    * Facebook groups  — the Groups API shut down Apr 2024, and
                         https://www.facebook.com/robots.txt states outright
                         that automated collection is prohibited without
                         written permission. A `site:` search reaches public
                         PAGES (businesses) only, which is why every "kept" row
                         was a competitor or a group NAME, never a person.
    * Nextdoor         — robots.txt names Googlebot/Slurp/msnbot and friends
                         individually; there is no crawlable public post index
                         for us. Only the search index sees it, and the index
                         does not surface enough DFW haul-away posts to matter.
    * OfferUp          — robots.txt says `Disallow: /search` and
                         `Disallow: /services/search`. Item pages are allowed
                         but there is no permitted way to DISCOVER them.

  Meanwhile, two channels are wide open, robots-permitted, and full of real,
  dated, verifiable demand. Measured live on 2026-08-26 from this machine:

    * craigslist (JSON search API, robots.txt disallows only /reply /fb/
      /suggest /flag /mf /mailflag /eaf /sitemap/ — not /search and not the
      posting pages). DFW free section alone: 403 live postings. The gigs
      section carries people literally hiring a hauler, e.g.
        "Junk Removal Needed - $125 Melissa TX ... Junk removal needed from a
         garage. Everything needs to be hauled away."
        "HELP NEEDED - GARAGE CLEANOUT 10am ... help move items out of a garage
         and haul trash to a dumpster. Must have your own truck"
    * estatesales.net (server-rendered listing index, robots.txt disallows only
      /account /homepages /v2 /v3 /legacy /api/user-view-details). 35 live
      DFW-area sale URLs on the metro index page, each with a city, a START and
      an END date, and the company running it. An estate sale is a cleanout
      event with a deadline attached — the leftovers have to leave the house.

WHAT A LEAD IS HERE, AND WHAT IT IS NOT
  Three tiers, because they are worth wildly different amounts and lumping them
  together is how you get a "lead list" nobody calls:

    hire   Someone is ASKING to pay a person with a truck, right now. The
           highest-value row this file can produce. Craigslist gigs.
    event  A dated cleanout event — an estate sale with an end date. You know
           the address, the city, the company, and the day the leftovers become
           somebody's problem. Craigslist + estatesales.net.
    signal A giveaway of something BULKY (sofa, appliance, hot tub, piano) by
           someone who is moving or decluttering. Not a job. A marketing-list
           row. Deliberately capped so it cannot drown the other two.

  "Free moving boxes" is not a lead. Neither is a free can of paint. The bulky
  gate is the whole difference between those and "Free hot tub!!! Come get it
  asap", and it is enforced, not advisory.

SUPPLY-SIDE NOISE IS THE MAIN ENEMY
  The gigs section is flooded with companies recruiting drivers. A real one
  found live: "Curbside Junk Removal Jobs - $50+ a load, Instant Approval ...
  We run a curbside junk pickup platform ... we need drivers". That is a
  COMPETITOR, and the naive gate keeps it because it says "junk removal". Every
  candidate is run through a supply filter over BOTH title and body before it
  can be kept.

CLIENTS ARE DATA
  Nothing in this file names a client, a company, or a city. A market is passed
  in — on the command line, or as a JSON file, or as a dict to run(). The
  built-in markets table is a convenience list of public craigslist area ids
  and bounding boxes, not a client roster.

HONESTY / FAILURE ACCOUNTING
  This project shipped a scraper that exited 0 with 0 rows four times before
  anyone noticed. That cannot happen here. Every provider reports attempts,
  transport errors, raw results, and kept rows, and the process exit code
  distinguishes:

    0  OK        leads found
    2  ZERO      providers answered with real results, none survived the gates
                 (a genuine "there is nothing to sell today")
    3  BLOCKED   providers were reachable but returned nothing at all across
                 every query, or answered 403/429 — a soft block looks exactly
                 like an empty day unless you compare across the whole run
    4  ERROR     transport failed for most attempts

  Anything other than 0 is a hard failure with a non-zero exit. There is no
  configuration that makes an empty run succeed.

WHERE IT CAN RUN
  PC-bound for now, same as the rest of the watcher: these endpoints answer a
  residential IP normally and rate-limit datacenter/CI ranges. Not solved here.

USAGE
    python source_junk.py --market dfw
    python source_junk.py --market dfw --out junk.jsonl --max-signal 15
    python source_junk.py --market-file mymarket.json --tier hire,event
    python source_junk.py --market dfw --provider estatesales
    python source_junk.py --self-test          # gate unit tests, no network

  Output is JSONL shaped for ingest/db.py to_row(), so:
    python source_junk.py --market dfw --out junk.jsonl
    python db.py --in junk.jsonl --source junk

  Every row carries `url`, and every url is a page a human can open.
"""
from __future__ import annotations

import argparse
import html as _html
import json
import math
import pathlib
import re
import sys
import time
from datetime import datetime, timezone

import requests

try:  # business names and post bodies routinely break cp1252 consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = pathlib.Path(__file__).resolve().parent

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")}

# Exit codes. Documented above; referenced by name everywhere below.
EXIT_OK, EXIT_ZERO, EXIT_BLOCKED, EXIT_ERROR = 0, 2, 3, 4


# ---------------------------------------------------------------------------
# Markets. Public geography, not clients. A market is:
#   cl_area  craigslist numeric area id (discoverable: load
#            https://<host>.craigslist.org/search/zip and read "areaId":N)
#   cl_host  craigslist hostname, used only for logging
#   bbox     (lat_min, lat_max, lng_min, lng_max) — craigslist "nearby areas"
#            spill results from other metros into a keyword search, so every
#            posting is geo-checked before it is kept
#   es_path  estatesales.net metro index path
#   es_zips  3-digit ZIP prefixes that are really in the service area. The
#            estatesales.net "metro" index reaches far wider than a truck will
#            drive — the live DFW index on 2026-08-26 carried Waco (76710,
#            ~90mi), Ben Wheeler (75754, ~75mi) and Bonham (75418, ~65mi).
#            Without this the list quietly fills with sales nobody can service.
# ---------------------------------------------------------------------------
MARKETS = {
    "dfw": {
        "name": "Dallas / Fort Worth / Arlington, TX",
        "cl_area": 21,
        "cl_host": "dallas.craigslist.org",
        "bbox": (32.35, 33.30, -97.60, -96.35),
        "es_path": "/TX/Dallas-Fort-Worth-Arlington",
        "es_zips": ["750", "751", "752", "753", "760", "761", "762"],
    },
    "houston": {
        "name": "Houston, TX",
        "cl_area": 45,
        "cl_host": "houston.craigslist.org",
        "bbox": (29.35, 30.25, -95.95, -94.90),
        "es_path": "/TX/Houston",
        "es_zips": ["770", "771", "772", "773", "774", "775"],
    },
    "austin": {
        "name": "Austin, TX",
        "cl_area": 5,
        "cl_host": "austin.craigslist.org",
        "bbox": (30.05, 30.65, -98.10, -97.35),
        "es_path": "/TX/Austin",
        "es_zips": ["786", "787"],
    },
}


# ---------------------------------------------------------------------------
# Vocabulary. Every phrase below was taken from live DFW postings read on
# 2026-08-26, not invented. See the module docstring for verbatim quotes.
# ---------------------------------------------------------------------------

# Somebody wants to PAY a person with a truck. This is the money tier.
HIRE_RX = re.compile(
    r"(?:junk|trash|debris|furniture|appliance|brush|dirt|scrap)\s*(?:/\s*\w+\s*)?"
    r"(?:removal|haul|hauling|pick\s?up|pickup)\s*(?:needed|wanted|help)"
    r"|(?:removal|haul|hauling|cleanout|clean\s?out|clean\s?up|dump\s?run)\s*(?:needed|wanted)"
    r"|help\s*(?:needed|wanted)[^.]{0,40}(?:cleanout|clean\s?out|haul|junk|trash|garage|debris)"
    r"|need(?:ed|s)?\s*(?:someone|somebody|a\s*guy|help)[^.]{0,40}(?:haul|remove|clear|clean\s?out|dump)"
    r"|looking\s*for\s*(?:someone|somebody|a\s*hauler)[^.]{0,40}(?:haul|remove|clear|clean\s?out|dump)"
    r"|must\s*have\s*(?:your\s*own\s*)?(?:pickup\s*)?truck"
    r"|(?:haul|hauling)\s*(?:it\s*)?(?:away|off)\s*(?:needed|wanted)",
    re.I)

# A dated cleanout event.
EVENT_RX = re.compile(
    r"estate\s*sale|estate\s*cleanout|estate\s*clean\s?out|moving\s*sale|tag\s*sale"
    r"|downsizing\s*sale|liquidation|going\s*out\s*of\s*business|hoard",
    re.I)

# Softer decluttering language — only ever produces a `signal` row, and only
# when it is paired with a BULKY item.
SIGNAL_RX = re.compile(
    r"clean(?:ing|ed)?\s?out\b|cleanout|clean(?:ing)?\s*up\s*the\s*(?:garage|house|shed|yard)"
    r"|get(?:ting)?\s*rid\s*of|declutter\w*|purg\w+|downsiz\w+"
    r"|\bmoving\b|\bmoved\s*out\b|\bmove\s*out\b|relocat\w+"
    r"|at\s*the\s*curb|curbside|on\s*the\s*curb|by\s*the\s*curb"
    r"|(?:must|needs?\s*to)\s*(?:go|be\s*gone)|everything\s*must\s*go|take\s*it\s*all"
    # Urgency. "FREE HENRY MILLER UPRIGHT PIANO - MUST PICK UP TODAY" is a real
    # live posting; without these it reads as ordinary for-sale language.
    r"|must\s*(?:pick\s?up|be\s*picked\s*up|be\s*removed|be\s*out|haul)"
    r"|pick\s?(?:ed\s*)?up\s*(?:today|asap|by\s|this\s)|\basap\b|today\s*only|gone\s*by"
    r"|all\s*or\s*none|come\s*get\s*it|you\s*haul|haul\s*(?:it\s*)?away"
    r"|too\s*(?:big|heavy)\s*(?:for|to)|before\s*(?:i|we)\s*(?:toss|trash|dump)",
    re.I)

# The single most useful discriminator this file has. "Free moving boxes" and
# "Free red semi-gloss interior paint" are real live postings that are worth
# nothing to a hauler. "Free hot tub!!! Come get it asap" is worth a truck.
BULKY_RX = re.compile(
    r"\b(?:couch|sofa|sectional|loveseat|recliner|armchair|futon|mattress|box\s*spring"
    r"|bed\s*frame|headboard|dresser|armoire|wardrobe|hutch|credenza|buffet|china\s*cabinet"
    r"|bookcase|bookshelf|entertainment\s*center|desk|dining\s*(?:table|set)|table\s*and\s*chairs"
    r"|piano|organ|pool\s*table|hot\s*tub|jacuzzi|spa|treadmill|elliptical|weight\s*bench"
    r"|refrigerator|fridge|freezer|washer|dryer|dishwasher|stove|range|oven|water\s*heater"
    r"|furnace|ac\s*unit|air\s*conditioner|lift\s*chair|hospital\s*bed|swing\s*set|playset"
    r"|playground|trampoline|shed|playhouse|fence\s*panels|deck|hot\s*water\s*tank"
    r"|cubicle|cubicles|office\s*furniture|filing\s*cabinet|conference\s*table|safe"
    r"|carpet|flooring|tile|drywall|lumber|fill\s*dirt|concrete|brick|rubble|debris"
    r"|tree\s*(?:limbs|branches|trunk)|brush\s*pile|riding\s*mower|boat|trailer|camper"
    r"|\d{2,}\s*(?:gallon|cu\.?\s*ft)"
    r"|(?:whole|entire)\s*(?:house|garage|apartment|storage\s*unit|estate)"
    r"|(?:garage|house|attic|basement|shed|storage\s*unit|apartment|office)\s*full)\b",
    re.I)

# Supply side. Companies advertising, or recruiting drivers. Checked against
# title AND body. "Curbside Junk Removal Jobs - $50+ a load ... we need drivers"
# is a live competitor posting that only this filter stops.
SUPPLY_RX = re.compile(
    r"now\s*hiring|we[''`]?re\s*hiring|\bhiring\b|apply\s*(?:now|today|here|online)"
    r"|join\s*(?:our|the)\s*(?:team|crew|network)|instant\s*approval|sign\s*up\s*(?:now|today)"
    r"|we\s*(?:offer|provide|specialize|haul|remove|do|handle)\b|our\s*(?:team|crew|company|drivers)"
    r"|free\s*(?:estimate|quote|consultation)|licensed\s*(?:and|&)\s*insured|fully\s*insured"
    r"|book\s*(?:now|online|today)|call\s*(?:us|now|today)|text\s*us\s*(?:now|today)"
    # Pay-rate advertising. The separator is optional on purpose: a live gigs
    # posting reads "Make $365/ OR $20 HR/ Moving General labor" with no slash
    # before HR, and that is a labor ad, not a customer.
    r"|competitive\s*pay|weekly\s*pay|earn\s*(?:up\s*to\s*)?\$"
    r"|\$\d[\d,]*\s*(?:/|per\s*)?\s*(?:hr|hour|hourly|week)\b"
    r"|get\s*paid\s*(?:today|daily|same\s*day)|general\s*labor|day\s*labor"
    r"|serving\s*(?:the\s*)?(?:dfw|metroplex|greater|all\s*of)"
    r"|best\s*(?:price|rates)|lowest\s*(?:price|rates)|same\s*day\s*service"
    r"|no\s*experience\s*(?:necessary|required)|1099|gig\s*(?:work|economy)|be\s*your\s*own\s*boss",
    re.I)

TIERS = ("hire", "event", "signal")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _clean(s: str) -> str:
    s = _html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _in_bbox(lat, lng, bbox) -> bool:
    if lat is None or lng is None:
        return False
    lo_a, hi_a, lo_o, hi_o = bbox
    return lo_a <= lat <= hi_a and lo_o <= lng <= hi_o


def classify(title: str, body: str = "") -> tuple[str | None, list[str], str | None]:
    """Return (tier, matched_phrases, reject_reason).

    Order matters: supply is checked first, because a competitor ad that says
    "junk removal" outscores a real customer on every keyword test.
    """
    t = _clean(title)
    b = _clean(body)
    blob = f"{t} {b}"

    m = SUPPLY_RX.search(blob)
    if m:
        return None, [], f"supply_side:{m.group(0).strip().lower()[:40]}"

    hits: list[str] = []
    hm = HIRE_RX.search(blob)
    if hm:
        hits.append(hm.group(0).strip().lower()[:60])
        return "hire", hits, None

    em = EVENT_RX.search(blob)
    if em:
        hits.append(em.group(0).strip().lower()[:60])
        return "event", hits, None

    sm = SIGNAL_RX.search(blob)
    if not sm:
        return None, [], "no_demand_language"
    hits.append(sm.group(0).strip().lower()[:60])

    bm = BULKY_RX.search(blob)
    if not bm:
        # Decluttering language about something small. "Free moving boxes".
        return None, hits, "not_bulky"
    hits.append(bm.group(0).strip().lower()[:60])
    return "signal", hits, None


def _candidate(source, source_id, url, title, body, tier, hits, market,
               lat=None, lng=None, place=None, posted=None, extra=None):
    """Shape a row for ingest/db.py to_row()."""
    row = {
        "source": source,
        "source_id": str(source_id),
        "url": url,
        "title": title,
        "desc": body,
        "place": place,
        "lat": lat,
        "lng": lng,
        "location_confidence": "geo" if (lat is not None and lng is not None) else "listed_city",
        "category": tier,
        "intent": tier,
        "created_utc": posted,
        "embeds": [{"type": source, "url": url}],
        "market": market,
        "matched": hits,
    }
    if extra:
        row.update(extra)
    return row


class Health:
    """Per-provider accounting. A soft block and an empty day are the same
    shape from inside one request; only the totals across a run tell them
    apart, so the totals are what gets carried."""

    def __init__(self, provider: str):
        self.provider = provider
        self.attempts = 0        # requests issued
        self.transport_errors = 0
        self.http_blocked = 0    # 403 / 429 / explicit block bodies
        self.raw_results = 0     # items the provider actually handed back
        self.geo_dropped = 0
        self.rejected: dict[str, int] = {}
        self.kept = 0

    def reject(self, reason: str):
        key = (reason or "unknown").split(":")[0]
        self.rejected[key] = self.rejected.get(key, 0) + 1

    @property
    def status(self) -> str:
        if self.attempts == 0:
            return "not_run"
        if self.transport_errors >= max(1, self.attempts) * 0.6:
            return "error"
        if self.http_blocked:
            return "blocked"
        if self.raw_results == 0:
            # Reachable, answered 200, handed back nothing at all, on every
            # single query. Not credible as a real empty day.
            return "blocked"
        if self.kept == 0:
            return "zero_yield"
        return "ok"

    def as_dict(self):
        return {"provider": self.provider, "status": self.status,
                "attempts": self.attempts, "transport_errors": self.transport_errors,
                "http_blocked": self.http_blocked, "raw_results": self.raw_results,
                "geo_dropped": self.geo_dropped, "rejected": self.rejected,
                "kept": self.kept}


def _get(url, health: Health, params=None, timeout=30):
    health.attempts += 1
    try:
        r = requests.get(url, params=params, headers=UA, timeout=timeout)
    except Exception as e:
        health.transport_errors += 1
        return None, f"transport:{type(e).__name__}"

    # estatesales.net serves UTF-8 without declaring a charset, so requests
    # falls back to ISO-8859-1 per RFC 2616 and every apostrophe in a company
    # name arrives as mojibake: "Annie's Estate Sales" -> "Annieas". These
    # names go straight onto a list a human reads and calls, so a garbled
    # business name is a real defect, not a cosmetic one. Trust the declared
    # charset when there is one; otherwise let requests sniff the bytes.
    if r.encoding and r.encoding.lower() in ("iso-8859-1", "latin-1"):
        if "charset" not in r.headers.get("content-type", "").lower():
            r.encoding = r.apparent_encoding or "utf-8"

    if r.status_code in (403, 429, 503):
        health.http_blocked += 1
        return None, f"http:{r.status_code}"
    if r.status_code != 200:
        return None, f"http:{r.status_code}"
    if "<title>blocked</title>" in r.text[:400].lower():
        health.http_blocked += 1
        return None, "block_page"
    return r, None


# ---------------------------------------------------------------------------
# Provider: craigslist
# ---------------------------------------------------------------------------
CL_API = "https://sapi.craigslist.org/web/v8/postings/search/full"

# searchPath -> queries. `zip` is the free section and is browsed whole; the
# gigs section is keyword-searched because it is mostly unrelated labor.
CL_SEARCHES = [
    ("zip", None),
    ("ggg", "junk"),
    ("ggg", "haul"),
    ("ggg", "clean out"),
    ("ggg", "cleanout"),
    ("ggg", "trash"),
    ("ggg", "debris"),
    ("ggg", "garage"),
    ("ggg", "demo"),
    ("ggg", "dump"),
]


def _cl_parse(data: dict, places=None):
    """Decode craigslist's positional item arrays into dicts.

    Item layout, confirmed against live data on 2026-08-26:
      it[0]  posting id delta, real id = data.decode.minPostingId + delta
      it[4]  "<subareaIdx>:<placeIdx>~<lat>~<lng>", both indexing
             data.decode.locationDescriptions. Only the SECOND is per-posting
             (the poster's own free-text location: "mid cities", "Westcliff",
             "Prosper"). The first is a shared subarea label and is wrong at the
             posting level — it reads "Frisco" for postings in Argyle, Garland
             and Prosper alike, so it is ignored. lat/lng is the authority; the
             place string is only there for a human reading the row.
      [6, s] url slug
      [13,s] canonical short token -> https://www.craigslist.org/view/d/<slug>/<token>
      it[-1] title
    The /view/d/ form is used because it resolves for every posting; the
    subarea form (/dal/zip/d/...) 404s whenever the subarea index is off.
    """
    out = []
    for it in data.get("items", []):
        if not isinstance(it, list) or not it:
            continue
        tok = next((e[1] for e in it
                    if isinstance(e, list) and len(e) > 1 and e[0] == 13), None)
        slug = next((e[1] for e in it
                     if isinstance(e, list) and len(e) > 1 and e[0] == 6), "")
        title = it[-1] if isinstance(it[-1], str) else ""
        if not tok or not title:
            continue
        lat = lng = None
        place = None
        geo = it[4] if len(it) > 4 and isinstance(it[4], str) else ""
        if "~" in geo:
            parts = geo.split("~")
            try:
                lat, lng = float(parts[1]), float(parts[2])
            except (IndexError, ValueError):
                lat = lng = None
            _, _, pidx = parts[0].partition(":")
            if places and pidx.isdigit() and 0 < int(pidx) < len(places):
                cand = places[int(pidx)]
                place = cand if isinstance(cand, str) else None
        out.append({"id": tok, "slug": slug, "title": title, "lat": lat, "lng": lng,
                    "place": place,
                    "url": f"https://www.craigslist.org/view/d/{slug}/{tok}"})
    return out


def _cl_detail(url, health: Health):
    r, err = _get(url, health, timeout=25)
    if r is None:
        return None, None
    m = re.search(r'id="postingbody">(.*?)</section>', r.text, re.S)
    body = _clean(m.group(1)).replace("QR Code Link to This Post", "").strip() if m else ""
    dt = re.search(r'datetime="([^"]+)"', r.text)
    posted = None
    if dt:
        try:
            posted = int(datetime.strptime(dt.group(1), "%Y-%m-%dT%H:%M:%S%z").timestamp())
        except ValueError:
            pass
    return body, posted


def provider_craigslist(market: dict, max_detail: int = 90, delay: float = 0.35):
    h = Health("craigslist")
    leads, seen = [], set()
    bbox = market["bbox"]
    shortlist = []

    for path, query in CL_SEARCHES:
        params = {"batch": f"{market['cl_area']}-0-360-0-0", "cc": "US",
                  "lang": "en", "searchPath": path}
        if query:
            params["query"] = query
        r, err = _get(CL_API, h, params=params)
        if r is None:
            print(f"  [craigslist] {path} q={query!r} -> {err}", file=sys.stderr)
            continue
        try:
            data = r.json().get("data", {})
        except ValueError:
            h.transport_errors += 1
            continue
        items = _cl_parse(data, places=data.get("decode", {}).get("locationDescriptions"))
        h.raw_results += len(items)

        for it in items:
            if it["url"] in seen:
                continue
            # A keyword search spills postings from neighbouring metros in
            # under the same area id. Geo is the only reliable guard.
            if not _in_bbox(it["lat"], it["lng"], bbox):
                h.geo_dropped += 1
                continue
            seen.add(it["url"])
            tier, hits, why = classify(it["title"])
            if tier is None and why in ("supply_side", None):
                pass
            # Title alone is enough to REJECT loudly (supply side), but never
            # enough to keep a `signal` — bulky lives in the body. Anything
            # with demand language in the title earns a detail fetch.
            if why and why.startswith("supply_side"):
                h.reject(why)
                continue
            shortlist.append(it)

        time.sleep(delay)

    # Prefer hire-shaped titles when the detail budget is tight.
    shortlist.sort(key=lambda i: 0 if HIRE_RX.search(i["title"]) else
                   (1 if EVENT_RX.search(i["title"]) else 2))

    for it in shortlist[:max_detail]:
        body, posted = _cl_detail(it["url"], h)
        if body is None:
            continue
        tier, hits, why = classify(it["title"], body)
        if tier is None:
            h.reject(why)
            continue
        leads.append(_candidate(
            "craigslist", it["id"], it["url"], it["title"], body[:1200],
            tier, hits, market["name"], lat=it["lat"], lng=it["lng"],
            place=it.get("place"), posted=posted))
        h.kept += 1
        time.sleep(delay)

    return leads, h


# ---------------------------------------------------------------------------
# Provider: estatesales.net
# ---------------------------------------------------------------------------
ES_ROOT = "https://www.estatesales.net"
ES_LINK_RX = re.compile(r"/[A-Z]{2}/[A-Za-z\-]+/\d{5}/\d{5,9}")
# "Annie's Estate Sales - Arlington starts on 8/27/2026"
ES_TITLE_RX = re.compile(r"^(.*?)\s+starts on\s+(\d{1,2}/\d{1,2}/\d{4})\s*$", re.I)
# "The sale starts Thursday, August 27 and runs through Sunday, August 30.
#  It is being run by Annie's Estate Sales."
ES_RUNS_RX = re.compile(r"runs through\s+([A-Za-z]+,\s*[A-Za-z]+\s*\d{1,2})", re.I)
ES_BY_RX = re.compile(r"being run by\s+(.+?)\.\s*$", re.I)


def provider_estatesales(market: dict, max_detail: int = 40, delay: float = 0.4):
    h = Health("estatesales")
    leads = []
    path = market.get("es_path")
    if not path:
        return leads, h

    r, err = _get(ES_ROOT + path, h)
    if r is None:
        print(f"  [estatesales] index -> {err}", file=sys.stderr)
        return leads, h

    links = []
    for m in ES_LINK_RX.finditer(r.text):
        if m.group(0) not in links:
            links.append(m.group(0))
    h.raw_results += len(links)

    zips = market.get("es_zips") or []
    if zips:
        in_area = [l for l in links if l.strip("/").split("/")[2][:3] in zips]
        h.geo_dropped += len(links) - len(in_area)
        links = in_area

    for link in links[:max_detail]:
        url = ES_ROOT + link
        d, err = _get(url, h)
        if d is None:
            continue
        tm = re.search(r"<title>(.*?)</title>", d.text, re.S)
        title = _clean(tm.group(1)) if tm else ""
        # estatesales.net serves soft 404s: HTTP 200 with a "Page Not Found"
        # title. Same trap as the CivicPlus city sites. Read the title.
        if not title or "page not found" in title.lower():
            h.reject("soft_404")
            continue
        dm = re.search(r'name="description" content="(.*?)"', d.text, re.S)
        desc = _clean(dm.group(1)) if dm else ""

        starts = None
        tmm = ES_TITLE_RX.match(title)
        if tmm:
            starts = tmm.group(2)
        ends = None
        rm = ES_RUNS_RX.search(desc)
        if rm:
            ends = rm.group(1)
        company = None
        bm = ES_BY_RX.search(desc)
        if bm:
            company = bm.group(1).strip()

        parts = link.strip("/").split("/")
        state, city, zipc, sid = parts[0], parts[1].replace("-", " "), parts[2], parts[3]

        tier, hits, why = classify(title, desc)
        if tier is None:
            # An estate sale index entry is a cleanout event by construction;
            # only a supply-side match should ever knock one out.
            if why and why.startswith("supply_side"):
                h.reject(why)
                continue
            tier, hits = "event", ["estate sale listing"]

        body = desc
        if ends:
            body += f" | Sale ends {ends} — leftovers need to leave the property."
        leads.append(_candidate(
            "estatesales", sid, url, title, body[:1200], tier, hits, market["name"],
            place=f"{city}, {state} {zipc}",
            extra={"starts_on": starts, "ends_on": ends, "run_by": company,
                   "zip": zipc}))
        h.kept += 1
        time.sleep(delay)

    return leads, h


PROVIDERS = {"craigslist": provider_craigslist, "estatesales": provider_estatesales}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def run(market: dict, providers=None, tiers=None, max_signal=25,
        max_detail=90, delay=0.35):
    """Return (leads, [Health, ...]). Never sends anything. Never raises on a
    provider failure — the failure is reported in the Health record instead."""
    providers = providers or list(PROVIDERS)
    tiers = tiers or list(TIERS)
    all_leads, healths = [], []

    for name in providers:
        fn = PROVIDERS.get(name)
        if not fn:
            print(f"  unknown provider: {name}", file=sys.stderr)
            continue
        kwargs = {"max_detail": max_detail, "delay": delay}
        leads, h = fn(market, **kwargs)
        healths.append(h)
        all_leads.extend(leads)

    all_leads = [l for l in all_leads if l["category"] in tiers]
    # `signal` rows are cheap and numerous; capped so they cannot bury the
    # rows worth calling. Sorted so the best tier survives the cap.
    order = {"hire": 0, "event": 1, "signal": 2}
    all_leads.sort(key=lambda l: (order.get(l["category"], 9),
                                  -(l.get("created_utc") or 0)))
    kept, n_signal = [], 0
    for l in all_leads:
        if l["category"] == "signal":
            if n_signal >= max_signal:
                continue
            n_signal += 1
        kept.append(l)
    return kept, healths


def decide_exit(leads, healths) -> tuple[int, str]:
    if leads:
        return EXIT_OK, "ok"
    statuses = [h.status for h in healths if h.status != "not_run"]
    if not statuses:
        return EXIT_ERROR, "no provider ran"
    if all(s == "error" for s in statuses):
        return EXIT_ERROR, "every provider failed at the transport layer"
    if any(s == "blocked" for s in statuses):
        return EXIT_BLOCKED, ("a provider was reachable but returned nothing at all "
                              "across every query, or answered 403/429 — treat as a "
                              "soft block, not as an empty day")
    return EXIT_ZERO, ("providers returned real results but nothing survived the "
                       "demand gates — zero yield on non-zero attempts")


# ---------------------------------------------------------------------------
# Self test — gates only, no network. Every string below is copied verbatim
# from a live DFW posting read on 2026-08-26.
# ---------------------------------------------------------------------------
SELFTEST = [
    # (title, body, expected_tier)
    ("Junk Removal Needed – $125 Melissa TX",
     "Junk removal needed from a garage. Everything needs to be hauled away. "
     "Must have: Pickup truck or box truck.", "hire"),
    ("HELP NEEDED – GARAGE CLEANOUT 10am 08/09/2026",
     "Looking for someone to help move items out of a garage and haul trash to a dumpster.",
     "hire"),
    ("Junk/Trash Haul Needed - Forth Worth, TX",
     "We are looking for a reliable individual to assist with the removal of trash "
     "from a property.", "hire"),
    ("Free Estate Sale Leftovers",
     "Estate sale leftovers - child's car seat, picture frames, pots, large mirror, and more.",
     "event"),
    ("Free hot tub!!! Come get it asap", "", "signal"),
    ("Electric lift chair",
     "A working lift chair. All electrical parts work. At the curb.", "signal"),
    ("FREE HENRY MILLER UPRIGHT PIANO - MUST PICK UP TODAY", "", "signal"),
    # --- must be rejected ---
    ("Curbside Junk Removal Jobs - $50+ a load, Instant Approval",
     "Got a truck? We run a curbside junk pickup platform across multiple cities. "
     "we need drivers to grab overflow pickups.", None),
    ("🏌️ NOW HIRING | Caddy Moving – Join the Best Crew in the Game", "", None),
    ("Real Estate - Finish Out Contractor Residential Fix and Flips",
     "We are a real estate investment and construction company seeking an experienced "
     "Finish-Out contractor.", None),
    ("Free moving boxes", "Some used, some unused. In front yard at the tree.", None),
    ("Free red semi-gloss interior paint",
     "Not sure why we have this red paint left over. Free to anyone who can use it.", None),
    ("Free Moving Boxes", "giving away 3 large flat boxes for TV's or artwork.", None),
]


def self_test() -> int:
    print("regexes (repr, so escape sequences are visible):")
    for nm, rx in (("HIRE_RX", HIRE_RX), ("EVENT_RX", EVENT_RX), ("SIGNAL_RX", SIGNAL_RX),
                   ("BULKY_RX", BULKY_RX), ("SUPPLY_RX", SUPPLY_RX)):
        print(f"  {nm} = {rx.pattern!r}\n")
    fails = 0
    for title, body, want in SELFTEST:
        got, hits, why = classify(title, body)
        ok = got == want
        fails += 0 if ok else 1
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] want={str(want):6} got={str(got):6} "
              f"{'(' + (why or '') + ')' if got is None else str(hits)}  | {title[:64]}")
    print(f"\n{len(SELFTEST) - fails}/{len(SELFTEST)} gate cases pass")
    return 1 if fails else 0


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", choices=sorted(MARKETS), help="built-in market key")
    ap.add_argument("--market-file", help="JSON file with name/cl_area/cl_host/bbox/es_path")
    ap.add_argument("--provider", default=",".join(PROVIDERS),
                    help="comma list: " + ",".join(PROVIDERS))
    ap.add_argument("--tier", default=",".join(TIERS), help="comma list: " + ",".join(TIERS))
    ap.add_argument("--max-signal", type=int, default=25)
    ap.add_argument("--max-detail", type=int, default=90,
                    help="detail-page fetch budget per provider")
    ap.add_argument("--delay", type=float, default=0.35)
    ap.add_argument("--out", help="write JSONL here (default: stdout summary only)")
    ap.add_argument("--json", action="store_true", help="dump leads as JSON to stdout")
    ap.add_argument("--self-test", action="store_true", help="run gate tests, no network")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()

    if a.market_file:
        market = json.loads(pathlib.Path(a.market_file).read_text(encoding="utf-8"))
        market["bbox"] = tuple(market["bbox"])
    elif a.market:
        market = MARKETS[a.market]
    else:
        ap.error("one of --market or --market-file is required")

    t0 = time.time()
    print(f"source_junk: market={market['name']} providers={a.provider}", file=sys.stderr)
    leads, healths = run(market,
                         providers=[p.strip() for p in a.provider.split(",") if p.strip()],
                         tiers=[t.strip() for t in a.tier.split(",") if t.strip()],
                         max_signal=a.max_signal, max_detail=a.max_detail, delay=a.delay)

    print("\n===== HEALTH =====", file=sys.stderr)
    for h in healths:
        print("  " + json.dumps(h.as_dict()), file=sys.stderr)

    by_tier = {}
    for l in leads:
        by_tier[l["category"]] = by_tier.get(l["category"], 0) + 1
    print(f"\n{len(leads)} leads in {time.time() - t0:.0f}s  {by_tier}", file=sys.stderr)
    for l in leads:
        print(f"  [{l['category']:6}] {(l['title'] or '')[:72]}\n           {l['url']}",
              file=sys.stderr)

    if a.out:
        p = pathlib.Path(a.out)
        p.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in leads) + "\n",
                     encoding="utf-8")
        print(f"\nwrote {len(leads)} -> {p}", file=sys.stderr)
    if a.json:
        print(json.dumps(leads, ensure_ascii=False, indent=1))

    code, why = decide_exit(leads, healths)
    if code != EXIT_OK:
        print(f"\nHARD FAILURE (exit {code}): {why}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
