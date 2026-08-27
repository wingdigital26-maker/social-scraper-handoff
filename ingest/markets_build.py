#!/usr/bin/env python3
"""
markets_build — discover the MARKET CATALOG, so a market is data and never code.

WHY THIS EXISTS
  source_junk shipped with a three-entry hand-written MARKETS dict (dfw,
  houston, austin). Every one of those entries carried a hand-typed craigslist
  area id, a hand-drawn bounding box, and a hand-guessed list of ZIP prefixes.
  That does not scale to 400 craigslist sites, and worse, it makes the market
  list a code change instead of a data change — which is exactly what the
  "no client name in logic" rule exists to prevent.

  Both catalogs are published by the sites themselves and are free, keyless,
  and robots-permitted:

    craigslist    https://reference.craigslist.org/Areas
                  707 areas as of 2026-08-27, each with AreaID, Hostname,
                  Region (state), Country, Latitude, Longitude and SubAreas.
                  This is craigslist's own reference service; it is the same
                  id space the sapi search endpoint takes in `batch`.

    estatesales   https://www.estatesales.net/site-maps -> /site-maps/main
                  lists 50 state pages (/TX, /OH, ...). Each state page links
                  every metro index it operates in that state
                  (/TX/Dallas-Fort-Worth-Arlington, /TX/Austin, ...).
                  robots.txt disallows only /account /homepages /v2 /v3
                  /legacy /api/user-view-details. Measured 2026-08-27.

  The two catalogs are joined by (state, name-token overlap). Nothing here is
  fuzzy-matched by a model — it is set arithmetic on lowercased word tokens,
  printed with its score so a wrong join is visible rather than silent.

GEOGRAPHY IS A RADIUS, NOT A ZIP LIST
  The old es_zips field was a hand-listed set of 3-digit ZIP prefixes per
  market. It cannot be auto-derived and it silently drops legitimate outlying
  cities. It is replaced by center + radius_mi:

    * craigslist postings carry lat/lng in the search response already.
    * estatesales.net SALE DETAIL pages carry a real "latitude"/"longitude"
      pair (verified live: /TX/Waco/76710/5054553 -> 31.545595). So the same
      haversine test works for both providers, and the Waco / Ben Wheeler /
      Bonham bleed-in that the ZIP list was invented to stop is handled by
      arithmetic instead of by a list somebody has to maintain.

  es_zips is still honoured if a market file supplies it, so nothing that
  already works breaks.

USAGE
    python markets_build.py --out markets_catalog.json
    python markets_build.py --out markets_catalog.json --country US
    python markets_build.py --show dfw
    python markets_build.py --out c.json --validate      # ping each area id

  The catalog is a plain JSON dict: key -> market dict consumable by
  source_junk.run() unchanged.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import sys
import time

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_CATALOG = HERE / "markets_catalog.json"

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")}

CL_AREAS_URL = "https://reference.craigslist.org/Areas"
ES_ROOT = "https://www.estatesales.net"
ES_SITEMAP = ES_ROOT + "/site-maps/main"

# A metro is worth roughly a 35-mile drive for a truck-based trade. Overridable
# per market in the catalog file; only ever used as a radius, never a hard list.
DEFAULT_RADIUS_MI = 35.0

# Tokens that carry no discriminating power when joining a craigslist area name
# to an estatesales metro name. "fort worth" vs "ft worth" is handled by the
# alias table below, not by a model.
STOPWORDS = {"the", "of", "and", "area", "co", "county", "region", "metro",
             "greater", "city", "cities", "north", "south", "east", "west",
             "central", "st", "saint", "ft", "mt"}
ALIASES = {"ft": "fort", "st": "saint", "mt": "mount", "n": "north",
           "s": "south", "e": "east", "w": "west"}

STATE_RX = re.compile(r"^/([A-Z]{2})$")
METRO_RX = re.compile(r'href="(/[A-Z]{2}/[A-Za-z][A-Za-z\-\.]*)"')


# ---------------------------------------------------------------------------
def haversine_mi(lat1, lng1, lat2, lng2) -> float:
    """Great-circle miles. Pure arithmetic; used by every geo gate downstream."""
    if None in (lat1, lng1, lat2, lng2):
        return float("inf")
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def bbox_from_center(lat: float, lng: float, radius_mi: float):
    """A bounding box that CONTAINS the radius circle. Deliberately generous:
    it is a cheap pre-filter in front of the exact haversine test, so a false
    keep here costs one arithmetic op and a false drop would cost a real lead."""
    dlat = radius_mi / 69.0
    coslat = max(0.15, math.cos(math.radians(lat)))
    dlng = radius_mi / (69.0 * coslat)
    return (round(lat - dlat, 4), round(lat + dlat, 4),
            round(lng - dlng, 4), round(lng + dlng, 4))


def tokens(name: str) -> set:
    words = re.split(r"[^a-z0-9]+", (name or "").lower())
    out = set()
    for w in words:
        if not w:
            continue
        w = ALIASES.get(w, w)
        if w in STOPWORDS or len(w) < 2:
            continue
        out.add(w)
    return out


def name_overlap(a: str, b: str) -> float:
    """Containment score in [0,1]: how much of the SMALLER name is covered by
    the larger. Containment, not Jaccard, because estatesales metro names are
    routinely longer than craigslist's ("Dallas-Fort-Worth-Arlington" vs
    "dallas / fort worth") and Jaccard would punish that correct match."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return re.sub(r"-+", "-", s)


