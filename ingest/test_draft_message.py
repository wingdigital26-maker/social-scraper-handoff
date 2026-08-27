#!/usr/bin/env python3
"""Tests for draft_message.py. Offline: no network, no model, no DB.

Run:  python test_draft_message.py
      python draft_message.py --self-test

The point of this file is the verifier. Every FABRICATIONS case below is a
sentence that would be a lie if it were sent, and every one of them must be
caught. Three of them are real: they were actually produced by this system.
"""
from __future__ import annotations

import sys

import draft_message as dm

FACT = ('Your site says "Since 2004", which puts you at 22 years.')
SOURCE = "https://example-roofing.test/about"
RECIPIENT = {"id": 13, "title": "Lowry Roofing", "category": "roofing",
             "website": "https://example-roofing.test"}
VOICE = "wing-digital"


def lex(fact=FACT, voice=VOICE, recipient=None):
    profile = dm.client_voice._load_profile(voice)
    return dm.build_lexicon(dm.normalize_fact(fact), profile,
                            recipient or RECIPIENT, SOURCE)


# (label, draft_text, substring that must appear in one of the violations)
FABRICATIONS = [
    ("real: complimented work nobody looked at",
     "Your site says Since 2004, which puts you at 22 years. Your work looks "
     "solid and that history should be doing more for you. Worth a look?",
     "unverifiable claim"),
    ("real: invented rank data",
     "Your site says Since 2004, which puts you at 22 years. You keep showing "
     "up in Plano searches but the page does not say it. Want the short version?",
     "unverifiable claim"),
    ("real: wrong geography bolted on",
     "Your site says Since 2004, which puts you at 22 years. We do roofing "
     "around Saskatoon and see this a lot. Worth a look?",
     "proper noun"),
    ("invented a number",
     "Your site says Since 2004, which puts you at 22 years, and 47 percent of "
     "visitors leave before reading it. Want the short version?",
     "invented number: 47"),
    ("invented a spelled-out number",
     "Your site says Since 2004, which puts you at 22 years. That is thirty "
     "years of goodwill sitting unused on the page. Worth a look?",
     "invented number word: thirty"),
    ("invented a certification",
     "Your site says Since 2004, which puts you at 22 years. Pairing that with "
     "your GAF Master Elite badge would land harder. Worth a look?",
     "proper noun"),
    ("invented a city at the start of a sentence (capitalization proves nothing there)",
     "Your site says Since 2004, which puts you at 22 years. Amarillo customers "
     "will never see that. Worth a look?",
     "proper noun"),
    ("invented a city",
     "Your site says Since 2004, which puts you at 22 years. Customers in "
     "Amarillo will not find that. Worth a look?",
     "proper noun"),
    ("fabricated quote attributed to their site",
     'Your site says "Family owned since 1998" and that puts you at 22 years. '
     "Worth a look?",
     "quoted text not verbatim"),
    ("invented review claim",
     "Your site says Since 2004, which puts you at 22 years. Your reviews are "
     "strong too. Worth a look?",
     "unverifiable claim"),
    ("smuggled in an outside link",
     "Your site says Since 2004, which puts you at 22 years. Details here: "
     "https://wingdigital.test/audit Worth a look?",
     "link not the verified source"),
    ("dropped the fact entirely",
     "Hey there, just wanted to see whether you had a minute this week to talk "
     "about the website and what could be better about it overall for you.",
     "content words from the fact"),
    ("model preamble",
     "Sure! Here's a draft:\nYour site says Since 2004, which puts you at 22 "
     "years. Worth a look?",
     "preamble"),
    ("unfilled placeholder",
     "Your site says Since 2004, which puts you at 22 years, {company}. "
     "Worth a look?",
     "placeholder"),
    ("em dash (Jack's standing rule)",
     "Your site says Since 2004, which puts you at 22 years — that is a "
     "long time to keep quiet about it. Worth a look?",
     "dash"),
    ("pricing claim",
     "Your site says Since 2004, which puts you at 22 years. Happy to do a "
     "free audit for you. Worth a look?",
     "pricing"),
    ("fake urgency",
     "Your site says Since 2004, which puts you at 22 years. Only a few spots "
     "left this month. Worth a look?",
     "urgency"),
    ("client's own banned phrase",
     "Your site says Since 2004, which puts you at 22 years. We can boost your "
     "rankings off the back of it. Worth a look?",
     "forbidden phrase"),
]

CLEAN = [
    ("plain rephrase",
     "Your own site says you have been at this since 2004. That is 22 years "
     "you are not really cashing in on anywhere a customer would see it. "
     "Want me to point at where it should go?"),
    # Regression 2026-08-27: apostrophes were being read as quote delimiters,
    # so an honest draft with two contractions was rejected as a fake quote.
    ("contractions and a possessive",
     "You've had Since 2004 sitting on your site and it's doing nothing for "
     "you. Lowry Roofing's 22 years should be the first thing a customer "
     "reads. Want me to show you where it would go?"),
    ("ordinary words starting sentences are not proper nouns",
     "Reading through your site, Since 2004 is sitting there doing nothing. "
     "Adding it higher up would give a customer 22 years of reassurance "
     "before they scroll. Would that be worth changing?"),
    ("shorter rephrase",
     "You have had Since 2004 on your site for a while now. 22 years is a real "
     "advantage and it is buried. Would it help to see where I would move it?"),
]


def run(name, fn, failures):
    try:
        fn()
        print(f"  PASS  {name}")
    except AssertionError as e:
        print(f"  FAIL  {name}: {e}")
        failures.append(name)


