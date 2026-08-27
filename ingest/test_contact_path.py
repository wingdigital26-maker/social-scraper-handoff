#!/usr/bin/env python3
"""
test_contact_path.py -- Tests for the contact-path resolver.

These are the tests that matter, not coverage theatre:

  1. The ASKED / NEVER-ASKED split is right, because mislabelling a cold
     business as someone who asked would licence contact that is not licensed.
  2. Nothing is ever fabricated -- no guessed address, no unconfirmed form URL,
     no invented path when the row is empty.
  3. Nothing forbidden is ever automated -- no /reply fetch, no form submit.
  4. Confidence tracks provenance, so "matched place record" outranks "scraped
     from a footer" and a human deciding whether to dial is told which.

No network. Every fetch is stubbed with bytes checked in here.

    python test_contact_path.py
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import contact_path as cp   # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  pass  {msg}")
    else:
        print(f"  FAIL  {msg}")
        FAILS.append(msg)


def section(t):
    print("\n" + t)
    print("-" * len(t))


# Real row shapes, copied from live output of each source on 2026-08-27.
CL_HIRE = {
    "source": "craigslist", "source_id": "7qkCNZVBksfneuKqTUfKhg",
    "url": "https://www.craigslist.org/view/d/melissa-junk-removal-needed-125-melissa/7qkCNZVBksfneuKqTUfKhg",
    "title": "Junk Removal Needed – $125 Melissa TX",
    "desc": "Need a few items hauled from the garage. Text 214-555-0134.",
    "category": "hire", "intent": "hire", "market": "dfw",
    "embeds": [{"type": "craigslist", "url": "https://example"}],
}
CL_EVENT = dict(CL_HIRE, source_id="4KKK", category="event", intent="event",
                title="Free Estate Sale Leftovers", desc="everything must go sunday")
ES_ROW = {
    "source": "estatesales", "source_id": "5027004",
    "url": "https://www.estatesales.net/TX/Arlington/76013/5027004",
    "title": "Annie's Estate Sales - Arlington starts on 8/27/2026",
    "desc": "It is being run by Annie's Estate Sales.",
    "category": "event", "intent": "event", "run_by": "Annie's Estate Sales",
    "place": "Arlington, TX 76013",
}
B2B_ROW = {
    "source": "b2b", "channel": "news", "company": "Some Brand",
    "url": "https://news.google.com/rss/articles/CBMib0FV",
    "score": 30, "tier": "C",
    "pain_signals": [{"kind": "volume_spike_event", "points": 30,
                      "quote": "closes Series A funding", "source_url": "https://n"}],
}
BIZ_VERIFIED = {
    "source": "linkedin", "id": 511, "title": "Apple Roofing",
    "url": "https://www.linkedin.com/company/x", "website": "https://appleroofing.net",
    "phone": "(972) 555-0100", "identity": "verified", "place_name_matched": True,
    "email": None, "contact_email": None,
}
BIZ_NAMED = dict(BIZ_VERIFIED, id=512, contact_email="dana@appleroofing.net",
                 email_kind="personal", contact_name="Dana Reyes",
                 contact_title="Owner", email_source="https://appleroofing.net/about")
BIZ_ROLE = dict(BIZ_VERIFIED, id=513, contact_email="info@appleroofing.net",
                email_kind="role", email_source="https://appleroofing.net/contact")
BIZ_BARE = {"source": "linkedin", "id": 514, "title": "No Data Roofing"}

section("1. ASKED vs NEVER-ASKED -- the split that licences contact")
p = cp.resolve(CL_HIRE)
check(p["permission"] == "asked", "a craigslist HIRE posting is ASKED")
check("publicly asked to hire" in p["permission_reason"], "the reason names the evidence")
check(cp.resolve(CL_EVENT)["permission"] == "never_asked",
      "a public-but-not-asking posting is NEVER-ASKED, not ASKED")
check(cp.resolve(ES_ROW)["permission"] == "never_asked",
      "an estate-sale listing is NEVER-ASKED (the company never asked)")
check(cp.resolve(B2B_ROW)["permission"] == "never_asked",
      "a b2b pain-signal prospect is NEVER-ASKED")
check(cp.resolve(BIZ_VERIFIED)["permission"] == "never_asked",
      "a verified business row is NEVER-ASKED")
check(all(cp.resolve(r)["permission"] in ("asked", "never_asked")
          for r in (CL_HIRE, CL_EVENT, ES_ROW, B2B_ROW, BIZ_BARE)),
      "permission is always explicit, never absent or implied")
check(cp.resolve({"source": "some_new_source_nobody_taught_us", "id": 1})["permission"]
      == "never_asked", "an unknown source defaults to COLD, never to asked")

section("2. Never fabricate")
pb = cp.resolve(BIZ_BARE)
check(pb["actionable"] is False, "an empty row yields NO path")
check(pb["paths"] == [], "and no invented one sneaks in")
check("no website on the row" in pb["missing"] and "no phone on the row" in pb["missing"],
      "it says exactly WHAT is missing instead of returning empty")
pv = cp.resolve(BIZ_VERIFIED)          # website present, no form lookup passed
check(not any(x["method"] == "contact_form" for x in pv["paths"]),
      "no contact-form path without a fetch that confirmed one")
check(pv.get("form_note") == "form discovery not run",
      "and it says so rather than implying the site has no form")
check(not any("@" in (x["action"] or "") and x["method"].startswith("email")
              for x in pv["paths"]),
      "no email path is constructed from a domain + a name")
check(cp.emails_in("reply to 7qk-abc@reply.craigslist.org") == [],
      "a craigslist relay address is never presented as the poster's own address")
check(cp.phones_in("lot size 2145550134 sqft") == [],
      "a bare digit run never becomes a number to dial")

section("3. Never automate what the platform forbids")
relay = [x for x in p["paths"] if x["method"] == "platform_reply"][0]
check("/reply" in relay["platform_rules"] and "never fetch" in relay["platform_rules"],
      "the craigslist relay carries its robots.txt rule with it")
check(relay["action"] == CL_HIRE["url"],
      "the surfaced link is the posting itself -- the /reply URL is not synthesised")
check(all(x["automatable"] is False for x in p["paths"]),
      "every path is marked for a human, none automatable")
check(not hasattr(cp, "submit_form") and not hasattr(cp, "submit"),
      "the module contains NO form submitter, by construction")
src = (HERE / "contact_path.py").read_text(encoding="utf-8")
check("requests.post" not in src, "the module never issues a POST at all")
pe = cp.resolve(CL_EVENT)
check(pe["paths"][0].get("unsolicited") is True,
      "a relay used on someone who did NOT ask is flagged UNSOLICITED")
check(relay.get("unsolicited") is None,
      "and that flag is absent for someone who did ask")

section("4. Confidence tracks provenance")
check(pv["paths"][0]["confidence"]["provenance"] == "place_matched",
      "a matched place record is the phone's provenance when place_name_matched")
scraped = cp.resolve(dict(BIZ_VERIFIED, identity=None, place_name_matched=None))
check(scraped["paths"][0]["confidence"]["score"]
      < pv["paths"][0]["confidence"]["score"],
      "a footer-scraped phone scores BELOW a matched place record")
pn = cp.resolve(BIZ_NAMED)
check(pn["paths"][0]["method"] == "email_person",
      "a named person's email outranks the phone")
check(pn["paths"][0]["person"] == "Dana Reyes", "and it names the human")
pr = cp.resolve(BIZ_ROLE)
check([x["method"] for x in pr["paths"]].index("phone")
      < [x["method"] for x in pr["paths"]].index("email_role"),
      "a role inbox ranks BELOW the phone (133 sends, 0 replies)")
check(all(0 < x["confidence"]["score"] <= 1 and x["confidence"]["label"]
          and x["reason"] for x in pn["paths"]),
      "every path carries a reason and a confidence")

section("5. Contact form -- confirmed only, and never submitted")
FORM_HTML = ('<html><body><form action="/wp/contact" method="post">'
             '<input type="text" name="your-name"><input type="email" name="your-email">'
             '<input type="hidden" name="nonce"><textarea name="message"></textarea>'
             '<div class="g-recaptcha" data-sitekey="x"></div>'
             '<input type="submit" value="Send"></form></body></html>')
f = cp.form_in_html(FORM_HTML, "https://example.com/contact")
check(f is not None, "a real contact form is confirmed")
check(f["fields"] == ["your-name", "your-email", "message"],
      "the field names a human must fill are recorded verbatim")
check("nonce" not in f["fields"], "hidden fields are not presented as things to fill")
check(f["captcha"] is True, "a CAPTCHA is REPORTED (never defeated)")
check(cp.form_in_html('<form><input type="search" name="s"></form>', "u") is None,
      "a search box is not a contact form")
check(cp.form_in_html('<form><input name="EMAIL"><input type="submit"></form>', "u")
      is None, "a newsletter signup is not a contact form")
check(cp.form_in_html('<iframe src="https://form.jotform.com/1234"></iframe>',
                      "https://x.com/contact")["thirdparty"] is True,
      "an embedded third-party form is confirmed and labelled as such")

plan = cp.resolve(BIZ_VERIFIED, form_lookup=lambda w: {"form": f, "reason": "ok"})
form_path = [x for x in plan["paths"] if x["method"] == "contact_form"][0]
check(form_path["fields"] == f["fields"], "the plan carries the field names")
check("human" in form_path["never_automate"], "the plan says a human fills it")
check([x["method"] for x in plan["paths"]].index("contact_form")
      > [x["method"] for x in plan["paths"]].index("phone"),
      "the form ranks below a call but is present as a real path")

# Failure to confirm must never become a claim.
plan2 = cp.resolve(BIZ_VERIFIED, form_lookup=lambda w: {"form": None,
                                                        "reason": "site unreachable"})
check(not any(x["method"] == "contact_form" for x in plan2["paths"]),
      "an unconfirmed form produces no form path")
check(plan2["form_note"] == "site unreachable",
      "and the reason it could not be confirmed is reported")

section("6. Listing-page enrichment (estate-sale company phone)")
before = cp.resolve(ES_ROW)
check(before["actionable"] is False, "without enrichment the row reads unreachable")
after = cp.resolve(ES_ROW, listing_lookup=lambda u: {
    "phone": "(817) 683-6668",
    "profile_url": "https://www.estatesales.net/companies/TX/Arlington/76013/20162",
    "reason": "read off the listing page"})
check(after["paths"][0]["method"] == "phone", "with it, the company phone is the path")
check(after["paths"][0]["confidence"]["provenance"] == "listing_published",
      "provenance says it came off the listing page")
check("Annie's Estate Sales" in after["paths"][0]["reason"],
      "and names WHO that number reaches")
check(any(r["step"] == "open_company_profile" for r in after["research"]),
      "the company profile is offered as research, for a repeat referral partner")
none_found = cp.resolve(ES_ROW, listing_lookup=lambda u: {
    "phone": None, "reason": "listing page publishes no phone"})
check(none_found["actionable"] is False
      and "listing page publishes no phone" in none_found["missing"],
      "when the page publishes nothing, it says so instead of inventing a number")

section("6b. b2b rows: the row's own domain vs somebody else's platform")
own = {"source": "b2b", "channel": "shipping_policy", "company": "Some Brand",
       "url": "https://somebrand.com/pages/shipping", "score": 40, "tier": "B"}
po = cp.resolve(own, form_lookup=lambda w: {"form": f, "reason": "ok"})
check(any(x["method"] == "contact_form" for x in po["paths"]),
      "a b2b row whose url IS the brand's site resolves through that site")
pnews = cp.resolve(B2B_ROW, form_lookup=lambda w: {"form": f, "reason": "ok"})
check(not pnews["paths"],
      "a news-aggregator link is never treated as the prospect's own site")
check(any("news.google.com" in m for m in pnews["missing"]),
      "and it says the domain is unknown rather than guessing one")

section("6c. A targeting area is not somebody to contact")
storm = {"source": "swdi", "source_id": "x1", "title": "Hail 2.0in near a city",
         "url": "https://www.ncdc.noaa.gov/swdiws/", "is_person": False,
         "lead_kind": "targeting_area", "category": "storm_severe"}
ps = cp.resolve(storm)
check(ps["contactable_entity"] is False, "a hail swath is not a contactable entity")
check(ps["paths"] == [] and "nobody to contact" in ps["missing"][0],
      "it says there is nobody to contact instead of inventing a path")
c2 = cp.coverage([ps, cp.resolve(CL_HIRE)])
check(c2["total"] == 1 and c2["pct"] == 100.0,
      "targeting-area rows stay OUT of the coverage denominator")
check(c2["by_source"]["swdi"]["not_an_entity"] == 1, "but they are still counted")

roof_ask = {"source": "craigslist", "source_id": "x2", "category": "ask",
            "url": "https://dallas.craigslist.org/x/2.html", "is_person": True,
            "lead_kind": "person_asked", "title": "Need roof repair after hail",
            "desc": "leaking, call 972-555-0166"}
pra = cp.resolve(roof_ask)
check(pra["permission"] == "asked",
      "a roofing homeowner flagged person_asked is ASKED (source_roofing shape)")
check(pra["paths"][0]["method"] == "phone"
      and pra["paths"][0]["action"] == "tel:+19725550166",
      "the number they published in their own post is the first thing to try")

section("7. Research steps are NOT contact paths")
pb2 = cp.resolve(ES_ROW)
check(pb2["actionable"] is False and pb2["research"],
      "research steps exist but do not make a lead 'actionable'")
cov = cp.coverage([pb2])
check(cov["actionable"] == 0, "a search URL never counts toward coverage")
check(all(not r["action"].startswith("mailto:") and not r["action"].startswith("tel:")
          for r in pb2["research"]), "research steps are lookups, not contacts")

section("8. Coverage arithmetic")
cov = cp.coverage([cp.resolve(CL_HIRE), cp.resolve(BIZ_NAMED), cp.resolve(BIZ_BARE)])
check(cov["total"] == 3 and cov["actionable"] == 2 and cov["pct"] == 66.7,
      "coverage counts only leads with a real path")
check(cov["by_source"]["linkedin"]["stuck"][0]["missing"],
      "every unreachable lead is listed with WHY")
check(cov["by_source"]["craigslist"]["asked"] == 1,
      "the asked / never-asked split is reported per source")

print("\n" + "=" * 60)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURES:")
for f_ in FAILS:
    print("  -", f_)
sys.exit(1 if FAILS else 0)
