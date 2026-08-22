#!/usr/bin/env python3
"""
Batch sweep — run the whole prospect pipeline across every DFW city x niche.

    discover -> enrich -> push to Supabase -> audit      (per city+niche pair)

Resumable by design: every completed pair is recorded in sweep_state.json, so
an interrupted run picks up exactly where it stopped instead of re-scraping.
Nothing here needs an API key.

    python sweep.py --niches roofing                       # one niche, all cities
    python sweep.py --niches roofing,hvac --cities Dallas,Plano
    python sweep.py --tier core                            # the 10 biggest cities
    python sweep.py --no-audit                             # discover only, audit later
    python sweep.py --reset                                # forget progress, start over
    python sweep.py --status                               # what is done, what is left

Pacing: the search index throttles aggressive querying, so pairs are spaced by
PAIR_SLEEP. A full 43-city x 1-niche sweep takes a few hours — run it overnight.
"""
import argparse
import functools
import json
import pathlib
import subprocess
import sys
import time

# Windows consoles default to cp1252 and business names routinely contain
# symbols and emoji it cannot encode. Without this, printing a single
# prospect name raises UnicodeEncodeError and kills the whole run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Long runs are almost always redirected to a log. Python block-buffers a
# redirected stdout, so progress would sit invisible for hours — flush always.
print = functools.partial(print, flush=True)  # noqa: A001

HERE = pathlib.Path(__file__).resolve().parent
STATE_FILE = HERE / "sweep_state.json"

PAIR_SLEEP = 20          # seconds between city+niche pairs, be kind to the index
STEP_TIMEOUT = 2700      # per-step ceiling (45 min)

# DFW metro, biggest first. Coordinates live in audit_prospect.CITY_COORDS for
# the Maps leg; cities missing there still work, they just skip Maps enrichment.
CORE_CITIES = [
    "Dallas", "Fort Worth", "Arlington", "Plano", "Irving",
    "Garland", "Frisco", "McKinney", "Denton", "Grand Prairie",
]
DFW_CITIES = CORE_CITIES + [
    "Mesquite", "Carrollton", "Richardson", "Lewisville", "Allen",
    "Flower Mound", "Coppell", "Farmers Branch", "Grapevine", "Euless",
    "Bedford", "Hurst", "North Richland Hills", "Haltom City", "Keller",
    "Southlake", "Mansfield", "Cedar Hill", "DeSoto", "Lancaster",
    "Duncanville", "Rockwall", "Addison", "Wylie", "Murphy", "Sachse",
    "The Colony", "Little Elm", "Prosper", "Celina", "Anna", "Forney",
    "Waxahachie", "Midlothian", "Cleburne",
]

