#!/usr/bin/env python3
"""
load_raw.py -- take JSONL from any source CLI and put it in leads_raw.

Every source tool (reddit_cli, craigslist, estatesales_cli, permits_cli,
websearch_cli) emits the same record shape on stdout, per SOURCE-CLI-CONTRACT.md.
This is the one place that writes them to the database, so the tools stay
credential-free and independently testable:

    python websearch_cli.py --client "Hero's Junk Removal" ... | python load_raw.py --confirm

Design notes that are deliberate, not incidental:

  * DRY RUN BY DEFAULT. A bare run parses, validates, and reports, and writes
    nothing. --confirm writes.

  * Upsert on (url, client), never delete. Re-running a collector must not
    duplicate rows, and must never destroy a judgement already written by the
    categorizer. So the update path touches ONLY the collection fields and
    leaves category, urgency, confidence, reason, quote, judged_at and
    judge_status exactly as they were.

  * posted_at is passed through as given, including null. It is NEVER defaulted
    to now(). A stale post that looks fresh is worse than one with no date at
    all: on 2026-08-27 an 18 day old cleanout job passed every filter and looked
    like a live lead precisely because nothing knew its age.

  * A record without a real url is rejected, counted, and reported. It is not
    quietly skipped. A row a human cannot open is not evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

# Credentials live in the OS repo's env file, never in this repo and never in
# the vault. Only the service key can reach leads_raw (RLS forced, see 0008).
ENV_CANDIDATES = [
    Path(r"C:\Users\wjack\wing-digital-os\.env.local"),
    Path(__file__).resolve().parents[2] / "wing-digital-os" / ".env.local",
]

COLLECTION_FIELDS = [
    "source", "platform", "url", "title", "body", "author_handle",
    "location_text", "posted_at", "event_date", "client", "query",
]


def load_env() -> tuple[str, str]:
    for p in ENV_CANDIDATES:
        if not p.exists():
            continue
        url = key = ""
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("SONAR_SUPABASE_URL="):
                url = line.split("=", 1)[1].strip()
            elif line.startswith("SONAR_SUPABASE_SERVICE_KEY="):
                key = line.split("=", 1)[1].strip()
        if url and key:
            return url, key
    sys.exit("Could not find SONAR_SUPABASE_URL / SONAR_SUPABASE_SERVICE_KEY. "
             "Looked in: " + ", ".join(str(p) for p in ENV_CANDIDATES))


def clean(rec: dict) -> dict | None:
    """Keep only contract fields. Return None if the record is unusable."""
    url = (rec.get("url") or "").strip()
    if not url.startswith("http"):
        return None
    out = {k: rec.get(k) for k in COLLECTION_FIELDS}
    out["url"] = url
    # Empty strings are not data. Anything unknown is null, per the contract.
    for k, v in list(out.items()):
        if isinstance(v, str) and not v.strip():
            out[k] = None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Load source-CLI JSONL into leads_raw.")
    ap.add_argument("--in", dest="infile", help="JSONL file. Default: stdin.")
    ap.add_argument("--confirm", action="store_true",
                    help="Actually write. Without this nothing is written.")
    ap.add_argument("--batch", type=int, default=200)
    args = ap.parse_args()

    stream = open(args.infile, encoding="utf-8") if args.infile else sys.stdin

    rows, bad_json, no_url, seen = [], 0, 0, set()
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            bad_json += 1
            continue
        c = clean(rec)
        if c is None:
            no_url += 1
            continue
        dedup = (c["url"], c.get("client"))
        if dedup in seen:
            continue
        seen.add(dedup)
        rows.append(c)

    dated = sum(1 for r in rows if r.get("posted_at") or r.get("event_date"))
    print(f"[load_raw] {len(rows)} usable records "
          f"({dated} carry a real date, {len(rows) - dated} have none)",
          file=sys.stderr)
    if bad_json:
        print(f"[load_raw] {bad_json} lines were not valid JSON and were skipped",
              file=sys.stderr)
    if no_url:
        print(f"[load_raw] {no_url} records had no usable url and were REJECTED. "
              f"A record a human cannot open is not evidence.", file=sys.stderr)

    if not rows:
        print("[load_raw] nothing to write.", file=sys.stderr)
        return 0

    if not args.confirm:
        print("[load_raw] DRY RUN. Nothing was written. Pass --confirm to write.",
              file=sys.stderr)
        for r in rows[:3]:
            print(f"    would insert: {(r.get('title') or '(no title)')[:60]}",
                  file=sys.stderr)
        return 0

    url, key = load_env()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # merge-duplicates so a re-collected row refreshes its collection fields.
        # Because the payload contains ONLY collection fields, an existing
        # judgement on that row is left untouched.
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    written = failed = 0
    for i in range(0, len(rows), args.batch):
        chunk = rows[i:i + args.batch]
        r = requests.post(f"{url}/rest/v1/leads_raw?on_conflict=url,client",
                          headers=headers, data=json.dumps(chunk), timeout=60)
        if r.ok:
            written += len(chunk)
        else:
            failed += len(chunk)
            print(f"[load_raw] batch {i // args.batch} FAILED HTTP {r.status_code}: "
                  f"{r.text[:200]}", file=sys.stderr)

    print(f"[load_raw] wrote {written}, failed {failed}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
