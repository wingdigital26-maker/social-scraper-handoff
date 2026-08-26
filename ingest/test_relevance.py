#!/usr/bin/env python3
"""
Offline tests for the relevance gate. No network, no AI, no DB.

Every string below is written to look like what a real
`site:nextdoor.com` / `site:reddit.com` search actually returns — titles with
the platform suffix, snippets with the relative-age stamp, business pages with
their review counts.

    python test_relevance.py
"""
import datetime as _dt
import sys

from relevance import score_hit

TODAY = _dt.date(2026, 8, 25)          # frozen so results are deterministic
TRADE = "roofing"
CITY = "Plano"
TERMS = ["roof", "roofer", "roofing", "shingle", "shingles", "hail",
         "gutter", "leak", "re-roof", "storm damage"]

PASS, FAIL = 0, 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}   {detail}")


def show(name, r):
    print(f"\n--- {name}")
    print(f"    score={r['score']}  reject={r['reject']}  "
          f"reason={r['reject_reason']}")
    print(f"    components={r['components']}")
    for x in r["reasons"]:
        print(f"      . {x}")


# ---------------------------------------------------------------- 1. real lead
good = score_hit(
    "Anyone recommend a roofer in Plano? - Nextdoor",
    "3 days ago — We took hail last week and I'm seeing shingles in the yard. "
    "Looking for someone reputable to come out and look at the roof. TIA neighbors.",
    "https://nextdoor.com/p/9fK2mQ",
    TRADE, CITY, TERMS, now=TODAY)
show("genuine ask, Plano, 3 days old", good)
check("genuine lead not rejected", good["reject"] is False, good["reject_reason"] or "")
check("genuine lead scores high (>=0.85)", good["score"] >= 0.85, f"got {good['score']}")

# --------------------------------------------------- 2. business page (URL)
biz_url = score_hit(
    "Pinnacle Roofing & Restoration - Plano, TX - Nextdoor",
    "Verified by Nextdoor. 214 recommendations from neighbors. Licensed and insured. "
    "Call us for a free estimate on your roof replacement.",
    "https://nextdoor.com/pages/pinnacle-roofing-restoration-plano-tx",
    TRADE, CITY, TERMS, now=TODAY)
show("Nextdoor business page (company URL)", biz_url)
check("business page rejected", biz_url["reject"] is True)
check("business reject_reason mentions business page",
      "business page" in (biz_url["reject_reason"] or "").lower(),
      biz_url["reject_reason"])

# ------------------------------------- 2b. business page with a neutral URL
biz_txt = score_hit(
    "Lone Star Roof Pros | Plano roofing contractor",
    "Family owned since 2004. We offer full roof replacement and repair. "
    "Free estimate, licensed and insured, financing available. 4.9 stars, 312 reviews.",
    "https://nextdoor.com/p/ZZtop77",
    TRADE, CITY, TERMS, now=TODAY)
show("business advertising, ordinary post URL", biz_txt)
check("ad-copy business post rejected", biz_txt["reject"] is True)
check("ad-copy reject_reason is human readable",
      bool(biz_txt["reject_reason"]) and len(biz_txt["reject_reason"]) > 20,
      biz_txt["reject_reason"])

# ------------------------------------------------------------ 3. out of state
oos = score_hit(
    "Anyone know a good roofer? : r/Seattle",
    "2 days ago — Need someone to look at a leak in Seattle, Washington before "
    "the rain gets worse. Any recommendations for a roofing company?",
    "https://www.reddit.com/r/Seattle/comments/1abcdef/anyone_know_a_good_roofer/",
    TRADE, CITY, TERMS, now=TODAY)
show("out-of-state ask (Seattle, WA)", oos)
check("out-of-state rejected", oos["reject"] is True)
check("out-of-state reason names the market problem",
      "out of market" in (oos["reject_reason"] or "").lower(), oos["reject_reason"])

