"""Score + classify intent + draft a templated reply for every candidate.

Pure Python, no AI. Reads candidates.jsonl, writes candidates.enriched.jsonl
(same rows with score / intent / draft_reply added). See specs/02-review-queue.md.

    python enrich.py                # candidates.jsonl -> candidates.enriched.jsonl
    python enrich.py --in x.jsonl --out y.jsonl
"""
import argparse
import hashlib
import json
import math
import pathlib
import sys
import time

# Windows consoles default to cp1252 and business names routinely contain
# symbols and emoji it cannot encode. Without this, printing a single
# prospect name raises UnicodeEncodeError and kills the whole run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = pathlib.Path(__file__).resolve().parent

# --- scoring weights (tune after a week of real data) -----------------------
W_VELOCITY, W_KEYWORD, W_RECENCY, W_LOCATION = 0.35, 0.35, 0.20, 0.10

# High-intent phrases: someone ASKING is a warm signal, worth double.
HOT_KEYWORDS = [
    "where is", "anyone know", "how do i get", "how to get in", "looking for",
    "any recommendations", "does anyone", "can someone", "need help finding",
    "what happened to", "is it still",
]
NORMAL_KEYWORDS = [
    "abandoned", "urbex", "ghost town", "ruins", "rooftop", "tunnel",
    "hidden", "secret", "underrated",
]
COMPLAINT_KEYWORDS = ["closed down", "torn down", "demolished", "fenced off", "no trespassing"]

# --- reply skeletons, rotated deterministically per post ---------------------
# Same rule as the prospect lane below: no line may assert something this file
# cannot source from the candidate row. Removed claims included "a couple more
# most people miss come to mind", "Was just looking into {place} recently",
# "similar {category} places nearby that get way less traffic" and "{place}
# keeps showing up in my saved posts" — enrich.py holds no index of nearby
# spots, no traffic data and no browsing history, so all of those were invented.
TEMPLATES = {
    "question": [
        "{place} comes up a lot for {category}. Happy to dig around and share whatever else I can turn up in that area.",
        "Good question — {place} is the one that gets asked about most. If it helps I can look into what else nearby fits the {category} brief.",
        "{place} still gets asked about. I can have a look for other {category} spots in that area if that's useful.",
    ],
    "showcase": [
        "Great shots of {place}. Are there other {category} spots around there you'd rate?",
        "{place} photographs really well. Was it easy to get to?",
        "Solid find. {place} is one I keep seeing come up for {category}.",
    ],
    "complaint": [
        "Shame about {place}. Has anything else {category} in that area gone the same way?",
        "Heard the same about {place}. Do you know if that's the whole site or just part of it?",
    ],
    # Prospect outreach: first-touch OPENING STUBS for businesses found via
    # social_discover.py.
    #
    # These are stubs, not finished outreach. enrich.py knows exactly four
    # things about a prospect: the name, the search niche, the search city and
    # the platform the profile was indexed on. It has NOT looked at their work,
    # their website, their ranking or their lead flow. Every line here is
    # limited to those four facts on purpose.
    #
    # The previous templates asserted things nobody measured — "your work looks
    # solid", "keeps showing up in {city} {category} searches, which is a good
    # sign", "active on social but I could not find much beyond it". Those are
    # fabrications: enrich.py has no rank data, no site crawl and no judgment of
    # anyone's work. They are gone. Do not reintroduce a claim this file cannot
    # source from the candidate row.
    #
    # NOTE the city is the CITY WE SEARCHED, not a verified location for this
    # business, so no stub states it back to them as fact.
    "prospect": [
        "Found {name} while looking through {category} businesses on {platform}. Quick question rather than a pitch — how are you handling leads that come in from social right now?",
        "Came across {name} on {platform} while going through {category} companies. Mind if I ask what you're doing today to catch enquiries off your posts?",
        "{name} came up while I was searching {category} on {platform}. Curious whether social is actually sending you calls, or mostly just views.",
        "Ran into {name} on {platform} looking at {category} businesses. Is capturing leads from social something you've tried to set up yet?",
    ],
}

# Every prospect stub above is a rotation, not personalization. Anything that
# goes out must be rewritten against real researched facts (research_leads.py /
# audit_prospect.py output) before a human approves it.
PROSPECT_DRAFT_NOTE = ("TEMPLATED STUB - not personalized. Rewrite against researched "
                       "facts before sending. Known facts only: name, search niche, "
                       "search city (unverified), indexed platform.")


def classify_intent(text: str, c: dict | None = None) -> str:
    # Prospects (from social_discover.py) get the outreach lane, not the
    # spot-commentary lanes — they were found by niche+city, not by a post.
    if c and c.get("prospect_type"):
        return "prospect"
    t = text.lower()
    if any(k in t for k in HOT_KEYWORDS):
        return "question"
    if any(k in t for k in COMPLAINT_KEYWORDS):
        return "complaint"
    return "showcase"


def keyword_strength(text: str) -> float:
    t = text.lower()
    hot = sum(1 for k in HOT_KEYWORDS if k in t)
    normal = sum(1 for k in NORMAL_KEYWORDS if k in t)
    # hot hits count double; saturate at 4 points
    return min(hot * 2 + normal, 4) / 4


