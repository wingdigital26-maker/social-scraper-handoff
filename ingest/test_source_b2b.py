#!/usr/bin/env python3
"""Tests for source_b2b.py.

The important ones are the NEGATIVES. Every regression here is a real error
this codebase has made or nearly made: reading a refund SLA as a shipping
delay, scoring a well-run brand as a lead, emitting a lead with no evidence,
and exiting 0 on a run that yielded nothing.

    python test_source_b2b.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import source_b2b as S  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# HANDLING_TIME must be anchored to an ORDER, not to a refund.
# Verbatim from milkandhoney.com/pages/faq, 2026-08-26 — a brand whose
# fulfillment is fine. An earlier draft scored this as a shipping delay.
# ---------------------------------------------------------------------------
REFUND_TEXT = ("Therabody, TheraFace, and SolaWave items are not eligible for "
               "returns. Refunds Processing Standard processing time is 2-3 "
               "business days after receipt of your return or confirmation of "
               "refund. Please note that your credit card company may take "
               "longer.")
check("refund SLA is NOT read as a handling time",
      S.worst_handling_days(REFUND_TEXT) is None,
      f"got {S.worst_handling_days(REFUND_TEXT)}")

# Verbatim from brooklyncandlestudio.com/policies/shipping-policy — a real
# order handling time, but a FAST one. Must parse, must not score as pain.
FAST = ("PROCESSING Most orders are packed and shipped within 1-3 business "
        "days, excluding weekends and holidays.")
w = S.worst_handling_days(FAST)
check("fast handling time parses", w is not None and w[0] == 3, f"got {w}")
check("an ordinary 1-3 day promise is NOT pain",
      w is not None and w[0] < S.SLOW_HANDLING_DAYS,
      "1-3 business days is the industry-normal promise, not a brand in trouble")

FASTER = "Orders are processed and shipped within 1-2 business days."
w2 = S.worst_handling_days(FASTER)
check("1-2 day brand is below the pain threshold",
      w2 is not None and w2[0] < S.SLOW_HANDLING_DAYS, f"got {w2}")

# Verbatim from thesill.com/policies/shipping-policy — genuinely slow.
SLOW = ("Orders with standard shipping will ship within 2-6 business days. "
        "Tracking information will be automatically sent to your email.")
w3 = S.worst_handling_days(SLOW)
check("6-day handling time is at/over the pain threshold",
      w3 is not None and w3[0] >= S.SLOW_HANDLING_DAYS, f"got {w3}")

# Verbatim from tropicaloasis.com/policies/shipping-policy, 2026-08-26. This
# brand's HANDLING time is 1 business day (healthy); the 6-10 days is the post
# office. A first cut of the source scored it as a slow self-fulfiller. Never
# again.
TRANSIT = ("Canada Standard Shipping Cost: Rates shown at checkout are based on "
           "your address and the order's weight and dimensions Processing Time: "
           "1 business day Delivery Time: 6-10 business days after processing")
w4 = S.worst_handling_days(TRANSIT)
check("carrier DELIVERY time is not scored as the brand's handling time",
      w4 is None or w4[0] < S.SLOW_HANDLING_DAYS,
      f"got {w4} -- 6-10 days here belongs to USPS, not to the brand")

check("a phone number is not a handling time",
      S.worst_handling_days("Call us about your order at 325-430 7282 days") is None)
check("reversed range is rejected",
      S.worst_handling_days("Orders ship in 9-2 business days") is None)

# ---------------------------------------------------------------------------
# OVERWHELM: admissions only, never inference.
# Verbatim from glasswingorganics.com/policies/shipping-policy, 2026-08-26.
# ---------------------------------------------------------------------------
ADMIT = ("I strive to provide quick order processing but sometimes due to the "
         "number of orders I receive or other circumstances it may take longer.")
check("real strain admission matches OVERWHELM", bool(S.OVERWHELM.search(ADMIT)))
check("real strain admission matches SOLO_VOICE", bool(S.SOLO_VOICE.search(ADMIT)))
check("a normal shipping sentence does not match OVERWHELM",
      not S.OVERWHELM.search("We use USPS for all our shipping needs."))

# ---------------------------------------------------------------------------
# HIRING must match role names, not stray words.
# ---------------------------------------------------------------------------
check("real job title matches HIRING",
      bool(S.HIRING.search("Now hiring: Fulfillment Associate (full-time)")))
check("pick and pack matches",
      bool(S.HIRING.search("Pick and Pack Associate, second shift")))
check("a warehouse ADDRESS does not match HIRING",
      not S.HIRING.search("Visit our warehouse at 100 Main St, Dallas TX"),
      "bare 'warehouse' must not fire -- this is the 'Customer SERVice' error class")

# ---------------------------------------------------------------------------
# News gates: physical shippers in, software rounds out.
# Real headlines pulled from Google News RSS on 2026-08-26.
# ---------------------------------------------------------------------------
REAL = "REMEDY Raises Series A To Expand Dermatologist-Developed Skincare Brand"
SOFTWARE = "ZyG Raises $60M To Build AI Operating System For eCommerce Scale"
check("physical-product funding headline passes both gates",
      bool(S.VOLUME_EVENT.search(REAL)) and not S.NOT_A_SHIPPER.search(REAL)
      and any(w in REAL.lower() for w in S.PHYSICAL_PRODUCT_WORDS))
check("AI/software funding headline is rejected",
      bool(S.NOT_A_SHIPPER.search(SOFTWARE)),
      "a company that ships no boxes is not a 3PL prospect")

# ---------------------------------------------------------------------------
# A candidate CANNOT exist without evidence.
# ---------------------------------------------------------------------------
c = S.Candidate("shipping_policy", "Some Brand", "https://example.com")
c.note_fit("shopify store, 40 products")
check("fit alone produces no candidate", c.finish() is None,
      "'ships physical products' is not a lead")

c2 = S.Candidate("shipping_policy", "Some Brand", "https://example.com/p")
try:
    c2.signal(30, "slow_self_fulfillment", "", "https://example.com/p")
    check("quote-less signal is refused", False, "no exception raised")
except ValueError:
    check("quote-less signal is refused", True)
try:
    c2.signal(30, "slow_self_fulfillment", "some quote", "")
    check("url-less signal is refused", False, "no exception raised")
except ValueError:
    check("url-less signal is refused", True)

c2.signal(30, "slow_self_fulfillment", "Orders ship in 5-7 business days",
          "https://example.com/p")
fin = c2.finish()
check("evidenced candidate is emitted with a verifiable url",
      fin is not None and fin["pain_signals"][0]["source_url"].startswith("http"))

# ---------------------------------------------------------------------------
# Channel accounting: blocked must never read as empty.
# ---------------------------------------------------------------------------
st = S.ChannelStat("shipping_policy")
st.attempted, st.blocked = 4, 4
check("all-blocked channel reports BLOCKED not DRY", st.verdict() == "BLOCKED",
      st.verdict())
st2 = S.ChannelStat("shipping_policy")
st2.attempted, st2.ok = 4, 4
check("tried-and-genuinely-empty channel reports DRY", st2.verdict() == "DRY",
      st2.verdict())
st3 = S.ChannelStat("reddit")
st3.attempted, st3.blocked = 3, 3
check("known-unavailable channel reports UNAVAILABLE",
      st3.verdict() == "UNAVAILABLE", st3.verdict())

check("known-unavailable channels are excluded from the yield verdict",
      "reddit" in S.KNOWN_UNAVAILABLE and "trustpilot" in S.KNOWN_UNAVAILABLE)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all tests passed")
