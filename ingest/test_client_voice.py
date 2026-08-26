"""Verification for ingest/client_voice.py. Plain asserts, no pytest needed.

Run:  python ingest/test_client_voice.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client_voice import (  # noqa: E402
    _load_profile,
    check_voice,
    draft_reply,
    list_clients,
)

CLIENTS = ["jackson-roofing", "heros-junk", "brilliant-fulfillment"]

POSTS = [
    ("Anyone know a good company?", "Been putting this off for months, finally ready."),
    ("Need help this week", "Got a deadline coming up and I am out of options."),
    ("Recommendations wanted", "Asking the group before I start calling around."),
    ("Question for the neighborhood", "Not sure how this normally works."),
    ("Second opinion?", "Someone already came out and told me one thing."),
    ("Advice appreciated", "First time dealing with anything like this."),
    ("Looking for a referral", "Prefer someone local if possible."),
    ("Is this normal?", "Trying to figure out if this is a real problem."),
]

BFF_FORBIDDEN = [
    "ice pack", "cold pack", "gel pack", "refrigerated truck",
    "melts in transit", "hot truck", "last mile", "faster shipping",
]

failures = []
checks = 0


def ok(cond, msg):
    global checks
    checks += 1
    if not cond:
        failures.append(msg)


print("=" * 70)
print("client_voice.py verification")
print("=" * 70)
print(f"profiles on disk: {list_clients()}")
print()

# ---- 1. every generated draft passes its own gate -------------------------
total_drafts = 0
for slug in CLIENTS:
    p = _load_profile(slug)
    for urgent in (False, True):
        for title, snippet in POSTS:
            text, note = draft_reply(
                slug, p["display_name"], p["trade"], p["default_city"],
                title, snippet, urgent,
            )
            total_drafts += 1
            v = check_voice(text, slug)
            ok(not v, f"{slug} urgent={urgent} '{title}' violations: {v}\n   {text}")
            ok(text.strip() != "", f"{slug} produced an empty draft")
            ok("{" not in text and "}" not in text,
               f"{slug} left an unrendered placeholder: {text}")
            ok(note.startswith(f"voice={slug}"),
               f"{slug} voice_note missing slug: {note}")
print(f"[1] gate: {total_drafts} drafts generated, all checked against check_voice()")

# ---- 2. determinism -------------------------------------------------------
for slug in CLIENTS:
    p = _load_profile(slug)
    for urgent in (False, True):
        for title, snippet in POSTS:
            a, na = draft_reply(slug, p["display_name"], p["trade"],
                                p["default_city"], title, snippet, urgent)
            b, nb = draft_reply(slug, p["display_name"], p["trade"],
                                p["default_city"], title, snippet, urgent)
            ok(a == b, f"{slug} non-deterministic for '{title}'")
            ok(na == nb, f"{slug} non-deterministic voice_note for '{title}'")
print("[2] determinism: same post -> identical draft + note, every client/urgency")

# ---- 3. variety: different posts pick different templates -----------------
for slug in CLIENTS:
    p = _load_profile(slug)
    for urgent in (False, True):
        drafts = {
            draft_reply(slug, p["display_name"], p["trade"], p["default_city"],
                        t, s, urgent)[0]
            for t, s in POSTS
        }
        pool = p["urgent_templates" if urgent else "templates"]
        ok(len(drafts) >= 3,
           f"{slug} urgent={urgent} only {len(drafts)} distinct drafts over {len(POSTS)} posts")
        ok(len(pool) >= 4, f"{slug} urgent={urgent} pool has only {len(pool)} templates")
        print(f"    {slug:<24} urgent={str(urgent):<5} "
              f"{len(drafts)} distinct drafts from {len(pool)} templates")
print("[3] variety: at least 4 templates per bucket, hash spreads across them")

# ---- 4. urgent differs from non-urgent ------------------------------------
for slug in CLIENTS:
    p = _load_profile(slug)
    for title, snippet in POSTS:
        calm = draft_reply(slug, p["display_name"], p["trade"],
                           p["default_city"], title, snippet, False)[0]
        hot = draft_reply(slug, p["display_name"], p["trade"],
                          p["default_city"], title, snippet, True)[0]
        ok(calm != hot, f"{slug} urgent and non-urgent identical for '{title}'")
print("[4] urgency: urgent drafts always differ from the calm variant")

# ---- 5. BFF forbidden language never appears ------------------------------
for urgent in (False, True):
    for title, snippet in POSTS:
        text = draft_reply("brilliant-fulfillment", "Brilliant Fulfillment",
                           "temperature controlled fulfillment", "Dallas",
                           title, snippet, urgent)[0].lower()
        for bad in BFF_FORBIDDEN:
            ok(bad not in text, f"BFF draft contains forbidden '{bad}': {text}")
print("[5] BFF: no ice/cold packs, no trucks, no transit, no faster-shipping framing")

# ---- 6. the gate actually catches bad text --------------------------------
bad_cases = [
    ("We are a cutting-edge roofer", None, "hype"),
    ("Call today, limited time only", None, "urgency"),
    ("Roof repair starting at $199", None, "pricing"),
    ("We do roofing — call us", None, "em dash"),
    ("We ship with ice packs", "brilliant-fulfillment", "BFF ban"),
    ("It melts in transit", "brilliant-fulfillment", "BFF ban"),
    ("Faster shipping fixes this", "brilliant-fulfillment", "BFF ban"),
    ("We charge per load", "heros-junk", "client ban"),
    ("We will handle the insurance claim", "jackson-roofing", "client ban"),
    ("", None, "empty"),
]
for text, slug, label in bad_cases:
    v = check_voice(text, slug)
    ok(bool(v), f"gate MISSED {label}: {text!r}")
print(f"[6] gate negatives: all {len(bad_cases)} known-bad strings rejected")

# ---- 7. unknown slug falls back safely ------------------------------------
text, note = draft_reply("no-such-client", "Someone", "gutters", "Waco",
                         "hello", "world", False)
ok(check_voice(text) == [], f"generic fallback failed the gate: {text}")
ok("_generic" in note, f"fallback note should name the generic profile: {note}")
print("[7] unknown slug: falls back to the generic profile, still clean")

print()
print("=" * 70)
print(f"{checks} assertions run")
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures[:20]:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
print("=" * 70)
print()
print("SAMPLE DRAFTS (one per client, judge the voice)")
print("=" * 70)
samples = [
    ("jackson-roofing", "Water spot on my ceiling after last night",
     "Woke up to a brown ring on the living room ceiling. Not sure what to do.", True),
    ("heros-junk", "Cleaning out my late father's house",
     "The whole garage and two bedrooms need to go before we can list it.", False),
    ("brilliant-fulfillment", "Customers saying our balm arrives grainy",
     "Third complaint this month about texture. We have not changed the formula.", False),
]
for slug, title, snippet, urgent in samples:
    p = _load_profile(slug)
    text, note = draft_reply(slug, p["display_name"], p["trade"],
                             p["default_city"], title, snippet, urgent)
    print()
    print(f"# {p['display_name']}  (urgent={urgent})")
    print(f"  post: {title}")
    print(f"  draft: {text}")
    print(f"  note: {note}")
print()
