#!/usr/bin/env python3
"""
sweep_scale — run the working sources across MANY markets, politely, resumably.

WHY THIS EXISTS
  source_junk proved the channel: one market (dfw), 31 leads, 46 seconds, no AI
  anywhere. The gap between that and a business is arithmetic — there are 413
  US craigslist markets in the published area reference and 865 estatesales.net
  metro index pages, and source_junk had three of them typed into a dict.

  Scaling a scraper is not "call it in a for loop". Four things break at volume,
  and this module exists for exactly those four:

    1. POLITENESS. One market is ~130 requests. Four hundred markets is ~50,000.
       Getting Jack's residential IP banned would destroy the only lead lane
       that currently works, and there is no second IP. A token bucket per host
       plus exponential backoff on any 403/429/503, plus a hard stop after
       repeated blocks, is the price of admission.

    2. DURABILITY. A full sweep is hours. A run that dies at 80% and starts over
       is not a run. Every completed market is checkpointed to disk immediately;
       a restart with the same --run-id skips what is already done.

    3. DEDUPLICATION. Craigslist areas overlap heavily — a posting in Denton
       surfaces under `dal` and again under `dtn`, and estatesales metro pages
       link their neighbours' sales outright. Thousands of rows across
       overlapping markets WILL collide. Dedupe is a sqlite unique index, which
       is cheap (O(log n), on disk, survives restarts) and correct (two keys:
       the provider's own id, and a content fingerprint for the same posting
       re-listed under a different id).

    4. HONEST REPORTING. Per source, per market: attempted, raw, kept, rejected
       with reasons, geo-dropped, blocked. Zero yield on non-zero attempts is a
       HARD FAILURE with a non-zero exit, at the market level and at the sweep
       level. There is no flag that makes an empty sweep exit 0.

  Zero AI. Everything above is regex (inherited from source_junk), arithmetic
  (haversine, token buckets), and sqlite.

MEASURED RATE LIMITS  (this machine, residential IP, 2026-08-27)
  sapi.craigslist.org/web/v8/postings/search/full
      60 sequential requests with NO delay: 6.0s wall, 601 req/min, zero 403,
      zero 429, zero 503. A minority of AreaIDs from the reference feed answer
      HTTP 400 — that is a bad id, not a block, and is counted separately.
  www.craigslist.org  (posting detail HTML)
      30 sequential detail-page fetches at 0.25s spacing: 193 req/min sustained,
      30x HTTP 200, zero blocks.
  www.estatesales.net  (sale detail HTML)
      30 sequential detail-page fetches at 0.25s spacing: 114 req/min sustained,
      30x HTTP 200, zero blocks. (Slower per response, so the same spacing
      yields a lower rate.)
  NO CEILING WAS ACTUALLY FOUND on any of the three. Probing until something
  bans you is not a measurement worth taking on the only IP that works, so the
  numbers above are lower bounds: "at least this fast is fine". The defaults
  below sit under every one of them. The ceiling is where you get banned; the
  default is where you stay welcome. Override per host with --rate host=rpm.

PC-BOUND
  These endpoints answer a residential IP normally and rate-limit datacenter /
  CI ranges. Unchanged, and not solved here.

USAGE
    python sweep_scale.py --markets dal,hou,aus
    python sweep_scale.py --state TX --limit 10
    python sweep_scale.py --all --limit 50 --run-id nightly
    python sweep_scale.py --state TX --resume --run-id nightly   # continue
    python sweep_scale.py --markets dal --provider estatesales
    python sweep_scale.py --self-test                            # no network

  Output is JSONL shaped for ingest/db.py to_row():
    python sweep_scale.py --state TX --out tx.jsonl
    python db.py --in tx.jsonl --source junk
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import random
import re
import sqlite3
import sys
import threading
import time
import traceback
from urllib.parse import urlsplit

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import markets_build  # noqa: E402
import source_junk  # noqa: E402

STATE_DIR = HERE / ".sweep_scale"
DEDUPE_DB = HERE / "sweep_dedupe.sqlite"

EXIT_OK, EXIT_ZERO, EXIT_BLOCKED, EXIT_ERROR = 0, 2, 3, 4


# ===========================================================================
# Politeness
# ===========================================================================

# host -> requests per minute. Every one of these is a fraction of the measured
# ceiling. sapi answered 601/min without complaint; it is given 120. The HTML
# hosts are slower to serve and more likely to be behind a WAF, so they get 60,
# i.e. one request per second, which is the rate a human browsing fast would
# generate.
DEFAULT_RATES = {
    "sapi.craigslist.org": 120.0,
    "www.craigslist.org": 90.0,
    "www.estatesales.net": 90.0,
    "*": 60.0,
}

# Consecutive block responses from one host before the whole host is abandoned
# for the rest of the sweep. Three, not one: a single 503 is ordinary internet.
BLOCK_STRIKES = 3
BLOCK_CODES = (403, 429, 503)


class HostBucket:
    """A leaky-bucket rate limiter with multiplicative backoff.

    Not a semaphore and not a fixed sleep: a fixed sleep sends the same load
    whether or not the far end is unhappy. This one halves its own rate every
    time it sees a block code and recovers slowly, so a sweep that trips a limit
    degrades into a slow sweep rather than into a ban."""

    def __init__(self, host: str, rpm: float):
        self.host = host
        self.base_interval = 60.0 / max(0.1, rpm)
        self.interval = self.base_interval
        self.next_ok = 0.0
        self.lock = threading.Lock()
        self.requests = 0
        self.blocks = 0
        self.strikes = 0
        self.slept = 0.0
        self.dead = False
        self.max_interval_seen = self.base_interval

    def wait(self):
        if self.dead:
            raise source_junk.Blocked(
                f"{self.host} returned {BLOCK_STRIKES} consecutive block responses; "
                f"refusing to keep hitting it")
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_ok - now)
            self.next_ok = max(now, self.next_ok) + self.interval
        if delay > 0:
            self.slept += delay
            time.sleep(delay)
        self.requests += 1

    def saw(self, status):
        if status in BLOCK_CODES:
            self.blocks += 1
            self.strikes += 1
            # Back off hard and immediately; the next request is already
            # scheduled at the OLD interval, so push it out too.
            self.interval = min(30.0, self.interval * 2.0)
            self.max_interval_seen = max(self.max_interval_seen, self.interval)
            with self.lock:
                self.next_ok = max(self.next_ok, time.monotonic() + self.interval)
            if self.strikes >= BLOCK_STRIKES:
                self.dead = True
        else:
            self.strikes = 0
            if self.interval > self.base_interval:
                # Recover 10% at a time. Fast to slow down, slow to speed up.
                self.interval = max(self.base_interval, self.interval * 0.9)

    def as_dict(self):
        return {"host": self.host, "requests": self.requests, "blocks": self.blocks,
                "dead": self.dead,
                "base_rpm": round(60.0 / self.base_interval, 1),
                "final_rpm": round(60.0 / self.interval, 1),
                "worst_rpm": round(60.0 / self.max_interval_seen, 1),
                "throttle_sleep_s": round(self.slept, 1)}


class Policy:
    """Installed into source_junk via set_policy(). See the hook contract there."""

    def __init__(self, rates=None):
        self.rates = dict(DEFAULT_RATES)
        if rates:
            self.rates.update(rates)
        self.buckets: dict[str, HostBucket] = {}

    def bucket(self, url: str) -> HostBucket:
        host = (urlsplit(url).hostname or "*").lower()
        b = self.buckets.get(host)
        if b is None:
            b = HostBucket(host, self.rates.get(host, self.rates["*"]))
            self.buckets[host] = b
        return b

    def before(self, url):
        self.bucket(url).wait()

    def after(self, url, status):
        self.bucket(url).saw(status)

    def any_dead(self):
        return [h for h, b in self.buckets.items() if b.dead]

    def report(self):
        return [b.as_dict() for b in sorted(self.buckets.values(),
                                            key=lambda x: -x.requests)]


# ===========================================================================
# Dedupe
# ===========================================================================

NORM_RX = re.compile(r"[^a-z0-9]+")


def content_key(lead: dict) -> str:
    """Fingerprint for the SAME posting arriving under a different provider id.

    Title plus coordinates rounded to 3 decimals (~110m). Not the url: craigslist
    hands out a different /view/d/<slug>/<token> per subarea for the same post,
    which is the exact collision this is here to catch. Not the body: bodies get
    edited between the two times a sweep sees them."""
    t = NORM_RX.sub("", (lead.get("title") or "").lower())[:80]
    lat, lng = lead.get("lat"), lead.get("lng")
    geo = f"{round(lat, 3)},{round(lng, 3)}" if lat is not None and lng is not None \
        else (lead.get("place") or "")
    return hashlib.sha1(f"{t}|{geo}".encode("utf-8")).hexdigest()


class Dedupe:
    """sqlite unique index. Cheap and correct, and it survives a restart, which
    an in-memory set does not — that matters because resumability is worthless
    if the resumed half re-emits everything the first half already wrote."""

    def __init__(self, path=DEDUPE_DB):
        self.path = pathlib.Path(path)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute("""CREATE TABLE IF NOT EXISTS seen (
                             k TEXT PRIMARY KEY,
                             kind TEXT NOT NULL,
                             market TEXT,
                             first_seen INTEGER)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS seen_market ON seen(market)")
        self.db.commit()
        self.dup_id = 0
        self.dup_content = 0
        self.collisions: list[tuple[str, str, str]] = []

    def _claim(self, k: str, kind: str, market: str) -> tuple[bool, str | None]:
        cur = self.db.execute("SELECT market FROM seen WHERE k=?", (k,))
        row = cur.fetchone()
        if row:
            return False, row[0]
        self.db.execute("INSERT INTO seen(k,kind,market,first_seen) VALUES(?,?,?,?)",
                        (k, kind, market, int(time.time())))
        return True, None

    def accept(self, lead: dict, market: str) -> bool:
        """True if this lead is new. Both keys are claimed for a new lead so the
        second sighting collides on whichever one matches first."""
        idk = f"id:{lead.get('source')}:{lead.get('source_id')}"
        new, prev = self._claim(idk, "id", market)
        if not new:
            self.dup_id += 1
            self.collisions.append(("id", market, prev or "?"))
            return False
        ck = f"c:{content_key(lead)}"
        new, prev = self._claim(ck, "content", market)
        if not new:
            self.dup_content += 1
            self.collisions.append(("content", market, prev or "?"))
            return False
        return True

    def commit(self):
        self.db.commit()

    def total(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM seen WHERE kind='id'").fetchone()[0]

    def report(self):
        by = {}
        for kind, here, there in self.collisions:
            by[f"{there} -> {here}"] = by.get(f"{there} -> {here}", 0) + 1
        return {"duplicate_by_provider_id": self.dup_id,
                "duplicate_by_content_fingerprint": self.dup_content,
                "total_unique_ids_ever": self.total(),
                "top_overlapping_market_pairs":
                    sorted(by.items(), key=lambda kv: -kv[1])[:10]}


# ===========================================================================
# Checkpoint
# ===========================================================================

class Checkpoint:
    """One JSON file per run id, rewritten atomically after every market.

    Atomic because the failure this guards against is the process dying, and a
    half-written checkpoint would be worse than none."""

    def __init__(self, run_id: str):
        STATE_DIR.mkdir(exist_ok=True)
        self.path = STATE_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', run_id)}.json"
        self.data = {"run_id": run_id, "markets": {}, "started": None}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                print(f"  checkpoint {self.path} unreadable; starting clean",
                      file=sys.stderr)
        self.data.setdefault("markets", {})
        self.data.setdefault("started", int(time.time()))

    def done(self, key: str) -> bool:
        return key in self.data["markets"]

    def record(self, key: str, payload: dict):
        self.data["markets"][key] = payload
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def completed(self) -> list[str]:
        return list(self.data["markets"])


# ===========================================================================
# Market selection — data, never code
# ===========================================================================

def select_markets(catalog: dict, keys=None, state=None, all_=False,
                   limit=None, require_es=False, shuffle_seed=None) -> list[str]:
    if keys:
        want = [k.strip() for k in keys if k.strip()]
        missing = [k for k in want if k not in catalog]
        if missing:
            raise SystemExit(f"unknown market key(s): {missing}\n"
                             f"  list them with: python markets_build.py --show <key>")
        sel = want
    elif state:
        states = {s.strip().upper() for s in state.split(",") if s.strip()}
        sel = [k for k, m in catalog.items() if (m.get("state") or "").upper() in states]
    elif all_:
        sel = list(catalog)
    else:
        raise SystemExit("pick markets: --markets, --state, or --all")

    if require_es:
        sel = [k for k in sel if catalog[k].get("es_path")]
    sel.sort()
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(sel)
    if limit:
        sel = sel[:limit]
    return sel


# ===========================================================================
# Sweep
# ===========================================================================

def sweep(catalog, keys, providers, tiers, max_signal, max_detail,
          checkpoint: Checkpoint, dedupe: Dedupe, policy: Policy,
          out_path: pathlib.Path | None, resume: bool):
    source_junk.set_policy(policy)
    ledger = []
    out_f = out_path.open("a", encoding="utf-8") if out_path else None
    written = 0
    t_sweep = time.time()

    try:
        for i, key in enumerate(keys, 1):
            if resume and checkpoint.done(key):
                prev = checkpoint.data["markets"][key]
                print(f"[{i}/{len(keys)}] {key:12} SKIP (already done: "
                      f"{prev.get('kept', 0)} kept)", file=sys.stderr)
                ledger.append(prev)
                continue

            market = dict(catalog[key])
            market.setdefault("name", key)
            t0 = time.time()
            blocked_host = None
            try:
                leads, healths = source_junk.run(
                    market, providers=providers, tiers=tiers,
                    max_signal=max_signal, max_detail=max_detail, delay=0.0)
            except source_junk.Blocked as e:
                leads, healths, blocked_host = [], [], str(e)
                print(f"  !! {e}", file=sys.stderr)
            except Exception as e:
                # One market must never be able to kill a multi-hour sweep.
                # This is not defensive padding: the first real 27-market run
                # died at market 5 on a craigslist response whose `decode` field
                # was an int instead of an object. The bug got fixed; the
                # isolation stays, because the next shape surprise is already
                # out there in the other 400 markets.
                leads, healths = [], []
                crashed = f"{type(e).__name__}: {e}"
                print(f"  !! {key} crashed and was skipped: {crashed}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                checkpoint.record(key, {"market": key, "name": market.get("name"),
                                        "state": market.get("state"),
                                        "elapsed_s": round(time.time() - t0, 1),
                                        "returned": 0, "kept": 0, "deduped": 0,
                                        "blocked": None, "crash": crashed,
                                        "providers": [], "verdict": "ERROR"})
                ledger.append(checkpoint.data["markets"][key])
                continue

            fresh, dup = [], 0
            for l in leads:
                l["market_key"] = key
                if dedupe.accept(l, key):
                    fresh.append(l)
                else:
                    dup += 1
            dedupe.commit()

            if out_f:
                for l in fresh:
                    out_f.write(json.dumps(l, ensure_ascii=False) + "\n")
                out_f.flush()
                os.fsync(out_f.fileno())   # a checkpoint that outlives its rows is a lie
                written += len(fresh)

            entry = {
                "market": key,
                "name": market.get("name"),
                "state": market.get("state"),
                "elapsed_s": round(time.time() - t0, 1),
                "returned": len(leads),
                "kept": len(fresh),
                "deduped": dup,
                "blocked": blocked_host,
                "providers": [h.as_dict() for h in healths],
                "verdict": market_verdict(leads, healths, blocked_host),
            }
            checkpoint.record(key, entry)
            ledger.append(entry)

            by_tier = {}
            for l in fresh:
                by_tier[l["category"]] = by_tier.get(l["category"], 0) + 1
            print(f"[{i}/{len(keys)}] {key:12} {entry['verdict']:10} "
                  f"{len(fresh):3} kept  {dup:3} dup  {entry['elapsed_s']:6.1f}s  "
                  f"{by_tier}", file=sys.stderr)

            if policy.any_dead():
                print(f"\nSTOPPING EARLY: host(s) {policy.any_dead()} are blocking. "
                      f"{len(keys) - i} market(s) not attempted. Restart with "
                      f"--resume to pick up where this left off.", file=sys.stderr)
                break
    finally:
        if out_f:
            out_f.close()
        source_junk.set_policy(None)

    return ledger, written, time.time() - t_sweep


