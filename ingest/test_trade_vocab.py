#!/usr/bin/env python3
"""Tests for trade_vocab. No network, no DB — pure assertions.

    python test_trade_vocab.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from trade_vocab import (TRADES, canonical_trade, intent_queries, is_relevant,
                         relevance_terms)

FAILS = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        FAILS.append(name)


print("\n[1] junk-removal queries carry the phrasings real posts use")
jq = " || ".join(intent_queries("junk removal", "Dallas", []))
for phrase in ['"get rid of"', '"haul away"', '"dump run"', '"estate cleanout"',
               '"someone to take"', '"garage cleanout"', '"junk removal"']:
    check(f"query list contains {phrase}", phrase in jq)
check("every junk query is localized to Dallas",
      all("Dallas" in q for q in intent_queries("junk removal", "Dallas", [])))
check("trade-native asks come before generic asks",
      intent_queries("junk removal", "Dallas", [])[0].startswith('"get rid of"'))
check("more than the old 16 generic phrases",
      len(intent_queries("junk removal", "Dallas", [])) > 30)

print("\n[2] the old single-word gate is what killed Hero's")
real_posts = [
    "Need someone to haul off Palm trimmings",
    "Need Someone to Haul Dirt",
    "Anyone know who I can pay to get rid of an old couch and a mattress?",
    "Doing a garage cleanout this weekend, who does dump runs?",
    "Moving out and need my old furniture picked up",
    "Estate cleanout after my mother passed - recommendations?",
]
old_gate = sum(1 for p in real_posts if "junk" in p.lower())
new_gate = sum(1 for p in real_posts if is_relevant("junk removal", p))
print(f"  old gate ('junk' in text): {old_gate}/{len(real_posts)}")
print(f"  new gate (relevance_terms): {new_gate}/{len(real_posts)}")
check("old gate rejects nearly all real demand posts", old_gate <= 1)
check("new gate accepts all real demand posts", new_gate == len(real_posts))

print("\n[3] relevance rejects unrelated posts")
for junkpost in ["Anyone recommend a good dentist in Plano?",
                 "Lost dog near the park, please help",
                 "Selling Pokemon cards, make an offer"]:
    check(f"rejects: {junkpost[:40]}", not is_relevant("junk removal", junkpost))

print("\n[4] relevance reads the URL slug when the snippet is empty")
check("reddit slug with no title/body still matches",
      is_relevant("junk removal", "Link to reddit.com", "",
                  "https://www.reddit.com/r/Dallas/comments/1cg/"
                  "cb_wants_you_to_haul_away_old_wood_and_pay_them/"))
check("unrelated reddit slug still rejected",
      not is_relevant("junk removal", "Link to reddit.com", "",
                      "https://www.reddit.com/r/montreal/comments/1dl/"
                      "can_anyone_recommend_a_cafe_where_i_can_practice/"))

print("\n[4b] marketplace listings and platform blog pages are excluded")
for t, b, u in [
    ("Set Of Gray Sofas For $200 In Dallas, TX | For Sale & Free — Nextdoor", "", ""),
    ("How to Start a Junk Removal Business | Nextdoor", "", ""),
    ("Your local business spring cleaning checklist", "", ""),
    ("For Sale & Free - Nextdoor", "Need someone to haul off Palm trimmings Free", ""),
    ("Some page", "", "https://business.nextdoor.com/en-us/small-business/resources/blog/x"),
]:
    check(f"noise rejected: {t[:48]}", not is_relevant("junk removal", t, b, u))
check("a genuine ask is still kept",
      is_relevant("junk removal",
                  "Who do you use to haul away an old couch?", "", ""))

print("\n[5] unknown trade still returns usable queries")
uq = intent_queries("pool cleaning", "Frisco", ["weekly service"])
check("unknown trade produces queries", len(uq) >= 10)
check("unknown trade queries name the trade", any("pool cleaning" in q for q in uq))
check("unknown trade queries are localized", all("Frisco" in q for q in uq))
check("unknown trade includes extra_terms", any("weekly service" in q for q in uq))
ut = relevance_terms("pool cleaning")
check("unknown trade relevance has its own words",
      "pool" in ut and "cleaning" in ut)
check("unknown trade relevance matches a plausible post",
      is_relevant("pool cleaning", "Anyone recommend a pool guy in Frisco?"))

print("\n[6] alias normalization")
for raw, want in [("Junk Removal", "junk removal"), ("hauling", "junk removal"),
                  ("Roofer", "roofing"), ("A/C", "hvac"),
                  ("air conditioning", "hvac"), ("plumber", "plumbing"),
                  ("Electrician", "electrical"), ("lawn care", "landscaping"),
                  ("Residential Roofing Services", "roofing")]:
    check(f"{raw!r} -> {want!r}", canonical_trade(raw) == want)

print("\n[7] every seeded trade is well formed")
for t, voc in TRADES.items():
    check(f"{t}: has asks/subject/confirm",
          len(voc["asks"]) >= 10 and voc["subject"] and len(voc["confirm"]) >= 10)
    check(f"{t}: queries generated", len(intent_queries(t, "Dallas", [])) > 20)

print("\n[8] trades do not bleed into each other")
check("roofing terms reject a junk post",
      not is_relevant("roofing", "Need someone to haul off my old mattress"))
check("hvac terms reject a roofing post",
      not is_relevant("hvac", "Missing shingles after the hail, need a roofer"))

print(f"\n=== {'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURES'} ===")
for f in FAILS:
    print("  -", f)
sys.exit(1 if FAILS else 0)