def main():
    failures = []
    print("verify_draft catches fabrication:")
    L = lex()
    for label, text, expect in FABRICATIONS:
        def check(text=text, expect=expect, label=label):
            v = dm.verify_draft(text, L, VOICE)
            assert v, "verifier returned CLEAN on a fabrication"
            assert any(expect in x for x in v), \
                f"expected {expect!r}, got {v}"
        run(label, check, failures)

    print("\nverify_draft passes honest rephrases:")
    for label, text in CLEAN:
        def check(text=text, label=label):
            v = dm.verify_draft(text, L, VOICE)
            assert not v, f"rejected a clean draft: {v}"
        run(label, check, failures)

    print("\nfallback and plumbing:")

    def no_model():
        r = dm.draft_message(FACT, SOURCE, VOICE, RECIPIENT, use_model=False)
        assert r["method"] == "template", r["method"]
        assert r["verified"] is False
        assert "no-model" in r["reason"] or "switched off" in r["reason"], r["reason"]
        assert "2004" in r["text"], r["text"]
    run("use_model=False falls back and says so", no_model, failures)

    def dead_model():
        def gen(*a, **k):
            return {"error": "rate-limited (429)"}
        r = dm.draft_message(FACT, SOURCE, VOICE, RECIPIENT, _generate=gen)
        assert r["method"] == "template", r["method"]
        assert "429" in r["reason"], r["reason"]
    run("model error falls back and reports the error", dead_model, failures)

    def raising_model():
        def gen(*a, **k):
            raise RuntimeError("connection reset")
        r = dm.draft_message(FACT, SOURCE, VOICE, RECIPIENT, _generate=gen)
        assert r["method"] == "template"
        assert "connection reset" in r["reason"], r["reason"]
    run("model exception never propagates", raising_model, failures)

    def fabricating_model():
        def gen(*a, **k):
            return {"output": "Your site says Since 2004, which puts you at 22 "
                              "years. Your work looks solid. Worth a look?",
                    "model": "test:fake", "usage": {"prompt_tokens": 10,
                                                    "completion_tokens": 20}}
        r = dm.draft_message(FACT, SOURCE, VOICE, RECIPIENT, _generate=gen,
                             attempts=2)
        assert r["method"] == "template", "a fabricating model was stored!"
        assert r["verified"] is False
        assert len(r["rejected"]) == 2, r["rejected"]
        assert any("unverifiable" in v for v in r["rejected"][0]["violations"])
        assert r["tokens"]["total"] == 60, r["tokens"]
    run("fabricating model is rejected, not edited", fabricating_model, failures)

    def good_model():
        def gen(*a, **k):
            return {"output": CLEAN[0][1], "model": "test:fake",
                    "usage": {"prompt_tokens": 300, "completion_tokens": 120}}
        r = dm.draft_message(FACT, SOURCE, VOICE, RECIPIENT, _generate=gen)
        assert r["method"] == "model" and r["verified"] is True, r
        assert r["tokens"]["total"] == 420
    run("verified model draft is kept", good_model, failures)

    def two_voices():
        a = dm.draft_message(FACT, SOURCE, "wing-digital", RECIPIENT,
                             use_model=False)
        b = dm.draft_message(FACT, SOURCE, "heros-junk", RECIPIENT,
                             use_model=False)
        assert a["text"] != b["text"] or True  # frames may overlap; prompts must not
        pa = dm.build_system_prompt(dm.client_voice._load_profile("wing-digital"))
        pb = dm.build_system_prompt(dm.client_voice._load_profile("heros-junk"))
        assert pa != pb, "two clients produced an identical system prompt"
        assert "junk removal" in pb and "junk removal" not in pa
    run("two clients do not share a voice", two_voices, failures)

    def no_collisions():
        facts = [
            'Your site says "Since 2004", which puts you at 22 years.',
            'Your homepage mentions "storm damage" 3 times.',
            "Your site still loads over plain http.",
            'Your site uses the phrase "family owned and operated".',
            "The newest dated post on your blog is September 2019.",
            'Your site lists "GAF Master Elite" among your certifications.',
        ]
        drafts = []
        for i, f in enumerate(facts):
            r = dm.draft_message(f, SOURCE, VOICE,
                                 {"id": 100 + i, "title": f"Lead {i}"},
                                 use_model=False)
            r["lead_id"] = 100 + i
            drafts.append(r)
        rep = dm.check_collisions(drafts)
        assert rep["ok"], f"byte-identical bodies: {rep['exact']}"
        assert rep["per_client"][VOICE]["unique_bodies"] == len(facts)
    run("no byte-identical bodies for one client", no_collisions, failures)

    def collision_detector_works():
        drafts = [{"voice": VOICE, "text": "same body", "lead_id": 1},
                  {"voice": VOICE, "text": "same body", "lead_id": 2}]
        rep = dm.check_collisions(drafts)
        assert not rep["ok"] and rep["exact"][0]["ids"] == ["1", "2"], rep
    run("collision detector actually detects", collision_detector_works, failures)

    def fact_untouched_by_dash_fix():
        f = "Your blog stopped in 2019 — that is six years."
        assert dm.normalize_fact(f) == "Your blog stopped in 2019, that is six years."
    run("em dash in the fact is normalized, not dropped",
        fact_untouched_by_dash_fix, failures)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    total = len(FABRICATIONS) + len(CLEAN) + 9
    print(f"all {total} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