def score(c: dict, now: float) -> float:
    posted = c.get("created_utc") or 0
    days = max((now - posted) / 86400, 0.5) if posted else 30.0
    upvotes = c.get("upvotes") or 0
    velocity = min(math.log1p(upvotes / days), 5) / 5
    text = f"{c.get('name', '')} {c.get('title', '')} {c.get('desc', '')}"
    kw = keyword_strength(text)
    recency = math.exp(-days / 14)
    loc = c.get("location_confidence") or 0
    return round(W_VELOCITY * velocity + W_KEYWORD * kw + W_RECENCY * recency + W_LOCATION * loc, 4)


def platform_of(c: dict) -> str | None:
    """Where this candidate was indexed, from its own row. No guessing."""
    for e in c.get("embeds") or []:
        if isinstance(e, dict) and e.get("type"):
            return str(e["type"])
    return c.get("source") or None


def draft(c: dict, intent: str) -> tuple[str | None, list[str]]:
    """Return (draft_text, missing_fields).

    A non-empty `missing` list is the honest empty state: it names exactly which
    facts were absent, instead of returning a silent None that reads as "nothing
    to say here".
    """
    pool = TEMPLATES[intent]
    if intent == "prospect":
        need = {"name": c.get("name"),
                "category": c.get("category"),
                "platform": platform_of(c)}
    else:
        need = {"place": c.get("place") or c.get("name"),
                "category": c.get("category")}
    missing = sorted(k for k, v in need.items() if not v)
    if missing:
        return None, missing  # can't fill honestly -> a human writes it

    seed = str(c.get("id") or need.get("name") or need.get("place"))
    pick = int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(pool)
    return pool[pick].format(**need), []


def score_basis(c: dict) -> str:
    """Name which signals actually fed the score.

    Index-discovered prospects carry no upvotes and no post timestamp, so
    velocity and recency contribute nothing and every one of them lands on an
    identical number. That number is not a ranking, and this field says so
    rather than letting a false ordering look authoritative.
    """
    have, absent = [], []
    (have if c.get("upvotes") else absent).append("engagement")
    (have if c.get("created_utc") else absent).append("recency")
    (have if c.get("location_confidence") else absent).append("location_confidence")
    text = f"{c.get('name', '')} {c.get('title', '')} {c.get('desc', '')}"
    (have if keyword_strength(text) else absent).append("keywords")
    if not have:
        return "no scoring signal present; score is not a ranking"
    return "from " + ", ".join(have) + ("; missing " + ", ".join(absent) if absent else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(HERE / "candidates.jsonl"))
    ap.add_argument("--out", dest="out", default=str(HERE / "candidates.enriched.jsonl"))
    ap.add_argument("--allow-empty", action="store_true",
                    help="treat an empty input as success (default: empty input is a failure, "
                         "because a scorer that scores nothing on a cron is a broken step)")
    args = ap.parse_args()

    inp = pathlib.Path(args.inp)
    if not inp.exists():
        sys.exit(f"FAIL: input not found: {inp}. Nothing upstream produced candidates.")

    now = time.time()
    rows, skipped, attempts = [], 0, 0
    for line in inp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        attempts += 1
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        text = f"{c.get('name', '')} {c.get('title', '')} {c.get('desc', '')}"
        c["intent"] = classify_intent(text, c)
        c["score"] = score(c, now)
        c["score_basis"] = score_basis(c)
        body, missing = draft(c, c["intent"])
        c["draft_reply"] = body
        # Provenance travels with the draft so no downstream reader can mistake
        # a rotated skeleton for personalized outreach.
        c["draft_is_template"] = bool(body)
        c["draft_needs_rewrite"] = bool(body)
        c["draft_missing_fields"] = missing
        if body and c["intent"] == "prospect":
            c["draft_note"] = PROSPECT_DRAFT_NOTE
        rows.append(c)

    # --- zero-yield gate ----------------------------------------------------
    # House rule: zero yield with non-zero attempts is a hard failure. A scorer
    # that silently scores nothing and exits 0 hides its own breakage.
    if attempts and not rows:
        sys.exit(f"FAIL: read {attempts} candidate line(s) from {inp} but enriched 0 "
                 f"({skipped} unparseable). Nothing written.")
    if not attempts and not args.allow_empty:
        sys.exit(f"FAIL: {inp} is empty - upstream discovery produced no candidates. "
                 f"Nothing to score. Re-run with --allow-empty if an empty batch is expected.")

    rows.sort(key=lambda r: r["score"], reverse=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for c in rows:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    drafted = [r["draft_reply"] for r in rows if r["draft_reply"]]
    undrafted = len(rows) - len(drafted)
    dupes = len(drafted) - len(set(drafted))
    print(f"enriched {len(rows)} candidates ({len(drafted)} drafted, {undrafted} left for a human, "
          f"{skipped} bad lines) -> {args.out}")
    if dupes:
        # Surfaced, not hidden: a rotation of N skeletons repeats by design.
        print(f"  NOTE: {dupes} of {len(drafted)} drafts are byte-identical to another draft. "
              f"These are templated stubs, not personalized outreach - rewrite before sending.")


if __name__ == "__main__":
    main()