def market_verdict(leads, healths, blocked_host) -> str:
    if blocked_host:
        return "BLOCKED"
    if leads:
        return "OK"
    statuses = [h.status for h in healths if h.status != "not_run"]
    if not statuses:
        return "NOT_RUN"
    if all(s == "error" for s in statuses):
        return "ERROR"
    if any(s == "blocked" for s in statuses):
        return "BLOCKED"
    return "DRY"


def reconcile_blocked(ledger):
    """Downgrade a false BLOCKED to EMPTY using evidence only a SWEEP has.

    source_junk calls a provider "blocked" when it answered HTTP 200 across
    every query and handed back zero items, because from inside one market a
    soft block and an empty day are identical. Across a sweep they are not: if
    craigslist returned 700 items for dallas twenty minutes ago and zero for
    del rio now, craigslist is not blocking us — del rio is a town of 35,000
    with nothing posted. Reclassifying that is the difference between an honest
    ledger and a scary one.

    A market keeps its BLOCKED verdict if it actually saw a 403/429/503, or if
    NO market in the sweep got anything out of that provider.

    Mutates and returns the ledger."""
    productive = set()
    for e in ledger:
        for p in e["providers"]:
            if p["raw_results"] > 0:
                productive.add(p["provider"])
    for e in ledger:
        if e["verdict"] != "BLOCKED" or e.get("blocked"):
            continue
        hard = any(p["http_blocked"] or p["transport_errors"] for p in e["providers"])
        if hard:
            continue
        if all(p["provider"] in productive for p in e["providers"]):
            e["verdict"] = "EMPTY"
            e["verdict_note"] = (
                "reachable, HTTP 200, zero items — and the same provider(s) "
                "produced results for other markets in this sweep, so this is a "
                "genuinely empty market, not a block")
    return ledger


