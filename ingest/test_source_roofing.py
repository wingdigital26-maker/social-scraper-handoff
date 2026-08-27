#!/usr/bin/env python3
"""
Tests for source_roofing.

Two halves, and the split matters:

  OFFLINE (default)  Gates, geometry, tiering, row shape and exit-code logic.
  No network at all, so this half is safe in CI and is the half that must
  never be allowed to rot.

  LIVE (--live)      Actually calls NCEI SWDI, IEM and craigslist. These
  endpoints answer this machine and rate-limit CI ranges, so the live half is
  opt-in. It asserts the SHAPE of what comes back, never a specific count:
  "there was 1.5 inch hail over Plano last week" is not a stable assertion,
  but "every row carries an openable url, and no storm row claims to be a
  person" is true forever.

    python test_source_roofing.py
    python test_source_roofing.py --live

Exit 0 on pass, 1 on any failure.
"""
from __future__ import annotations

import argparse
import json
import sys

import source_roofing as S

FAILS: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [ok  ] {name}")
    else:
        FAILS.append(name)
        print(f"  [FAIL] {name}  {detail}")


# ---------------------------------------------------------------------------
# The single most important property in this file.
#
# A hail ZIP is not a person. If this ever passes when it should not, Jackson
# calls a homeowner and opens with "I hear you need a roof" to somebody who
# never said any such thing.
# ---------------------------------------------------------------------------
def test_targeting_areas_are_never_people():
    print("\ntargeting areas are never people:")
    for tier in ("storm", "storm_severe"):
        r = S._row("swdi", "1", "https://x", "t", "b", tier, [], "m",
                   is_person=False)
        check(f"{tier} row is_person is False", r["is_person"] is False)
        check(f"{tier} row lead_kind is targeting_area",
              r["lead_kind"] == "targeting_area", r["lead_kind"])
    r = S._row("craigslist", "1", "https://x", "t", "b", "ask", [], "m",
               is_person=True)
    check("ask row is_person is True", r["is_person"] is True)
    check("ask row lead_kind is person_asked", r["lead_kind"] == "person_asked")

    # The provider that emits storm rows must not be able to emit a person by
    # accident, so the wiring is asserted at the source, not just the helper.
    src = (S.provider_swdi.__doc__ or "") + open(S.__file__, encoding="utf-8").read()
    swdi_block = src.split("def provider_swdi")[1].split("def provider_lsr")[0]
    check("provider_swdi never passes is_person=True",
          "is_person=True" not in swdi_block)
    lsr_block = src.split("def provider_lsr")[1].split("def _cl_parse")[0]
    check("provider_lsr never passes is_person=True",
          "is_person=True" not in lsr_block)


def test_hail_size_gate():
    print("\nhail size gate:")
    cases = [(2.75, "storm_severe"), (1.75, "storm_severe"), (1.74, "storm"),
             (1.00, "storm"), (0.99, None), (0.25, None), (0.0, None),
             (None, None)]
    for size, want in cases:
        got, why = S.grade_hail(size)
        check(f"{size} -> {want}", got == want, f"got {got} ({why})")
    # The threshold is a parameter, but raising it must actually raise it.
    check("min_hail=1.75 rejects a 1.5in stone",
          S.grade_hail(1.5, 1.75)[0] is None)
    check("min_hail=1.75 keeps a 2.0in stone",
          S.grade_hail(2.0, 1.75)[0] == "storm_severe")


def test_supply_side_gate():
    """Every string here is verbatim from a live DFW craigslist posting read
    on 2026-08-26/27. Replying to any of them means pitching a competitor."""
    print("\nsupply-side gate (live competitor text):")
    competitors = [
        ("Roofing Services, Roof Replacements, Insurance Claims, Roof Inspection", ""),
        ("!!!!DISCOUNTED ROOFING)(K.O) THE COMPETITION- SAVE MONEY CALL US ANYTIME !!", ""),
        ("CONTRACTORS WANTED - Start With 1 Job & Get Steady Work", ""),
        ("PROPERTY PRESERVATION CONTRACTORS NEEDED - FULL REO OPPORTUNITIES", ""),
        ("Busco rooferos / Looking for roofers", ""),
        ("Roof and leak repair  tile, metal ,shingles Roofer", "We do it all."),
        ("Stucco, brick, roofing, stone repairs", "We do all types of masonry."),
        ("Fascia Replacement",
         "***Please respond with code TX75002*** We are currently seeking reliable "
         "property maintenance, handyman, property preservation, and tree trimming "
         "crews to service foreclosed properties in ALLEN, TX 75002. What We Offer: "
         "1. Same-day payment 2. Consistent work volume. Additional Work Available: "
         "Small roof repair and roof tarping."),
        ("TrashOut",
         "***Please respond with code TX75074*** We are currently seeking reliable "
         "property maintenance crews to service foreclosed properties in PLANO, TX "
         "75074. Additional Work Available: Small roof repair"),
        ("Texas Elite Gutter Solutions. Install, repairs, more. Dependable",
         "Free estimates. Licensed and insured."),
        ("DFW Building Shell Homes, Additions, Apts Dry-In  $23.50 per SF", ""),
    ]
    for title, body in competitors:
        tier, hits, why = S.classify_ask(title, body)
        check(f"rejected: {title[:52]}", tier is None, f"kept as {tier}")


