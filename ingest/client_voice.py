"""Per-client message voice for the Wing Digital social watcher.

Standalone. No LLM calls, no network, no DB. Pure templates + rules.

Public API
----------
draft_reply(client_slug, client_name, trade, city, post_title, post_snippet, urgent)
    -> (draft_text, voice_note)

check_voice(text, client_slug=None) -> list[str]
    Returns a list of violation strings. Empty list means the text is clean.

list_clients() -> list[str]

Why this exists: watch_social.py used to draft every reply from one hardcoded
line, so a roofer, a junk hauler and a 3PL all sounded like the same bot.
Voice profiles live in ingest/voices/*.json so a human can edit them without
touching Python.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Dict, List, Optional, Tuple

VOICES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voices")

# ---------------------------------------------------------------------------
# Global bans (apply to every client)
# ---------------------------------------------------------------------------

# Jack's standing rule: no em dashes. En dash treated the same when used as
# a sentence break.
_DASH_RE = re.compile(r"[—–]")

HYPE_WORDS = [
    "revolutionary",
    "game-changing",
    "game changing",
    "gamechanging",
    "cutting-edge",
    "cutting edge",
    "synergy",
    "synergies",
    "unlock",
    "unlocks",
    "unlocking",
    "seamless",
    "seamlessly",
    "world-class",
    "best-in-class",
    "state of the art",
    "state-of-the-art",
]

FAKE_URGENCY = [
    "act now",
    "act fast",
    "limited time",
    "limited spots",
    "only a few spots",
    "spots are filling",
    "last chance",
    "don't miss out",
    "dont miss out",
    "hurry",
    "while supplies last",
    "today only",
    "expires soon",
    "book before",
    "slots left",
]

# Any pricing claim at all. We never quote money in a public social reply.
PRICING_PATTERNS = [
    r"\$\s*\d",
    r"\b\d+\s*(?:dollars|bucks)\b",
    r"\b\d+\s*%\s*off\b",
    r"\bpercent off\b",
    # "free audit" was missed here until 2026-08-27: the list was written for
    # trade clients, and Wing's own B2B outreach gives away a different noun.
    r"\bfree (?:estimate|quote|inspection|consultation|haul|pickup|audit|"
    r"review|report|trial|sample|month|mockup|demo|teardown|analysis)\b",
    r"\b(?:on the house|at no charge|my treat)\b",
    r"\bno cost\b",
    r"\bcheapest\b",
    r"\blowest price\b",
    r"\bdiscount(?:ed|s)?\b",
    r"\bspecial pricing\b",
    r"\bbeat any (?:price|quote)\b",
    r"\baffordable rates?\b",
    r"\bstarting at\b",
    r"\bper (?:load|hour|square|pallet)\b",
]
_PRICING_RES = [re.compile(p, re.IGNORECASE) for p in PRICING_PATTERNS]

# Guessing at the poster's situation. Templates must stay generic.
FABRICATION_PATTERNS = [
    r"\bi (?:saw|noticed) (?:your|that you)\b.*\b(?:kids|wife|husband|divorce|moved|died|passed)\b",
    r"\byour (?:mother|father|mom|dad|grandmother|grandfather) (?:passed|died)\b",
    r"\bsince you (?:just|recently) (?:bought|sold|inherited|lost)\b",
]
_FABRICATION_RES = [re.compile(p, re.IGNORECASE) for p in FABRICATION_PATTERNS]


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------

_PROFILE_CACHE: Dict[str, dict] = {}

GENERIC_PROFILE = {
    "slug": "_generic",
    "display_name": "",
    "trade": "",
    "default_city": "",
    "service_area": [],
    "voice_note_label": "generic fallback voice (no profile on file for this slug)",
    "banned_phrases": [],
    "templates": [
        "We do {trade} work around {city}. Happy to answer questions here if it helps.",
        "{trade} crew covering {city}. Ask away if you want a second opinion before you call anybody out.",
        "We work {city} and nearby for {trade}. Glad to walk through what we would check.",
        "Local {trade} outfit in the {city} area. Happy to help either way.",
    ],
    "urgent_templates": [
        "That one sounds time sensitive. We do {trade} in {city} and can take a look.",
        "Worth handling soon. We cover {city} for {trade} if you want somebody out.",
        "We can move quick on that. {trade} crew working {city}.",
        "Do not let that sit. We do {trade} around {city}.",
    ],
    "signoff": "",
}

# Absolute last resort. Deliberately boring and provably clean.
SAFE_FALLBACK = "We do {trade} work around {city}. Happy to answer questions here if that helps."


def _load_profile(slug: str) -> dict:
    slug = (slug or "").strip().lower()
    if slug in _PROFILE_CACHE:
        return _PROFILE_CACHE[slug]
    path = os.path.join(VOICES_DIR, f"{slug}.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            profile = json.load(fh)
    else:
        profile = dict(GENERIC_PROFILE)
    # Fill any missing keys from the generic profile so callers never KeyError.
    for key, value in GENERIC_PROFILE.items():
        profile.setdefault(key, value)
    _PROFILE_CACHE[slug] = profile
    return profile


def list_clients() -> List[str]:
    if not os.path.isdir(VOICES_DIR):
        return []
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(VOICES_DIR)
        if f.endswith(".json")
    )


# ---------------------------------------------------------------------------
# The voice gate
# ---------------------------------------------------------------------------


def check_voice(text: str, client_slug: Optional[str] = None) -> List[str]:
    """Return a list of voice violations. Empty list means clean."""
    violations: List[str] = []
    if not text or not text.strip():
        return ["empty draft"]

    low = text.lower()

    if _DASH_RE.search(text):
        violations.append("em dash or en dash used (Jack's standing rule: never)")

    for word in HYPE_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", low):
            violations.append(f"hype word: {word}")

    for phrase in FAKE_URGENCY:
        if phrase in low:
            violations.append(f"fake urgency/scarcity: {phrase}")

    for rx in _PRICING_RES:
        m = rx.search(text)
        if m:
            violations.append(f"pricing claim: {m.group(0).strip()}")

    for rx in _FABRICATION_RES:
        m = rx.search(text)
        if m:
            violations.append(f"fabricated detail about the poster: {m.group(0).strip()}")

    if client_slug:
        profile = _load_profile(client_slug)
        for phrase in profile.get("banned_phrases", []):
            if phrase.lower() in low:
                violations.append(
                    f"{profile.get('slug', client_slug)} forbidden phrase: {phrase}"
                )

    # De-dupe, keep order.
    seen = set()
    out = []
    for v in violations:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# Buying-intent scoring.
#
# WHY THIS EXISTS. watch_social.py has its own relevance.py gate upstream that
# scores topic/geo/recency. What it does NOT check is whether the specific
# words in the post look like a customer buying, versus a bystander, a
# competitor, or someone talking about something else entirely that merely
# shares vocabulary with the trade. Two real Supabase rows proved this:
#
#   Row 64: r/okc, "The Gold Dome - What would you do?" scored 0.80 and got a
#     junk-removal draft. The post is not about junk removal, hauling, moving,
#     or anything the client sells. It is a local-landmark discussion thread.
#     There is no buying-intent language anywhere in it because there is
#     nothing to buy. draft_reply used to ignore post_title/post_snippet
#     entirely when picking a template, so a mismatch like this could sail
#     through with a fully generic capability statement.
#
#   Row 65: r/PPC, "First Paid Google Ads Campaign Looking for Advice" scored
#     0.89, the HIGHEST score in the batch. r/PPC is a marketing/advertising
#     subreddit; the poster is asking about running ads, not asking for a
#     junk-removal crew. The drafted body ("junk removal crew working Dallas
#     and the metroplex...") is the same generic capability line, again
#     because nothing checked the post content against buying intent.
#
# This scorer is deliberately NOT a duplicate of relevance.py's topic/geo
# gate. It answers one narrower, more dangerous question: does the text of
# THIS post read like a person who wants this service right now, or does it
# read like a competitor advertising, a marketing/business discussion, or a
# news/chat thread that happens to share a word or two with the trade?
#
# Score is 0-1. Every signal that fired is recorded by name in `signals` so a
# human can see exactly why a post scored what it scored — no bare float.
# ---------------------------------------------------------------------------

# Strong buying-intent language: an explicit ask for a provider/recommendation.
_INTENT_ASK_STRONG = re.compile(
    r"(anyone\s+(know|recommend|have|used|dealt\s+with|worked\s+with)|"
    r"can\s+anyone\s+recommend|"
    r"any\s+recommendations?|looking\s+for\s+(a|an|someone)|"
    r"need\s+(a|an|someone|help)|in\s+need\s+of|who\s+(do|did)\s+you\s+use|"
    r"recommendations?\s+for|any\s+suggestions?|does\s+anyone\s+know|"
    r"where\s+(can|should)\s+i|has\s+anyone\s+used|any\s+quotes?|"
    # DISPOSAL DEMAND. Haul-away asks are not worded like a contractor
    # recommendation, which is why "Need to get rid of a couch ... need it
    # hauled" scored 0.00 before these. trade_vocab.py records the same
    # lesson: the demand phrase is "need to get rid of", never "junk
    # removal". The verb carries the intent, so the noun can be anything.
    r"need\s+(to\s+)?(get\s+rid\s+of|dispose|haul|clear|remove)|"
    r"(need|want)s?\s+(it|them|this|these|everything|all\s+of\s+it)\s+(hauled|gone|removed|cleared|picked\s+up|out)|"
    r"(have|has|needs?)\s+to\s+(go|be\s+(hauled|gone|removed|cleared))|"
    r"(haul|hauling)\s+(it|this|them|away)|come\s+(get|pick)\s+(it|this|them)|"
    r"(cleanout|clean\s?out|junk\s+haul)\s+(help|needed|wanted)|"
    r"looking\s+to\s+pay\s+someone|"
    # "HELP NEEDED - GARAGE CLEANOUT" - the ask is inverted, and the noun
    # after haul is a thing, not a pronoun. Both from real DFW postings.
    r"(help|labor|crew|hauler|truck)\s+needed|"
    r"haul\s+(trash|junk|debris|furniture|stuff|items|everything|off))",
    re.I,
)
# Softer ask language: still worth something, on its own not enough.
_INTENT_ASK_SOFT = re.compile(
    r"(recommend|suggestions?|referral|quote|estimate\s+for|help\s+with|"
    r"thoughts\s+on|how\s+much\s+(should|does))", re.I,
)
# The poster describing their own problem in first person ("my garage is
# packed", "we have a bunch of junk", "our roof is leaking").
_INTENT_OWN_PROBLEM = re.compile(
    r"\b(my|our)\s+[a-z]{2,20}\s+(is|are|has|have|needs?|full\s+of|packed\s+with|"
    r"piled\s+with|leaking|damaged|broken|falling\s+apart)\b|"
    r"\b(needs?\s+to\s+(happen|be\s+(done|hauled|cleared|removed|handled))|"
    r"has\s+to\s+(happen|be\s+(done|hauled|cleared|removed|handled)))\b", re.I,
)
_INTENT_FIRST_PERSON_NEED = re.compile(
    r"\b(i|we)\s+(need|needed|want|wanted|have\s+to|'?m\s+trying\s+to|"
    r"are\s+trying\s+to)\b", re.I,
)
_INTENT_URGENCY = re.compile(
    r"\b(asap|urgent|emergency|today|tomorrow|this\s+week|right\s+away|"
    r"before\s+(the\s+)?(move|weekend|closing))\b", re.I,
)

# NEGATIVE signals. Any one of these should crush the score toward zero,
# because it means the post is not a customer buying the service.

# Marketing / business / advice subreddits and forums. A post that lives here
# is about running a business, not hiring a junk hauler or a roofer.
_MARKETING_CONTEXT = re.compile(
    r"\b(ppc|seo|adwords|google\s*ads|facebook\s*ads|paid\s+ads?|ad\s+campaign|"
    r"ctr|cpc|cpa|roas|marketing\s+(agency|strategy|budget)|"
    r"small\s*business(?!\s+(saturday))|entrepreneur|growing\s+my\s+business|"
    r"my\s+startup|conversion\s+rate|landing\s+page|sales\s+funnel)\b", re.I,
)
# Competitor / vendor voice: the poster is OFFERING the service, not buying it.
_COMPETITOR_VOICE = re.compile(
    r"\b(we|i)\s+(do|offer|run|provide|specialize\s+in|handle)\s+[a-z ]{0,20}"
    r"(junk|hauling|roofing|removal|repair|installs?|cleanouts?)\b|"
    r"\b(call|text|dm|message)\s+(us|me)\s+(today|now|for)\b|"
    r"\bfree\s+(estimate|quote|inspection)\b|"
    r"\blicensed\s+(and|&)\s+insured\b|"
    r"\bbook\s+(now|today|online)\b|"
    r"\bserving\s+[a-z .]{2,25}\s+(since|for)\b", re.I,
)
# General news / chat / opinion threads: "what would you do", "thoughts?",
# "AITA", "does anyone else" with no service ask attached.
_DISCUSSION_THREAD = re.compile(
    r"\bwhat\s+would\s+you\s+do\b|\bam\s+i\s+the\s+only\s+one\b|"
    r"\bdoes\s+anyone\s+else\b(?!.*\b(need|recommend|know\s+a))|"
    r"\baita\b|\bjust\s+curious\b|\bunpopular\s+opinion\b|"
    r"\bbreaking\s*:?\s*news\b", re.I,
)


def score_buying_intent(post_title: str, post_snippet: str) -> dict:
    """Score whether a post reads like someone who wants the service NOW.

    Returns {"score": float 0-1, "signals": [str], "verdict": "buy"|"reject"}.
    `signals` lists every rule that fired, positive and negative, so a human
    reviewer never has to reverse-engineer a bare number. Negative signals
    (competitor advertising, marketing-subreddit context, discussion/news
    threads) crush the score toward zero regardless of how many keywords the
    post happens to share with the trade -- topic overlap is not intent.
    """
    text = f"{post_title or ''} {post_snippet or ''}".strip()
    signals: List[str] = []

    if not text:
        return {"score": 0.0, "signals": ["no post text to judge"], "verdict": "reject"}

    negative_hit = False
    if _MARKETING_CONTEXT.search(text):
        signals.append("NEGATIVE: marketing/business/advertising context, not a service request")
        negative_hit = True
    if _COMPETITOR_VOICE.search(text):
        signals.append("NEGATIVE: competitor/vendor voice, the poster is advertising, not buying")
        negative_hit = True
    if _DISCUSSION_THREAD.search(text):
        signals.append("NEGATIVE: generic discussion/news/opinion thread, no service ask")
        negative_hit = True

    positive = 0.0
    if _INTENT_ASK_STRONG.search(text):
        positive = max(positive, 0.75)
        signals.append("POSITIVE: explicit ask for a recommendation/provider")
    elif _INTENT_ASK_SOFT.search(text):
        positive = max(positive, 0.4)
        signals.append("POSITIVE: soft ask language (recommend/quote/estimate)")
    if _INTENT_OWN_PROBLEM.search(text):
        positive = max(positive, 0.7)
        signals.append("POSITIVE: describes their own problem the service solves")
    if _INTENT_FIRST_PERSON_NEED.search(text):
        positive = min(1.0, positive + 0.2)
        signals.append("POSITIVE: first-person statement of need")
    if _INTENT_URGENCY.search(text):
        positive = min(1.0, positive + 0.15)
        signals.append("POSITIVE: urgency language")
    if "?" in text and positive > 0:
        positive = min(1.0, positive + 0.05)
        signals.append("POSITIVE: framed as a question")

    if negative_hit:
        # Crushed regardless of positive signals. A competitor ad or a
        # marketing-subreddit thread does not become a lead just because it
        # also contains the word "quote" or a question mark.
        score = min(positive, 1.0) * 0.05
    else:
        score = positive

    if not signals or (not negative_hit and positive == 0.0):
        signals.append("no buying-intent language found: no ask, no problem "
                        "description, no urgency -- topic words alone are not intent")

    verdict = "buy" if (score >= 0.4 and not negative_hit) else "reject"
    return {"score": round(score, 3), "signals": signals, "verdict": verdict}


# Minimum buying-intent score required before draft_reply will produce
# anything at all. Below this, or on any negative signal, no draft is made.
MIN_BUYING_INTENT = 0.4


def _extract_situation_detail(post_title: str, post_snippet: str) -> Optional[str]:
    """Pull a short, VERBATIM fragment describing the poster's own situation.

    Never paraphrases and never invents wording -- it only returns text that
    already appears in the post, trimmed to a clause. Returns None when no
    real situational detail can be found, which is the honest outcome for a
    lot of posts (a bare "anyone recommend a good guy?" with no other detail
    has nothing specific to quote back).
    """
    text = f"{post_title or ''}. {post_snippet or ''}"
    for rx in (_INTENT_OWN_PROBLEM, _INTENT_FIRST_PERSON_NEED, _INTENT_ASK_STRONG):
        m = rx.search(text)
        if not m:
            continue
        # Walk back to the start of the clause (last clause boundary before
        # the match, or the start of the containing sentence) so the fragment
        # reads as a real phrase instead of starting mid-clause.
        start = m.start()
        back_boundary = -1
        for boundary in (".", "?", "!", ","):
            idx = text.rfind(boundary, 0, start)
            if idx > back_boundary:
                back_boundary = idx
        if back_boundary != -1:
            start = back_boundary + 1
        while start < m.start() and text[start] in " \t":
            start += 1
        # Walk forward to the next clause boundary.
        end = m.end()
        for boundary in (".", "?", "!", ","):
            idx = text.find(boundary, m.end())
            if idx != -1 and (end == m.end() or idx < end):
                end = idx
        if end == m.end():
            end = min(len(text), m.end() + 60)
        frag = text[start:end].strip(" .,!?")
        if 8 <= len(frag) <= 120:
            return frag
    return None


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


def _post_key(post_title: str, post_snippet: str) -> str:
    """Stable key for a post. Callers may pass the post URL as post_title if
    that is the more stable identifier; either way the same input maps to the
    same draft forever."""
    raw = f"{(post_title or '').strip()}||{(post_snippet or '').strip()[:400]}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _pick_index(key: str, n: int, salt: str = "") -> int:
    if n <= 0:
        return 0
    h = hashlib.sha256((salt + key).encode("utf-8")).hexdigest()
    return int(h[:16], 16) % n


def _render(template: str, client_name: str, trade: str, city: str, detail: str = "") -> str:
    text = template.format(
        client_name=client_name or "",
        trade=trade or "",
        city=city or "",
        detail=detail or "",
    )
    # Collapse artifacts from empty substitutions.
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def _append_signoff(body: str, signoff: str) -> str:
    """Add the client's signoff, dropping any sentence the body already says."""
    signoff = (signoff or "").strip()
    if not signoff:
        return body
    body_low = body.lower()
    keep = [
        s.strip()
        for s in re.split(r"(?<=[.?!])\s+", signoff)
        if s.strip() and s.strip().lower() not in body_low
    ]
    if not keep:
        return body
    return f"{body} {' '.join(keep)}".strip()


