#!/usr/bin/env python3
"""
permits_cli.py - public building permit records source tool for the Sonar
ingest pipeline. Follows ingest/SOURCE-CLI-CONTRACT.md exactly.

Coverage as re-verified 2026-08-30:

- Collin County (dataset 82ee-gbj5, "Collin CAD Permits" on data.texas.gov,
  Socrata / SoQL, no key needed). THIS IS THE LIVE ONE. Measured 2026-08-30:
  110,969 rows, permitissueddate spanning 2023-01-01 to 2026-12-29, datadate
  refreshed 2026-08-28. It carries a real permittypedescr including
  "Roof/Re-Roof", "New Construction", "Demolition", a builder name, a permit
  value, and a full situs address. It honours a recency window, which is the
  signal this source exists to provide.
  Covered situs cities include Plano, Frisco, McKinney, Allen, Prosper,
  Wylie, Celina, Melissa, Anna, Princeton, Murphy, Sachse, Farmersville,
  Josephine, Lucas, Fairview, Parker, Nevada, Blue Ridge, Weston.

- Dallas (dataset e7gq-4sah, "Building Permits" on www.dallasopendata.com,
  Socrata / SoQL, no key needed). This is the only dataset the portal
  exposes under any permit/demolition/roofing search that is queryable via
  SoQL. IMPORTANT HONEST FINDING: despite the catalog metadata claiming an
  updatedAt of 2026-06-05, the actual issued_date column stops dead at
  2019-12-31 (verified by grouping and by date-suffix counts down to zero
  for every year 2021-2026). This dataset is NOT fresh. It is still useful
  for identifying contractor/company names and permit types, but it cannot
  deliver the "permitted job happening this week" freshness signal the task
  wanted. That signal did not pan out for Dallas with what is public and
  queryable today.
- Fort Worth: data.fortworthtexas.gov advertises a "Development Permits"
  dataset in the Socrata catalog API, but the actual host has migrated to
  ArcGIS Hub and the old Socrata resource endpoint returns an "ArcGIS Hub
  Unsupported" HTML page, not JSON. Not usable without a real ArcGIS
  feature-service URL, which was not found in the time budget for this task.
- Arlington, Plano, Irving, Frisco, Garland: no Socrata catalog results at
  all under the domain names checked (data.arlingtontx.gov, data.plano.gov,
  gis.irvingtx.gov, data.friscotexas.gov, data.garlandtx.gov). Not ruled
  out entirely, just not found quickly; do not assume they are closed.

Permit type text is used to tag demolition and roofing permits specifically,
since those are the two permit categories that mean physical debris or
material removal is imminent. Every other permit type still comes through
as a general building-permit record.
"""

import argparse
import datetime
import json
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SonarLeadEngine/1.0 (+https://wingdigital.io)"

DALLAS_BASE = "https://www.dallasopendata.com/resource/e7gq-4sah.json"
COLLIN_BASE = "https://data.texas.gov/resource/82ee-gbj5.json"

DALLAS_LABEL = "Dallas Building Permits (e7gq-4sah)"
COLLIN_LABEL = "Collin CAD Permits (82ee-gbj5)"

# The Dallas set is FROZEN: its issued_date stops at 2019-12-31. It is kept
# only as a historic contractor-name lookup and is never used to answer a
# recency window, because it cannot. See the file header.
FROZEN_DATASETS = {DALLAS_BASE}

# Situs cities that actually appear in the Collin CAD set.
COLLIN_CITIES = [
    "plano", "frisco", "mckinney", "allen", "prosper", "wylie", "celina",
    "melissa", "anna", "princeton", "murphy", "sachse", "farmersville",
    "josephine", "lucas", "fairview", "parker", "nevada", "blue ridge",
    "weston", "new hope", "lavon", "copeville", "westminster",
]

# city -> (dataset base url, dataset label, schema key)
CITY_DATASETS = {
    "dallas": (DALLAS_BASE, DALLAS_LABEL, "dallas"),
}
for _c in COLLIN_CITIES:
    CITY_DATASETS[_c] = (COLLIN_BASE, COLLIN_LABEL, "collin")
# Aliases so a caller can ask for the county directly.
CITY_DATASETS["collin"] = (COLLIN_BASE, COLLIN_LABEL, "collin")
CITY_DATASETS["collin county"] = (COLLIN_BASE, COLLIN_LABEL, "collin")

# DFW cities with no dataset of their own. When one of these is requested we
# still run Collin CAD county-wide and say so out loud, rather than returning
# a confident zero. Nothing is fabricated: every emitted record carries the
# real situs city it came from, which will be a Collin city, not the one that
# was asked for. The caller can see that in location_text and platform.
DFW_NO_DATASET = {
    "fort worth", "arlington", "irving", "garland", "grand prairie",
    "mesquite", "carrollton", "richardson", "denton", "lewisville",
    "flower mound", "euless", "bedford", "hurst", "keller", "mansfield",
    "north richland hills", "rowlett", "dallas",
}


def log(msg):
    print(msg, file=sys.stderr)


def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body), resp.status


