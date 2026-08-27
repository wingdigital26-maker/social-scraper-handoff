#!/usr/bin/env python3
"""Tests for source_junk. No network.

Two things are worth testing here and nothing else is:

  1. The gates. Every fixture string is copied VERBATIM from a live DFW posting
     read on 2026-08-26 — including the ones that must be rejected, because the
     rejections are what the old watcher got wrong. If a phrasing is not in a
     real post it does not belong in this file.
  2. The failure accounting. The previous scraper in this project exited 0 with
     0 rows four separate times before anyone noticed. Every path that produces
     no leads must produce a distinct non-zero exit code, and "blocked" must be
     distinguishable from "genuinely nothing" from "error".

    python test_source_junk.py
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import source_junk as SJ  # noqa: E402

FAILS: list[str] = []


def check(name, got, want):
    if got == want:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}: got {got!r} want {want!r}")


# ---------------------------------------------------------------------------
# 1. Gates
# ---------------------------------------------------------------------------
# Real people paying a real person with a truck. The money tier.
HIRE = [
    ("Junk Removal Needed – $125 Melissa TX",
     "Junk removal needed from a garage. Everything needs to be hauled away. "
     "Must have: Pickup truck or box truck."),
    ("HELP NEEDED – GARAGE CLEANOUT 10am 08/09/2026",
     "Looking for someone to help move items out of a garage and haul trash to a "
     "dumpster. Must have your own truck – mandatory."),
    ("Junk/Trash Haul Needed - Forth Worth, TX",
     "We are looking for a reliable and professional individual or service to assist "
     "with the removal of trash from a property."),
]

# Dated cleanout events.
EVENT = [
    ("Free Estate Sale Leftovers",
     "Estate sale leftovers - child's car seat, picture frames, pots, pillows, "
     "large mirror, and more."),
    ("vintage couch in good cond",
     "vintage couch in good cond Free Free Free moving sale please txt / e-mail. "
     "lots of house hold items and furnitures, garden tools, painting tools."),
    ("Estate/ Going Out Of Business Of Eagle Solder and Stain Glass Company", ""),
]

# Bulky giveaways by someone decluttering. Marketing-list rows, not jobs.
SIGNAL = [
    ("Free hot tub!!! Come get it asap", ""),
    ("Electric lift chair",
     "A working lift chair. Cushion is not torn but worn. At the curb."),
    ("FREE HENRY MILLER UPRIGHT PIANO - MUST PICK UP TODAY", ""),
    ("FREE PICK UP ASAP WASHER AND DRYER AND BED", ""),
    ("Free Office Cubicles – 18 Sets Available (Plano)",
     "We are offering 18 office cubicle sets free of charge due to an upcoming "
     "office relocation."),
]

# Must be rejected. These are the exact shapes that poisoned the old runs.
REJECT = [
    # A competitor recruiting drivers. Says "junk removal" in the title, which
    # is why a single-keyword gate keeps it.
    ("Curbside Junk Removal Jobs - $50+ a load, Instant Approval",
     "Got a truck? We run a curbside junk pickup platform across multiple cities. "
     "More jobs come in than we can handle — we need drivers.", "supply_side"),
    ("🏌️ NOW HIRING | Caddy Moving – Join the Best Crew in the Game", "", "supply_side"),
    ("Make $365/ OR $20 HR/ Moving General labor. work today get paid today!",
     "MOVING JOB TODAY IN PLANO, TEXAS. A Truck IS NEEDED.", "supply_side"),
    # Decluttering language, nothing bulky. Worth nothing to a hauler.
    ("Free moving boxes", "Some used, some unused. In front yard at the tree.", "not_bulky"),
    ("Free Moving Boxes", "giving away 3 large flat boxes for TV's or artwork.", "not_bulky"),
    ("Free moving packing materials need to go",
     "We have a lot of moving packing materials which needs to go. Big and large "
     "Boxes, bubble wraps and plastic wraps.", "not_bulky"),
    # No demand language at all.
    ("Free red semi-gloss interior paint",
     "Not sure why we have this red paint left over. Free to anyone who can use it.",
     "no_demand_language"),
    ("Decorative Statue for outdoor/backyard",
     "Heavy, decorative, beautiful statue with lights.", "no_demand_language"),
]

print("gate: hire")
for t, b in HIRE:
    check(t[:52], SJ.classify(t, b)[0], "hire")
print("gate: event")
for t, b in EVENT:
    check(t[:52], SJ.classify(t, b)[0], "event")
print("gate: signal")
for t, b in SIGNAL:
    check(t[:52], SJ.classify(t, b)[0], "signal")
print("gate: reject (with reason)")
for t, b, why in REJECT:
    tier, _hits, reason = SJ.classify(t, b)
    check(t[:52], (tier, (reason or "").split(":")[0]), (None, why))

# Supply wins over demand even when the demand phrasing is a perfect match.
# This ordering is the whole reason the gate is not a bag of keywords.
print("gate: supply beats demand")
check("competitor saying the magic words",
      SJ.classify("Junk Removal Needed", "We offer same day service, free estimate, "
                                         "licensed and insured. Call us today.")[0], None)

# ---------------------------------------------------------------------------
# 2. Geography
# ---------------------------------------------------------------------------
print("geo")
DFW = SJ.MARKETS["dfw"]["bbox"]
check("fort worth in bbox", SJ._in_bbox(32.7705, -97.3077, DFW), True)
check("austin not in bbox", SJ._in_bbox(30.2672, -97.7431, DFW), False)
check("tulsa not in bbox", SJ._in_bbox(36.15, -95.99, DFW), False)
check("missing coords excluded", SJ._in_bbox(None, None, DFW), False)


# ---------------------------------------------------------------------------
# 3. Failure accounting — the part that stops a silent empty run
# ---------------------------------------------------------------------------
def health(provider="p", **kw):
    h = SJ.Health(provider)
    for k, v in kw.items():
        setattr(h, k, v)
    return h


print("health status")
check("never ran", health().status, "not_run")
check("transport dead", health(attempts=10, transport_errors=10).status, "error")
check("http 403", health(attempts=10, raw_results=0, http_blocked=4).status, "blocked")
# The one that matters: 200 OK on every query, zero items on every query. That
# is not a quiet day, that is a soft block, and it must not read as "nothing".
check("200s but zero items everywhere", health(attempts=10, raw_results=0).status, "blocked")
check("results but all gated out", health(attempts=10, raw_results=400).status, "zero_yield")
check("leads found", health(attempts=10, raw_results=400, kept=7).status, "ok")

print("exit codes")
lead = [{"category": "hire"}]
check("leads -> 0", SJ.decide_exit(lead, [health(attempts=1, raw_results=1, kept=1)])[0], SJ.EXIT_OK)
check("no provider ran -> ERROR", SJ.decide_exit([], [health()])[0], SJ.EXIT_ERROR)
check("all transport dead -> ERROR",
      SJ.decide_exit([], [health(attempts=5, transport_errors=5)])[0], SJ.EXIT_ERROR)
check("any blocked -> BLOCKED",
      SJ.decide_exit([], [health(attempts=5, raw_results=0),
                          health("q", attempts=5, raw_results=90)])[0], SJ.EXIT_BLOCKED)
check("real results, nothing kept -> ZERO",
      SJ.decide_exit([], [health(attempts=5, raw_results=90)])[0], SJ.EXIT_ZERO)
check("every failure code is non-zero",
      all(c != 0 for c in (SJ.EXIT_ZERO, SJ.EXIT_BLOCKED, SJ.EXIT_ERROR)), True)

# ---------------------------------------------------------------------------
# 4. Craigslist decoding — every lead must carry an openable URL
# ---------------------------------------------------------------------------
print("craigslist decode")
FAKE = {
    "decode": {"minPostingId": 7924888552},
    "items": [
        [24018424, 3905285, 133, -1, "1:1~32.7705~-97.3077", 0, -2,
         [13, "hh7zEDqMD3WJPkwRYdJj15"], [6, "fort-worth-free-hot-tub"], [10, "free"],
         "Free hot tub!!! Come get it asap"],
        # No canonical token -> unusable, must be dropped rather than emitted
        # with a URL nobody can open.
        [1, 2, 3, -1, "1:1~32.7~-97.3", 0, [6, "no-token"], "Orphan"],
    ],
}
got = SJ._cl_parse(FAKE)
check("drops tokenless item", len(got), 1)
check("url is the canonical /view/d/ form", got[0]["url"],
      "https://www.craigslist.org/view/d/fort-worth-free-hot-tub/hh7zEDqMD3WJPkwRYdJj15")
check("lat decoded", got[0]["lat"], 32.7705)
check("lng decoded", got[0]["lng"], -97.3077)

print("candidate shape")
row = SJ._candidate("craigslist", "abc", got[0]["url"], got[0]["title"], "body",
                    "signal", ["hot tub"], "DFW", lat=32.77, lng=-97.30)
for f in ("source", "source_id", "url", "title", "desc", "lat", "lng", "category", "embeds"):
    check(f"has {f}", f in row, True)
check("url is openable http", row["url"].startswith("https://"), True)

# ---------------------------------------------------------------------------
print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all tests pass")