# ---------------------------------------------------------------- 4. listicle
listicle = score_hit(
    "10 Best Roofing Companies in Plano, TX (2026 Reviews)",
    "Our editors ranked the top 10 roofers in Plano based on price, warranty and "
    "customer reviews. Compare quotes and find the best roofing contractor near you.",
    "https://www.example-homeguide.com/blog/best-roofers-plano-tx",
    TRADE, CITY, TERMS, now=TODAY)
show("aggregator listicle", listicle)
check("listicle rejected", listicle["reject"] is True)
check("listicle reason says listicle/aggregator",
      "listicle" in (listicle["reject_reason"] or "").lower()
      or "aggregator" in (listicle["reject_reason"] or "").lower(),
      listicle["reject_reason"])

# ------------------------------------------------------------ 5. no date at all
nodate = score_hit(
    "Looking for a roofer in Frisco - Nextdoor",
    "My roof has a leak over the garage and I need someone to take a look. "
    "Who have you all used?",
    "https://nextdoor.com/p/Q7wm10",
    TRADE, CITY, TERMS, now=TODAY)
show("dateless but otherwise good post", nodate)
check("dateless post not rejected", nodate["reject"] is False, nodate["reject_reason"] or "")
check("dateless recency is exactly neutral 0.5",
      nodate["components"]["recency"] == 0.5, str(nodate["components"]))
check("dateless post records the unknown in reasons",
      any("no date found" in r for r in nodate["reasons"]))
check("dateless scores below the dated lead",
      nodate["score"] < good["score"], f"{nodate['score']} vs {good['score']}")

# ------------------------------------------------------ 6. stale dated thread
stale = score_hit(
    "Anyone recommend a roofer in Plano? - Nextdoor",
    "Mar 4, 2019 — Storm last night, shingles everywhere, looking for someone "
    "who can come out this week.",
    "https://nextdoor.com/p/oldthread2019",
    TRADE, CITY, TERMS, now=TODAY)
show("same ask, but from 2019", stale)
check("dead 2019 thread rejected", stale["reject"] is True)
check("dead-thread reason names the date and the age",
      "2019-03-04" in (stale["reject_reason"] or ""), stale["reject_reason"])

# 6b. aging but not dead — should survive, ranked low
aging = score_hit(
    "Anyone recommend a roofer in Plano? - Nextdoor",
    "Jan 12, 2026 — Roof is leaking after the last storm, looking for someone "
    "who can come take a look at the shingles.",
    "https://nextdoor.com/p/aging26",
    TRADE, CITY, TERMS, now=TODAY)
show("same ask, ~7 months old", aging)
check("aging thread not rejected", aging["reject"] is False, aging["reject_reason"] or "")
check("aging thread scores well below the fresh one",
      aging["score"] < good["score"] - 0.2, f"{aging['score']} vs {good['score']}")

# ------------------------------------------------------------- 7. off-trade
offtrade = score_hit(
    "Anyone recommend a piano teacher in Plano? - Nextdoor",
    "1 day ago — My daughter wants to start lessons this fall.",
    "https://nextdoor.com/p/aa11bb",
    TRADE, CITY, TERMS, now=TODAY)
show("right city, right shape, wrong trade", offtrade)
check("off-trade rejected", offtrade["reject"] is True)
check("off-trade reason explains it",
      "trade" in (offtrade["reject_reason"] or "").lower(), offtrade["reject_reason"])

# --------------------------------------------------------- 8. platform chrome
chrome = score_hit(
    "Roofing near me | Nextdoor",
    "Browse roofing businesses recommended by neighbors in your area.",
    "https://nextdoor.com/pages_directory/roofing/tx/",
    TRADE, CITY, TERMS, now=TODAY)
show("platform directory page", chrome)
check("directory page rejected", chrome["reject"] is True)

login = score_hit(
    "Log in to Nextdoor",
    "Sign in to see roofing recommendations from your neighbors.",
    "https://nextdoor.com/login/?next=/p/abc",
    TRADE, CITY, TERMS, now=TODAY)