def sweep_exit(ledger, policy) -> tuple[int, str]:
    """Sweep-level exit contract. Mirrors source_junk.decide_exit, one level up.

    The rule that matters: attempts without yield is NEVER a success. A market
    being dry is a legitimate answer; every market being dry is a symptom."""
    ran = [e for e in ledger if e["verdict"] != "NOT_RUN"]
    if not ran:
        return EXIT_ERROR, "no market ran"
    kept = sum(e["kept"] for e in ran)
    attempts = sum(p["attempts"] for e in ran for p in e["providers"])
    if kept:
        return EXIT_OK, "ok"
    if not attempts:
        return EXIT_ERROR, "no provider issued a single request"
    if policy.any_dead() or any(e["verdict"] == "BLOCKED" for e in ran):
        return EXIT_BLOCKED, (f"{attempts} attempt(s), 0 leads, and at least one host "
                              f"was blocking. Access problem, not a demand problem — "
                              f"do not 'fix' it by loosening the gates.")
    if all(e["verdict"] == "ERROR" for e in ran):
        return EXIT_ERROR, "every market failed at the transport layer"
    return EXIT_ZERO, (f"{attempts} attempt(s) across {len(ran)} market(s) returned "
                       f"real results and 0 survived the demand gates")


# ===========================================================================
# Reporting
# ===========================================================================

