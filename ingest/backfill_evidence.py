#!/usr/bin/env python3
"""
backfill_evidence.py -- repair outbound rows that have no evidence_url.

Jack cannot review a draft he cannot click back to. This walks every
`outbound` row with evidence_url IS NULL, tries to find the exact `leads_raw`
row it was drafted from, and copies that row's real url over.

Non-negotiables, and the reason each exists:

  * DRY RUN BY DEFAULT. A bare run prints every proposed write and touches
    nothing. --confirm writes.

  * MATCH ON STRONG SIGNALS ONLY. Same client, plus one of:
      - the outbound personalization contains the lead's verified quote
        (or vice versa) -- the quote was substring-verified against the
        source page upstream, so this is a real fingerprint;
      - the outbound recipient / subject matches the lead's title exactly.
    Fuzzy scoring is deliberately absent. A plausible-looking link is worse
    than a missing one, because a missing one is visibly missing.

  * NEVER INVENT A URL. Nothing is written that did not come out of a matched
    leads_raw row. Rows with no match, or with more than one conflicting
    match, are left NULL and reported as unmatched.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import requests

# Same env file the rest of the ingest lane reads. Values are never printed.
ENV_CANDIDATES = [
    Path(r"C:\Users\wjack\wing-digital-os\.env.local"),
    Path(__file__).resolve().parents[2] / "wing-digital-os" / ".env.local",
]

MIN_QUOTE_LEN = 25  # shorter "quotes" are titles/boilerplate and match anything


def load_env() -> tuple[str, str]:
    """Read SUPABASE_URL / SUPABASE_SERVICE_KEY from a plain KEY=VALUE file.

    The Sonar database is keyed under the SONAR_ prefix in that file; plain
    names are accepted too so this works from a real environment file that
    only holds one project.
    """
    for p in ENV_CANDIDATES:
        if not p.exists():
            continue
        vals: dict[str, str] = {}
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
        url = vals.get("SONAR_SUPABASE_URL") or vals.get("SUPABASE_URL") or ""
        key = vals.get("SONAR_SUPABASE_SERVICE_KEY") or vals.get("SUPABASE_SERVICE_KEY") or ""
        if url and key:
            return url, key
    sys.exit("Could not find SUPABASE_URL / SUPABASE_SERVICE_KEY. Looked in: "
             + ", ".join(str(p) for p in ENV_CANDIDATES))


def sb_headers(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"}


def fetch_all(url: str, key: str, table: str, params: dict) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        p = dict(params, limit=1000, offset=offset)
        r = requests.get(f"{url}/rest/v1/{table}", headers=sb_headers(key),
                         params=p, timeout=60)
        r.raise_for_status()
        chunk = r.json()
        out += chunk
        if len(chunk) < 1000:
            return out
        offset += 1000


def norm(s: str | None) -> str:
    """Lowercase, collapse whitespace, drop punctuation that quoting mangles."""
    if not s:
        return ""
    s = s.replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = re.sub(r"\s+", " ", s)
    return s.strip().strip('"').strip().lower()


def match_lead(ob: dict, leads: list[dict]) -> tuple[dict | None, str]:
    """Return (lead, reason) or (None, why-not). Strong signals only."""
    client = ob.get("client")
    pool = [l for l in leads
            if l.get("client") == client and (l.get("url") or "").startswith("http")]
    if not pool:
        return None, f"no leads_raw rows with a url for client {client!r}"

    pers = norm(ob.get("personalization"))
    body = norm(ob.get("body"))
    hits: list[tuple[dict, str]] = []

    # Signal 1: the judge-verified quote appears in the draft's own text.
    for l in pool:
        q = norm(l.get("quote"))
        if len(q) < MIN_QUOTE_LEN:
            continue
        if q in pers or q in body:
            hits.append((l, f"quote match: {q[:70]!r}"))

    # Signal 2: exact title match against recipient or subject.
    if not hits:
        recip = norm(ob.get("recipient"))
        subj = norm(ob.get("subject"))
        for l in pool:
            t = norm(l.get("title"))
            if len(t) < 8:
                continue
            if t and (t == recip or t == subj):
                hits.append((l, f"exact title match: {t[:70]!r}"))

    if not hits:
        return None, "no strong signal (no quote overlap, no exact title match)"

    urls = {l["url"] for l, _ in hits}
    if len(urls) > 1:
        return None, f"ambiguous: {len(urls)} different leads_raw urls matched, refusing to guess"
    return hits[0][0], hits[0][1]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill outbound.evidence_url from the leads_raw row each draft came from.")
    ap.add_argument("--confirm", action="store_true",
                    help="Actually write. Without this nothing is written.")
    ap.add_argument("--client", help="Restrict to one outbound.client value.")
    args = ap.parse_args()

    url, key = load_env()

    ob_params = {"select": "*", "evidence_url": "is.null"}
    if args.client:
        ob_params["client"] = f"eq.{args.client}"
    targets = fetch_all(url, key, "outbound", ob_params)
    leads = fetch_all(url, key, "leads_raw", {"select": "*"})

    print(f"[backfill_evidence] {len(targets)} outbound rows with evidence_url NULL; "
          f"{len(leads)} leads_raw rows to match against")

    proposals, unmatched = [], []
    for ob in sorted(targets, key=lambda r: r.get("id") or 0):
        lead, reason = match_lead(ob, leads)
        if lead:
            proposals.append((ob, lead, reason))
        else:
            unmatched.append((ob, reason))

    print("\n=== PROPOSED WRITES ===")
    if not proposals:
        print("  (none)")
    for ob, lead, reason in proposals:
        print(f"  outbound.id={ob['id']} <- leads_raw.id={lead['id']}")
        print(f"    url:    {lead['url']}")
        print(f"    reason: {reason}")

    print(f"\n=== UNMATCHED (left NULL): {len(unmatched)} ===")
    for ob, reason in unmatched:
        print(f"  outbound.id={ob['id']} client={ob.get('client')!r} "
              f"recipient={str(ob.get('recipient'))[:40]!r}")
        print(f"    {reason}")

    print(f"\n[backfill_evidence] matched={len(proposals)} unmatched={len(unmatched)}")

    if not args.confirm:
        print("[backfill_evidence] DRY RUN. Nothing was written. Pass --confirm to write.")
        return 0

    written = 0
    for ob, lead, _ in proposals:
        r = requests.patch(f"{url}/rest/v1/outbound",
                           headers={**sb_headers(key), "Prefer": "return=minimal"},
                           params={"id": f"eq.{ob['id']}"},
                           json={"evidence_url": lead["url"]}, timeout=30)
        if r.ok:
            written += 1
        else:
            print(f"  FAILED id={ob['id']} HTTP {r.status_code}: {r.text[:200]}")
    print(f"[backfill_evidence] wrote {written} of {len(proposals)}; "
          f"{len(unmatched)} rows remain NULL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
