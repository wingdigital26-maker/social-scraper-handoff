#!/usr/bin/env python3
"""
test_sweep_scale — network-free tests for the multi-market scaling layer.

sweep_scale.py carries its own --self-test for the mechanisms (rate limiter,
dedupe, checkpoint, exit contract). This file covers the things AROUND them:
market selection, catalog integrity, the stability of the dedupe fingerprint,
and the promise that resume actually skips.

    python test_sweep_scale.py

Exits non-zero on any failure, so it can gate a run.
"""
from __future__ import annotations

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import markets_build  # noqa: E402
import source_junk  # noqa: E402
import sweep_scale  # noqa: E402

FAILS = 0


def check(label, got, want):
    global FAILS
    ok = got == want
    FAILS += 0 if ok else 1
    print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")


def section(t):
    print(t)


# A catalog stand-in. Deliberately fake: the tests must not depend on the live
# catalog file existing or on any particular city being in it.
FAKE = {
    "aaa": {"name": "alpha (TX)", "state": "TX", "cl_area": 1,
            "center": [32.0, -96.0], "radius_mi": 35.0, "es_path": "/TX/Alpha"},
    "bbb": {"name": "beta (TX)", "state": "TX", "cl_area": 2,
            "center": [32.2, -96.2], "radius_mi": 35.0, "es_path": None},
    "ccc": {"name": "gamma (OK)", "state": "OK", "cl_area": 3,
            "center": [35.0, -97.0], "radius_mi": 35.0, "es_path": "/OK/Gamma"},
}