def test_ask_gate_keeps_real_demand_language():
    print("\nask gate keeps homeowner language:")
    asks = [
        ("Need someone to look at my roof", "It has been leaking since the storm."),
        ("My roof is leaking", ""),
        ("Water stain on my ceiling", "Brown spot spreading in the hallway."),
        ("Shingles came off in the storm", ""),
        ("Missing shingles after last night", ""),
        ("Hail damage - should I file a claim?", "The roof took hail on Tuesday."),
        ("Looking for a roofer", "Need an honest one for a small repair."),
        ("Tree limb fell on my roof", ""),
    ]
    for title, body in asks:
        tier, hits, why = S.classify_ask(title, body)
        check(f"kept as ask: {title[:52]}", tier == "ask", f"got {tier} ({why})")


def test_ask_gate_ignores_trade_language():
    """Trade language is NOT demand language. relevance/trade_vocab encode the
    same distinction; this is the roofing instance of it."""
    print("\nask gate ignores trade language:")
    for t in ["roof replacement services", "commercial roofing solutions",
              "TPO membrane installation", "roofing contractor near me",
              "residential re-roof specialists"]:
        tier, hits, why = S.classify_ask(t, "")
        check(f"not an ask: {t}", tier is None, f"got {tier}")


def test_geo_gate():
    print("\ngeo gate:")
    m = S.MARKETS["dfw_north"]
    cases = [
        (33.0195, -96.6989, True, "Plano"),
        (33.1507, -96.8236, True, "Frisco"),
        (33.1972, -96.6398, True, "McKinney"),
        (32.9483, -96.7299, True, "Richardson"),
        # Live 2026-08-26 hail, but Tarrant County — the wrong side of the metro.
        (32.93, -97.31, False, "Haslet 1.5in LSR"),
        (32.96, -97.26, False, "Keller 1.0in LSR"),
        (29.7604, -95.3698, False, "Houston"),
        (35.83, -101.44, False, "Stinnett (panhandle)"),
        (None, None, False, "no coordinates"),
    ]
    for lat, lng, want, label in cases:
        check(f"{label} in_market={want}", S.in_market(m, lat, lng) == want)
    check("market with no geometry keeps nothing",
          S.in_market({"name": "nowhere"}, 33.0, -96.7) is False)


def test_zip_resolution():
    print("\nzip resolution:")
    m = S.MARKETS["dfw_north"]
    # Plano city hall area. Should land on a Plano-adjacent ZIP centroid.
    hits = S._zip_hits(m, 33.0195, -96.6989)
    check("a Plano point resolves to at least one ZIP", len(hits) >= 1,
          str(hits[:2]))
    if hits:
        check("nearest ZIP is within the hit radius",
              hits[0][0] <= S.ZIP_HIT_MI, str(hits[0][0]))
    check("Houston resolves to no ZIP in this market",
          S._zip_hits(m, 29.7604, -95.3698) == [])
    zips = m["zips"]
    check("every configured zip has real coordinates",
          all(isinstance(z["lat"], float) and isinstance(z["lng"], float)
              for z in zips))
    check("every configured zip is inside the market radius",
          all(S.in_market(m, z["lat"], z["lng"]) for z in zips),
          str([z["zip"] for z in zips if not S.in_market(m, z["lat"], z["lng"])]))
    check("zips are unique", len({z["zip"] for z in zips}) == len(zips))


