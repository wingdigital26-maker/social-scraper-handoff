#!/usr/bin/env python3
"""
permits_cli.py - public building permit records source tool for the Sonar
ingest pipeline. Follows ingest/SOURCE-CLI-CONTRACT.md exactly.

Coverage as actually verified 2026-08-27:

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
import json
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SonarLeadEngine/1.0 (+https://wingdigital.io)"

DALLAS_BASE = "https://www.dallasopendata.com/resource/e7gq-4sah.json"

# city -> (dataset base url, dataset label). Only Dallas is live today.
CITY_DATASETS = {
    "dallas": (DALLAS_BASE, "Dallas Building Permits (e7gq-4sah)"),
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
            f"Arlington, Plano, Irving, Frisco, and Garland returned no "
            f"Socrata catalog results at all. Skipping these, not fabricating.")

    if not covered:
        log("permits_cli: none of the requested cities have a working dataset. No data pulled.")
        sys.exit(0)

    qterms = []
    for grp in args.query:
        qterms.extend([q.strip() for q in grp.split(",") if q.strip()])

    all_records = []
    for city in covered:
        base_url, label = CITY_DATASETS[city]
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
            log(f"permits_cli: zero rows returned for {city} "
                f"(query filter: {qterms if qterms else 'none'}). "
                f"Note this Dallas dataset's issued_date data stops at "
                f"2019-12-31 regardless of --since, see file header.")
            time.sleep(1)
            continue

        for row in rows:
            all_records.append(row_to_record(row, city, args.client, label))

        time.sleep(1)  # polite delay between city requests

    if args.since is not None:
        log("permits_cli: --since requested, but this Dallas dataset's "
            "issued_date field does not extend past 2019-12-31, so no rows "
            "can satisfy a recent-days window. Reporting via stderr per "
            "contract rather than silently filtering to zero.")

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
