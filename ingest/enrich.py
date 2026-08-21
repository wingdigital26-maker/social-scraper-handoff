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
import time

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
TEMPLATES = {
    "question": [
        "{place} is a solid pick. If you're into {category} spots around there, a couple more most people miss come to mind. Happy to share.",
        "Was just looking into {place} recently. There's a bit more to that area than people realize if {category} stuff is your thing.",
        "Good question. {place} still comes up a lot. If you want, I can point you at a map of similar {category} spots nearby.",
    ],
    "showcase": [
        "Great shots of {place}. That whole area has a few more {category} spots worth a look if you're ever back.",
        "{place} photographs so well. If you liked it, there are a couple of similar {category} places nearby that get way less traffic.",
        "This is why {place} keeps showing up in my saved posts. Solid find.",
    ],
    "complaint": [
        "Shame about {place}. A few similar {category} spots in the area are still accessible if you're looking for a replacement.",
        "Heard the same about {place}. If it helps, not everything like it in that area is gone yet.",
    ],
    # Prospect outreach: first-touch openers for businesses found via
    # social_discover.py. Deliberately low-pressure and specific — no pitch,
    # no pricing, no link. A human edits and sends these from the queue.
    "prospect": [
        "Been seeing {name} around the {city} {category} scene. Curious how you're currently handling new leads coming in from social.",
        "Came across {name} while looking at {category} companies in {city}. Your work looks solid. Are you doing anything to capture leads off these posts?",
        "{name} keeps showing up in {city} {category} searches, which is a good sign. Is the phone ringing from it, or mostly just views?",
        "Noticed {name} is active on social but I could not find much beyond it. Would it be useful to see what {city} folks are searching for in {category}?",
    ],
}


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


def draft(c: dict, intent: str) -> str | None:
    place = c.get("place") or c.get("name")
    category = c.get("category")
    if not place or not category:
        return None  # can't fill cleanly -> human writes it
    pool = TEMPLATES[intent]
    pick = int(hashlib.sha1(str(c.get("id", place)).encode()).hexdigest(), 16) % len(pool)
    return pool[pick].format(place=place, category=category,
                             name=c.get("name") or place, city=c.get("place") or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(HERE / "candidates.jsonl"))
    ap.add_argument("--out", dest="out", default=str(HERE / "candidates.enriched.jsonl"))
    args = ap.parse_args()

    now = time.time()
    rows, skipped = [], 0
    for line in pathlib.Path(args.inp).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        text = f"{c.get('name', '')} {c.get('title', '')} {c.get('desc', '')}"
        c["intent"] = classify_intent(text, c)
        c["score"] = score(c, now)
        c["draft_reply"] = draft(c, c["intent"])
        rows.append(c)

    rows.sort(key=lambda r: r["score"], reverse=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for c in rows:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    drafted = sum(1 for r in rows if r["draft_reply"])
    print(f"enriched {len(rows)} candidates ({drafted} drafted, {skipped} bad lines) -> {args.out}")


if __name__ == "__main__":
    main()