def test_market_is_data_not_code():
    """No client name and no city list may appear in logic. The MARKETS table
    is public geography and is exempt by construction; everything below the
    table must be client-agnostic."""
    print("\nmarket is data, not code:")
    src = open(S.__file__, encoding="utf-8").read()
    body = src.split("# Vocabulary for the `ask` tier.", 1)[-1]
    lowered = body.lower()
    for banned in ["jackson", "plano", "frisco", "mckinney", "allen roofing"]:
        # Docstrings and comments in the tail are allowed to *name* the finding;
        # what must not exist is a code path keyed on one. Check executable
        # lines only.
        offenders = [ln.strip() for ln in body.splitlines()
                     if banned in ln.lower()
                     and not ln.strip().startswith("#")
                     and '"' not in ln and "'" not in ln]
        check(f"no executable line mentions {banned!r}", not offenders,
              str(offenders[:2]))
    check("MARKETS carries no client names",
          "jackson" not in json.dumps(S.MARKETS).lower())
    check("run() takes the market as an argument",
          "def run(market" in src)


def test_exit_codes():
    print("\nexit-code logic:")

    def hz(**kw):
        h = S.Health("p")
        for k, v in kw.items():
            setattr(h, k, v)
        return h

    check("rows found -> OK",
          S.decide_exit([{"x": 1}], [hz(attempts=1, raw_results=1, kept=1)])[0]
          == S.EXIT_OK)
    check("no provider ran -> ERROR",
          S.decide_exit([], [])[0] == S.EXIT_ERROR)
    check("all transport failures -> ERROR",
          S.decide_exit([], [hz(attempts=5, transport_errors=5)])[0] == S.EXIT_ERROR)
    check("403/429 -> BLOCKED",
          S.decide_exit([], [hz(attempts=5, http_blocked=2, raw_results=0)])[0]
          == S.EXIT_BLOCKED)
    # The distinction this project got wrong four times.
    check("reachable, 200, but zero raw results everywhere -> BLOCKED (not empty)",
          S.decide_exit([], [hz(attempts=8, raw_results=0)])[0] == S.EXIT_BLOCKED)
    check("real results, none survived the gates -> ZERO",
          S.decide_exit([], [hz(attempts=8, raw_results=118, kept=0)])[0]
          == S.EXIT_ZERO)
    check("ZERO is a non-zero exit", S.EXIT_ZERO != 0)
    check("BLOCKED is a non-zero exit", S.EXIT_BLOCKED != 0)
    check("no run with zero rows can exit 0",
          all(S.decide_exit([], [hz(attempts=a, raw_results=rr, kept=0,
                                    transport_errors=te, http_blocked=hb)])[0] != 0
              for a, rr, te, hb in [(1, 0, 0, 0), (9, 500, 0, 0), (3, 1, 3, 0),
                                    (4, 2, 0, 1)]))


def test_health_status():
    print("\nhealth status:")
    h = S.Health("p")
    check("no attempts -> not_run", h.status == "not_run")
    h.attempts, h.raw_results = 5, 0
    check("answered but empty on every query -> blocked", h.status == "blocked")
    h.raw_results = 100
    check("results but nothing kept -> zero_yield", h.status == "zero_yield")
    h.kept = 3
    check("results and keeps -> ok", h.status == "ok")
    h.http_blocked = 1
    check("any 403/429 outranks ok -> blocked", h.status == "blocked")
    h2 = S.Health("p")
    h2.attempts, h2.transport_errors = 5, 4
    check("majority transport failure -> error", h2.status == "error")


def test_row_shape():
    print("\nrow shape (db.py to_row contract):")
    r = S._row("swdi", "75093-x", "https://example.invalid/q", "t", "b",
               "storm", ["hail:1.50in"], "M", lat=33.0, lng=-96.8,
               place="75093", posted=1750000000, is_person=False,
               extra={"zip": "75093", "hail_in": 1.5})
    for k in ("source", "source_id", "url", "title", "desc", "place", "lat",
              "lng", "location_confidence", "category", "intent", "created_utc",
              "embeds", "market", "matched", "is_person", "lead_kind"):
        check(f"row carries {k}", k in r)
    check("category and intent agree", r["category"] == r["intent"])
    check("embeds points at the same url", r["embeds"][0]["url"] == r["url"])
    check("extra fields survive", r["hail_in"] == 1.5)
    check("row is json-serialisable", isinstance(json.dumps(r), str))


def test_tier_ordering():
    print("\ntier ordering:")
    order = {"ask": 0, "storm_severe": 1, "storm": 2}
    check("ask outranks storm_severe", order["ask"] < order["storm_severe"])
    check("storm_severe outranks storm", order["storm_severe"] < order["storm"])
    check("declared TIERS match the ordering keys",
          set(S.TIERS) == set(order))
    # A person who asked beats the biggest hailstorm on record, always.
    rows = [S._row("swdi", "1", "u", "big hail", "", "storm_severe", [], "m",
                   is_person=False, extra={"hail_in": 4.0}),
            S._row("cl", "2", "u", "my roof is leaking", "", "ask", [], "m",
                   is_person=True)]
    rows.sort(key=lambda r: (order.get(r["category"], 9),
                             -(r.get("hail_in") or 0),
                             -(r.get("created_utc") or 0)))
    check("a person sorts above 4in hail", rows[0]["category"] == "ask")