def print_report(ledger, dedupe, policy, written, elapsed, out_path):
    print("\n" + "=" * 92, file=sys.stderr)
    print("VOLUME LEDGER  — per market, per provider. 'raw' is what the site "
          "handed back; 'kept' is what survived.", file=sys.stderr)
    print("=" * 92, file=sys.stderr)
    print(f"{'MARKET':12} {'VERDICT':9} {'PROVIDER':13} {'TRIED':>6} {'RAW':>6} "
          f"{'GEO-':>5} {'KEPT':>5} {'ERR':>4} {'BLK':>4}  REJECTED", file=sys.stderr)
    for e in ledger:
        for p in e["providers"]:
            rej = ", ".join(f"{k}={v}" for k, v in
                            sorted(p["rejected"].items(), key=lambda kv: -kv[1])[:4])
            print(f"{e['market']:12} {e['verdict']:9} {p['provider']:13} "
                  f"{p['attempts']:>6} {p['raw_results']:>6} {p['geo_dropped']:>5} "
                  f"{p['kept']:>5} {p['transport_errors']:>4} {p['http_blocked']:>4}  "
                  f"{rej}", file=sys.stderr)
        if not e["providers"]:
            print(f"{e['market']:12} {e['verdict']:9} {'(none ran)':13}", file=sys.stderr)

    tot_kept = sum(e["kept"] for e in ledger)
    tot_ret = sum(e["returned"] for e in ledger)
    tot_dup = sum(e["deduped"] for e in ledger)
    tot_att = sum(p["attempts"] for e in ledger for p in e["providers"])
    dry = [e["market"] for e in ledger if e["verdict"] in ("DRY", "EMPTY")]

    print("\n" + "=" * 92, file=sys.stderr)
    print("THROTTLE  (measured this run; 'worst_rpm' is how far backoff pushed it)",
          file=sys.stderr)
    for b in policy.report():
        print(f"  {json.dumps(b)}", file=sys.stderr)

    print("\nDEDUPE", file=sys.stderr)
    for k, v in dedupe.report().items():
        print(f"  {k}: {v}", file=sys.stderr)

    print("\nTOTALS", file=sys.stderr)
    print(f"  markets      : {len(ledger)}", file=sys.stderr)
    print(f"  http requests: {tot_att}", file=sys.stderr)
    print(f"  leads passed gates: {tot_ret}", file=sys.stderr)
    print(f"  duplicates dropped: {tot_dup}", file=sys.stderr)
    print(f"  NEW leads    : {tot_kept}   (whole run-id, resumed markets included)",
          file=sys.stderr)
    print(f"  written      : {written} this invocation -> {out_path}", file=sys.stderr)
    if written != tot_kept:
        print(f"                 the {tot_kept - written} difference is markets "
              f"replayed from the checkpoint; their rows were written by the "
              f"earlier invocation and are NOT re-emitted.", file=sys.stderr)
    print(f"  wall clock   : {elapsed:.0f}s "
          f"({tot_kept / (elapsed / 3600.0):.0f} new leads/hour, "
          f"{tot_att / (elapsed / 60.0):.0f} req/min sustained)"
          if elapsed > 0 else "", file=sys.stderr)
    if dry:
        print(f"\n  DRY markets ({len(dry)}) — real requests, nothing to sell there "
              f"today, not fabricated into leads:\n    {', '.join(dry[:40])}",
              file=sys.stderr)