# ---------------------------------------------------------------------------
def fetch_cl_areas(session: requests.Session, country: str | None = "US") -> list[dict]:
    r = session.get(CL_AREAS_URL, timeout=45)
    r.raise_for_status()
    areas = r.json()
    if country:
        areas = [a for a in areas if (a.get("Country") or "").upper() == country.upper()]
    return areas


def fetch_es_metros(session: requests.Session, delay: float = 0.4) -> dict[str, list[str]]:
    """{state -> [metro_path, ...]} straight off estatesales.net's own sitemap."""
    r = session.get(ES_SITEMAP, timeout=45)
    r.raise_for_status()
    states = []
    for loc in re.findall(r"<loc>([^<]+)</loc>", r.text):
        p = loc.replace(ES_ROOT, "")
        m = STATE_RX.match(p)
        if m:
            states.append(m.group(1))

    out: dict[str, list[str]] = {}
    for st in states:
        try:
            sr = session.get(f"{ES_ROOT}/{st}", timeout=45)
        except Exception as e:
            print(f"  [es] {st} -> {type(e).__name__}", file=sys.stderr)
            continue
        if sr.status_code != 200:
            print(f"  [es] {st} -> HTTP {sr.status_code}", file=sys.stderr)
            continue
        metros = []
        for m in METRO_RX.finditer(sr.text):
            path = m.group(1)
            # A state page carries a handful of cross-state "featured" links.
            # Keep only paths under the state we asked for.
            if path.startswith(f"/{st}/") and path not in metros:
                metros.append(path)
        out[st] = metros
        print(f"  [es] {st}: {len(metros)} metro index page(s)", file=sys.stderr)
        time.sleep(delay)
    return out


