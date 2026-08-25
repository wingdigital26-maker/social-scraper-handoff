#!/usr/bin/env python3
"""
Sonar's 24/7 cloud entrypoint — one bounded slice of work per run.

A full DFW sweep takes hours and would blow the job limit, so each run takes a
rotating slice of the city x niche matrix. The slice is chosen from the date, so
no state file has to survive between runs: day 1 takes pairs 0-3, day 2 takes
4-7, and it wraps around the matrix on its own.

Re-discovering a prospect is harmless. The Supabase table is unique on
(source, source_id), so a repeat insert is a no-op and only costs a search call.

Verified 2026-08-25 that a GitHub-hosted runner can reach the search index
(3/3), fetch prospect sites (3/3) and write to Supabase (HTTP 200). The Google
Maps leg is Windows-only and skips itself cleanly in the cloud; that enrichment
is added later by a local run.

    python cloud_run.py --pairs 4          # discover+enrich+push, then audit
    python cloud_run.py --audit-only       # just work the unaudited backlog
"""
import argparse
import datetime
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sweep as S  # reuse the city list and niches

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

NICHES = ["roofing", "hvac", "plumbing", "electrical", "landscaping"]


def matrix():
    """Every niche x city pair, in a stable order so the rotation is even."""
    return [(n, c) for n in NICHES for c in S.DFW_CITIES]


def todays_slice(size):
    pairs = matrix()
    # Day-of-epoch keeps advancing even across year boundaries.
    day = (datetime.date.today() - datetime.date(2026, 1, 1)).days
    start = (day * size) % len(pairs)
    picked = [pairs[(start + i) % len(pairs)] for i in range(size)]
    return picked, len(pairs)


def run(args_list, label):
    print(f"    -> {label}", flush=True)
    r = subprocess.run([sys.executable] + args_list, cwd=str(HERE),
                       capture_output=True, text=True, timeout=2700,
                       encoding="utf-8", errors="replace")
    tail = " | ".join(l.strip() for l in (r.stdout or "").strip().splitlines()[-2:])
    print(f"       {tail[:170]}", flush=True)
    if r.returncode != 0:
        err = " | ".join(l.strip() for l in (r.stderr or "").strip().splitlines()[-2:])
        print(f"       FAILED: {err[:170]}", flush=True)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=4)
    ap.add_argument("--audit-only", action="store_true")
    ap.add_argument("--audit-limit", type=int, default=60)
    args = ap.parse_args()

    if not args.audit_only:
        picked, total = todays_slice(args.pairs)
        print(f"Sonar cloud run — {len(picked)} of {total} pairs this slice\n", flush=True)
        cand = HERE / "candidates.jsonl"
        for niche, city in picked:
            print(f"  [{niche} / {city}]", flush=True)
            cand.unlink(missing_ok=True)
            (HERE / "candidates.enriched.jsonl").unlink(missing_ok=True)
            if not run(["social_discover.py", "--niche", niche, "--city", city], "discover"):
                continue
            if not cand.exists() or not cand.read_text(encoding="utf-8").strip():
                print("       nothing new", flush=True)
                continue
            if run(["enrich.py"], "enrich"):
                run(["db.py", "--source", "social"], "push")

    # Always work the backlog, so a discovery hiccup never starves the queue.
    print("\nAuditing unaudited backlog", flush=True)
    run(["audit_prospect.py", "--limit", str(args.audit_limit), "--workers", "4"], "audit")
    print("\nDone. Review at queue/serve.py", flush=True)


if __name__ == "__main__":
    main()
