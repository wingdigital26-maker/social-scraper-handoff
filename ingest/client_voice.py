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


def _render(template: str, client_name: str, trade: str, city: str) -> str:
    text = template.format(
        client_name=client_name or "",
        trade=trade or "",
        city=city or "",
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


def draft_reply(
    client_slug: str,
    client_name: str,
    trade: str,
    city: str,
    post_title: str,
    post_snippet: str,
    urgent: bool,
) -> Tuple[str, str]:
    """Draft a per-client social reply.

    Returns (draft_text, voice_note). voice_note explains which profile,
    which template bucket and which template index was used, plus any
    fallback that had to happen, so a human reviewer can see the reasoning.

    The draft is guaranteed to pass check_voice() for this client.
    """
    profile = _load_profile(client_slug)
    slug = profile.get("slug") or (client_slug or "_generic")

    trade = (trade or profile.get("trade") or "").strip()
    city = (city or profile.get("default_city") or "").strip()
    client_name = (client_name or profile.get("display_name") or "").strip()

    bucket = "urgent" if urgent else "standard"
    pool = list(profile.get("urgent_templates" if urgent else "templates") or [])
    if not pool:
        pool = list(GENERIC_PROFILE["urgent_templates" if urgent else "templates"])

    key = _post_key(post_title, post_snippet)
    start = _pick_index(key, len(pool), salt=f"{slug}:{bucket}:")

    notes: List[str] = []
    chosen_text = None
    chosen_index = None

    # Deterministic walk: start at the hashed index, then step forward until a
    # template passes the gate. Same post always lands on the same draft.
    for step in range(len(pool)):
        idx = (start + step) % len(pool)
        candidate = _render(pool[idx], client_name, trade, city)
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
        chosen_text = _render(SAFE_FALLBACK, client_name, trade, city)
        chosen_index = -1
        notes.append("every template failed the gate, used the safe fallback line")
        residual = check_voice(chosen_text, slug)
        if residual:
            # Nothing left to do but hand back something inert.
            chosen_text = "Happy to answer questions here if that helps."
            notes.append(f"safe fallback also failed ({'; '.join(residual)}), used inert line")

    label = profile.get("voice_note_label") or slug
    voice_note = (
        f"voice={slug} [{label}] | bucket={bucket} | template="
        f"{'fallback' if chosen_index == -1 else '#' + str(chosen_index)}"
        f" of {len(pool)} | key={key[:10]} | gate=clean"
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
