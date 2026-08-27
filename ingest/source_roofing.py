#!/usr/bin/env python3
"""
source_roofing — a demand-side lead source for residential roofing.

READ THIS BEFORE YOU TRUST A ROW
  This file produces two fundamentally different kinds of row and it will not
  let you confuse them, because confusing them is the whole failure mode of
  roofing lead-gen:

    ask    A PERSON who has said, in public, that they need roof work. A name
           attached to a request. This is a lead in the ordinary sense.
    storm  A NEIGHBOURHOOD that took damaging hail on a known date. This is
           NOT a person and nobody in it has asked for anything. It is a
           targeting area — a ZIP code plus a date plus a hailstone diameter.
           It tells a canvasser which streets to knock and a marketer which
           ZIP to buy. Presenting one of these as "a lead who needs a roof"
           is a lie, and `is_person` is False on every one of them so that no
           downstream consumer can make that mistake by accident.

  The tier is also carried in `category`/`intent` so db.py to_row() keeps it.

WHAT THE RESEARCH ACTUALLY FOUND (measured live, 2026-08-26/27, this machine)

  DEAD — homeowners asking in public
    craigslist DFW.  Eight demand-shaped queries across the gigs and labor
      sections (roof, shingle, fascia, soffit, leak, water damage, patch) ->
      69 unique live postings -> ZERO homeowner asks. Every posting that
      mentioned a roof was supply side. The four that matched homeowner-ish
      language were the SAME property-preservation recruiting template:
        "Fascia Replacement" /view/d/allen-fascia-replacement/c5Ki3Wy8iHB7y7ygzP9ByF
        "***Please respond with code TX75002*** We are currently seeking
         reliable property maintenance, handyman, property preservation ...
         crews to service foreclosed properties in ALLEN, TX 75002 ...
         Additional Work Available: ... Small roof repair"
      The `hss` (services offered) section is 87 results of pure competitor
      advertising: "Roofing Services, Roof Replacements, Insurance Claims,
      Roof Inspection", "!!!!DISCOUNTED ROOFING)(K.O) THE COMPETITION".
      The craigslist provider below is still shipped, still real, and still
      gated — but on DFW evidence it is expected to return zero, and a zero
      is reported as a hard failure rather than dressed up.
      WHY it differs from junk removal: a cleanout is a cash gig you hire a
      guy with a truck for, so it gets posted. A roof is a $15k licensed job
      usually paid by an insurer. Homeowners phone a contractor; they do not
      post a classified ad.

    reddit.  https://www.reddit.com/robots.txt is `User-agent: * Disallow: /`
      as of 2026-08-27, AND every unauthenticated request to
      /r/<sub>/search.json answered HTTP 403 (tested r/Dallas, r/Plano,
      r/FortWorth, r/askdfw). Blocked twice over. Not built on.

    facebook / nextdoor / offerup.  Established previously and not
      re-litigated here; see source_junk.py's docstring for the measurements.

    municipal roofing permits.  Dallas OpenData e7gq-4sah is a real, open,
      keyless Socrata dataset with roof permits, addresses and contractors —
      and it is FROZEN. A `max(issued_date)` query on 2026-08-27 returned
      12/31/19 over 126,840 rows. It is a 2019 archive, not a feed.
      Plano / Frisco / McKinney / Allen / Richardson permit independently and
      publish nothing machine-readable: Frisco offers monthly PDFs only and
      states outright that Development Services "does not generate customized
      reports". opendata.plano.gov, data.mckinneytexas.org and
      data.richardsontexas.gov do not resolve. The ArcGIS hubs that do exist
      (plano-opendata.opendata.arcgis.com, data-cor.opendata.arcgis.com)
      answer 401 on their dataset search API. There is no permit provider in
      this file because there is no permit feed to build one on.

    NOAA SPC and api.weather.gov.  Both serve `User-agent: * Disallow: /`
      (measured 2026-08-27). The SPC daily hail CSVs are excellent data and
      are NOT used here for that reason, even though they answered 200.

  LIVE — hail, which is what actually drives Texas roofing demand
    NCEI SWDI  https://www.ncei.noaa.gov/swdiws/  Keyless. robots.txt
      disallows only /data* and /orders*, so /swdiws/ is permitted. The
      `nx3hail` product is the NEXRAD hail-signature detection: one row per
      radar-detected hail cell, with coordinates, a UTC timestamp and
      MAXSIZE in inches. Dense enough to draw a swath, which point reports
      never are. Live example, Jackson's own bounding box, May 2026:
      41 cells, 23 of them >= 1.00 inch, topping out at 1.50.
    IEM LSR   https://mesonet.agron.iastate.edu/geojson/lsr.geojson  Keyless.
      robots.txt sets Crawl-delay 120 and disallows only /usage /tmp
      /data/NIDS /data/nexrd2 /data/model /archive/nexrad /archive/raw/snet.
      Human-confirmed local storm reports, with a place name and a source.
      Live example, WFO FWD, 2026-08-26:
        H 1.5  2 SE Haslet, Tarrant   2026-08-26T22:59Z
        H 1.0  3 NW Keller, Tarrant   2026-08-26T22:55Z
      (Both Tarrant County — west of Jackson's cities. The geo gate below
      drops them for a Plano/Frisco market, which is the gate working.)

WHY A SIZE GATE IS THE SUPPLY-SIDE GATE'S TWIN
  Pea and dime hail does not damage an asphalt shingle. A run that keeps
  0.50" cells produces a "storm lead" for every summer thunderstorm in Texas
  and is worth exactly nothing. MIN_HAIL_IN is enforced, not advisory:
    >= 1.75 in  storm_severe  golf ball and up; replacement is likely
    >= 1.00 in  storm         quarter and up; the industry damage threshold
    <  1.00 in  rejected      too_small
  The threshold is a parameter, but its default is not zero and there is no
  flag that turns the gate off.

SUPPLY SIDE
  Only the `ask` provider needs it, and it needs it badly: roofing classifieds
  are ~99% roofers. Every candidate is run through SUPPLY_RX over title AND
  body before it can be kept, same contract as source_junk.classify().

CLIENTS ARE DATA
  No client name and no city list appears in any logic below. MARKETS is a
  table of public geography — Census place centroids and Census ZCTA
  centroids, both downloaded from www2.census.gov, not typed from memory.
  Pass --market-file to use your own.

HONESTY / FAILURE ACCOUNTING
  Identical contract to source_junk.py:
    0  OK       rows found
    2  ZERO     providers returned real results, none survived the gates
    3  BLOCKED  reachable but returned nothing at all across every query, or
                403/429 — a soft block and an empty day are the same shape
                inside one request, so only run totals can tell them apart
    4  ERROR    transport failed for most attempts
  There is no configuration that makes an empty run exit 0.

WHERE IT CAN RUN
  PC-bound, same as the rest of the watcher. NCEI and IEM answer a
  residential IP fine and rate-limit CI ranges; IEM asks for Crawl-delay 120
  and this file does not honour anything like that on a per-request basis, so
  keep the query count low and do not put it on a tight cron in CI.

USAGE
    python source_roofing.py --market dfw_north
    python source_roofing.py --market dfw_north --days 120 --out roof.jsonl
    python source_roofing.py --market dfw_north --provider swdi --min-hail 1.75
    python source_roofing.py --market-file mymarket.json --tier ask
    python source_roofing.py --self-test        # gates only, no network

  Output is JSONL shaped for ingest/db.py to_row():
    python source_roofing.py --market dfw_north --out roof.jsonl
    python db.py --in roof.jsonl --source roofing

  Every row carries `url`, and every url is a page a human can open and check.
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
from datetime import datetime, timedelta, timezone

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")}

EXIT_OK, EXIT_ZERO, EXIT_BLOCKED, EXIT_ERROR = 0, 2, 3, 4

# Hail diameters in inches. Asphalt-shingle damage begins around a quarter.
HAIL_DAMAGING = 1.00
HAIL_SEVERE = 1.75

# How close a hail cell has to be to a ZIP centroid before that ZIP counts as
# hit. A ZCTA centroid is a point standing in for a polygon, so this is a
# deliberate approximation and it is documented on every row it produces
# (`geo_method`). 3 miles is about the radius of a typical suburban DFW ZCTA.
ZIP_HIT_MI = 3.0

TIERS = ("ask", "storm_severe", "storm")


# ---------------------------------------------------------------------------
# Markets. Public geography only.
#
#   cl_area   craigslist numeric area id (the `ask` provider)
#   center    (lat, lng) + radius_mi, used for the geo gate
#   lsr_wfos  NWS forecast-office ids whose LSRs cover this market
#   zips      [{zip, lat, lng, near_city}, ...]
#
# PROVENANCE, so nobody has to trust that these were not invented:
#   center per city  = 2020 Census place gazetteer, 2020_gaz_place_48.txt
#                      (Plano city 33.050769,-96.747944; Frisco city
#                       33.155427,-96.822596; McKinney city 33.201125,
#                       -96.664161; Allen city 33.109736,-96.673032;
#                       Richardson city 32.972291,-96.708069)
#   zips             = 2020 Census ZCTA gazetteer, 2020_Gaz_zcta_national.txt,
#                      every ZCTA whose centroid is within 6.5 mi of one of
#                      those five place centroids.
#   `near_city` is the NEAREST configured service city, not the ZIP's postal
#   city. 75240 and 75254 are Dallas addresses that happen to sit closest to
#   Richardson; they are in the truck's range, which is the only thing the
#   field is for. It is named `near_city` and not `city` on purpose.
# ---------------------------------------------------------------------------
MARKETS = {
    "dfw_north": {
        "name": "North DFW (Plano / Frisco / McKinney / Allen / Richardson), TX",
        "cl_area": 21,
        "cl_host": "dallas.craigslist.org",
        "center": (33.098, -96.723),   # centroid of the five place centroids
        "radius_mi": 18.0,
        "lsr_wfos": ["FWD"],
        "zips": [
            {"zip": "75002", "near_city": "Allen", "lat": 33.08985, "lng": -96.60860},
            {"zip": "75013", "near_city": "Allen", "lat": 33.11433, "lng": -96.69396},
            {"zip": "75023", "near_city": "Plano", "lat": 33.05679, "lng": -96.73086},
            {"zip": "75024", "near_city": "Plano", "lat": 33.07542, "lng": -96.80269},
            {"zip": "75025", "near_city": "Plano", "lat": 33.09009, "lng": -96.74001},
            {"zip": "75034", "near_city": "Frisco", "lat": 33.14943, "lng": -96.86097},
            {"zip": "75035", "near_city": "Frisco", "lat": 33.15739, "lng": -96.77867},
            {"zip": "75040", "near_city": "Richardson", "lat": 32.92890, "lng": -96.61978},
            {"zip": "75042", "near_city": "Richardson", "lat": 32.91170, "lng": -96.67484},
            {"zip": "75044", "near_city": "Richardson", "lat": 32.96461, "lng": -96.64969},
            {"zip": "75069", "near_city": "McKinney", "lat": 33.17824, "lng": -96.59022},
            {"zip": "75070", "near_city": "McKinney", "lat": 33.17170, "lng": -96.69509},
            {"zip": "75071", "near_city": "McKinney", "lat": 33.24582, "lng": -96.63072},
            {"zip": "75074", "near_city": "Plano", "lat": 33.03156, "lng": -96.67316},
            {"zip": "75075", "near_city": "Plano", "lat": 33.02127, "lng": -96.74156},
            {"zip": "75078", "near_city": "Frisco", "lat": 33.24133, "lng": -96.81239},
            {"zip": "75080", "near_city": "Richardson", "lat": 32.97606, "lng": -96.74208},
            {"zip": "75081", "near_city": "Richardson", "lat": 32.94892, "lng": -96.70972},
            {"zip": "75082", "near_city": "Richardson", "lat": 32.99157, "lng": -96.66295},
            {"zip": "75093", "near_city": "Plano", "lat": 33.03422, "lng": -96.81161},
            {"zip": "75094", "near_city": "Richardson", "lat": 33.02179, "lng": -96.61548},
            {"zip": "75238", "near_city": "Richardson", "lat": 32.87850, "lng": -96.70782},
            {"zip": "75240", "near_city": "Richardson", "lat": 32.93034, "lng": -96.78750},
            {"zip": "75243", "near_city": "Richardson", "lat": 32.91263, "lng": -96.73664},
            {"zip": "75248", "near_city": "Richardson", "lat": 32.96970, "lng": -96.79733},
            {"zip": "75251", "near_city": "Richardson", "lat": 32.91903, "lng": -96.77218},
            {"zip": "75252", "near_city": "Plano", "lat": 32.99737, "lng": -96.78821},
            {"zip": "75254", "near_city": "Richardson", "lat": 32.94412, "lng": -96.80009},
            {"zip": "75287", "near_city": "Plano", "lat": 32.99931, "lng": -96.84169},
        ],
    },
}


# ---------------------------------------------------------------------------
# Vocabulary for the `ask` tier.
#
# Real demand language, not trade language. Nobody types "roof replacement
# services"; they type "my roof is leaking". trade_vocab.py encodes the same
# idea and this is the roofing instance of it.
#
# NOTE ON EVIDENCE: unlike source_junk.py, these phrases could NOT all be
# copied from live DFW postings, because the live DFW postings do not exist —
# see the docstring. They are written from the shape of the ask (first-person
# possessive + a roof noun + a damage/help verb) and they are deliberately
# narrow: a phrase that cannot be evidenced is a phrase that must not be
# allowed to match loosely. Nothing below is presented as a quote.
# ---------------------------------------------------------------------------

# First person + a roof/ceiling noun. The possessive is load-bearing: it is
# what separates a homeowner from a contractor talking about roofs in general.
ASK_RX = re.compile(
    r"(?:my|our)\s+(?:roof|shingles|ceiling|attic|gutters?|soffit|fascia|chimney)"
    r"|(?:roof|ceiling)\s+(?:is\s+)?(?:leak\w*|leaks)"
    r"|leak\w*\s+(?:in|from|through)\s+(?:my|our|the)\s+(?:roof|ceiling|attic)"
    r"|water\s+(?:stain|spot|damage|dripping|coming\s+in)\w*\s*(?:on|in|from|through)?"
    r"\s*(?:my|our|the)?\s*(?:ceiling|roof|attic|wall)"
    r"|shingles?\s+(?:came|blew|blown|flew|fell|missing|off)\b"
    r"|missing\s+shingles?|lost\s+shingles?"
    r"|hail\s+damage[^.]{0,40}(?:roof|house|home|claim)"
    r"|(?:roof|shingle)[^.]{0,30}hail\s+damage"
    r"|need\s+(?:someone|somebody|a\s+roofer|help)[^.]{0,40}"
    r"(?:roof|shingle|leak|gutter|soffit|fascia)"
    r"|looking\s+for\s+(?:a\s+)?(?:roofer|roofing\s+contractor)"
    r"|(?:roof|shingle|leak)[^.]{0,30}(?:repair|patch|fix|inspect\w*)\s+(?:needed|wanted)"
    r"|(?:roof|shingle)\s+(?:repair|patch|replacement)\s+needed"
    r"|(?:should|do)\s+i\s+file\s+(?:an?\s+)?(?:insurance\s+)?claim"
    r"|tree\s+(?:fell|limb)[^.]{0,30}(?:roof|house)",
    re.I)

# Supply side. Built from live DFW craigslist text read 2026-08-26/27 — the
# quotes are in the module docstring. Checked over title AND body.
SUPPLY_RX = re.compile(
    r"now\s*hiring|we[''`]?re\s*hiring|\bhiring\b|contractors?\s*(?:wanted|needed)"
    r"|subcontractors?\s*(?:wanted|needed)|crews?\s*(?:wanted|needed)"
    r"|(?:seeking|looking\s*for)\s*(?:reliable|experienced|qualified)?\s*"
    r"(?:crews?|contractors?|subs?|roofers?|installers?|laborers?|helpers?)"
    r"|busco\s+\w+|se\s+solicitan?\b"
    r"|apply\s*(?:now|today|here|online)|join\s*(?:our|the)\s*(?:team|crew|network)"
    r"|instant\s*approval|sign\s*up\s*(?:now|today)|steady\s*work|consistent\s*work"
    r"|same[-\s]?day\s*payment|property\s*preservation|foreclosed\s*propert"
    r"|please\s*respond\s*with\s*code"
    r"|we\s*(?:offer|provide|specialize|install|repair|do|handle|serve)\b"
    r"|our\s*(?:team|crew|company|technicians?|installers?)"
    r"|free\s*(?:estimate|quote|inspection|consultation)"
    r"|licensed\s*(?:and|&)\s*insured|fully\s*insured|bonded"
    r"|book\s*(?:now|online|today)|call\s*(?:us|now|today)|text\s*us\s*(?:now|today)"
    r"|competitive\s*pay|weekly\s*pay|earn\s*(?:up\s*to\s*)?\$"
    r"|\$\d[\d,]*\s*(?:/|per\s*)?\s*(?:hr|hour|hourly|week|sf|square)\b"
    r"|get\s*paid\s*(?:today|daily|same\s*day)|general\s*labor|day\s*labor"
    r"|serving\s*(?:the\s*)?(?:dfw|metroplex|greater|all\s*of)"
    r"|best\s*(?:price|rates)|lowest\s*(?:price|rates)|discount\w*\s*roofing"
    r"|insurance\s*claims?\s*(?:specialist|assistance|help|welcome)"
    r"|roof\s*(?:replacements?|inspections?),"
    r"|no\s*experience\s*(?:necessary|required)|1099|be\s*your\s*own\s*boss",
    re.I)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _clean(s: str) -> str:
    s = _html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def haversine_mi(lat1, lng1, lat2, lng2) -> float:
    """Great-circle miles. Pure arithmetic, no dependencies."""
    if None in (lat1, lng1, lat2, lng2):
        return float("inf")
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def market_bbox(market: dict):
    """(lat_min, lat_max, lng_min, lng_max), from an explicit bbox or from
    center+radius. Used to ask the upstream services for a rectangle; the
    exact radius test still runs afterwards, because a rectangle drawn around
    a circle is ~27% too big and the corners are where the neighbouring-metro
    bleed-in lives."""
    bb = market.get("bbox")
    if bb:
        return tuple(bb)
    center = market.get("center")
    if not center:
        return None
    lat, lng = center[0], center[1]
    rad = float(market.get("radius_mi") or 25.0)
    dlat = rad / 69.0
    dlng = rad / (69.0 * max(0.15, math.cos(math.radians(lat))))
    return (lat - dlat, lat + dlat, lng - dlng, lng + dlng)


def in_market(market: dict, lat, lng) -> bool:
    bb = market_bbox(market)
    if bb is None:
        # No geometry supplied. Refuse to guess: a market with no geography
        # keeps nothing, rather than keeping the whole country.
        return False
    if lat is None or lng is None:
        return False
    lo_a, hi_a, lo_o, hi_o = bb
    if not (lo_a <= lat <= hi_a and lo_o <= lng <= hi_o):
        return False
    center = market.get("center")
    if not center:
        return True
    return haversine_mi(center[0], center[1], lat, lng) <= float(
        market.get("radius_mi") or 25.0)


def grade_hail(size_in: float, min_in: float = HAIL_DAMAGING):
    """(tier, reason). The size gate. Sub-threshold hail does not damage an
    asphalt shingle and must not become a 'lead'."""
    if size_in is None:
        return None, "no_size"
    if size_in < min_in:
        return None, f"too_small:{size_in}"
    if size_in >= HAIL_SEVERE:
        return "storm_severe", None
    return "storm", None


def classify_ask(title: str, body: str = ""):
    """(tier, matched_phrases, reject_reason) for the `ask` tier.

    Supply is checked FIRST, deliberately. A roofing company's ad outscores a
    real homeowner on every keyword test ever written, so the only safe order
    is to throw the competitors out before scoring anything.
    """
    blob = f"{_clean(title)} {_clean(body)}"
    m = SUPPLY_RX.search(blob)
    if m:
        return None, [], f"supply_side:{m.group(0).strip().lower()[:40]}"
    am = ASK_RX.search(blob)
    if not am:
        return None, [], "no_demand_language"
    return "ask", [am.group(0).strip().lower()[:60]], None


def _row(source, source_id, url, title, body, tier, hits, market,
         lat=None, lng=None, place=None, posted=None, is_person=False, extra=None):
    """Shape a row for ingest/db.py to_row().

    `is_person` is the field that stops a targeting area from being mistaken
    for a human being. It is False for everything the storm providers emit.
    """
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
        "is_person": bool(is_person),
        "lead_kind": "person_asked" if is_person else "targeting_area",
        "created_utc": posted,
        "embeds": [{"type": source, "url": url}],
        "market": market,
        "matched": hits,
    }
    if extra:
        row.update(extra)
    return row


class Health:
    """Per-provider accounting. A soft block and an empty day look identical
    from inside a single request; only run totals separate them."""

    def __init__(self, provider: str):
        self.provider = provider
        self.attempts = 0
        self.transport_errors = 0
        self.http_blocked = 0
        self.raw_results = 0
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


POLICY = None


class Blocked(Exception):
    """Raised by a POLICY that has decided a host must not be touched again."""


def set_policy(policy):
    global POLICY
    POLICY = policy


def _get(url, health: Health, params=None, timeout=60):
    health.attempts += 1
    if POLICY is not None:
        POLICY.before(url)
    try:
        r = requests.get(url, params=params, headers=UA, timeout=timeout)
    except Exception as e:
        health.transport_errors += 1
        if POLICY is not None:
            POLICY.after(url, None)
        return None, f"transport:{type(e).__name__}"
    if POLICY is not None:
        POLICY.after(url, r.status_code)
    if r.status_code in (403, 429, 503):
        health.http_blocked += 1
        return None, f"http:{r.status_code}"
    if r.status_code != 200:
        return None, f"http:{r.status_code}"
    return r, None


def _zip_hits(market: dict, lat, lng, radius_mi=ZIP_HIT_MI):
    """Which configured ZIPs a point falls on top of. Centroid distance, which
    is an approximation of a polygon test and is labelled as one on the row."""
    out = []
    for z in market.get("zips") or []:
        d = haversine_mi(lat, lng, z["lat"], z["lng"])
        if d <= radius_mi:
            out.append((round(d, 2), z))
    out.sort()
    return out


# ---------------------------------------------------------------------------
# Provider: NCEI SWDI nx3hail — radar-detected hail cells.
#
# robots.txt on www.ncei.noaa.gov disallows /data* and /orders* only, so
# /swdiws/ is permitted. Keyless. Response shape confirmed live 2026-08-27:
#   {"swdiJsonResponse":{...},"result":[{"PROB":"100","SHAPE":"POINT (lng lat)",
#    "WSR_ID":"KFWS","CELL_ID":"L9","ZTIME":"2026-08-26T22:22:42Z",
#    "SEVPROB":"80","MAXSIZE":"2"}, ...]}
# An empty answer is a *valid* summary document with count 0, not an error —
# which is exactly why raw_results is counted separately from kept.
# ---------------------------------------------------------------------------
SWDI_ROOT = "https://www.ncei.noaa.gov/swdiws"
SWDI_POINT_RX = re.compile(r"POINT\s*\(\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s*\)")


def _swdi_human_url(rng: str, bbox: str) -> str:
    """A URL a human can paste into a browser and read. The csv form of the
    same query renders as plain text, so this is genuinely checkable."""
    return f"{SWDI_ROOT}/csv/nx3hail/{rng}?bbox={bbox}"


def provider_swdi(market: dict, days: int = 90, min_hail: float = HAIL_DAMAGING,
                  window_days: int = 31, delay: float = 1.0, **_):
    h = Health("swdi")
    leads = []
    bb = market_bbox(market)
    if bb is None:
        return leads, h
    lo_a, hi_a, lo_o, hi_o = bb
    bbox = f"{lo_o:.4f},{lo_a:.4f},{hi_o:.4f},{hi_a:.4f}"

    end = datetime.now(timezone.utc).date() + timedelta(days=1)
    start = end - timedelta(days=days)
    # SWDI wants bounded ranges; long spans are chunked rather than asked for
    # in one go, which also means one bad month cannot lose the whole window.
    cursor = start
    windows = []
    while cursor < end:
        stop = min(end, cursor + timedelta(days=window_days))
        windows.append((cursor, stop))
        cursor = stop

    # zip -> best cell seen. A single storm produces dozens of cells over one
    # neighbourhood; a ZIP is one targeting area, not thirty.
    best: dict[str, dict] = {}

    for w0, w1 in windows:
        rng = f"{w0:%Y%m%d}:{w1:%Y%m%d}"
        r, err = _get(f"{SWDI_ROOT}/json/nx3hail/{rng}", h, params={"bbox": bbox})
        if r is None:
            print(f"  [swdi] {rng} -> {err}", file=sys.stderr)
            continue
        try:
            payload = json.loads(r.text)
        except ValueError:
            h.transport_errors += 1
            continue
        results = payload.get("result") or []
        h.raw_results += len(results)

        for cell in results:
            pm = SWDI_POINT_RX.search(cell.get("SHAPE") or "")
            if not pm:
                h.reject("no_shape")
                continue
            lng, lat = float(pm.group(1)), float(pm.group(2))
            if not in_market(market, lat, lng):
                h.geo_dropped += 1
                continue
            try:
                size = float(cell.get("MAXSIZE"))
            except (TypeError, ValueError):
                size = None
            tier, why = grade_hail(size, min_hail)
            if tier is None:
                h.reject(why)
                continue
            hits = _zip_hits(market, lat, lng)
            if not hits:
                # Inside the service radius but not on top of any configured
                # ZIP centroid. Real hail, but it does not name a place a
                # canvasser can be sent to, so it is not a targeting area.
                h.geo_dropped += 1
                continue
            when = cell.get("ZTIME") or ""
            for dist, z in hits:
                prev = best.get(z["zip"])
                if prev is None or size > prev["size"]:
                    best[z["zip"]] = {"size": size, "tier": tier, "when": when,
                                      "lat": lat, "lng": lng, "zip": z,
                                      "dist": dist, "rng": rng,
                                      "radar": cell.get("WSR_ID"),
                                      "sevprob": cell.get("SEVPROB")}
        time.sleep(delay)

    for zc, b in best.items():
        z = b["zip"]
        try:
            ts = int(datetime.strptime(b["when"], "%Y-%m-%dT%H:%M:%SZ")
                     .replace(tzinfo=timezone.utc).timestamp())
        except (ValueError, TypeError):
            ts = None
        day = b["when"][:10] or "unknown date"
        title = (f"{b['size']:.2f}in hail over {zc} ({z['near_city']} area) on {day}")
        body = (
            f"TARGETING AREA, NOT A PERSON. Nobody in ZIP {zc} has asked for help. "
            f"NEXRAD radar {b['radar']} detected a hail cell with a maximum "
            f"estimated stone of {b['size']:.2f} inches at {b['lat']:.4f},{b['lng']:.4f} "
            f"at {b['when']}, {b['dist']} mi from the {zc} ZCTA centroid "
            f"(severe probability {b['sevprob']}). "
            f"{b['size']:.2f}in is at or above the {min_hail:.2f}in asphalt-shingle "
            f"damage threshold. Use this to choose where to canvass or advertise; "
            f"a homeowner in this ZIP still has to be contacted and asked."
        )
        leads.append(_row(
            "swdi", f"{zc}-{b['when']}", _swdi_human_url(b["rng"], bbox),
            title, body, b["tier"], [f"hail:{b['size']:.2f}in"], market["name"],
            lat=b["lat"], lng=b["lng"], place=f"{zc} ({z['near_city']} area), TX",
            posted=ts, is_person=False,
            extra={"zip": zc, "near_city": z["near_city"],
                   "hail_in": b["size"], "storm_date": day,
                   "radar_site": b["radar"],
                   "geo_method": f"zcta_centroid_within_{ZIP_HIT_MI}mi",
                   "evidence": "NCEI SWDI nx3hail (NEXRAD hail signature)"}))
        h.kept += 1

    return leads, h


# ---------------------------------------------------------------------------
# Provider: IEM Local Storm Reports — human-confirmed reports.
#
# Lower volume than radar, higher confidence: a spotter, an emergency manager
# or the public physically saw the stone. robots.txt permits /geojson/.
# Response confirmed live 2026-08-27 for WFO FWD.
# ---------------------------------------------------------------------------
IEM_LSR = "https://mesonet.agron.iastate.edu/geojson/lsr.geojson"


def provider_lsr(market: dict, days: int = 90, min_hail: float = HAIL_DAMAGING,
                 delay: float = 1.0, **_):
    h = Health("lsr")
    leads = []
    wfos = market.get("lsr_wfos") or []
    if not wfos:
        return leads, h

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    sts, ets = f"{start:%Y-%m-%dT%H:%MZ}", f"{end:%Y-%m-%dT%H:%MZ}"

    for wfo in wfos:
        r, err = _get(IEM_LSR, h, params={"sts": sts, "ets": ets, "wfos": wfo})
        if r is None:
            print(f"  [lsr] {wfo} -> {err}", file=sys.stderr)
            continue
        try:
            feats = r.json().get("features", [])
        except ValueError:
            h.transport_errors += 1
            continue
        h.raw_results += len(feats)

        for f in feats:
            p = f.get("properties") or {}
            # LSR type code "H" is hail; magnitude is the diameter in inches.
            if (p.get("type") or "").upper() != "H":
                h.reject("not_hail")
                continue
            coords = (f.get("geometry") or {}).get("coordinates") or [None, None]
            lng, lat = coords[0], coords[1]
            if not in_market(market, lat, lng):
                h.geo_dropped += 1
                continue
            try:
                size = float(p.get("magnitude"))
            except (TypeError, ValueError):
                size = None
            tier, why = grade_hail(size, min_hail)
            if tier is None:
                h.reject(why)
                continue
            hits = _zip_hits(market, lat, lng)
            zc = hits[0][1]["zip"] if hits else None
            near = hits[0][1]["near_city"] if hits else None

            when = p.get("valid") or ""
            try:
                ts = int(datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ")
                         .replace(tzinfo=timezone.utc).timestamp())
            except (ValueError, TypeError):
                ts = None
            city = p.get("city") or ""
            county = p.get("county") or ""
            where = zc or f"{city}, {county} County"
            title = f"{size:.2f}in hail confirmed near {city} on {when[:10]}"
            body = (
                f"TARGETING AREA, NOT A PERSON. A human-confirmed local storm report: "
                f"{p.get('source') or 'a reporting party'} measured or estimated a "
                f"{size:.2f} inch hailstone at {city} ({county} County) at {when}. "
                f"Nobody here has asked for roof work. This tells you which "
                f"neighbourhood took damaging hail and on what day; the doors still "
                f"have to be knocked."
            )
            # A human-openable page showing this office's reports for the window.
            url = (f"https://mesonet.agron.iastate.edu/lsr/#{wfo}/"
                   f"{start:%Y%m%d%H%M}/{end:%Y%m%d%H%M}")
            leads.append(_row(
                "lsr", f"{wfo}-{when}-{lat}-{lng}", url, title, body, tier,
                [f"hail:{size:.2f}in"], market["name"], lat=lat, lng=lng,
                place=where, posted=ts, is_person=False,
                extra={"zip": zc, "near_city": near, "hail_in": size,
                       "storm_date": when[:10], "county": county,
                       "report_source": p.get("source"),
                       "geo_method": "lsr_point" + (
                           f"+zcta_centroid_within_{ZIP_HIT_MI}mi" if zc else ""),
                       "evidence": "NWS Local Storm Report via Iowa Env. Mesonet"}))
            h.kept += 1
        time.sleep(delay)

    return leads, h


# ---------------------------------------------------------------------------
# Provider: craigslist — the ONLY provider here that can produce a real person.
#
# On DFW evidence it is expected to yield zero (see the docstring: 69 unique
# postings, zero homeowner asks). It ships anyway, for three reasons: the
# measurement has to be repeatable rather than a one-off claim in a comment;
# a market that is not DFW may behave differently; and a channel that dries up
# and a channel that was never wet should be told apart by the same code path.
# ---------------------------------------------------------------------------
CL_API = "https://sapi.craigslist.org/web/v8/postings/search/full"

# Sections that can contain a homeowner hiring someone. `hss` (services
# OFFERED) is deliberately absent: it is 100% supply side by definition, and
# searching it is how a competitor list gets mistaken for a lead list.
CL_SEARCHES = [
    ("ggg", "roof"),
    ("ggg", "shingle"),
    ("ggg", "roof leak"),
    ("ggg", "ceiling leak"),
    ("ggg", "water damage"),
    ("ggg", "fascia"),
    ("ggg", "soffit"),
    ("lbg", "roof"),
]


def _cl_parse(data: dict, places=None):
    """Decode craigslist's positional item arrays. Same layout as
    source_junk._cl_parse; see that file for the field-by-field notes."""
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


def provider_craigslist(market: dict, max_detail: int = 60, delay: float = 0.35, **_):
    h = Health("craigslist")
    leads, seen, shortlist = [], set(), []
    if not market.get("cl_area"):
        return leads, h

    for path, query in (market.get("cl_searches") or CL_SEARCHES):
        params = {"batch": f"{market['cl_area']}-0-360-0-0", "cc": "US",
                  "lang": "en", "searchPath": path, "query": query}
        r, err = _get(CL_API, h, params=params)
        if r is None:
            print(f"  [craigslist] {path} q={query!r} -> {err}", file=sys.stderr)
            continue
        try:
            data = r.json().get("data", {})
        except ValueError:
            h.transport_errors += 1
            continue
        decode = data.get("decode")
        places = decode.get("locationDescriptions") if isinstance(decode, dict) else None
        items = _cl_parse(data, places=places)
        h.raw_results += len(items)

        for it in items:
            if it["url"] in seen:
                continue
            if not in_market(market, it["lat"], it["lng"]):
                h.geo_dropped += 1
                continue
            seen.add(it["url"])
            # A title is enough to REJECT a competitor loudly, never enough to
            # KEEP an ask — "my roof is leaking" lives in the body.
            _, _, why = classify_ask(it["title"])
            if why and why.startswith("supply_side"):
                h.reject(why)
                continue
            shortlist.append(it)
        time.sleep(delay)

    for it in shortlist[:max_detail]:
        body, posted = _cl_detail(it["url"], h)
        if body is None:
            continue
        tier, hits, why = classify_ask(it["title"], body)
        if tier is None:
            h.reject(why)
            continue
        leads.append(_row(
            "craigslist", it["id"], it["url"], it["title"], body[:1200],
            "ask", hits, market["name"], lat=it["lat"], lng=it["lng"],
            place=it.get("place"), posted=posted, is_person=True,
            extra={"evidence": "public classified posting"}))
        h.kept += 1
        time.sleep(delay)

    return leads, h


PROVIDERS = {"swdi": provider_swdi, "lsr": provider_lsr,
             "craigslist": provider_craigslist}


# ---------------------------------------------------------------------------
def run(market: dict, providers=None, tiers=None, days=90,
        min_hail=HAIL_DAMAGING, max_detail=60, delay=1.0):
    """Return (rows, [Health, ...]). Never sends anything, never raises on a
    provider failure — the failure lands in the Health record instead."""
    providers = providers or list(PROVIDERS)
    tiers = tiers or list(TIERS)
    all_rows, healths = [], []

    for name in providers:
        fn = PROVIDERS.get(name)
        if not fn:
            print(f"  unknown provider: {name}", file=sys.stderr)
            continue
        rows, h = fn(market, days=days, min_hail=min_hail,
                     max_detail=max_detail, delay=delay)
        healths.append(h)
        all_rows.extend(rows)

    all_rows = [r for r in all_rows if r["category"] in tiers]
    # A person who asked outranks every storm row, always, regardless of how
    # big the hail was. Then severity, then recency.
    order = {"ask": 0, "storm_severe": 1, "storm": 2}
    all_rows.sort(key=lambda r: (order.get(r["category"], 9),
                                 -(r.get("hail_in") or 0),
                                 -(r.get("created_utc") or 0)))
    return all_rows, healths


def decide_exit(rows, healths) -> tuple[int, str]:
    if rows:
        return EXIT_OK, "ok"
    statuses = [h.status for h in healths if h.status != "not_run"]
    if not statuses:
        return EXIT_ERROR, "no provider ran"
    if all(s == "error" for s in statuses):
        return EXIT_ERROR, "every provider failed at the transport layer"
    if any(s == "blocked" for s in statuses):
        return EXIT_BLOCKED, ("a provider was reachable but returned nothing at all, "
                              "or answered 403/429 — treat as a soft block, not as "
                              "an empty day")
    return EXIT_ZERO, ("providers returned real results but nothing survived the "
                       "gates — zero yield on non-zero attempts")


# ---------------------------------------------------------------------------
# Self test — gates only, no network.
#
# The competitor strings are verbatim from live DFW craigslist postings read
# on 2026-08-26/27. The homeowner strings are NOT quotes and are not presented
# as any: DFW craigslist contained no homeowner roof asks to quote. They are
# written to the shape the gate is meant to catch.
# ---------------------------------------------------------------------------
SELFTEST_ASK = [
    # --- must be kept (constructed, not quoted) ---
    ("Need someone to look at my roof",
     "We had a storm last week and my roof is leaking into the upstairs bedroom.",
     "ask"),
    ("Water stain on my ceiling",
     "There is a brown water stain spreading on the ceiling in the hallway.", "ask"),
    ("Shingles came off in the storm", "Lost shingles off the back slope.", "ask"),
    ("Hail damage - do I file a claim?",
     "Got hail damage on the roof and I am not sure whether to file a claim.", "ask"),
    ("Tree limb fell on the house", "A tree limb fell on my roof last night.", "ask"),
    # --- must be rejected: live competitor / recruiting text ---
    ("Roofing Services, Roof Replacements, Insurance Claims, Roof Inspection",
     "We offer free estimates. Licensed and insured. Serving the DFW metroplex.", None),
    ("CONTRACTORS WANTED - Start With 1 Job & Get Steady Work", "", None),
    ("Fascia Replacement",
     "***Please respond with code TX75002*** We are currently seeking reliable "
     "property maintenance, handyman, property preservation, and tree trimming "
     "crews to service foreclosed properties in ALLEN, TX 75002.", None),
    ("Busco rooferos / Looking for roofers", "", None),
    ("!!!!DISCOUNTED ROOFING)(K.O) THE COMPETITION- SAVE MONEY CALL US ANYTIME !!",
     "", None),
    ("Property Preservation Subcontractor - Tarrant County",
     "Additional Work Available: Lock change, Trimming shrubs & trees, "
     "Small roof repair and roof tarping.", None),
    ("DFW Building Shell Homes, Additions, Apts Dry-In  $23.50 per SF", "", None),
    ("Stucco, brick, roofing, stone repairs", "We do all types of masonry.", None),
    # --- must be rejected: no demand language ---
    ("Delivery Drivers with Cargo Van/ $180-$200 Daily", "", None),
    ("Free moving boxes", "Some used, some unused.", None),
]

SELFTEST_HAIL = [
    (2.00, "storm_severe"), (1.75, "storm_severe"), (1.74, "storm"),
    (1.50, "storm"), (1.00, "storm"), (0.99, None), (0.75, None),
    (0.50, None), (None, None),
]


def self_test() -> int:
    print("regexes (repr, so escape sequences are visible):")
    for nm, rx in (("ASK_RX", ASK_RX), ("SUPPLY_RX", SUPPLY_RX),
                   ("SWDI_POINT_RX", SWDI_POINT_RX)):
        print(f"  {nm} = {rx.pattern!r}\n")

    fails = 0
    print("ask gate:")
    for title, body, want in SELFTEST_ASK:
        got, hits, why = classify_ask(title, body)
        ok = got == want
        fails += 0 if ok else 1
        print(f"  [{'ok  ' if ok else 'FAIL'}] want={str(want):5} got={str(got):5} "
              f"{'(' + (why or '') + ')' if got is None else str(hits)}  | {title[:60]}")

    print("\nhail size gate (inches):")
    for size, want in SELFTEST_HAIL:
        got, why = grade_hail(size)
        ok = got == want
        fails += 0 if ok else 1
        print(f"  [{'ok  ' if ok else 'FAIL'}] {str(size):5} -> {str(got):12} "
              f"{why or ''}")

    print("\ngeo gate:")
    m = MARKETS["dfw_north"]
    geo_cases = [
        # (lat, lng, in_market?, label)
        (33.0195, -96.6989, True, "Plano, TX"),
        (32.9300, -97.3100, False, "Haslet (live 1.5in LSR 2026-08-26, Tarrant)"),
        (32.9600, -97.2600, False, "Keller (live 1.0in LSR 2026-08-26, Tarrant)"),
        (29.7604, -95.3698, False, "Houston"),
        (None, None, False, "no coordinates"),
    ]
    for lat, lng, want, label in geo_cases:
        got = in_market(m, lat, lng)
        ok = got == want
        fails += 0 if ok else 1
        print(f"  [{'ok  ' if ok else 'FAIL'}] want={str(want):5} got={str(got):5}"
              f"  | {label}")

    print("\nno-geometry market keeps nothing:")
    got = in_market({"name": "x"}, 33.0, -96.7)
    ok = got is False
    fails += 0 if ok else 1
    print(f"  [{'ok  ' if ok else 'FAIL'}] in_market(no bbox/center) -> {got}")

    print("\nstorm rows are never marked as people:")
    r = _row("swdi", "1", "u", "t", "b", "storm", [], "m", is_person=False)
    ok = r["is_person"] is False and r["lead_kind"] == "targeting_area"
    fails += 0 if ok else 1
    print(f"  [{'ok  ' if ok else 'FAIL'}] is_person={r['is_person']} "
          f"lead_kind={r['lead_kind']}")

    total = len(SELFTEST_ASK) + len(SELFTEST_HAIL) + len(geo_cases) + 2
    print(f"\n{total - fails}/{total} gate cases pass")
    return 1 if fails else 0


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", choices=sorted(MARKETS), help="built-in market key")
    ap.add_argument("--market-file", help="JSON file with name/center/radius_mi/zips/...")
    ap.add_argument("--provider", default=",".join(PROVIDERS),
                    help="comma list: " + ",".join(PROVIDERS))
    ap.add_argument("--tier", default=",".join(TIERS), help="comma list: " + ",".join(TIERS))
    ap.add_argument("--days", type=int, default=90, help="storm lookback window")
    ap.add_argument("--min-hail", type=float, default=HAIL_DAMAGING,
                    help=f"inches; below {HAIL_DAMAGING} an asphalt shingle is not "
                         "damaged and the row is not a lead")
    ap.add_argument("--max-detail", type=int, default=60,
                    help="craigslist detail-page fetch budget")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--out", help="write JSONL here")
    ap.add_argument("--json", action="store_true", help="dump rows as JSON to stdout")
    ap.add_argument("--self-test", action="store_true", help="gate tests, no network")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()

    if a.market_file:
        market = json.loads(pathlib.Path(a.market_file).read_text(encoding="utf-8"))
    elif a.market:
        market = MARKETS[a.market]
    else:
        ap.error("one of --market or --market-file is required")

    if a.min_hail < HAIL_DAMAGING:
        print(f"WARNING: --min-hail {a.min_hail} is below the {HAIL_DAMAGING}in "
              "asphalt-shingle damage threshold. Rows produced below that line "
              "are weather, not demand.", file=sys.stderr)

    t0 = time.time()
    print(f"source_roofing: market={market['name']} providers={a.provider} "
          f"days={a.days} min_hail={a.min_hail}", file=sys.stderr)
    rows, healths = run(market,
                        providers=[p.strip() for p in a.provider.split(",") if p.strip()],
                        tiers=[t.strip() for t in a.tier.split(",") if t.strip()],
                        days=a.days, min_hail=a.min_hail,
                        max_detail=a.max_detail, delay=a.delay)

    print("\n===== HEALTH =====", file=sys.stderr)
    for h in healths:
        print("  " + json.dumps(h.as_dict()), file=sys.stderr)

    by_tier = {}
    for r in rows:
        by_tier[r["category"]] = by_tier.get(r["category"], 0) + 1
    n_people = sum(1 for r in rows if r.get("is_person"))
    print(f"\n{len(rows)} rows in {time.time() - t0:.0f}s  {by_tier}", file=sys.stderr)
    print(f"  {n_people} are PEOPLE who asked for help; "
          f"{len(rows) - n_people} are TARGETING AREAS (nobody asked)", file=sys.stderr)
    for r in rows:
        kind = "PERSON" if r.get("is_person") else "area  "
        print(f"  [{r['category']:12}] {kind} {(r['title'] or '')[:66]}\n"
              f"           {r['url']}", file=sys.stderr)

    if a.out:
        p = pathlib.Path(a.out)
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                     encoding="utf-8")
        print(f"\nwrote {len(rows)} -> {p}", file=sys.stderr)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))

    code, why = decide_exit(rows, healths)
    if code != EXIT_OK:
        print(f"\nHARD FAILURE (exit {code}): {why}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