def normalize_issued_date(raw):
    """Dallas issued_date is text like '12/31/19' (MM/DD/YY). Convert to an
    ISO-ish date string if it parses cleanly, otherwise pass through null."""
    if not raw or raw == "NULL":
        return None
    try:
        mm, dd, yy = raw.split("/")
        yy = int(yy)
        yy_full = 2000 + yy if yy < 70 else 1900 + yy
        return f"{yy_full:04d}-{int(mm):02d}-{int(dd):02d}"
    except Exception:
        return None


def collin_row_to_record(row, client, dataset_label):
    """Map a Collin CAD permit row onto the SOURCE-CLI-CONTRACT record shape.

    permitissueddate arrives as a floating ISO timestamp ('2026-08-14T00:00:00.000');
    we keep the date half only. situs* fields are split, so the address is
    reassembled from the pre-joined situsconcat when present.
    """
    permit_type = row.get("permittypedescr") or None
    permit_number = row.get("permitnum") or None
    builder = row.get("permitbuildername") or None
    if builder and builder.strip().upper() in ("NOT GIVEN", "N/A", "NONE", "UNKNOWN"):
        builder = None

    parts = []
    if permit_type:
        parts.append(permit_type)
    comments = row.get("permitcomments")
    if comments and comments.strip():
        parts.append(comments.strip())
    if builder:
        parts.append("Builder: " + builder)
    value = row.get("permitvalue")
    if value and value not in ("0", "0.00", "NULL"):
        parts.append("Permit value: $" + str(value))
    res_com = row.get("proprescom")
    if res_com:
        parts.append(res_com)
    body = " | ".join(parts) if parts else None

    situs = row.get("situsconcat") or row.get("situsconcatshort")
    situs_city = (row.get("situscity") or "").title() or None
    if situs:
        location_text = situs.replace(" ,", ",").strip()
    elif situs_city:
        location_text = situs_city
    else:
        location_text = None

    posted_at = None
    raw_date = row.get("permitissueddate")
    if raw_date and len(raw_date) >= 10:
        posted_at = raw_date[:10]

    url = None
    if permit_number:
        url = (COLLIN_BASE + "?"
               + urllib.parse.urlencode({"permitnum": permit_number,
                                         "permitid": row.get("permitid") or ""}))

    return {
        "source": "permits",
        "platform": dataset_label,
        "url": url,
        "title": permit_type,
        "body": body,
        "author_handle": builder,
        "location_text": location_text,
        "posted_at": posted_at,
        "event_date": None,
        "query": None,
        "client": client,
    }


def row_to_record(row, city, client, dataset_label):
    permit_type = row.get("permit_type") or None
    address = row.get("street_address") or None
    zip_code = row.get("zip_code")
    if zip_code == "NULL":
        zip_code = None
    contractor = row.get("contractor") or None
    permit_number = row.get("permit_number") or None

    parts = []
    if permit_type:
        parts.append(permit_type)
    if row.get("work_description") and row["work_description"] != "NULL":
        parts.append(row["work_description"])
    if contractor:
        parts.append("Contractor: " + contractor)
    if row.get("value") and row["value"] not in ("0", "NULL"):
        parts.append("Permit value: $" + row["value"])
    body = " | ".join(parts) if parts else None

    location_text = None
    if address and city:
        location_text = f"{address}, {city.title()}"
    elif address:
        location_text = address
    elif city:
        location_text = city.title()

    url = None
    if permit_number:
        url = (
            "https://www.dallasopendata.com/resource/e7gq-4sah.json?"
            + urllib.parse.urlencode({"permit_number": permit_number})
        )

    return {
        "source": "permits",
        "platform": dataset_label,
        "url": url,
        "title": permit_type,
        "body": body,
        "author_handle": contractor,
        "location_text": location_text,
        "posted_at": normalize_issued_date(row.get("issued_date")),
        "event_date": None,
        "query": None,
        "client": client,
    }


def build_where_clause(query_terms):
    if not query_terms:
        return None
    clauses = []
    for q in query_terms:
        q_escaped = q.replace("'", "''")
        clauses.append(f"permit_type like '%{q_escaped}%'")
    return " OR ".join(clauses)