def test_no_ai():
    print("\nzero AI:")
    src = open(S.__file__, encoding="utf-8").read().lower()
    for token in ["openai", "anthropic", "llm_router", "import openai",
                  "gpt-", "claude-", "completion(", "chat.completions",
                  "generate_text", "groq", "cerebras"]:
        check(f"no {token!r} anywhere", token not in src)


# ---------------------------------------------------------------------------
# LIVE — opt-in. Asserts shape, never counts.
# ---------------------------------------------------------------------------
def test_live(market_key="dfw_north", days=180):
    print(f"\nLIVE against {market_key} ({days}d) — real network:")
    m = S.MARKETS[market_key]

    rows, healths = S.run(m, providers=["swdi"], days=days, delay=0.5)
    hh = {h.provider: h for h in healths}
    check("swdi was reachable", hh["swdi"].status != "error",
          hh["swdi"].status)
    check("swdi returned raw results", hh["swdi"].raw_results > 0,
          str(hh["swdi"].as_dict()))
    check("swdi geo gate dropped out-of-market cells",
          hh["swdi"].geo_dropped > 0)
    check("swdi size gate rejected sub-threshold hail",
          "too_small" in hh["swdi"].rejected, str(hh["swdi"].rejected))
    for r in rows:
        check_once = True
        if not (r["url"].startswith("http") and r["is_person"] is False
                and r["hail_in"] >= S.HAIL_DAMAGING
                and r["category"] in ("storm", "storm_severe")
                and r.get("zip") and r.get("storm_date")):
            check(f"malformed storm row: {r['title'][:50]}", False, json.dumps(r)[:300])
            check_once = False
        if not check_once:
            break
    else:
        check(f"all {len(rows)} swdi rows well-formed, geo-gated and non-person",
              True)
    check("no swdi row is one ZIP twice",
          len({r["zip"] for r in rows}) == len(rows))

    rows2, healths2 = S.run(m, providers=["lsr"], days=days, delay=0.5)
    h2 = healths2[0]
    check("lsr was reachable", h2.status != "error", h2.status)
    check("lsr returned raw results", h2.raw_results > 0, str(h2.as_dict()))
    check("lsr dropped non-hail report types", "not_hail" in h2.rejected)
    check("every lsr row is hail at or above threshold",
          all(r["hail_in"] >= S.HAIL_DAMAGING for r in rows2))
    check("every lsr row is a targeting area, not a person",
          all(r["is_person"] is False for r in rows2))

    # The channel this research declared dead. The assertion is deliberately
    # NOT "kept == 0" — that would break the day a real homeowner posts, which
    # is the outcome we want. What must hold is that it ran, was reachable, and
    # that anything it DID keep is a person, never a competitor.
    rows3, healths3 = S.run(m, providers=["craigslist"], max_detail=25, delay=0.4)
    h3 = healths3[0]
    check("craigslist was reachable", h3.status != "error", h3.status)
    check("craigslist returned raw results", h3.raw_results > 0, str(h3.as_dict()))
    print(f"       craigslist yield: kept={h3.kept} of {h3.raw_results} raw "
          f"(rejects: {h3.rejected})")
    check("every craigslist row is a real person",
          all(r["is_person"] is True and r["category"] == "ask" for r in rows3))
    if h3.kept == 0:
        check("craigslist zero yield is reported as a hard failure",
              S.decide_exit([], [h3])[0] != 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="also hit the real network")
    ap.add_argument("--days", type=int, default=180)
    a = ap.parse_args()

    test_targeting_areas_are_never_people()
    test_hail_size_gate()
    test_supply_side_gate()
    test_ask_gate_keeps_real_demand_language()
    test_ask_gate_ignores_trade_language()
    test_geo_gate()
    test_zip_resolution()
    test_market_is_data_not_code()
    test_exit_codes()
    test_health_status()
    test_row_shape()
    test_tier_ordering()
    test_no_ai()
    if a.live:
        test_live(days=a.days)

    print("\n" + ("-" * 60))
    if FAILS:
        print(f"{len(FAILS)} FAILURES:")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("all checks pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