# ===========================================================================
# Self test — no network
# ===========================================================================

def self_test() -> int:
    print("regexes (repr):")
    print(f"  NORM_RX             = {NORM_RX.pattern!r}")
    print(f"  markets_build.STATE_RX = {markets_build.STATE_RX.pattern!r}")
    print(f"  markets_build.METRO_RX = {markets_build.METRO_RX.pattern!r}")
    print(f"  source_junk.ES_LAT_RX  = {source_junk.ES_LAT_RX.pattern!r}")
    print(f"  source_junk.ES_LNG_RX  = {source_junk.ES_LNG_RX.pattern!r}\n")

    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += 0 if ok else 1
        print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")

    # --- geography ---------------------------------------------------------
    # Dallas -> Fort Worth is ~32 miles; Dallas -> Waco is ~90.
    d_fw = markets_build.haversine_mi(32.7833, -96.8000, 32.7555, -97.3308)
    d_wa = markets_build.haversine_mi(32.7833, -96.8000, 31.5456, -97.1467)
    check("haversine DFW->Fort Worth in 28..36mi", 28 < d_fw < 36, True)
    check("haversine DFW->Waco in 84..96mi", 84 < d_wa < 96, True)

    mk = {"center": [32.7833, -96.8000], "radius_mi": 35.0}
    check("Fort Worth inside 35mi market", source_junk.in_market(mk, 32.7555, -97.3308), True)
    check("Waco outside 35mi market", source_junk.in_market(mk, 31.5456, -97.1467), False)
    # The bbox corner that the radius test exists to cut off.
    check("bbox corner rejected by radius",
          source_junk.in_market(mk, 33.28, -97.39), False)
    check("no geometry keeps nothing", source_junk.in_market({}, 32.78, -96.80), False)

    # --- name join ---------------------------------------------------------
    check("dallas/fort worth joins Dallas-Fort-Worth-Arlington",
          markets_build.name_overlap("dallas / fort worth", "Dallas Fort Worth Arlington") >= 0.99, True)
    check("ft worth alias resolves",
          markets_build.name_overlap("ft worth", "Fort Worth") >= 0.99, True)
    check("cumberland valley does NOT join lehigh valley at 0.67",
          markets_build.name_overlap("cumberland valley", "Lehigh Valley") >= 0.67, False)

    # --- dedupe ------------------------------------------------------------
    tmp = HERE / ".sweep_selftest.sqlite"
    if tmp.exists():
        tmp.unlink()
    dd = Dedupe(tmp)
    a = {"source": "craigslist", "source_id": "AAA", "title": "Free hot tub",
         "lat": 32.7833, "lng": -96.8000}
    b = dict(a)                       # same id, different market
    c = dict(a, source_id="BBB")      # different id, same posting content
    d = {"source": "craigslist", "source_id": "CCC", "title": "Free piano",
         "lat": 32.7833, "lng": -96.8000}
    check("first sighting accepted", dd.accept(a, "dal"), True)
    check("same provider id rejected", dd.accept(b, "dtn"), False)
    check("same content different id rejected", dd.accept(c, "dtn"), False)
    check("genuinely different lead accepted", dd.accept(d, "dal"), True)
    check("collision counters", (dd.dup_id, dd.dup_content), (1, 1))
    dd.commit()
    dd.db.close()
    tmp.unlink()

    # --- rate limiter ------------------------------------------------------
    bk = HostBucket("example.test", 600.0)   # 0.1s interval
    t0 = time.monotonic()
    for _ in range(5):
        bk.wait()
    span = time.monotonic() - t0
    check("5 requests at 600rpm take >=0.35s", span >= 0.35, True)
    before = bk.interval
    bk.saw(429)
    check("429 doubles the interval", bk.interval >= before * 2 - 1e-9, True)
    bk.saw(429)
    bk.saw(429)
    check("three strikes kills the host", bk.dead, True)
    try:
        bk.wait()
        check("dead host raises Blocked", False, True)
    except source_junk.Blocked:
        check("dead host raises Blocked", True, True)

    bk2 = HostBucket("example.test", 60.0)
    bk2.saw(429)
    hot = bk2.interval
    for _ in range(3):
        bk2.saw(200)
    check("success recovers the rate", bk2.interval < hot, True)

    # --- exit contract -----------------------------------------------------
    ph = source_junk.Health("craigslist")
    ph.attempts, ph.raw_results = 10, 40
    dry_ledger = [{"market": "x", "verdict": "DRY", "kept": 0, "returned": 0,
                   "providers": [ph.as_dict()]}]
    code, _ = sweep_exit(dry_ledger, Policy())
    check("dry sweep exits ZERO not OK", code, EXIT_ZERO)
    ok_ledger = [{"market": "x", "verdict": "OK", "kept": 3, "returned": 3,
                  "providers": [ph.as_dict()]}]
    check("productive sweep exits OK", sweep_exit(ok_ledger, Policy())[0], EXIT_OK)
    blk = [{"market": "x", "verdict": "BLOCKED", "kept": 0, "returned": 0,
            "providers": [ph.as_dict()]}]
    check("blocked sweep exits BLOCKED", sweep_exit(blk, Policy())[0], EXIT_BLOCKED)

    # --- checkpoint --------------------------------------------------------
    cp = Checkpoint("__selftest__")
    cp.data["markets"] = {}
    cp.record("dal", {"market": "dal", "kept": 7})
    cp2 = Checkpoint("__selftest__")
    check("checkpoint survives a fresh process", cp2.done("dal"), True)
    check("checkpoint carries the count", cp2.data["markets"]["dal"]["kept"], 7)
    cp.path.unlink()

    print(f"\n{'FAIL' if fails else 'PASS'}: {fails} failing check(s)")
    return 1 if fails else 0