def main():
    p = argparse.ArgumentParser(description="Public building permit records source tool")
    p.add_argument("--query", action="append", default=[])
    p.add_argument("--cities", default=None)
    p.add_argument("--client", default=None)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--since", type=int, default=None)
    p.add_argument("--json", action="store_true", default=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.cities:
        log("permits_cli: --cities is required, this source is city-scoped "
            "public data and never guesses a city.")
        sys.exit(1)

    requested_cities = [c.strip().lower() for c in args.cities.split(",") if c.strip()]
    covered = [c for c in requested_cities if c in CITY_DATASETS]
    uncovered = [c for c in requested_cities if c not in CITY_DATASETS]

    if uncovered:
        log(f"permits_cli: no open, queryable permit dataset found for: "
            f"{', '.join(uncovered)}. Verified 2026-08-27: Fort Worth's "
            f"Socrata endpoint now 404s to an ArcGIS Hub migration notice; "
            f"Arlington, Irving, and Garland returned no Socrata catalog "
            f"results at all. Skipping these, not fabricating.")

    # A recency window is the whole point of this source. Every dataset in
    # FROZEN_DATASETS is structurally incapable of answering one, so when
    # --since is given they are dropped here instead of being queried and
    # then reported as a healthy "40 records" of 2019 data every single run.
    if args.since is not None:
        frozen = [c for c in covered if CITY_DATASETS[c][0] in FROZEN_DATASETS]
        if frozen:
            log(f"permits_cli: dropping {', '.join(frozen)} for this run -- that "
                f"dataset's issued_date stops at 2019-12-31 and cannot satisfy a "
                f"--since {args.since} window. It is historic-only; querying it "
                f"here would report stale rows as fresh finds.")
            covered = [c for c in covered if c not in frozen]

    # DFW cities with no dataset of their own: fall back to Collin CAD
    # county-wide rather than returning a confident zero. Announced loudly;
    # every emitted record still carries its own real situs city.
    if not covered:
        metro_asked = [c for c in requested_cities if c in DFW_NO_DATASET]
        if metro_asked:
            log(f"permits_cli: no live per-city dataset for {', '.join(metro_asked)}. "
                f"Falling back to {COLLIN_LABEL} county-wide, which is the only "
                f"live permit feed in the metro today (verified 2026-08-30). "
                f"Records will carry their real Collin County situs city, NOT "
                f"the city that was requested. Adjacent coverage, not a match.")
            covered = ["collin"]

    if not covered:
        log("permits_cli: none of the requested cities have a working dataset. No data pulled.")
        sys.exit(0)

    # Collapse duplicate hits on the same county-wide dataset.
    seen_bases, deduped = set(), []
    for c in covered:
        base = CITY_DATASETS[c][0]
        if base in seen_bases:
            continue
        seen_bases.add(base)
        deduped.append(c)
    covered = deduped

    qterms = []
    for grp in args.query:
        qterms.extend([q.strip() for q in grp.split(",") if q.strip()])

    all_records = []
    for city in covered:
        base_url, label, schema = CITY_DATASETS[city]
        if schema == "collin":
            params = {"$limit": str(min(args.limit, 1000)),
                      "$order": "permitissueddate DESC"}
            clauses = []
            if args.since is not None:
                cutoff = (datetime.date.today()
                          - datetime.timedelta(days=int(args.since))).isoformat()
                clauses.append(f"permitissueddate >= '{cutoff}T00:00:00'")
            if city not in ("collin", "collin county"):
                clauses.append("upper(situscity) = '%s'" % city.upper().replace("'", "''"))
            if qterms:
                ors = " OR ".join(
                    "upper(permittypedescr) like upper('%%%s%%')" % q.replace("'", "''")
                    for q in qterms)
                clauses.append("(" + ors + ")")
            if clauses:
                params["$where"] = " AND ".join(clauses)
        else:
            params = {"$limit": str(min(args.limit, 1000)), "$order": "issued_date DESC"}
            where = build_where_clause(qterms)
            if where:
                params["$where"] = where
        url = base_url + "?" + urllib.parse.urlencode(params)

        log(f"permits_cli: querying {label} for {city}")
        try:
            rows, status = fetch_json(url)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                log(f"permits_cli: source refused us for {city}, HTTP {e.code}")
                sys.exit(2)
            log(f"permits_cli: HTTP error {e.code} querying {city}: {e}")
            sys.exit(1)
        except urllib.error.URLError as e:
            log(f"permits_cli: could not reach dataset for {city}: {e}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            log(f"permits_cli: dataset for {city} did not return valid JSON: {e}")
            sys.exit(1)

        if status in (403, 429):
            log(f"permits_cli: source refused us for {city}, HTTP {status}")
            sys.exit(2)

        if not rows:
            log(f"permits_cli: zero rows returned for {city} from {label} "
                f"(query filter: {qterms if qterms else 'none'}, "
                f"since={args.since}). Genuine empty result, not an error.")
            time.sleep(1)
            continue

        for row in rows:
            if schema == "collin":
                all_records.append(collin_row_to_record(row, args.client, label))
            else:
                all_records.append(row_to_record(row, city, args.client, label))

        log(f"permits_cli: {label} returned {len(rows)} rows for {city}")
        time.sleep(1)  # polite delay between city requests

    all_records = all_records[: args.limit]

    if not all_records:
        sys.exit(0)

    if args.dry_run:
        log(f"permits_cli: --dry-run, emitting {len(all_records)} records, writing nothing")

    for r in all_records:
        print(json.dumps(r, ensure_ascii=False))

    sys.exit(0)


if __name__ == "__main__":
    main()