_DETAIL_TEMPLATES = [
    "Saw you mentioned {detail}. We do {trade} work around {city} and can help with that if you want a hand.",
    "For what it's worth on \"{detail}\": that is the kind of job we handle around {city} for {trade}. Happy to help if useful.",
    "Re: {detail} -- we cover {city} for {trade} and can take that off your hands if you want.",
]
_DETAIL_URGENT_TEMPLATES = [
    "Saw you mentioned {detail} -- that sounds time sensitive. We do {trade} around {city} and can move on it.",
    "Re: {detail}. Worth handling soon. We cover {city} for {trade} if you want somebody out quick.",
]


def draft_reply(
    client_slug: str,
    client_name: str,
    trade: str,
    city: str,
    post_title: str,
    post_snippet: str,
    urgent: bool,
) -> Tuple[Optional[str], str]:
    """Draft a per-client social reply, or refuse to.

    Returns (draft_text, voice_note). draft_text is None when the post does
    not clear the buying-intent bar or when no genuine, specific detail about
    the poster's situation can be found -- an empty queue is a valid, honest
    result. voice_note always explains the reasoning (intent score, signals
    fired, which template bucket/index was used, any fallback) so a human
    reviewer never has to guess why something did or did not get drafted.

    When a draft IS produced, it references a verbatim fragment of what the
    poster actually said. Nothing about the poster is invented. The draft is
    guaranteed to pass check_voice() for this client.
    """
    profile = _load_profile(client_slug)
    slug = profile.get("slug") or (client_slug or "_generic")

    trade = (trade or profile.get("trade") or "").strip()
    city = (city or profile.get("default_city") or "").strip()
    client_name = (client_name or profile.get("display_name") or "").strip()

    intent = score_buying_intent(post_title, post_snippet)
    intent_note = (
        f"buying-intent score={intent['score']:.2f} verdict={intent['verdict']} | "
        + " ; ".join(intent["signals"])
    )
    if intent["verdict"] != "buy" or intent["score"] < MIN_BUYING_INTENT:
        return None, (
            f"voice={slug} | NO DRAFT: post failed the buying-intent gate | "
            + intent_note
        )

    detail = _extract_situation_detail(post_title, post_snippet)
    if not detail:
        return None, (
            f"voice={slug} | NO DRAFT: post shows buying intent but no specific, "
            f"real detail about the poster's situation could be quoted -- refusing "
            f"to send a generic capability statement | " + intent_note
        )

    bucket = "urgent" if urgent else "standard"
    pool = list(_DETAIL_URGENT_TEMPLATES if urgent else _DETAIL_TEMPLATES)

    key = _post_key(post_title, post_snippet)
    start = _pick_index(key, len(pool), salt=f"{slug}:{bucket}:detail:")

    notes: List[str] = []
    chosen_text = None
    chosen_index = None

    # Deterministic walk: start at the hashed index, then step forward until a
    # template passes the gate. Same post always lands on the same draft.
    for step in range(len(pool)):
        idx = (start + step) % len(pool)
        candidate = _render(pool[idx], client_name, trade, city, detail)
        candidate = _append_signoff(candidate, profile.get("signoff") or "")
        problems = check_voice(candidate, slug)
        if not problems:
            chosen_text = candidate
            chosen_index = idx
            if step:
                notes.append(f"stepped forward from #{start} to #{idx}")
            break
        notes.append(f"template #{idx} rejected: {'; '.join(problems)}")

    if chosen_text is None:
        # Every detail-referencing template failed the voice gate (usually
        # because the poster's own words happen to contain a banned phrase,
        # e.g. a price). Per the hard rule, a generic capability statement is
        # not an acceptable substitute for a personalized reply -- refuse.
        notes.append("every detail-referencing template failed the voice gate, no draft made")
        return None, (
            f"voice={slug} | NO DRAFT: {'; '.join(notes)} | " + intent_note
        )

    label = profile.get("voice_note_label") or slug
    voice_note = (
        f"voice={slug} [{label}] | bucket={bucket} | template=#{chosen_index}"
        f" of {len(pool)} | key={key[:10]} | detail=\"{detail}\" | "
        f"{intent_note} | gate=clean"
    )
    if notes:
        voice_note += " | " + " ; ".join(notes)
    return chosen_text, voice_note


if __name__ == "__main__":  # pragma: no cover
    for s in list_clients():
        p = _load_profile(s)
        d, n = draft_reply(
            s, p["display_name"], p["trade"], p["default_city"],
            "sample post title", "sample post body", False,
        )
        print(f"--- {s} ---\n{d}\n{n}\n")
