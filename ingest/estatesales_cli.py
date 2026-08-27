#!/usr/bin/env python3
"""
estatesales_cli.py - estatesales.net source tool for the Sonar ingest pipeline.

Follows ingest/SOURCE-CLI-CONTRACT.md exactly: one JSON record per line on
stdout, all logs and errors on stderr, exit 0/1/2 per the contract.

estatesales.net groups DFW into one metro listing page that 301-redirects
any city name to https://www.estatesales.net/TX/Dallas-Fort-Worth-Arlington.
There is no separate per-city URL to hit, so --cities is applied as a
client-side filter against each sale's own address (addressLocality) after
fetching the shared metro page. Sales carry real start/end dates in
schema.org SaleEvent JSON-LD blocks embedded in the page, which is where
event_date comes from. The site does not expose a "listed on" date anywhere
on the page, so posted_at is always null for this source, never guessed.

Each sale is run by a company (the organizer). That company name and its
sale-detail URL are captured because an operator running sales most weekends
is a recurring hauler-need signal, worth more than the single sale date.
"""

import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.error

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SonarLeadEngine/1.0 (+https://wingdigital.io)"
METRO_URL = "https://www.estatesales.net/TX/Dallas-Fort-Worth-Arlington"
LD_JSON_RE = re.compile(r'application/ld\+json">(\{.*?\})</script>', re.DOTALL)


def log(msg):
    print(msg, file=sys.stderr)


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace"), resp.status


def parse_sales(html):
    sales = []
    for raw in LD_JSON_RE.findall(html):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if data.get("@type") != "SaleEvent":
            continue
        sales.append(data)
    return sales


def sale_to_record(sale, client):
    location = sale.get("location") or {}
    address = location.get("address") or {}
    city = address.get("addressLocality") or None
    organizer = sale.get("organizer") or {}
    company_name = organizer.get("name") or None
    company_url = organizer.get("url") or None
    if company_url == "":
        company_url = None

    parts = []
    if company_name:
        parts.append("Run by: " + company_name)
    if organizer.get("telephone"):
        parts.append("Phone: " + organizer["telephone"])
    if sale.get("description"):
        parts.append(sale["description"])
    if location.get("name"):
        parts.append("Location: " + location["name"])
    body = " | ".join(parts) if parts else None

    location_text = None
    if city and address.get("addressRegion"):
        location_text = f"{city}, {address['addressRegion']}"
    elif city:
        location_text = city

    return {
        "source": "estatesales",
        "platform": "estatesales.net",
        "url": sale.get("url"),
        "title": sale.get("name"),
        "body": body,
        "author_handle": company_name,
        "location_text": location_text,
        "posted_at": None,
        "event_date": sale.get("startDate"),
        "query": None,
        "client": client,
        "_city_for_filter": (city or "").strip().lower(),
        "_company_url": company_url,
    }


def main():
    p = argparse.ArgumentParser(description="estatesales.net source tool")
    p.add_argument("--query", action="append", default=[])
    p.add_argument("--cities", default=None)
    p.add_argument("--client", default=None)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--since", type=int, default=None)
    p.add_argument("--json", action="store_true", default=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.since is not None:
        log("estatesales_cli: --since is not supported by this source. "
            "estatesales.net gives no listing-created date, only sale "
            "start/end dates, so a posted-in-last-N-days filter cannot be "
            "applied. Ignoring --since; use event_date downstream instead.")

    cities = None
    if args.cities:
        cities = [c.strip().lower() for c in args.cities.split(",") if c.strip()]

    log(f"estatesales_cli: fetching {METRO_URL}")
    try:
        html, status = fetch(METRO_URL)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            log(f"estatesales_cli: source refused us, HTTP {e.code}")
            sys.exit(2)
        log(f"estatesales_cli: HTTP error {e.code}: {e}")
        sys.exit(1)
    except urllib.error.URLError as e:
        log(f"estatesales_cli: could not reach estatesales.net: {e}")
        sys.exit(1)
    except Exception as e:
        log(f"estatesales_cli: unexpected error fetching page: {e}")
        sys.exit(1)

    if status == 403 or status == 429:
        log(f"estatesales_cli: source refused us, HTTP {status}")
        sys.exit(2)

    sales = parse_sales(html)
    if not sales:
        log("estatesales_cli: page fetched but no SaleEvent listings found "
            "in the page's JSON-LD. Either the site changed its markup or "
            "there are genuinely zero sales listed right now.")
        sys.exit(0)

    time.sleep(1)  # polite pause even though this was a single fetch

    records = [sale_to_record(s, args.client) for s in sales]

    if cities:
        before = len(records)
        records = [r for r in records if r["_city_for_filter"] in cities]
        log(f"estatesales_cli: filtered {before} sales down to {len(records)} "
            f"matching --cities {cities}")
        if not records:
            log("estatesales_cli: no sales matched the requested cities "
                "(they may all be in other DFW metro cities right now)")

    if args.query:
        qterms = [q.strip().lower() for grp in args.query for q in grp.split(",") if q.strip()]
        if qterms:
            before = len(records)
            records = [
                r for r in records
                if any(q in (r["title"] or "").lower() or q in (r["body"] or "").lower()
                       for q in qterms)
            ]
            log(f"estatesales_cli: filtered {before} sales down to {len(records)} "
                f"matching --query {qterms}")

    records = records[: args.limit]

    if not records:
        sys.exit(0)

    if args.dry_run:
        log(f"estatesales_cli: --dry-run, emitting {len(records)} records, writing nothing")

    for r in records:
        r.pop("_city_for_filter", None)
        r.pop("_company_url", None)
        print(json.dumps(r, ensure_ascii=False))

    sys.exit(0)


if __name__ == "__main__":
    main()
