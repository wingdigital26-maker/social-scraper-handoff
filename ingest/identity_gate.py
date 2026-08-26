#!/usr/bin/env python3
"""
Identity gate — decide whether a candidate is a real, in-region business
BEFORE anything downstream treats it as a lead.

WHY THIS EXISTS. A fact-check of 24 sampled candidate rows against the live web
found 63% were outright junk. The dominant failure was not bad enrichment, it
was bad IDENTITY: the discovery layer took a DFW city name, searched it as a
keyword, and accepted whatever came back. That produced "Max Roofing, Addison"
(Addison, ILLINOIS), "QueenCity Roofing, Bedford" (New Hampshire), "Ivan
Murphy, Murphy TX" (a painter in Halifax, Nova Scotia), and a Brazilian port
operator filed under Carrollton. 46% of stored phone numbers are outside DFW.
79 LinkedIn PERSON profiles carry Google Business ratings, which is impossible
and proves the enrichment wrote to the wrong entity.

You cannot audit a company you have not correctly identified, so every other
quality fix is second-order to this one.

WHAT IT DOES. For each candidate it tries to resolve the business against the
free Google Maps scrape (the gosom binary, cached once per niche+city by
audit_prospect.maps_cache) and classifies it:

    verified       matched a real Maps business in the target metro. Website,
                   phone and rating are taken FROM THE PLACE RECORD, which is
                   the same data the prospect sees on their own dashboard.
    out_of_region  positive evidence it is somewhere else (non-DFW area code,
                   non-US domain, another state named).
    not_a_business a person profile, a trade association, a directory, a
                   newspaper, a lead-gen shell.
    unresolved     no Maps match and no disqualifying evidence. NOT the same as
                   wrong — Maps fast-mode returns a limited set per city, so
                   absence is weak evidence. These are held back from the call
                   queue but not deleted.

Deliberately NOT deleting anything. The failure mode this replaces was
confidently-wrong data; replacing it with confidently-deleted data would be the
same mistake pointing the other way.

    python identity_gate.py --dry-run --limit 40
    python identity_gate.py --city Plano --niche roofing
    python identity_gate.py            # everything unchecked
"""
import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import audit_prospect as A
from db import load_env

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Toll-free numbers are legitimate for a local business, so they are not
# evidence of being out of region either way.
TOLL_FREE = {"800", "833", "844", "855", "866", "877", "888"}

# Entities that are never a prospect for a local-services client, regardless of
# how well the name matches. Each of these was found in the live data.
NOT_A_BUSINESS = re.compile(
    r"\b(association|alliance|institute|federation|council|society|"
    r"newspaper|times|tribune|gazette|herald|journal|"
    r"supply|distributor|wholesale|manufacturer|manufacturing|"
    r"directory|listings?|marketplace|classifieds)\b", re.I)

# Lead-gen doorway shells: "<city>roofingpro.com" style, several sharing one
# phone number across different cities.
DOORWAY = re.compile(r"(roofingpro|roofing-pro|prosroofing|roofers?near|"
                     r"bestroofers?|top\d+roof)", re.I)


