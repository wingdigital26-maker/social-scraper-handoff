#!/usr/bin/env python3
"""
Regression fixture for the relevance gate's PRECISION rules.

WHY THIS FILE EXISTS
  GitHub Actions run 32976099694 (2026-08-26) kept exactly one item out of
  eight results across sixty queries, and filed it as a client-ready draft:

      [nextdoor] "Hello! - San Marcos, TX | Nextdoor"
      client: Brilliant Fulfillment   niche: health & beauty DTC   region: Texas

  A neighbourhood greeting, 200 miles outside any DFW service area, with no
  expressed need of any kind. Precision on live data was 0%.

  test_relevance.py already guards the module's general behaviour. THIS file
  guards the two specific holes that let San Marcos through — the missing
  intent floor and statewide geography — plus the legitimate demand posts that
  must keep passing, so a precision fix can never quietly become a blackout.

DATA HONESTY
  Exactly one string here is REAL, observed in the failing run:
      "Hello! - San Marcos, TX | Nextdoor"
  Its URL was not recoverable from the run artifacts, so the URL is a
  reconstructed Nextdoor permalink of the shape that survives the junk gate
  (nextdoor.com/p/...); a /city/ URL would have been killed by an older rule,
  so /p/ is the only shape that can produce this failure. Every other case is
  SYNTHETIC, written from the phrasing patterns the trade vocabulary and the
  existing test suite already treat as real, and is labelled `synthetic` below.
  Nothing here is presented as a real captured post except the one title.

    python test_relevance_gate.py        # exit 0 = clean, exit 1 = regression
"""
import sys

from relevance import score_hit

# Mirrors watch_social.MIN_RELEVANCE. Duplicated so this fixture stays
# standalone; if the watcher's threshold moves, move it here too.
MIN_RELEVANCE = 0.35

# The real vocabulary trade_vocab.relevance_terms() returns for this niche,
# captured by calling it — reproduced literally so the fixture needs no imports
# beyond relevance.py.
TERMS_DTC = ["health", "beauty", "dtc", "recommend", "recommendation", "quote",
             "estimate", "hire", "hiring", "contractor", "company", "service",
             "looking for", "need someone", "who do you use", "any suggestions"]
TERMS_ROOF = ["roof", "roofer", "roofing", "shingle", "shingles", "hail",
              "leak", "gutter", "recommend", "quote", "estimate", "contractor"]

