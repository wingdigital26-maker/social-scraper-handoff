#!/usr/bin/env python3
"""
run_pipeline.py -- the ONE orchestrator that runs the whole Sonar ingest
pipeline for one or more clients: collect -> load -> categorize -> draft.

Every piece it calls already exists as its own independent tool. This file
does not reimplement any of them. It calls each one as a SUBPROCESS by
filename (never imports), because two of those tools (categorize_raw.py,
draft_from_leads.py) are owned by other agents and may not exist yet, or may
change shape under this file at any moment. A missing or changed downstream
tool must show up in the report as a clearly labeled SKIP, never as a crash
and never as a silent zero.

THE REPORT IS THE PRODUCT. The single thing that has gone wrong all day is
"found nothing" getting confused with "could not run". This file exists to
keep those two facts visibly separate at every stage:

    OK        ran, here is the real count (may be zero -- that is a fact)
    EMPTY     ran, definitely found nothing, source said why
    SKIPPED   could not run at all -- refused us (exit 2), missing file, or
              missing creds -- a reason is always attached, never a bare zero
    FAILED    crashed or returned a real error (exit 1 or an exception)

--dry-run is the default and propagates everywhere: to load_raw.py's
--confirm gate and to categorize_raw.py's --confirm gate. A bare run of this
file writes nothing, anywhere. Collectors are always read-only regardless.

Client configs come from ONLY the Sonar crm_clients table (slug, name,
scrape_niche, scrape_cities, scrape_terms, active). Nothing here invents a
client, a city, or a search term.

Usage:
    python run_pipeline.py                     # every active client, dry run
    python run_pipeline.py --client heros       # one client, dry run
    python run_pipeline.py --client heros --confirm   # actually write
    python run_pipeline.py --only collect       # just run the collectors
    python run_pipeline.py --only categorize --client heros --confirm
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
SUBPROCESS_KW = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")

ENV_CANDIDATES = [
    Path(r"C:\Users\wjack\wing-digital-os\.env.local"),
    HERE.parents[1] / "wing-digital-os" / ".env.local",
]

# The CLI collectors, per SOURCE-CLI-CONTRACT.md. craigslist_source.py is
# deliberately excluded: the contract itself says it is a library with a
# search() function, not a CLI, so it has no stdin/stdout contract to run
# here as a subprocess.
# takes_query says whether the client's search terms should be passed to this
# collector at all.
#
# For a KEYWORD source (a web index, Reddit) the query is how you find anything,
# so it is essential. For a DIRECTORY source it is actively harmful: the source
# itself is the signal. Every listing on estatesales.net is an estate sale, and
# an estate sale is a cleanout opportunity whether or not its text happens to
# contain the words "haul away".
#
# This was a real bug, caught 2026-08-27 by the orchestrator's own report.
# Running estatesales_cli directly returned 9 real companies with phone numbers.
# Running it through the pipeline returned ZERO, because the client's terms were
# passed through and filtered all 9 away. The report read "EMPTY", which was
# technically true and completely misleading. The best source we have was
# silently contributing nothing.
COLLECTORS = [
    ("websearch",   HERE / "websearch_cli.py",   {"takes_query": True}),
    ("estatesales", HERE / "estatesales_cli.py", {"takes_query": False}),
    ("permits",     HERE / "permits_cli.py",     {"takes_query": False}),
    ("reddit",      HERE / "reddit_cli.py",      {"takes_query": True}),
]

# ---------------------------------------------------------------------------
# COLLECTOR YIELD LEDGER
#
# The failure mode this exists to kill is the "confident zero": a collector
# that exits 0 having found nothing, so every monitor reads it as healthy.
# The per-run report already prints EMPTY/SKIPPED honestly, but nobody reads
# every run, and a source that has been dead for a fortnight looks exactly
# like a source that found nothing interesting this afternoon.
#
# So the streak is persisted. Each collector, per client, carries a count of
# consecutive runs that produced no records and the timestamp of the last run
# that did. Anything past DEAD_AFTER_RUNS is printed as a DEAD SOURCES block
# at the end of the run -- which lands in logs/pipeline.log locally and in the
# Actions log in the cloud -- and mirrored to ghl-cli/heartbeats where Da Boss
# already looks for heartbeat files.
#
# Deliberately file-based and best-effort: a ledger write must never be able
# to fail a scrape run.
# ---------------------------------------------------------------------------
YIELD_LEDGER = HERE.parent / "logs" / "collector_yield.json"
BOSS_HEARTBEATS = Path(r"C:\Users\wjack\ghl-cli\heartbeats")
DEAD_AFTER_RUNS = 3


def load_ledger() -> dict:
    try:
        return json.loads(YIELD_LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return {}


def update_ledger(ledger: dict, client_name: str, results: list) -> list[dict]:
    """Fold this client's collector results into the ledger. Returns the list
    of entries that are now dead (zero yield for DEAD_AFTER_RUNS runs)."""
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    dead = []
    for res in results:
        key = f"{client_name}::{res.name}"
        entry = ledger.setdefault(key, {
            "client": client_name, "collector": res.name,
            "zero_streak": 0, "last_yield_at": None, "last_yield_records": 0,
        })
        produced = res.status == "OK" and (res.records or 0) > 0
        entry["last_run_at"] = stamp
        entry["last_status"] = res.status
        entry["last_reason"] = (res.reason or "")[:300]
        if produced:
            entry["zero_streak"] = 0
            entry["last_yield_at"] = stamp
            entry["last_yield_records"] = res.records
        else:
            entry["zero_streak"] = int(entry.get("zero_streak", 0)) + 1
            if entry["zero_streak"] >= DEAD_AFTER_RUNS:
                dead.append(entry)
    return dead


def write_ledger(ledger: dict) -> None:
    for target in (YIELD_LEDGER, BOSS_HEARTBEATS / "scraper-yield.json"):
        try:
            if target.parent.exists() or target is YIELD_LEDGER:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(ledger, indent=2, sort_keys=True),
                                  encoding="utf-8")
        except Exception:
            pass  # a ledger write must never break a scrape run


def print_dead_sources(dead: list[dict]) -> None:
    if not dead:
        print("collector yield: no source is past its "
              f"{DEAD_AFTER_RUNS}-run zero-yield threshold.")
        return
    print("")
    print("!" * 72)
    print(f"DEAD SOURCES -- {len(dead)} collector(s) have produced ZERO records "
          f"for {DEAD_AFTER_RUNS}+ consecutive runs.")
    print("A source in this list is not 'quiet'. It is not working, or it is "
          "pointed at something that no longer exists.")
    for e in sorted(dead, key=lambda x: -x["zero_streak"]):
        last = e.get("last_yield_at") or "never, since this ledger began"
        print(f"  {e['collector']:<12} client={e['client']}")
        print(f"      zero runs in a row: {e['zero_streak']}   last produced: {last}")
        print(f"      last status: {e.get('last_status')} -- {e.get('last_reason') or 'no reason given'}")
    print(f"  ledger: {YIELD_LEDGER}")
    print("!" * 72)


CATEGORIZE_TOOL = HERE / "categorize_raw.py"
DRAFT_TOOL = HERE / "draft_from_leads.py"
LOAD_TOOL = HERE / "load_raw.py"

STAGES = ["collect", "load", "categorize", "draft"]

DEFAULT_LIMIT = 40
DEFAULT_SINCE = 21


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


def fetch_clients(url: str, key: str, slug: str | None) -> list[dict]:
    r = requests.get(
        f"{url}/rest/v1/crm_clients", timeout=30,
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        params={"active": "is.true", "select": "*"},
    )
    if not r.ok:
        sys.exit(f"Could not read crm_clients: HTTP {r.status_code} {r.text[:200]}")
    rows = r.json()
    if slug:
        rows = [c for c in rows
                if c.get("slug", "").lower() == slug.lower()
                or c.get("name", "").lower() == slug.lower()]
    return rows


def csv_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value).split(",") if x.strip()]


class SourceResult:
    def __init__(self, name):
        self.name = name
        self.status = None      # OK / EMPTY / BLOCKED / SKIPPED / FAILED
        self.reason = ""
        self.records = None     # None means "not a real count"
        self.seconds = 0.0


def run_collector(name: str, path: Path, client: dict, limit: int, since: int,
                  opts: dict | None = None) -> tuple[SourceResult, list[str]]:
    res = SourceResult(name)
    t0 = time.time()
    if not path.exists():
        res.status = "SKIPPED"
        res.reason = f"{path.name} not found on disk"
        res.seconds = time.time() - t0
        return res, []

    terms = csv_list(client.get("scrape_terms")) or csv_list(client.get("scrape_niche"))
    cities = csv_list(client.get("scrape_cities"))
    if not terms:
        res.status = "SKIPPED"
        res.reason = "client has no scrape_terms / scrape_niche configured in crm_clients"
        res.seconds = time.time() - t0
        return res, []

    opts = opts or {}
    cmd = [sys.executable, str(path),
           "--client", client.get("name", ""),
           "--limit", str(limit),
           "--since", str(since),
           "--json", "--dry-run"]
    # Only keyword sources get the client's terms. A directory source is
    # already scoped to the right kind of thing, and filtering it by keyword
    # throws away real leads. See the COLLECTORS table.
    if opts.get("takes_query", True) and terms:
        cmd += ["--query", ",".join(terms)]
    if cities:
        cmd += ["--cities", ",".join(cities)]

    try:
        proc = subprocess.run(cmd, timeout=180, **SUBPROCESS_KW)
    except FileNotFoundError:
        res.status = "SKIPPED"
        res.reason = f"could not execute {path.name}"
        res.seconds = time.time() - t0
        return res, []
    except subprocess.TimeoutExpired:
        res.status = "FAILED"
        res.reason = "timed out after 180s"
        res.seconds = time.time() - t0
        return res, []

    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    stderr_all = [ln for ln in proc.stderr.strip().splitlines() if ln.strip()]
    stderr_tail = stderr_all[-3:]
    stderr_head = stderr_all[:2]
    res.seconds = time.time() - t0

    if proc.returncode == 2:
        # exit 2 per SOURCE-CLI-CONTRACT.md: the source refused us (or, for
        # reddit, refuses to run at all without creds). This is a SKIP, not a
        # silent zero, and must never be confused with "found nothing".
        res.status = "SKIPPED"
        res.reason = ("; ".join(stderr_head) if stderr_head
                      else "source refused us / could not run (exit 2), no reason on stderr")
        return res, []
    if proc.returncode == 1:
        res.status = "FAILED"
        res.reason = "; ".join(stderr_tail) or "exit 1, no reason on stderr"
        return res, []
    if proc.returncode != 0:
        res.status = "FAILED"
        res.reason = f"unexpected exit code {proc.returncode}: " + ("; ".join(stderr_tail))
        return res, []

    res.records = len(lines)
    if res.records == 0:
        res.status = "EMPTY"
        res.reason = "; ".join(stderr_tail) or "0 results, no reason given on stderr"
    else:
        res.status = "OK"
        res.reason = "; ".join(stderr_tail)
    return res, lines


def run_load(all_lines: list[str], confirm: bool) -> SourceResult:
    res = SourceResult("load_raw")
    t0 = time.time()
    if not LOAD_TOOL.exists():
        res.status = "SKIPPED"
        res.reason = "load_raw.py not found on disk"
        res.seconds = time.time() - t0
        return res

    cmd = [sys.executable, str(LOAD_TOOL)]
    if confirm:
        cmd.append("--confirm")

    try:
        proc = subprocess.run(cmd, input="\n".join(all_lines), timeout=120, **SUBPROCESS_KW)
    except subprocess.TimeoutExpired:
        res.status = "FAILED"
        res.reason = "timed out after 120s"
        res.seconds = time.time() - t0
        return res

    res.seconds = time.time() - t0
    stderr = proc.stderr.strip()
    if proc.returncode not in (0,):
        res.status = "FAILED"
        res.reason = stderr[-400:] or f"exit {proc.returncode}"
        return res

    res.status = "OK"
    res.reason = stderr
    return res


def run_downstream_stage(label: str, path: Path, client: dict, confirm: bool,
                         extra_args: list[str]) -> SourceResult:
    res = SourceResult(label)
    t0 = time.time()
    if not path.exists():
        res.status = "SKIPPED"
        res.reason = f"{path.name} does not exist yet (owned by another in-progress build)"
        res.seconds = time.time() - t0
        return res

    cmd = [sys.executable, str(path), "--client", client.get("name", "")] + extra_args
    if confirm:
        cmd.append("--confirm")

    try:
        proc = subprocess.run(cmd, timeout=600, **SUBPROCESS_KW)
    except FileNotFoundError:
        res.status = "SKIPPED"
        res.reason = f"could not execute {path.name}"
        res.seconds = time.time() - t0
        return res
    except subprocess.TimeoutExpired:
        res.status = "FAILED"
        res.reason = "timed out after 600s"
        res.seconds = time.time() - t0
        return res

    res.seconds = time.time() - t0
    stderr = proc.stderr.strip()
    if proc.returncode != 0:
        res.status = "FAILED"
        res.reason = (stderr[-400:] or f"exit {proc.returncode}")
        return res

    res.status = "OK"
    res.reason = stderr[-600:]
    return res


def fmt_secs(s: float) -> str:
    return f"{s:.1f}s"


def print_report(client_name: str, source_results: list[SourceResult],
                 load_res: SourceResult | None,
                 categorize_res: SourceResult | None,
                 draft_res: SourceResult | None,
                 total_seconds: float, only: str | None):
    print("")
    print("=" * 72)
    print(f"CLIENT: {client_name}")
    print("=" * 72)

    if source_results:
        print("\n-- collect --")
        for r in source_results:
            count = "n/a" if r.records is None else str(r.records)
            print(f"  {r.name:<12} {r.status:<8} records={count:<6} time={fmt_secs(r.seconds)}"
                  + (f"  ({r.reason})" if r.reason else ""))
        ok = sum(1 for r in source_results if r.status == "OK")
        skipped = sum(1 for r in source_results if r.status == "SKIPPED")
        failed = sum(1 for r in source_results if r.status == "FAILED")
        empty = sum(1 for r in source_results if r.status == "EMPTY")
        print(f"  summary: {ok} ok, {empty} empty, {skipped} skipped, {failed} failed")

    if load_res:
        print("\n-- load --")
        print(f"  {load_res.status:<8} time={fmt_secs(load_res.seconds)}")
        if load_res.reason:
            for ln in load_res.reason.splitlines():
                print(f"    {ln}")

    if categorize_res:
        print("\n-- categorize --")
        print(f"  {categorize_res.status:<8} time={fmt_secs(categorize_res.seconds)}")
        if categorize_res.reason:
            for ln in categorize_res.reason.splitlines():
                print(f"    {ln}")

    if draft_res:
        print("\n-- draft --")
        print(f"  {draft_res.status:<8} time={fmt_secs(draft_res.seconds)}")
        if draft_res.reason:
            for ln in draft_res.reason.splitlines():
                print(f"    {ln}")

    print(f"\ntotal time for {client_name}: {fmt_secs(total_seconds)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the full Sonar ingest pipeline: "
                                              "collect -> load -> categorize -> draft.")
    ap.add_argument("--client", help="crm_clients slug or name. Default: every active client.")
    ap.add_argument("--only", choices=STAGES, help="run exactly one stage instead of all four.")
    ap.add_argument("--confirm", action="store_true",
                    help="actually write (load_raw --confirm, categorize_raw --confirm). "
                         "Default is --dry-run: nothing is written anywhere.")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help="max records per collector per client")
    ap.add_argument("--since", type=int, default=DEFAULT_SINCE,
                    help="freshness window in days passed to collectors")
    args = ap.parse_args()
    dry_run = not args.confirm

    print(f"[run_pipeline] mode: {'DRY RUN (writes nothing)' if dry_run else 'CONFIRM (will write)'}")
    print("[run_pipeline] categorization is the slow stage: on 2026-08-27, 45 "
          "candidates took over two minutes. Budget time accordingly.")

    url, key = load_env()
    clients = fetch_clients(url, key, args.client)
    if not clients:
        print(f"[run_pipeline] no active client matched. Nothing to run.", file=sys.stderr)
        return 1

    run_collect = args.only in (None, "collect")
    run_load_stage = args.only in (None, "load")
    run_categorize = args.only in (None, "categorize")
    run_draft = args.only in (None, "draft")

    grand_t0 = time.time()
    ledger = load_ledger()
    dead_all: list[dict] = []

    for client in clients:
        client_t0 = time.time()
        client_name = client.get("name") or client.get("slug")

        source_results: list[SourceResult] = []
        all_lines: list[str] = []
        load_res = categorize_res = draft_res = None

        if run_collect:
            for name, path, opts in COLLECTORS:
                res, lines = run_collector(name, path, client, args.limit, args.since, opts)
                source_results.append(res)
                all_lines.extend(lines)

        if run_load_stage:
            if run_collect:
                load_res = run_load(all_lines, confirm=args.confirm)
            else:
                load_res = SourceResult("load_raw")
                load_res.status = "SKIPPED"
                load_res.reason = "collect stage did not run in this invocation, nothing to load"

        # If load actually FAILED, everything after it would be judging and
        # drafting from whatever happened to already be in the table, and would
        # report cheerful OK numbers for work that has nothing to do with this
        # run. That happened for real on 2026-08-27: load failed all 89 records
        # and the report still showed categorize OK and draft OK below it.
        #
        # A run whose input never landed has not succeeded, so the later stages
        # are skipped and say why. Note this is FAILED only. A load that ran and
        # legitimately had nothing to write is not a failure and does not block.
        load_failed = load_res is not None and load_res.status == "FAILED"
        blocked_reason = (
            "skipped because the load stage FAILED, so nothing from this run "
            "reached the table. Judging now would report on stale rows and "
            "look like success."
        )

        if run_categorize:
            if load_failed:
                categorize_res = SourceResult("categorize_raw")
                categorize_res.status = "SKIPPED"
                categorize_res.reason = blocked_reason
            else:
                categorize_res = run_downstream_stage(
                    "categorize_raw", CATEGORIZE_TOOL, client, args.confirm, [])

        if run_draft:
            if load_failed:
                draft_res = SourceResult("draft_from_leads")
                draft_res.status = "SKIPPED"
                draft_res.reason = blocked_reason
            else:
                draft_res = run_downstream_stage(
                    "draft_from_leads", DRAFT_TOOL, client, args.confirm, [])

        total = time.time() - client_t0
        print_report(client_name, source_results, load_res, categorize_res,
                    draft_res, total, args.only)

        # Only a run that actually invoked the collectors is evidence about
        # them. A --only load run must not count as a zero-yield run for every
        # source, or the ledger would invent dead sources out of nothing.
        if run_collect and source_results:
            dead_all.extend(update_ledger(ledger, client_name, source_results))

    grand_total = time.time() - grand_t0
    if run_collect:
        write_ledger(ledger)
        print("")
        print_dead_sources(dead_all)
    print("")
    print("=" * 72)
    print(f"TOTAL RUN TIME: {fmt_secs(grand_total)} across {len(clients)} client(s)")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
