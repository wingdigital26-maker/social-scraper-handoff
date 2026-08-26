#!/usr/bin/env python3
"""
Sonar's 24/7 cloud entrypoint — one bounded slice of work per run.

A full DFW sweep takes hours and would blow the job limit, so each run takes a
rotating slice of the city x niche matrix. The slice is chosen from the date, so
no state file has to survive between runs: day 1 takes pairs 0-3, day 2 takes
4-7, and it wraps around the matrix on its own. Verified: 225 pairs, and
--pairs 4 covers every one of them within 60 days.

Re-discovering a prospect is harmless. The Supabase table is unique on
(source, source_id), so a repeat insert is a no-op and only costs a search call.

Verified 2026-08-25 that a GitHub-hosted runner can reach the search index
(3/3), fetch prospect sites (3/3) and write to Supabase (HTTP 200). The Google
Maps leg is Windows-only and skips itself cleanly in the cloud; that enrichment
is added later by a local run.

WHY THIS EXITS NON-ZERO. This script runs ON a GitHub hosted runner, and the
search index soft-blocks datacenter IPs by answering HTTP 200 with an empty
page — measured 0.13 results/query on a hosted runner against ~3.1 from a
workstation. It surfaces as DDGSException("No results found."), which is
indistinguishable from a genuinely empty city ON ONE QUERY. So the run-level
verdict is the only honest one: a slice where nothing was discovered and
nothing was audited is a FAILURE, and the Actions run must go red. It used to
return None no matter what happened, so a fully blocked run reported success.

    python cloud_run.py --pairs 4          # discover+enrich+push, then audit
    python cloud_run.py --audit-only       # just work the unaudited backlog

Exit codes:
    0  the slice did real work
    1  a step failed, or the slice produced nothing at all
    2  the search index refused this host — discovery aborted (see
       social_discover.py, which also exits 2 for this)
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

STEP_TIMEOUT = 2700

# social_discover.py exits 2 when the index stopped answering. Every later pair
# in the slice would hit the same wall, so the slice stops instead of burning
# the remaining budget and calling the result "nothing new".
INDEX_DOWN = 2


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
    """Run one pipeline step. Returns its exit code (127 if it never ran)."""
    print(f"    -> {label}", flush=True)
    try:
        r = subprocess.run([sys.executable] + args_list, cwd=str(HERE),
                           capture_output=True, text=True, timeout=STEP_TIMEOUT,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        # Previously this escaped as a traceback and killed the whole run,
        # including the audit backlog step that had nothing to do with it.
        print(f"       FAILED: {label} timed out after {STEP_TIMEOUT}s", flush=True)
        return 127
    tail = " | ".join(l.strip() for l in (r.stdout or "").strip().splitlines()[-4:])
    print(f"       {tail[:300]}", flush=True)
    if r.returncode != 0:
        err = " | ".join(l.strip() for l in (r.stderr or "").strip().splitlines()[-2:])
        print(f"       FAILED (exit {r.returncode}): {err[:200]}", flush=True)
    return r.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=int, default=4)
    ap.add_argument("--audit-only", action="store_true")
    ap.add_argument("--audit-limit", type=int, default=60)
    args = ap.parse_args()

    problems = []
    discovered_pairs = 0     # pairs that produced at least one candidate row
    attempted_pairs = 0
    index_down = False

    if not args.audit_only:
        picked, total = todays_slice(args.pairs)
        print(f"Sonar cloud run — {len(picked)} of {total} pairs this slice\n", flush=True)
        cand = HERE / "candidates.jsonl"
        for niche, city in picked:
            print(f"  [{niche} / {city}]", flush=True)
            attempted_pairs += 1
            cand.unlink(missing_ok=True)
            (HERE / "candidates.enriched.jsonl").unlink(missing_ok=True)
            rc = run(["social_discover.py", "--niche", niche, "--city", city], "discover")
            if rc == INDEX_DOWN:
                index_down = True
                problems.append(
                    f"the search index stopped answering at [{niche} / {city}]. "
                    f"Aborting the slice — every remaining pair would read as "
                    f"'nothing new' for the same reason.")
                break
            if rc != 0:
                problems.append(f"discover failed for [{niche} / {city}] (exit {rc})")
                continue
            if not cand.exists() or not cand.read_text(encoding="utf-8").strip():
                # Honest zero: the index answered, this city just had nothing new.
                print("       nothing new (index answered, no new prospects)", flush=True)
                continue
            discovered_pairs += 1
            if run(["enrich.py"], "enrich") != 0:
                problems.append(f"enrich failed for [{niche} / {city}]")
                continue
            if run(["db.py", "--source", "social"], "push") != 0:
                problems.append(f"push to Supabase failed for [{niche} / {city}]")

    # Always work the backlog, so a discovery hiccup never starves the queue.
    print("\nAuditing unaudited backlog", flush=True)
    audit_rc = run(["audit_prospect.py", "--limit", str(args.audit_limit),
                    "--workers", "4"], "audit")
    if audit_rc != 0:
        problems.append(f"audit failed (exit {audit_rc})")

    print("\n=== run summary ===", flush=True)
    print(f"  pairs attempted : {attempted_pairs}", flush=True)
    print(f"  pairs with new prospects : {discovered_pairs}", flush=True)
    print(f"  audit exit : {audit_rc}", flush=True)

    # A discovery slice that attempted work, hit no explicit error, and still
    # produced nothing from every single pair is the exact signature of a soft
    # block. Reporting that as success is how four empty runs went unnoticed.
    if attempted_pairs and discovered_pairs == 0 and not index_down and audit_rc != 0:
        problems.append(
            f"all {attempted_pairs} pairs produced zero prospects AND the audit "
            f"failed — this run did no work at all.")

    print("\nDone. Review at queue/serve.py", flush=True)

    if problems:
        print("\n=== HARD FAILURE ===", flush=True)
        for p in problems:
            print(f"  ! {p}", flush=True)
        sys.exit(INDEX_DOWN if index_down else 1)


if __name__ == "__main__":
    main()