def main() -> int:
    section("market selection (a market is DATA — no city, no client, in logic)")
    check("explicit keys", sweep_scale.select_markets(FAKE, keys=["ccc", "aaa"]),
          ["aaa", "ccc"])
    check("by state", sweep_scale.select_markets(FAKE, state="TX"), ["aaa", "bbb"])
    check("state list is case-insensitive",
          sweep_scale.select_markets(FAKE, state="tx,ok"), ["aaa", "bbb", "ccc"])
    check("--all", sweep_scale.select_markets(FAKE, all_=True), ["aaa", "bbb", "ccc"])
    check("limit truncates", sweep_scale.select_markets(FAKE, all_=True, limit=2),
          ["aaa", "bbb"])
    check("require-estatesales drops unjoined markets",
          sweep_scale.select_markets(FAKE, all_=True, require_es=True), ["aaa", "ccc"])
    check("shuffle is deterministic for a given seed",
          sweep_scale.select_markets(FAKE, all_=True, shuffle_seed=7),
          sweep_scale.select_markets(FAKE, all_=True, shuffle_seed=7))
    try:
        sweep_scale.select_markets(FAKE, keys=["nope"])
        check("unknown key is a hard error", False, True)
    except SystemExit:
        check("unknown key is a hard error", True, True)
    try:
        sweep_scale.select_markets(FAKE)
        check("no selector is a hard error", False, True)
    except SystemExit:
        check("no selector is a hard error", True, True)

    section("\ndedupe fingerprint")
    a = {"source": "craigslist", "source_id": "1", "title": "Free Hot Tub!!!",
         "lat": 32.78331, "lng": -96.80002}
    b = {"source": "craigslist", "source_id": "2", "title": "free hot tub",
         "lat": 32.78329, "lng": -96.79998}
    c = {"source": "craigslist", "source_id": "3", "title": "Free hot tub",
         "lat": 30.0, "lng": -97.0}
    check("punctuation and case do not change the fingerprint",
          sweep_scale.content_key(a), sweep_scale.content_key(b))
    check("same title 200mi away is a different lead",
          sweep_scale.content_key(a) != sweep_scale.content_key(c), True)
    no_geo = {"source": "estatesales", "source_id": "9", "title": "Estate Sale",
              "place": "Dallas, TX 75208"}
    check("a lead with no coordinates still fingerprints",
          len(sweep_scale.content_key(no_geo)), 40)

    section("\nresume must not swallow a market's own interrupted leads")
    tmp = HERE / ".test_forget.sqlite"
    if tmp.exists():
        tmp.unlink()
    dd = sweep_scale.Dedupe(tmp)
    lead = {"source": "craigslist", "source_id": "X1", "title": "Free hot tub",
            "lat": 32.0, "lng": -96.0}
    check("first pass accepts", dd.accept(lead, "abc"), True)
    check("a naive re-run would swallow it", dd.accept(lead, "abc"), False)
    n = dd.forget_market("abc")
    check("forget_market withdraws both keys", n, 2)
    check("after withdrawal the re-run keeps it", dd.accept(lead, "abc"), True)
    other = {"source": "craigslist", "source_id": "Y9", "title": "Free piano",
             "lat": 30.0, "lng": -97.0}
    dd.accept(other, "xyz")
    dd.forget_market("abc")
    check("forgetting one market leaves another alone",
          dd.accept(other, "xyz"), False)
    dd.commit()
    dd.db.close()
    tmp.unlink()

    section("\nresume semantics")
    cp = sweep_scale.Checkpoint("__test_resume__")
    cp.data["markets"] = {}
    cp.record("aaa", {"market": "aaa", "kept": 3, "verdict": "OK",
                      "returned": 3, "providers": []})
    check("recorded market reads back as done", cp.done("aaa"), True)
    check("unrecorded market is not done", cp.done("bbb"), False)
    reread = sweep_scale.Checkpoint("__test_resume__")
    check("checkpoint is durable across construction", reread.completed(), ["aaa"])
    cp.path.unlink()

    section("\nexit contract (zero yield on non-zero attempts is NEVER exit 0)")
    h = source_junk.Health("craigslist")
    h.attempts, h.raw_results, h.kept = 12, 55, 0
    empty = [{"market": "aaa", "verdict": "DRY", "kept": 0, "returned": 0,
              "providers": [h.as_dict()]}]
    check("dry -> EXIT_ZERO", sweep_scale.sweep_exit(empty, sweep_scale.Policy())[0],
          sweep_scale.EXIT_ZERO)
    h2 = source_junk.Health("craigslist")
    h2.attempts = 0
    none_ran = [{"market": "aaa", "verdict": "NOT_RUN", "kept": 0, "returned": 0,
                 "providers": [h2.as_dict()]}]
    check("nothing ran -> EXIT_ERROR",
          sweep_scale.sweep_exit(none_ran, sweep_scale.Policy())[0],
          sweep_scale.EXIT_ERROR)

    section("\nblocked-vs-empty reconciliation (evidence only a SWEEP has)")

    def prov(name, attempts=10, raw=0, blk=0, err=0):
        p = source_junk.Health(name)
        p.attempts, p.raw_results, p.http_blocked, p.transport_errors = \
            attempts, raw, blk, err
        return p.as_dict()

    led = [
        {"market": "big", "verdict": "OK", "kept": 20, "returned": 20,
         "blocked": None, "providers": [prov("craigslist", raw=700)]},
        {"market": "tiny", "verdict": "BLOCKED", "kept": 0, "returned": 0,
         "blocked": None, "providers": [prov("craigslist", raw=0)]},
        {"market": "real", "verdict": "BLOCKED", "kept": 0, "returned": 0,
         "blocked": None, "providers": [prov("craigslist", raw=0, blk=10)]},
    ]
    sweep_scale.reconcile_blocked(led)
    check("an empty market next to a productive one becomes EMPTY",
          led[1]["verdict"], "EMPTY")
    check("EMPTY carries the reasoning", bool(led[1].get("verdict_note")), True)
    check("a market that actually saw 403s stays BLOCKED", led[2]["verdict"], "BLOCKED")
    check("a productive market is untouched", led[0]["verdict"], "OK")

    all_dead = [
        {"market": "a", "verdict": "BLOCKED", "kept": 0, "returned": 0,
         "blocked": None, "providers": [prov("craigslist", raw=0)]},
        {"market": "b", "verdict": "BLOCKED", "kept": 0, "returned": 0,
         "blocked": None, "providers": [prov("craigslist", raw=0)]},
    ]
    sweep_scale.reconcile_blocked(all_dead)
    check("if NO market got anything, BLOCKED stands",
          [e["verdict"] for e in all_dead], ["BLOCKED", "BLOCKED"])

    section("\ngeography helpers")
    lo_a, hi_a, lo_o, hi_o = markets_build.bbox_from_center(32.0, -96.0, 35.0)
    # Every edge of the box must sit at least the radius away from the center,
    # or the box is clipping the circle it is supposed to pre-filter for.
    north = markets_build.haversine_mi(32.0, -96.0, hi_a, -96.0)
    south = markets_build.haversine_mi(32.0, -96.0, lo_a, -96.0)
    east = markets_build.haversine_mi(32.0, -96.0, 32.0, hi_o)
    west = markets_build.haversine_mi(32.0, -96.0, 32.0, lo_o)
    check("bbox never clips the circle it wraps",
          min(north, south, east, west) >= 34.9, True)
    check("bbox is not absurdly oversized", max(north, south, east, west) <= 40.0, True)
    mk = {"center": [32.0, -96.0], "radius_mi": 10.0}
    check("point 5mi north is in", source_junk.in_market(mk, 32.0724, -96.0), True)
    check("point 20mi north is out", source_junk.in_market(mk, 32.29, -96.0), False)
    check("missing coordinates never pass", source_junk.in_market(mk, None, None), False)

    section("\nlive catalog sanity (skipped if not built yet)")
    cat_path = markets_build.DEFAULT_CATALOG
    if not cat_path.exists():
        print("  [skip] no markets_catalog.json — run markets_build.py first")
    else:
        cat = json.loads(cat_path.read_text(encoding="utf-8"))
        check("catalog is non-trivial", len(cat) > 300, True)
        bad_area = [k for k, v in cat.items() if not isinstance(v.get("cl_area"), int)]
        check("every market has an integer craigslist area id", bad_area, [])
        bad_geo = [k for k, v in cat.items()
                   if not (v.get("center") and len(v["center"]) == 2)]
        check("every market has a center", bad_geo, [])
        bad_rad = [k for k, v in cat.items() if not (5 <= (v.get("radius_mi") or 0) <= 200)]
        check("every radius is sane", bad_rad, [])
        # The join is scored, so a low-confidence join must never have been kept.
        weak = [k for k, v in cat.items()
                if v.get("es_path") and (v.get("es_join_score") or 0) < 0.67]
        check("no weak estatesales joins survived", weak, [])
        cross = [k for k, v in cat.items() if v.get("es_path")
                 and v["es_path"].split("/")[1] != v.get("state")]
        check("no estatesales metro is joined across state lines", cross, [])
        # And nothing client-shaped leaked into the catalog keys.
        check("catalog keys are craigslist abbreviations, not client names",
              all(len(k) <= 12 for k in cat), True)

    print(f"\n{'FAILED' if FAILS else 'all tests pass'} ({FAILS} failure(s))")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