def build(country="US", radius_mi=DEFAULT_RADIUS_MI, min_join=0.5,
          delay=0.4) -> dict:
    s = requests.Session()
    s.headers.update(UA)

    print("fetching craigslist area reference ...", file=sys.stderr)
    areas = fetch_cl_areas(s, country)
    print(f"  {len(areas)} area(s) in {country}", file=sys.stderr)

    print("fetching estatesales.net state -> metro index ...", file=sys.stderr)
    es = fetch_es_metros(s, delay=delay)
    total_metros = sum(len(v) for v in es.values())
    print(f"  {total_metros} metro index page(s) across {len(es)} state(s)",
          file=sys.stderr)

    catalog = {}
    joined = 0
    for a in areas:
        region = (a.get("Region") or "").upper()
        desc = a.get("Description") or a.get("ShortDescription") or ""
        lat, lng = a.get("Latitude"), a.get("Longitude")
        if lat is None or lng is None:
            continue
        key = slug(f"{a.get('Abbreviation') or a.get('Hostname')}")
        if not key or key in catalog:
            key = slug(f"{a.get('Hostname')}-{a.get('AreaID')}")

        best_path, best_score = None, 0.0
        for path in es.get(region, []):
            cand = path.split("/")[-1].replace("-", " ")
            sc = name_overlap(desc, cand)
            if sc > best_score:
                best_path, best_score = path, sc
        if best_score < min_join:
            best_path = None
        else:
            joined += 1

        catalog[key] = {
            "name": f"{desc} ({region})" if region else desc,
            "cl_area": a.get("AreaID"),
            "cl_host": f"{a.get('Hostname')}.craigslist.org",
            "state": region,
            "center": [lat, lng],
            "radius_mi": radius_mi,
            "bbox": list(bbox_from_center(lat, lng, radius_mi)),
            "es_path": best_path,
            "es_join_score": round(best_score, 2),
        }

    print(f"\n{len(catalog)} market(s); {joined} joined to an estatesales metro "
          f"(threshold {min_join})", file=sys.stderr)
    return catalog


def validate(catalog: dict, delay: float = 0.15, limit: int | None = None) -> dict:
    """Ping each craigslist area id once. Some AreaIDs in the reference feed do
    not answer the search endpoint (measured: HTTP 400 on a minority of ids).
    Recording that here means a sweep never wastes ten queries on a dead id."""
    api = "https://sapi.craigslist.org/web/v8/postings/search/full"
    s = requests.Session()
    s.headers.update(UA)
    keys = list(catalog)[:limit] if limit else list(catalog)
    live = dead = 0
    for k in keys:
        m = catalog[k]
        try:
            r = s.get(api, params={"batch": f"{m['cl_area']}-0-360-0-0", "cc": "US",
                                   "lang": "en", "searchPath": "zip"}, timeout=25)
            ok = r.status_code == 200
        except Exception:
            ok = False
        m["cl_validated"] = bool(ok)
        live += ok
        dead += (not ok)
        time.sleep(delay)
    print(f"validate: {live} live, {dead} dead craigslist area id(s)", file=sys.stderr)
    return catalog


def load_catalog(path: pathlib.Path | str = DEFAULT_CATALOG) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        raise SystemExit(f"no market catalog at {p} — run: python markets_build.py --out {p}")
    cat = json.loads(p.read_text(encoding="utf-8"))
    for m in cat.values():
        if isinstance(m.get("bbox"), list):
            m["bbox"] = tuple(m["bbox"])
    return cat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_CATALOG)
    ap.add_argument("--country", default="US")
    ap.add_argument("--radius-mi", type=float, default=DEFAULT_RADIUS_MI)
    ap.add_argument("--min-join", type=float, default=0.5,
                    help="minimum name-overlap score to bind an estatesales metro")
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--validate-limit", type=int)
    ap.add_argument("--show", help="print one market from the existing catalog and exit")
    a = ap.parse_args(argv)

    if a.show:
        cat = load_catalog(a.out)
        m = cat.get(a.show)
        if not m:
            hits = [k for k in cat if a.show.lower() in k or
                    a.show.lower() in (cat[k]["name"] or "").lower()]
            print(f"no exact key '{a.show}'. near: {hits[:20]}")
            return 1
        print(json.dumps(m, indent=2))
        return 0

    cat = build(country=a.country, radius_mi=a.radius_mi,
                min_join=a.min_join, delay=a.delay)
    if a.validate:
        cat = validate(cat, limit=a.validate_limit)
    a.out.write_text(json.dumps(cat, indent=1, sort_keys=True), encoding="utf-8")
    print(f"wrote {len(cat)} market(s) -> {a.out}", file=sys.stderr)

    with_es = sum(1 for m in cat.values() if m.get("es_path"))
    print(f"  craigslist-capable : {len(cat)}", file=sys.stderr)
    print(f"  estatesales-capable: {with_es}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
