#!/usr/bin/env python3
"""
Promote reviewed Reddit candidates into a publishable spots file.

Review model (deliberately simple + safe):
  1. reddit_ingest.py writes candidates.jsonl (one candidate per line).
  2. YOU review: open candidates.jsonl and delete any junk lines, OR rely on
     the --min-conf / --min-upvotes gates below. Auto-collected spots always
     carry needs_review + legal_status:"unverified" so nothing pretends to be
     hand-vetted.
  3. promote.py assigns ids, dedupes, and writes ingested-spots.js
     (window.INGESTED_SPOTS) — the app loads it just like the other spot files.

Usage:
    python promote.py                       # promote all candidates that pass gates
    python promote.py --min-conf 0.9        # only exactly-located spots
    python promote.py --min-upvotes 50      # only well-loved posts

The output is a static JS file; wiring it into the live app (adding one
<script> tag + a spread in data.js) is a one-line follow-up we do together,
so a bad batch can never silently hit production.
"""
import json, re, argparse, pathlib, sys
import config as C

# Windows consoles default to cp1252 and spot names routinely contain symbols
# it cannot encode; without this a single print kills the run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = pathlib.Path(__file__).resolve().parent
ID_START = 1000  # ingested spots live in their own id range, above hand-curated ones


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def num(v):
    """Coerce to float, or None. A missing/None/garbage value is UNKNOWN, which
    is not the same as zero — returning 0 here would silently pass a row through
    a gate it was never measured against."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def coords(c):
    """(lat, lng) if this candidate is genuinely geocoded, else None.

    social_discover.py emits lat/lng as None for every prospect it finds (the
    city comes from the search query, not from geocoding). promote.py used to
    do round(c["lat"], 2) unguarded, which raised KeyError/TypeError and killed
    the entire batch on the first such row."""
    lat, lng = num(c.get("lat")), num(c.get("lng"))
    if lat is None or lng is None:
        return None
    return lat, lng


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-conf", type=float, default=0.5)
    ap.add_argument("--min-upvotes", type=int, default=C.MIN_UPVOTES)
    ap.add_argument("--out", default="ingested-spots.js")
    args = ap.parse_args()

    cand_path = HERE / C.CANDIDATES_FILE
    if not cand_path.exists():
        raise SystemExit(f"No {cand_path} yet — run reddit_ingest.py first.")

    seen_keys, spots = set(), []
    total = passed = 0
    # Every rejection is counted and named. Nothing is deleted and nothing is
    # dropped silently — an unresolved row is unproven, not junk.
    rej = {"unparseable": 0, "low_confidence": 0, "low_upvotes": 0,
           "unresolved_location": 0, "missing_fields": 0, "duplicate": 0}
    for line in cand_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        total += 1
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            rej["unparseable"] += 1
            continue

        conf = num(c.get("location_confidence"))
        if conf is None or conf < args.min_conf:
            rej["low_confidence"] += 1
            continue
        ups = num(c.get("upvotes"))
        if ups is None or ups < args.min_upvotes:
            rej["low_upvotes"] += 1
            continue

        latlng = coords(c)
        if latlng is None:
            # Real demand with no resolvable location: parked for a human, not
            # discarded. It stays in candidates.jsonl untouched.
            rej["unresolved_location"] += 1
            continue
        if not c.get("name") or not c.get("cat") or c.get("desc") is None:
            rej["missing_fields"] += 1
            continue

        key = (norm(c["name"])[:24], round(latlng[0], 2), round(latlng[1], 2))
        if key in seen_keys:
            rej["duplicate"] += 1
            # same spot from another post -> attach its embed instead of duplicating
            for s in spots:
                if (norm(s["name"])[:24], round(s["lat"], 2), round(s["lng"], 2)) == key:
                    s["embeds"].extend(c.get("embeds", []))
                    break
            continue
        seen_keys.add(key)
        spots.append({
            "id": ID_START + len(spots),
            "name": c["name"],
            "cat": c["cat"],
            "lat": latlng[0],
            "lng": latlng[1],
            "zip": c.get("zip", ""),
            "desc": c["desc"],
            "tags": c.get("tags", []),
            "danger": 2,                       # neutral default until reviewed
            "rating": None,
            "reviews": [],
            "embeds": c.get("embeds", []),
            "needs_review": True,
            "legal_status": c.get("legal_status", "unverified"),
            "source_author": c.get("author"),
        })
        passed += 1

    breakdown = ", ".join(f"{k} {v}" for k, v in rej.items() if v) or "none"

    # --- zero-yield gate ----------------------------------------------------
    # House rule: zero yield with non-zero attempts is a hard failure. Writing
    # the file anyway would also blank an existing published ingested-spots.js,
    # replacing real data with an empty array. Refuse instead.
    out_path = HERE / args.out
    if not spots:
        raise SystemExit(
            f"FAIL: read {total} candidate(s) and promoted 0.\n"
            f"  rejected: {breakdown}\n"
            f"  gates: conf>={args.min_conf}, upvotes>={args.min_upvotes}\n"
            f"  {out_path} left untouched (writing an empty file would destroy published spots)."
        )

    js = ("// AUTO-GENERATED by promote.py from Reddit candidates. Do not hand-edit.\n"
          "// Every spot here is auto-collected and UNVERIFIED — review before trusting.\n"
          f"// Region: {C.REGION_NAME}. Count: {len(spots)}.\n"
          "window.INGESTED_SPOTS = " + json.dumps(spots, ensure_ascii=False, indent=1) + ";\n")
    out_path.write_text(js, encoding="utf-8")

    print(f"Read {total} candidates -> promoted {len(spots)} unique spots "
          f"(conf>={args.min_conf}, upvotes>={args.min_upvotes}).")
    print(f"  rejected: {breakdown}")
    if rej["unresolved_location"]:
        print(f"  NOTE: {rej['unresolved_location']} candidate(s) have no resolvable lat/lng. "
              f"They are unproven, not junk — left in {cand_path.name} for review.")
    print(f"Wrote {out_path}")
    print("To go live: add <script src=\"ingest/ingested-spots.js\"></script> and spread\n"
          "  ...(window.INGESTED_SPOTS || [])  into SEED_SPOTS in data.js. We do that together.")


if __name__ == "__main__":
    main()