# The trades Wing Digital sells to.
DEFAULT_NICHES = ["roofing"]
KNOWN_NICHES = ["roofing", "hvac", "plumbing", "electrical", "landscaping",
                "pool service", "pest control", "general contractor"]


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"done": [], "failed": [], "stats": {}}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def run_step(args_list, label):
    """Run one pipeline step. Returns (ok, tail_of_output)."""
    try:
        r = subprocess.run([sys.executable] + args_list, cwd=str(HERE),
                           capture_output=True, text=True, timeout=STEP_TIMEOUT,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, f"{label}: timed out after {STEP_TIMEOUT}s"
    out = (r.stdout or "") + (r.stderr or "")
    tail = " | ".join(l.strip() for l in out.strip().splitlines()[-2:])
    return r.returncode == 0, f"{label}: {tail[:160]}"


def sweep_pair(niche, city, do_audit):
    """discover -> enrich -> push -> audit for one city+niche. Returns (ok, notes)."""
    notes = []

    ok, msg = run_step(["social_discover.py", "--niche", niche, "--city", city], "discover")
    notes.append(msg)
    if not ok:
        return False, notes

    # Nothing found is a valid outcome, not a failure — skip the rest cleanly.
    cand = HERE / "candidates.jsonl"
    if not cand.exists() or not cand.read_text(encoding="utf-8").strip():
        notes.append("no new prospects")
        return True, notes

    ok, msg = run_step(["enrich.py"], "enrich")
    notes.append(msg)
    if not ok:
        return False, notes

    ok, msg = run_step(["db.py", "--source", "social"], "push")
    notes.append(msg)
    if not ok:
        return False, notes

    # Count what we just pushed so the audit batch stays bounded. Without this
    # the audit re-scans every unaudited row in the DB, so each city takes
    # longer than the last until it times out.
    pushed = len([l for l in cand.read_text(encoding="utf-8").splitlines() if l.strip()])

    # Clear the staging files so the next pair starts clean and we never
    # re-push the previous city's prospects.
    cand.unlink(missing_ok=True)
    (HERE / "candidates.enriched.jsonl").unlink(missing_ok=True)

    if do_audit:
        ok, msg = run_step(
            ["audit_prospect.py", "--limit", str(max(pushed, 10))], "audit")
        notes.append(msg)
        # The prospects are already safely in Supabase at this point, so a
        # failed audit is a warning, not a lost city. `audit_prospect.py`
        # re-runs pick up anything still unaudited.
        if not ok:
            notes.append("audit incomplete — rerun audit_prospect.py later")
    return True, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niches", default=",".join(DEFAULT_NICHES),
                    help=f"comma-separated. known: {', '.join(KNOWN_NICHES)}")
    ap.add_argument("--cities", default="", help="comma-separated; default = all DFW")
    ap.add_argument("--tier", choices=["core", "all"], default="all",
                    help="core = 10 biggest cities")
    ap.add_argument("--no-audit", action="store_true", help="discover only")
    ap.add_argument("--limit-pairs", type=int, default=0, help="stop after N pairs")
    ap.add_argument("--reset", action="store_true", help="forget progress")
    ap.add_argument("--status", action="store_true", help="show progress and exit")
    args = ap.parse_args()

    if args.reset:
        STATE_FILE.unlink(missing_ok=True)
        print("Progress cleared.")
        return

    niches = [n.strip() for n in args.niches.split(",") if n.strip()]
    cities = ([c.strip() for c in args.cities.split(",") if c.strip()]
              or (CORE_CITIES if args.tier == "core" else DFW_CITIES))
    pairs = [(n, c) for n in niches for c in cities]

    state = load_state()
    done = {tuple(p) for p in state["done"]}
    todo = [p for p in pairs if p not in done]

    if args.status:
        print(f"{len(done)} pairs done, {len(todo)} remaining of {len(pairs)}")
        if state["failed"]:
            print(f"failed: {len(state['failed'])}")
            for f in state["failed"][-5:]:
                print("  -", f)
        return

    if not todo:
        print("Nothing left to sweep. Use --reset to start over.")
        return

    mins = len(todo) * (9 if not args.no_audit else 2)
    print(f"Sweeping {len(todo)} city+niche pairs "
          f"({len(done)} already done). Rough estimate: {mins//60}h {mins%60}m.")
    print("Resumable — safe to stop with Ctrl-C and rerun.\n")

    processed = 0
    try:
        for niche, city in todo:
            processed += 1
            print(f"[{processed}/{len(todo)}] {niche} in {city}")
            ok, notes = sweep_pair(niche, city, not args.no_audit)
            for n in notes:
                print(f"    {n}")
            if ok:
                state["done"].append([niche, city])
                # Clear any earlier failure for this pair — otherwise --status
                # keeps reporting cities that have since succeeded.
                state["failed"] = [f for f in state["failed"]
                                   if not f.startswith(f"{niche}/{city}:")]
            else:
                state["failed"].append(f"{niche}/{city}: {notes[-1] if notes else 'unknown'}")
            save_state(state)
            if args.limit_pairs and processed >= args.limit_pairs:
                print("\nReached --limit-pairs.")
                break
            time.sleep(PAIR_SLEEP)
    except KeyboardInterrupt:
        print("\nStopped. Progress saved — rerun to resume.")
    finally:
        save_state(state)

    print(f"\n{len(state['done'])} pairs complete, {len(state['failed'])} failed.")
    print("Review at: python queue/serve.py")


if __name__ == "__main__":
    main()