show("login wall", login)
check("login wall rejected", login["reject"] is True)
check("login reason mentions the surface",
      "junk surface" in (login["reject_reason"] or "").lower(), login["reject_reason"])

# ---------------------------------------------- 9. nearby suburb still in market
nearby = score_hit(
    "Need a roofer ASAP in McKinney : r/DFW",
    "5 hours ago — Hail last night put a hole through the roof over my kitchen, "
    "water is coming in. Can anyone recommend somebody who can come out today?",
    "https://www.reddit.com/r/DFW/comments/1zz9xx/need_a_roofer_asap_in_mckinney/",
    TRADE, CITY, TERMS, now=TODAY)
show("nearby DFW suburb, urgent, hours old", nearby)
check("nearby suburb not rejected", nearby["reject"] is False, nearby["reject_reason"] or "")
check("nearby+urgent scores very high (>=0.9)", nearby["score"] >= 0.9,
      f"got {nearby['score']}")

# ----------------------------------------------------- 10. statement, not ask
statement = score_hit(
    "Finally got the new roof finished - Nextdoor",
    "2 days ago — Just wanted to share, the roof in Plano is done and it looks great.",
    "https://nextdoor.com/p/done999",
    TRADE, CITY, TERMS, now=TODAY)
show("statement, not a request", statement)
check("statement scores below a real ask", statement["score"] < good["score"] - 0.15,
      f"{statement['score']} vs {good['score']}")

# --------------------------------------------------- 11. published_hint honored
hinted = score_hit(
    "Looking for a roofer in Plano - Nextdoor",
    "Roof leak over the porch, need someone to take a look.",
    "https://nextdoor.com/p/hint01",
    TRADE, CITY, TERMS, published_hint="2026-08-24", now=TODAY)
show("date supplied via published_hint", hinted)
check("published_hint drives recency to 1.0", hinted["components"]["recency"] == 1.0,
      str(hinted["components"]))
check("published_hint is credited in reasons",
      any("published_hint" in r for r in hinted["reasons"]))

# ------------------------------------------------------------- 12. determinism
runs = [score_hit(
    "Anyone recommend a roofer in Plano? - Nextdoor",
    "3 days ago — We took hail last week and I'm seeing shingles in the yard. "
    "Looking for someone reputable to come out and look at the roof. TIA neighbors.",
    "https://nextdoor.com/p/9fK2mQ",
    TRADE, CITY, TERMS, now=TODAY) for _ in range(50)]
check("50 identical calls give identical scores",
      len({r["score"] for r in runs}) == 1, str({r["score"] for r in runs}))
check("50 identical calls give identical reasons",
      len({tuple(r["reasons"]) for r in runs}) == 1)

# ------------------------------------------------- 13. contract shape holds
for name, r in (("good", good), ("biz", biz_url), ("oos", oos),
                ("listicle", listicle), ("nodate", nodate)):
    check(f"{name}: score is a float in 0..1",
          isinstance(r["score"], float) and 0.0 <= r["score"] <= 1.0)
    check(f"{name}: reasons is a non-empty list of str",
          isinstance(r["reasons"], list) and r["reasons"]
          and all(isinstance(x, str) for x in r["reasons"]))
    check(f"{name}: reject implies a reject_reason string",
          (not r["reject"]) or isinstance(r["reject_reason"], str) and r["reject_reason"])
    check(f"{name}: no reject implies reject_reason is None",
          r["reject"] or r["reject_reason"] is None)

# ------------------------------------------------------------------ 14. ranking
ranked = sorted(
    [("nearby urgent", nearby), ("fresh Plano ask", good),
     ("dateless ask", nodate), ("statement", statement), ("7-month-old ask", aging)],
    key=lambda kv: -kv[1]["score"])
print("\n--- ranking a mixed queue (what a human would see, best first)")
for n, r in ranked:
    print(f"    {r['score']:.3f}  {n}")
check("the urgent nearby lead ranks first", ranked[0][0] == "nearby urgent")
check("the 7-month-old ask ranks last", ranked[-1][0] == "7-month-old ask")

print(f"\n==== {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
