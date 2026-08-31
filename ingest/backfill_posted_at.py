#!/usr/bin/env python3
"""
backfill_posted_at.py -- fill in the REAL publication date on leads_raw rows
that were collected without one.

WHY THIS EXISTS
  Every actionable lead collected through the web-search index landed with
  posted_at = NULL, because a search index does not hand back a post date.
  Display and sorting then fell back to collected_at, whose median age is about
  two days -- so a Reddit thread from 2017 rendered as a lead collected two days
  ago. Jack's words: "it's got good leads, but they're two years old."

  Nothing was lying about any individual field. The rows were honestly null.
  The damage was done by the FALLBACK: an unknown date presented as a fresh one.

WHAT IT DOES
  For each actionable row (category consumer_lead or partner) with a null
  posted_at, it opens the post's own page and reads the real publication date,
  then patches only that one column.

  Reddit dating uses the single verified implementation in websearch_cli
  (old.reddit.com HTML, anchored to the post's own t3_ container). There is no
  second copy of that logic here, so the collector and the backfill can never
  drift apart and disagree about what a post's date is.

HARD RULES
  * DRY RUN BY DEFAULT. --confirm writes.
  * Only ever writes posted_at. Never touches a judgement, never touches
    collected_at, never deletes or reorders anything.
  * NEVER invents a date. A row whose date cannot be read stays null and is
    reported as unresolved with the specific reason.
  * Rate limited. This walks other people's servers.

    python backfill_posted_at.py                # dry run, shows what it found
    python backfill_posted_at.py --confirm      # write the dates
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from websearch_cli import DATE_FETCH_SLEEP, resolve_posted_at  # noqa: E402

ENV_CANDIDATES = [
    Path(r"C:\Users\wjack\wing-digital-os\.env.local"),
    Path(__file__).resolve().parents[2] / "wing-digital-os" / ".env.local",
]

ACTIONABLE_CATEGORIES = ("consumer_lead", "partner")


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


def sb_headers(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"}


def fetch_rows(url: str, key: str) -> list[dict]:
    params = {
        "select": "id,source,platform,url,title,category,posted_at,event_date,"
                  "collected_at,client",
        "category": "in.(" + ",".join(ACTIONABLE_CATEGORIES) + ")",
        "limit": "2000",
    }
    r = requests.get(f"{url}/rest/v1/leads_raw", headers=sb_headers(key),
                     params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def patch_posted_at(url: str, key: str, row_id, iso: str) -> tuple[bool, str]:
    r = requests.patch(
        f"{url}/rest/v1/leads_raw",
        headers={**sb_headers(key), "Prefer": "return=minimal"},
        params={"id": f"eq.{row_id}"},
        json={"posted_at": iso},
        timeout=30,
    )
    if r.ok:
        return True, ""
    return False, f"HTTP {r.status_code}: {r.text[:160]}"


def parse_iso(value) -> datetime | None:
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_days(value, now: datetime) -> float | None:
    dt = parse_iso(value)
    if dt is None:
        return None
    return (now - dt).total_seconds() / 86400.0


def effective_date(row: dict):
    """The date that actually describes WHEN this lead is about.

    posted_at first. event_date second, because an estate sale listing's
    operative date is the sale, not when the page went up. collected_at is
    deliberately NOT in this chain -- collection time is when WE looked, not
    when the thing happened, and conflating the two is the whole bug.
    """
    return row.get("posted_at") or row.get("event_date")


def print_distribution(rows: list[dict], now: datetime, label: str) -> None:
    ages, unknown = [], 0
    for r in rows:
        a = age_days(effective_date(r), now)
        if a is None:
            unknown += 1
        else:
            ages.append(a)

    print(f"\n=== TRUE age distribution: {label} ===")
    print(f"  actionable rows          : {len(rows)}")
    print(f"  with a real date         : {len(ages)}")
    print(f"  age UNKNOWN (no date)    : {unknown}")
    if not ages:
        print("  (no dated rows to summarize)")
        return
    ages.sort()
    print(f"  newest (min age)         : {ages[0]:>8.1f} days")
    print(f"  median age               : {statistics.median(ages):>8.1f} days")
    print(f"  oldest (max age)         : {ages[-1]:>8.1f} days")
    print(f"  mean age                 : {statistics.mean(ages):>8.1f} days")
    print("  --")
    for cut in (30, 90, 365):
        n = sum(1 for a in ages if a < cut)
        pct = 100.0 * n / len(rows)
        print(f"  under {cut:>4} days           : {n:>4} of {len(rows)} actionable ({pct:.1f}%)")
    n_old = sum(1 for a in ages if a >= 365)
    print(f"  365 days or older        : {n_old:>4} of {len(rows)} actionable "
          f"({100.0 * n_old / len(rows):.1f}%)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill real publication dates onto actionable leads_raw rows.")
    ap.add_argument("--confirm", action="store_true",
                    help="Actually write. Without this nothing is written.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap how many rows to attempt this run.")
    ap.add_argument("--sleep", type=float, default=DATE_FETCH_SLEEP,
                    help="Seconds between page fetches. Be polite.")
    args = ap.parse_args()

    url, key = load_env()
    now = datetime.now(timezone.utc)

    rows = fetch_rows(url, key)
    print(f"[backfill] {len(rows)} actionable rows (category in "
          f"{ACTIONABLE_CATEGORIES})")

    have_posted = [r for r in rows if r.get("posted_at")]
    print(f"[backfill] posted_at populated BEFORE : {len(have_posted)} / {len(rows)}")
    print(f"[backfill] event_date populated       : "
          f"{sum(1 for r in rows if r.get('event_date'))} / {len(rows)}")

    print_distribution(rows, now, "BEFORE backfill")

    targets = [r for r in rows if not r.get("posted_at")]
    if args.limit:
        targets = targets[:args.limit]
    print(f"\n[backfill] {len(targets)} rows have a null posted_at and will be "
          f"attempted")

    session = requests.Session()
    resolved, unresolved, patched, patch_failed = [], [], 0, 0
    reasons: dict[str, int] = {}

    for i, row in enumerate(targets, 1):
        iso, outcome = resolve_posted_at(row.get("url"), row.get("platform"),
                                         session=session)
        reasons[outcome] = reasons.get(outcome, 0) + 1
        if iso:
            a = age_days(iso, now)
            resolved.append((row, iso, a))
            print(f"  [{i}/{len(targets)}] OK   {iso}  ({a:6.0f}d old)  "
                  f"{(row.get('title') or '')[:52]}")
            if args.confirm:
                good, err = patch_posted_at(url, key, row.get("id"), iso)
                if good:
                    patched += 1
                else:
                    patch_failed += 1
                    print(f"      PATCH FAILED id={row.get('id')}: {err}")
            # Reflect locally so the AFTER distribution is computed from the
            # same values that were just written.
            row["posted_at"] = iso
        else:
            unresolved.append((row, outcome))
            print(f"  [{i}/{len(targets)}] --   unresolved ({outcome})  "
                  f"{(row.get('title') or '')[:52]}")
        time.sleep(args.sleep)

    print(f"\n=== resolution summary ===")
    print(f"  attempted   : {len(targets)}")
    print(f"  resolved    : {len(resolved)}")
    print(f"  unresolved  : {len(unresolved)}")
    for outcome, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"      {n:>4}  {outcome}")

    if unresolved:
        print(f"\n  These rows keep posted_at = NULL. They are NOT fresh and "
              f"they are NOT stale -- their age is unknown, and downstream "
              f"must treat them that way:")
        for row, outcome in unresolved[:20]:
            print(f"      id={row.get('id')} [{outcome}] {row.get('url')}")
        if len(unresolved) > 20:
            print(f"      ... and {len(unresolved) - 20} more")

    if args.confirm:
        print(f"\n[backfill] WROTE posted_at on {patched} rows "
              f"({patch_failed} patch failures)")
    else:
        print(f"\n[backfill] DRY RUN. Nothing was written. "
              f"Pass --confirm to write {len(resolved)} dates.")

    print_distribution(rows, now,
                       "AFTER backfill" if args.confirm else
                       "AFTER backfill (PROJECTED -- dry run, nothing written)")

    return 1 if patch_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