def classify(row, maps_recs):
    """Return (identity, reason, place_record_or_None)."""
    name = (row.get("title") or "").strip()
    url = row.get("url") or ""
    website = row.get("website") or ""
    phone = row.get("phone") or ""

    # 1. People are not businesses. The rows say so themselves.
    if "/in/" in url or (row.get("need_score") is None and row.get("audit_gaps")
                         and any("person profile" in str(g) for g in row["audit_gaps"])):
        return "not_a_business", "LinkedIn person profile, not a company", None

    # 2. Entities that exist but can never buy local marketing services.
    m = NOT_A_BUSINESS.search(name)
    if m:
        return "not_a_business", f"'{m.group(0)}' — not a local service business", None
    if website and DOORWAY.search(website):
        return "not_a_business", "lead-gen doorway domain, not a real company", None

    # 3. Positive evidence of being somewhere else. Checked BEFORE Maps,
    #    because a confident wrong match is worse than no match.
    if phone:
        digits = re.sub(r"\D", "", phone)
        if len(digits) >= 10:
            ac = digits[-10:-7]
            if ac not in A.DFW_AREA_CODES and ac not in TOLL_FREE:
                return "out_of_region", f"area code {ac} is not DFW or toll-free", None
    if website and A.FOREIGN_TLD.search(website.split("//")[-1].split("/")[0]):
        return "out_of_region", "non-US domain", None

    # 4. The authoritative check: does this business exist in the Maps scrape
    #    for its own metro? A match means the place record is the truth.
    place = A.match_business(name, maps_recs or [])
    if place:
        return "verified", f"matched Maps listing '{place.get('title')}'", place

    return ("unresolved",
            "no Google Maps listing found for this metro — not proof it is wrong, "
            "but not evidence it is right either", None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--city")
    ap.add_argument("--niche")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--recheck", action="store_true",
                    help="re-check rows already classified")
    args = ap.parse_args()

    env = load_env()
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json", "Prefer": "return=minimal"}

    params = {"select": "id,title,place_name,category,source,url,website,phone,"
                        "need_score,audit_gaps,gmb_rating,gmb_reviews",
              "order": "id.asc"}
    if not args.recheck:
        params["identity"] = "is.null"
    if args.city:
        params["place_name"] = f"eq.{args.city}"
    if args.niche:
        params["category"] = f"eq.{args.niche}"
    if args.limit:
        params["limit"] = str(args.limit)

    r = A.sb_request("GET", f"{url}/rest/v1/candidates", headers=h, params=params)
    if r is None or not r.ok:
        sys.exit("could not read candidates")
    rows = r.json()
    if not rows:
        print("Nothing to classify.")
        return

    # A candidate's "city" came from the SEARCH QUERY, not from reality — a
    # Plano roofer discovered by a Frisco query is still a Plano roofer. Match
    # against the whole metro's listings, not just one city's. Safe only
    # because the matcher now requires the DISTINCTIVE part of the name to
    # agree; on length alone, three different companies collided with "Texas
    # Roofing & Construction Inc".
    import glob, json as _json
    metro = []
    for f in glob.glob(str(pathlib.Path(__file__).resolve().parent / ".maps_cache" / "*.json")):
        try:
            metro += _json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
        except Exception:
            pass
    print(f"metro-wide Maps listings available: {len(metro)}")


    # One Maps scrape per niche+city, shared by every row in that group.
    groups = {}
    for row in rows:
        groups.setdefault((row.get("category") or "", row.get("place_name") or ""), []).append(row)

    tally = {}
    print(f"Classifying {len(rows)} candidates across {len(groups)} niche+city groups"
          f"{' (DRY RUN)' if args.dry_run else ''}\n")

    for (niche, city), group in groups.items():
        recs = (A.maps_cache(niche, city) if (niche and city) else [])
        # Own city first (best signal), then the rest of the metro.
        seen_titles = {r.get("title") for r in recs}
        recs = recs + [r for r in metro if r.get("title") not in seen_titles]
        print(f"[{niche} / {city}]  {len(group)} rows, {len(recs)} Maps listings")
        for row in group:
            identity, reason, place = classify(row, recs)
            tally[identity] = tally.get(identity, 0) + 1
            patch = {"identity": identity, "identity_reason": reason,
                     "identity_checked_at": "now()"}
            if place:
                patch["place_name_matched"] = place.get("title")
                # The place record outranks anything scraped from a snippet.
                if place.get("phone"):
                    patch["phone"] = A.clean_phone(place["phone"])
                if place.get("website"):
                    patch["website"] = place["website"].split("?")[0]
                if place.get("review_rating"):
                    patch["gmb_rating"] = place["review_rating"]
            if not args.dry_run:
                A.sb_request("PATCH", f"{url}/rest/v1/candidates",
                             headers=h, params={"id": f"eq.{row['id']}"}, json=patch)
        shown = {k: v for k, v in sorted(tally.items())}
        print(f"    running tally: {shown}")

    print("\n=== identity ===")
    for k in ("verified", "unresolved", "out_of_region", "not_a_business"):
        print(f"  {k:16} {tally.get(k, 0)}")
    print("\nOnly 'verified' rows should reach a call list. 'unresolved' means "
          "unproven, not wrong — nothing was deleted.")


if __name__ == "__main__":
    main()