# ===========================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", type=pathlib.Path,
                    default=markets_build.DEFAULT_CATALOG)
    ap.add_argument("--markets", help="comma list of catalog keys")
    ap.add_argument("--state", help="comma list of two-letter states")
    ap.add_argument("--all", action="store_true", help="every market in the catalog")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--require-estatesales", action="store_true")
    ap.add_argument("--shuffle-seed", type=int,
                    help="deterministic shuffle, so a --limit run is a fair sample")
    ap.add_argument("--provider", default=",".join(source_junk.PROVIDERS))
    ap.add_argument("--tier", default=",".join(source_junk.TIERS))
    ap.add_argument("--max-signal", type=int, default=25)
    ap.add_argument("--max-detail", type=int, default=90)
    ap.add_argument("--rate", action="append", default=[],
                    help="host=rpm override, repeatable")
    ap.add_argument("--run-id", default="default")
    ap.add_argument("--resume", action="store_true",
                    help="skip markets already recorded under this --run-id")
    ap.add_argument("--fresh", action="store_true",
                    help="delete this run-id's checkpoint before starting")
    ap.add_argument("--out", type=pathlib.Path, default=HERE / "sweep_leads.jsonl")
    ap.add_argument("--dedupe-db", type=pathlib.Path, default=DEDUPE_DB)
    ap.add_argument("--reset-dedupe", action="store_true",
                    help="forget every lead ever seen (testing / a fresh campaign)")
    ap.add_argument("--ledger-out", type=pathlib.Path)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()

    catalog = markets_build.load_catalog(a.catalog)
    keys = select_markets(catalog,
                          keys=a.markets.split(",") if a.markets else None,
                          state=a.state, all_=a.all, limit=a.limit,
                          require_es=a.require_estatesales,
                          shuffle_seed=a.shuffle_seed)
    if not keys:
        print("no markets selected", file=sys.stderr)
        return EXIT_ERROR

    if a.reset_dedupe and pathlib.Path(a.dedupe_db).exists():
        pathlib.Path(a.dedupe_db).unlink()
    cp = Checkpoint(a.run_id)
    if a.fresh and cp.path.exists():
        cp.path.unlink()
        cp = Checkpoint(a.run_id)

    rates = {}
    for r in a.rate:
        host, _, rpm = r.partition("=")
        rates[host.strip()] = float(rpm)
    policy = Policy(rates)
    dedupe = Dedupe(a.dedupe_db)

    print(f"sweep_scale: {len(keys)} market(s), providers={a.provider}, "
          f"run-id={a.run_id}{' (resume)' if a.resume else ''}", file=sys.stderr)
    print(f"  rate caps: " + ", ".join(f"{h}={v:g}/min" for h, v in
                                       sorted(policy.rates.items())), file=sys.stderr)

    ledger, written, elapsed = sweep(
        catalog, keys,
        providers=[p.strip() for p in a.provider.split(",") if p.strip()],
        tiers=[t.strip() for t in a.tier.split(",") if t.strip()],
        max_signal=a.max_signal, max_detail=a.max_detail,
        checkpoint=cp, dedupe=dedupe, policy=policy,
        out_path=a.out, resume=a.resume)

    ledger = reconcile_blocked(ledger)
    print_report(ledger, dedupe, policy, written, elapsed, a.out)

    if a.ledger_out:
        a.ledger_out.write_text(json.dumps(
            {"markets": ledger, "dedupe": dedupe.report(),
             "throttle": policy.report()}, indent=1), encoding="utf-8")

    code, why = sweep_exit(ledger, policy)
    if code != EXIT_OK:
        print(f"\nHARD FAILURE (exit {code}): {why}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