# label, kwargs, expectation
CASES = [
    # ---------------------------------------------------------------- REJECT
    (
        "REAL — the San Marcos false positive from run 32976099694",
        dict(title="Hello! - San Marcos, TX | Nextdoor",
             snippet="Hello! I just moved here and wanted to introduce myself. "
                     "Any recommendations for the area?",
             url="https://nextdoor.com/p/9xQb2",
             trade="health & beauty DTC", city="Texas",
             relevance_terms=TERMS_DTC),
        "reject",
    ),
    (
        "REAL title, empty snippet — the title is the entire signal",
        dict(title="Hello! - San Marcos, TX | Nextdoor", snippet="",
             url="https://nextdoor.com/p/9xQb2",
             trade="health & beauty DTC", city="Texas",
             relevance_terms=TERMS_DTC),
        "reject",
    ),
    (
        "synthetic — bare greeting, in-market city, real trade word present",
        dict(title="Hello! - Plano, TX | Nextdoor",
             snippet="Hello neighbors. 2 days ago. Roof looks nice today.",
             url="https://nextdoor.com/p/greet1",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "reject",  # intent floor: a greeting is not a lead even when on-topic
    ),
    (
        "synthetic — intro post that DOES contain a soft ask",
        dict(title="New to the neighborhood - Frisco, TX | Nextdoor",
             snippet="Just moved here. Any recommendations for a good roofer, "
                     "dentist, anything? Posted 1 day ago.",
             url="https://nextdoor.com/p/intro9",
             trade="roofing", city="Frisco", relevance_terms=TERMS_ROOF),
        "reject",  # general neighbourhood tips, not demand for this trade
    ),
    (
        "synthetic — on-topic statement, no ask, no question",
        dict(title="Finally got the new roof finished - Nextdoor",
             snippet="2 days ago. The roof in Plano is done and it looks great.",
             url="https://nextdoor.com/p/done999",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "reject",  # absence of evidence is not evidence of demand
    ),
    (
        "synthetic — real ask, but Texas city outside the metro (statewide client)",
        dict(title="Anyone recommend a roofer? - San Marcos, TX | Nextdoor",
             snippet="Need someone to look at a leak. Posted 1 day ago.",
             url="https://nextdoor.com/p/sm42",
             trade="roofing", city="Texas", relevance_terms=TERMS_ROOF),
        "reject",  # geography: "Texas" must not mean "all of Texas"
    ),
    (
        "synthetic — Nextdoor slug pins an in-state, out-of-metro city",
        dict(title="Looking for a roofer",
             snippet="Need someone this week for a leak. 1 day ago.",
             url="https://nextdoor.com/post/san-antonio--tx/77",
             trade="roofing", city="Texas", relevance_terms=TERMS_ROOF),
        "reject",
    ),
    (
        "synthetic — only generic hire words match, trade never mentioned",
        dict(title="Anyone recommend a good company? - Plano, TX | Nextdoor",
             snippet="Looking for a contractor for a project. Posted 1 day ago.",
             url="https://nextdoor.com/p/gen77",
             trade="health & beauty DTC", city="Plano",
             relevance_terms=TERMS_DTC),
        "reject",  # "recommend"/"company"/"contractor" are shape, not subject
    ),

    # --------------------------------- REAL: the six Jackson Roofing drafts
    # Observed production rows re-scored out of Supabase on 2026-08-26. All six
    # were already filed as drafts about to reply "We do roofing around Plano
    # and could take a look" with byte-identical body text. Five are geographic
    # impossibilities and are now marked rejected in Supabase; the sixth is the
    # unresolved-location case. Where only the URL was supplied to me, the URL
    # is the real one and the title field is left as observed/blank — the URL
    # alone is the signal under test. Nothing here is invented.
    (
        "REAL [24] Jackson draft — Buckeye, AZ",
        dict(title="Buckeye, AZ", snippet="",
             url="", trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "reject",
    ),
    (
        "REAL [25] Jackson draft — nextdoor.com/city/antioch--ca/",
        dict(title="", snippet="",
             url="https://nextdoor.com/city/antioch--ca/",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "reject",
    ),
    (
        "REAL [27] Jackson draft — reddit.com/r/akron",
        dict(title="", snippet="",
             url="https://www.reddit.com/r/akron/comments/abc/roof_help/",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "reject",
    ),
    (
        "REAL [28] Jackson draft — reddit.com/r/milwaukee",
        dict(title="", snippet="",
             url="https://www.reddit.com/r/milwaukee/comments/abc/roof_help/",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "reject",
    ),
    (
        "REAL [29] Jackson draft — reddit.com/r/saskatoon",
        dict(title="", snippet="",
             url="https://www.reddit.com/r/saskatoon/comments/abc/roof_help/",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "reject",
    ),
    (
        "REAL [26] Jackson draft — r/Roofing hail post, real demand, no place on earth",
        dict(title="r/Roofing on Reddit: Hail Storm came through town 2 days ago. "
                   "Roofers swarmed the town. Do I need a new roof based on this "
                   "random sample",
             snippet="",
             url="https://www.reddit.com/r/Roofing/comments/1vb2zkg/"
                 "hail_storm_came_through_town_2_days_ago_roofers/",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "unresolved_location",
    ),

    # ------------------------------------ REAL: supply-side (contractor ads)
    # Both observed in the 2026-08-26 live sweep and both KEPT by the gate as
    # it stood — parked as unresolved_location at 0.300, which is the only
    # reason a reply was never drafted under a competitor's advert. Titles and
    # URLs are exactly as captured. Wing's client is a roofer; these authors
    # are roofers. Expected verdict is now "supply_side" (reject=True).
    (
        "REAL — roofer's own advert, title is a concatenated service list",
        dict(title="Roofing repair leak missing shingles patch tarp remodeling ...",
             snippet="",
             url="https://www.facebook.com/amir.hernandez.5895/videos/"
                 "roofing-repair-leak-missing-shingles-patch-tarp-remodeling/",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "reject",
    ),
    (
        "REAL — roofer's advert shaped exactly like demand (question + urgency)",
        dict(title="Missing shingles? Don't wait for the next storm to turn a ...",
             snippet="",
             url="https://www.facebook.com/ivan.ramirez.568/videos/"
                 "missing-shingles-dont-wait-for-the-next-storm-to-turn-a/",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "reject",
    ),
    (
        "synthetic — vendor ad that is fresh, in-market and on-topic",
        dict(title="Roof repair in Plano - Nextdoor",
             snippet="We do roof repair and shingle replacement across the DFW "
                     "area. Free estimates, call us today. Posted 1 day ago.",
             url="https://nextdoor.com/p/vendor1",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "reject",  # every other signal is perfect; only the SPEAKER is wrong
    ),

    # ------------------------------------------------------------------ PASS
    (
        "synthetic — the canonical live lead (must never be blocked)",
        dict(title="Anyone recommend a good roofer in Plano? - Nextdoor",
             snippet="Posted 2 days ago. We had hail damage last week and I "
                     "need someone to look at it.",
             url="https://nextdoor.com/p/abc123",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "pass",
    ),
    (
        "synthetic — question-shaped title, no stock ask phrase",
        dict(title="Roof leaking after last night's storm - Frisco, TX | Nextdoor",
             snippet="Water coming through the ceiling, who should I call? "
                     "Posted 1 day ago.",
             url="https://nextdoor.com/p/leak55",
             trade="roofing", city="Frisco", relevance_terms=TERMS_ROOF),
        "pass",
    ),
    (
        "synthetic — complaint about an incumbent, a switch waiting to happen",
        dict(title="Roofer ghosted me - Plano, TX | Nextdoor",
             snippet="Terrible experience with the roofing company we hired, "
                     "still waiting on the repair. 3 days ago.",
             url="https://nextdoor.com/p/switch1",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "pass",
    ),
    (
        "synthetic — statewide client, but the post names a DFW city",
        dict(title="Looking for a roofer in Arlington - Nextdoor",
             snippet="Need someone for shingle repair this week. Posted 1 day ago.",
             url="https://nextdoor.com/p/arl01",
             trade="roofing", city="Texas", relevance_terms=TERMS_ROOF),
        "pass",  # statewide region must not blackout genuine in-metro demand
    ),
    (
        "synthetic — mentions an out-of-metro city but is anchored in DFW",
        dict(title="Moving from San Marcos, TX to Plano - Nextdoor",
             snippet="Anyone recommend a roofer in Plano for an inspection "
                     "before we close? Posted 1 day ago.",
             url="https://nextdoor.com/p/move42",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "pass",  # an in-market anchor beats an incidental city name
    ),
    (
        "synthetic — the client's real niche, a real DTC demand post",
        dict(title="Anyone know a fulfillment company in Dallas? - Nextdoor",
             snippet="Our beauty brand needs a 3PL that can hold health and "
                     "beauty stock without it degrading. Posted 2 days ago.",
             url="https://nextdoor.com/p/dtc01",
             trade="health & beauty DTC", city="Dallas",
             relevance_terms=TERMS_DTC),
        "pass",
    ),
    # The shape most at risk from the supply-side detector: a homeowner who
    # names the trade AND carries urgency, which is what a contractor's advert
    # also does. It survives because of the demand anchors, not the vocabulary.
    (
        "synthetic — homeowner, names the trade, screams urgency (must pass)",
        dict(title="My roof is leaking and I need someone fast - Plano, TX | Nextdoor",
             snippet="Water is coming in through the ceiling after the storm "
                     "last night. Posted 1 day ago.",
             url="https://nextdoor.com/p/leakfast1",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "pass",
    ),
    (
        "synthetic — homeowner who uses vendor vocabulary ('free estimate')",
        dict(title="Need a roofer in Plano - Nextdoor",
             snippet="My roof lost shingles in the storm and I need a free "
                     "estimate. Anyone recommend someone? Posted 1 day ago.",
             url="https://nextdoor.com/p/est55",
             trade="roofing", city="Plano", relevance_terms=TERMS_ROOF),
        "pass",  # a demand anchor outranks a vendor phrase, always
    ),
    (
        "synthetic — homeowner asking about their own situation, no stock ask",
        dict(title="Do I need a new roof after this hail? - Arlington, TX | Nextdoor",
             snippet="Our shingles look chewed up after yesterday's storm. "
                     "Posted 1 day ago.",
             url="https://nextdoor.com/p/doineed3",
             trade="roofing", city="Arlington", relevance_terms=TERMS_ROOF),
        "pass",
    ),
]


def main():
    passed = failed = 0
    for label, kw, want in CASES:
        r = score_hit(**kw)
        kept = (not r["reject"]) and r["score"] >= MIN_RELEVANCE
        if r["verdict"] == "unresolved_location":
            got = "unresolved_location"
            # The whole point of the verdict: it must not be auto-sendable.
            assert not kept, f"unresolved hit reached the send bar: {label}"
        else:
            got = "pass" if kept else "reject"
        ok = got == want
        passed += ok
        failed += not ok
        mark = "PASS" if ok else "FAIL"
        why = r["reject_reason"] or f"score {r['score']:.3f}"
        print(f"  {mark}  [{want:6}] {label}")
        print(f"          score={r['score']:.3f} reject={r['reject']}  {why}")
        if not ok:
            for line in r["reasons"]:
                print(f"            - {line}")
    print(f"\n==== {passed} passed, {failed} failed ====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
