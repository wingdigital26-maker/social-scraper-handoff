#!/usr/bin/env python3
"""draft_message.py — turn ONE verified fact into ONE human-sounding message.

THE BOUNDARY (Jack's instruction, verbatim):
    "if we need seedance to look over it for free then thats ok especially with
     writing messages or emails but i want the scfrapping to be done by
     completly pyhton code"

So: scraping and fact-finding are 100% pure Python (personalize.py, untouched).
A free model is permitted for exactly ONE job here: taking a fact that Python
already verified and rephrasing it into a sentence a person would write.

    A model may REPHRASE a fact.
    A model may NEVER discover, infer, or supply a fact.

That boundary is enforced mechanically, not by prompt politeness. Every draft
the model returns is run through verify_draft() BEFORE it can be stored:

  * every digit-run in the draft must already exist in the input fact or the
    caller-supplied context
  * every spelled-out number must map to a number that already exists there
  * every proper noun must already exist there (or be an ordinary English word)
  * every quoted span must be a verbatim span of the input fact
  * no unverifiable compliment ("your work looks solid", "you keep showing up
    in X searches") — those are banned by pattern, because this system has
    actually produced both
  * client_voice.check_voice() must come back clean
  * at least one content word from the fact must survive into the draft, or it
    is not personalized at all

A draft that fails ANY of those is REJECTED, never edited. Rejection falls back
to the deterministic template and the result says method="template" plus the
exact reason. Nothing unverified is ever returned as if it had been checked.

This is the same discipline intel_propose.py uses on transcript quotes, and for
the same reason: an agent fabricated a claim once, so verification stopped being
optional.

Fabrications this system has already shipped, which the verifier is aimed at:
    "Your work looks solid"                 nobody looked at their work
    "keeps showing up in {city} searches"   no rank data existed
    "we do roofing around Plano"            sent to posts in AZ, OH, WI and SK

Sonar NEVER sends. This module drafts; a human approves; Grant sends.

Usage:
    python draft_message.py --limit 10               # real leads from Supabase
    python draft_message.py --limit 10 --voice heros-junk
    python draft_message.py --limit 10 --no-model    # force the fallback lane
    python draft_message.py --show-regexes
    python draft_message.py --self-test              # offline, no network
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import client_voice  # noqa: E402  (same package, path fixed above)

try:  # Windows consoles are cp1252 and business names are not.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------------------
# The free-model lane. Imported lazily so this module is usable with no network.
# ---------------------------------------------------------------------------

ROUTER_PATH = os.environ.get("LLM_ROUTER_PATH", r"C:\Users\wjack\ghl-cli\llm_router.py")

# gpt-oss on Groq spends completion tokens on hidden reasoning before it writes
# a single visible character. At max_tokens=20 it returned an EMPTY string for
# a prompt as small as "Reply with exactly: OK" (verified live 2026-08-27,
# 60 completion tokens consumed to emit two characters). A stingy budget here
# does not produce a short message, it produces no message.
MODEL_ALIAS = "voice"
MAX_TOKENS = 700
TEMPERATURE = 0.8


class ModelUnavailable(Exception):
    """The free lane could not produce anything. Never fatal, always reported."""


def _load_router():
    """Import llm_router by path. Returns the module, or raises ModelUnavailable."""
    if os.environ.get("SONAR_NO_MODEL"):
        raise ModelUnavailable("SONAR_NO_MODEL is set (model lane disabled)")
    import importlib.util
    if not os.path.isfile(ROUTER_PATH):
        raise ModelUnavailable(f"router not found at {ROUTER_PATH}")
    spec = importlib.util.spec_from_file_location("llm_router", ROUTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # pragma: no cover
        raise ModelUnavailable(f"router import failed: {str(e)[:120]}")
    return mod


# ---------------------------------------------------------------------------
# Regexes. Printed by --show-regexes, per the project rule.
# ---------------------------------------------------------------------------

# Digits appearing in the draft. Every one must be traceable.
DIGITS_RE = re.compile(r"\d+")

# Anything the model chose to put in quotes. Must be verbatim from the fact.
# Apostrophes are deliberately NOT delimiters here: treating them as such made
# "Helsley Roofing's expertise ... it's worth" look like a quoted span and
# false-rejected honest drafts (seen live on lead 15 on 2026-08-27).
QUOTED_RE = re.compile(r"[\"\u201c\u201d]([^\"\u201c\u201d]{3,80})[\"\u201c\u201d]")

# Word-ish tokens, apostrophes kept so "don't" stays one token.
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-\.]*")

# Sentence starts, so a capital at position 0 is not read as a proper noun.
SENT_START_RE = re.compile(r"(?:^|(?<=[.!?])\s+|(?<=\n))([A-Z][A-Za-z'\-]*)")

# URLs. Only the caller's source_url may appear.
URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)

# Claims about work, reputation or rankings that nobody measured. These are
# the exact shapes this system has already shipped, plus their near neighbours.
UNVERIFIABLE_PATTERNS = [
    r"\byour (?:work|craftsmanship|jobs?|installs?|builds?) (?:looks?|seems?|is|are)\b",
    r"\b(?:looks|seems) (?:solid|great|good|impressive|professional|clean)\b",
    r"\bkeeps? (?:showing|coming) up\b",
    r"\b(?:rank(?:ing|s)?|ranked|position) (?:well|high|top|first|number)\b",
    r"\byour (?:reviews?|ratings?|reputation) (?:are|is)\b",
    r"\b(?:i|we) (?:saw|noticed|watched|checked out|came across) your (?:work|jobs?|crew|team|trucks?)\b",
    r"\byou'?re (?:clearly|obviously|one of)\b",
    r"\b(?:everyone|people|customers|folks) (?:say|says|love|loves|rave)\b",
    r"\byou (?:must|probably) (?:be|have|get)\b",
    r"\b(?:busiest|best|top|leading|biggest|fastest growing) (?:in|around|near)\b",
    r"\bi(?:'ve| have) been following\b",
    r"\byour (?:google|facebook|instagram|yelp) (?:page|profile|presence) (?:is|looks)\b",
    # Inference dressed as fact: the model deciding what a credential PROVES
    # about the recipient. Listing a badge is a fact. Meeting a standard is not.
    r"\b(?:that|which|this|it) (?:signals|means|proves|shows|confirms) (?:you|your)\b",
]
_UNVERIFIABLE_RES = [re.compile(p, re.IGNORECASE) for p in UNVERIFIABLE_PATTERNS]

# Model chatter that is not part of a message.
PREAMBLE_PATTERNS = [
    r"^\s*(?:sure|certainly|here'?s|here is|of course|absolutely)\b[^\n]*[:\n]",
    r"^\s*(?:draft|message|email|subject)\s*:",
    r"^\s*```",
]
_PREAMBLE_RES = [re.compile(p, re.IGNORECASE) for p in PREAMBLE_PATTERNS]

# Placeholders the model left unfilled.
PLACEHOLDER_RE = re.compile(r"[\[\{<](?:[A-Za-z_ ]{2,30})[\]\}>]")

# Em/en dash. personalize.py writes facts with them; Jack's rule bans them in
# outgoing copy. So the FACT is normalized before drafting rather than the
# draft being punished for the fact's own punctuation.
_FACT_DASH_RE = re.compile(r"\s*[\u2014\u2013]\s*")


NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000,
}
# Counting words that carry no factual weight, so they are not policed.
NUMBER_WORDS_FREE = {"one", "two", "zero"}

MONTHS = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}

# Ordinary words that get capitalized mid-sentence without being proper nouns.
# Anything NOT in here and NOT in the allowed lexicon is treated as a fabricated
# proper noun and kills the draft.
COMMON_CAPS = {
    "i", "i'm", "i've", "i'll", "a", "an", "the", "and", "or", "but", "so",
    "if", "it", "it's", "that", "this", "these", "those", "there", "they",
    "we", "we're", "we've", "you", "you're", "your", "yours", "he", "she",
    "my", "our", "his", "her", "no", "not", "yes", "ok", "okay", "hi",
    "hey", "hello", "thanks", "thank", "cheers", "best", "regards",
    "when", "what", "where", "which", "who", "why", "how", "either",
    "most", "more", "less", "some", "any", "every", "all", "one", "two",
    "just", "still", "even", "also", "then", "than", "as", "at", "by",
    "for", "from", "in", "into", "of", "on", "to", "up", "with", "worth",
    "happy", "glad", "quick", "short", "long", "first", "last", "next",
    "nothing", "anything", "something", "everything", "nobody", "somebody",
    "here", "sorry", "sure", "would", "could", "should", "can", "will",
    "do", "does", "did", "done", "is", "are", "was", "were", "be", "been",
    "let", "put", "give", "take", "make", "made", "run", "ran", "saw",
    "see", "looking", "look", "looked", "noticed", "noticing", "asking",
    "ask", "asked", "worked", "work", "working", "want", "wanted", "need",
    "figured", "thought", "think", "reading", "read", "wrote", "write",
    "google", "chrome", "http", "https", "seo", "site", "website", "page",
    "pages", "blog", "posts", "post", "phone", "number", "numbers",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "today", "yesterday", "tomorrow",
}
COMMON_CAPS |= MONTHS


# ---------------------------------------------------------------------------
# Lexicon: what the model is ALLOWED to say
# ---------------------------------------------------------------------------


# Typography the model emits that Python did not ask for: smart quotes, curly
# apostrophes, non-breaking hyphens, and mid-message hard line breaks. Folding
# these is NOT editing a claim, and normalize_typography() asserts that the
# letter-and-digit sequence is byte-identical afterwards, so it cannot quietly
# change a word. Anything that would alter a word raises instead.
TYPOGRAPHY_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "‑": "-", "−": "-", "‐": "-",
    " ": " ", " ": " ", " ": " ", "​": "",
    "…": "...",
}
_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_typography(text: str) -> str:
    """Straighten quotes, fold hard line breaks. Words are never touched."""
    if not text:
        return text
    out = text
    for bad, good in TYPOGRAPHY_MAP.items():
        out = out.replace(bad, good)
    out = re.sub(r"\s*\n+\s*", " ", out)
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    before = _ALNUM_RE.sub("", text.lower())
    after = _ALNUM_RE.sub("", out.lower())
    if before != after:  # pragma: no cover - a map entry would have to be wrong
        raise AssertionError("typography normalization altered the words")
    return out


def _tokens(text: str) -> set:
    """Lowercase word tokens. Hyphenated and slashed compounds are also split,
    so the fact "family-owned" is recognised in a draft that writes it as
    "family owned". Lead 36 fell back to a template over exactly this."""
    out = set()
    for m in WORD_RE.finditer(text or ""):
        raw = m.group(0).lower().strip(".'-")
        if not raw:
            continue
        out.add(raw)
        out.update(p for p in re.split(r"[-.']+", raw) if p)
    return out


def _digits(text: str) -> set:
    return set(DIGITS_RE.findall(text or ""))


def _spelled_numbers(text: str) -> List[str]:
    out = []
    for m in WORD_RE.finditer(text or ""):
        w = m.group(0).lower().strip(".'-")
        if w in NUMBER_WORDS and w not in NUMBER_WORDS_FREE:
            out.append(w)
    return out


def build_lexicon(fact: str, profile: dict, recipient: Optional[dict] = None,
                  source_url: str = "") -> dict:
    """Everything the model is permitted to reuse.

    The fact, the client's own approved copy (their voice file, which a human
    wrote), and the recipient's own identifying details. Nothing else.
    """
    recipient = recipient or {}
    pieces = [fact or "", source_url or ""]
    for key in ("display_name", "trade", "default_city", "signoff",
                "voice_note_label"):
        pieces.append(str(profile.get(key) or ""))
    for key in ("service_area", "templates", "urgent_templates", "openers",
                "closers", "style_notes"):
        val = profile.get(key) or []
        if isinstance(val, list):
            pieces.extend(str(v) for v in val)
        else:
            pieces.append(str(val))
    for key in ("name", "title", "place_name", "city", "category", "website",
                "region"):
        pieces.append(str(recipient.get(key) or ""))
    blob = " ".join(pieces)
    return {
        "words": _tokens(blob),
        "digits": _digits(blob),
        "numbers": {NUMBER_WORDS[w] for w in _spelled_numbers(blob)}
                   | {int(d) for d in _digits(blob) if len(d) <= 6},
        "blob": blob,
        "fact": fact or "",
        "source_url": source_url or "",
    }


# ---------------------------------------------------------------------------
# The verifier
# ---------------------------------------------------------------------------


def _norm_for_quote(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def verify_draft(text: str, lexicon: dict, client_slug: Optional[str] = None,
                 min_words: int = 12, max_words: int = 130) -> List[str]:
    """Return a list of reasons this draft must be REJECTED. Empty means clean.

    Nothing in here edits the draft. A draft either survives untouched or dies.
    """
    problems: List[str] = []
    if not text or not text.strip():
        return ["empty draft"]

    stripped = text.strip()

    for rx in _PREAMBLE_RES:
        if rx.search(stripped):
            problems.append("model preamble/formatting instead of a plain message")
            break

    m = PLACEHOLDER_RE.search(stripped)
    if m:
        problems.append(f"unfilled placeholder: {m.group(0)}")

    n_words = len(WORD_RE.findall(stripped))
    if n_words < min_words:
        problems.append(f"too short ({n_words} words)")
    if n_words > max_words:
        problems.append(f"too long ({n_words} words)")

    # --- 1. numbers -------------------------------------------------------
    for d in DIGITS_RE.findall(stripped):
        if d in lexicon["digits"]:
            continue
        if len(d) <= 6 and int(d) in lexicon["numbers"]:
            continue
        problems.append(f"invented number: {d}")

    for w in _spelled_numbers(stripped):
        if NUMBER_WORDS[w] not in lexicon["numbers"]:
            problems.append(f"invented number word: {w}")

    # --- 2. quotes must be verbatim from the fact -------------------------
    fact_norm = _norm_for_quote(lexicon["fact"])
    for q in QUOTED_RE.findall(stripped):
        if _norm_for_quote(q) and _norm_for_quote(q) not in fact_norm:
            problems.append(f'quoted text not verbatim in the fact: "{q[:50]}"')

    # --- 3. proper nouns --------------------------------------------------
    sentence_starts = set()
    for m in SENT_START_RE.finditer(stripped):
        sentence_starts.add(m.start(1))
    for m in WORD_RE.finditer(stripped):
        tok = m.group(0)
        if not tok[:1].isupper():
            continue
        low = tok.lower().strip(".'-")
        # "Roofing's" is the same proper noun as "Roofing", not a new one.
        # "Roofing's" is the same proper noun as "Roofing"; "You've" is the
        # ordinary word "you". Contractions and possessives are not new names.
        bare = low.split("'")[0]
        if not low or low in COMMON_CAPS or low in lexicon["words"]:
            continue
        if bare and (bare in COMMON_CAPS or bare in lexicon["words"]):
            continue
        if m.start() in sentence_starts and len(low) <= 12:
            # Sentence-initial capital of a word that is otherwise unremarkable.
            # Still policed if it looks like a name (see below), but a common
            # verb starting a sentence is not a fabrication.
            if low.isalpha() and low not in NUMBER_WORDS:
                # Only let it pass if it is lowercase-plausible English, i.e.
                # it also appears lowercased elsewhere or is short and common.
                if low in COMMON_CAPS:
                    continue
        problems.append(f"proper noun not in the source fact or client profile: {tok}")

    # --- 4. links ---------------------------------------------------------
    allowed_url = (lexicon.get("source_url") or "").rstrip("/")
    for u in URL_RE.findall(stripped):
        if u.rstrip("/.,)") != allowed_url:
            problems.append(f"link not the verified source: {u[:60]}")

    # --- 5. unverifiable claims ------------------------------------------
    for rx in _UNVERIFIABLE_RES:
        m = rx.search(stripped)
        if m:
            problems.append(f"unverifiable claim: {m.group(0).strip()!r}")

    # --- 6. the draft must actually carry the fact ------------------------
    fact_content = {
        w for w in _tokens(lexicon["fact"])
        if len(w) > 3 and w not in COMMON_CAPS
    }
    if fact_content and not (fact_content & _tokens(stripped)):
        problems.append("draft does not carry any content word from the fact")

    # --- 7. the existing voice gate --------------------------------------
    problems.extend(client_voice.check_voice(stripped, client_slug))

    seen, out = set(), []
    for p in problems:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# The prompt (the leash)
# ---------------------------------------------------------------------------


def normalize_fact(fact: str) -> str:
    """personalize.py writes facts containing em dashes. Jack's copy rule bans
    them. Rewriting our OWN Python-authored punctuation is not a model touching
    a fact, so it happens here, deterministically, before the model sees it."""
    return _FACT_DASH_RE.sub(", ", (fact or "").strip())


def build_system_prompt(profile: dict) -> str:
    examples = list(profile.get("templates") or [])[:3]
    banned = list(profile.get("banned_phrases") or [])
    lines = [
        "You rewrite ONE supplied fact into ONE short outreach message.",
        "",
        "ABSOLUTE RULE: the fact you are given is the ONLY thing you know about "
        "this business. You may rephrase it. You may not add to it.",
        "Never add a number, a year, a city, a certification, a review count, a "
        "ranking, or any opinion about their work. You have not seen their work.",
        "If you catch yourself about to write something you were not told, stop "
        "and write less instead.",
        "",
        f"You are writing as: {profile.get('display_name') or 'the sender'}.",
        f"Trade: {profile.get('trade') or 'unspecified'}.",
        f"Home area: {profile.get('default_city') or 'unspecified'}.",
    ]
    if profile.get("style_notes"):
        lines.append("Style: " + " ".join(profile["style_notes"]))
    if examples:
        lines.append("")
        lines.append("This is how this sender talks. Match the register, not the words:")
        lines.extend(f"  - {e}" for e in examples)
    lines += [
        "",
        "FORMAT: 2 to 4 plain sentences. No greeting line, no signature, no "
        "subject line, no markdown, no preamble. Just the message body.",
        "No em dashes or en dashes. No prices, discounts or free offers. "
        "No fake urgency. No hype words.",
        "Do not put anything in quotation marks unless you are quoting the "
        "supplied fact word for word.",
    ]
    if banned:
        lines.append("Never use these phrases: " + ", ".join(banned[:20]) + ".")
    return "\n".join(lines)


def build_user_prompt(fact: str, recipient: dict, profile: dict, ask: str) -> str:
    who = recipient.get("title") or recipient.get("name") or "this business"
    parts = [
        f"Recipient: {who}",
    ]
    if recipient.get("category"):
        parts.append(f"Their trade: {recipient['category']}")
    parts += [
        "",
        "THE ONE VERIFIED FACT (this is all you know, do not go past it):",
        fact,
        "",
        "Write the message. Open by mentioning that fact the way a person would "
        "say it out loud, not the way a report would state it. Then say, in one "
        "sentence, why it matters. Then " + ask,
        "",
        "Add nothing that is not in the fact above.",
    ]
    return "\n".join(parts)


DEFAULT_ASK = ("ask one low-pressure question. Do not pitch, do not list "
               "services, do not promise a result.")


# ---------------------------------------------------------------------------
# The deterministic fallback
# ---------------------------------------------------------------------------

# These carry the FACT, which is unique per lead, so two leads cannot collide
# unless they literally share a fact. The old sha1 % 4 skeleton rotation
# guaranteed collisions because the fact never entered the body.
FALLBACK_FRAMES = [
    "{fact} That is the kind of thing that quietly costs a business calls. "
    "Worth a look?",
    "Ran across something on your site. {fact} Small thing to fix, easy thing "
    "to miss. Want me to point at it?",
    "{fact} Not a crisis, but it is the sort of detail that adds up. "
    "Happy to show you where.",
    "Noticed this while reading your site. {fact} Would it help to see what I "
    "would change first?",
    "{fact} Most people never see that from their own side of the screen. "
    "Want the short version?",
    "Quick one. {fact} That is worth thirty seconds of your time to check.",
    "{fact} If that is deliberate, ignore me. If it is not, it is a quick fix.",
    "{fact} I only mention it because it is the first thing a customer would "
    "run into too.",
]


def _pick(seed: str, n: int, salt: str = "") -> int:
    h = hashlib.sha256((salt + seed).encode("utf-8", "replace")).hexdigest()
    return int(h[:16], 16) % max(n, 1)


def deterministic_draft(fact: str, profile: dict, recipient: dict,
                        client_slug: str) -> Tuple[str, str]:
    """Fact-carrying template. Always produces something gate-clean."""
    frames = list(profile.get("fallback_frames") or FALLBACK_FRAMES)
    seed = str(recipient.get("id") or recipient.get("title") or fact)
    start = _pick(seed, len(frames), salt=f"{client_slug}:fb:")
    lexicon = build_lexicon(fact, profile, recipient)
    for step in range(len(frames)):
        idx = (start + step) % len(frames)
        body = frames[idx].format(fact=fact).strip()
        body = re.sub(r"\s{2,}", " ", body)
        if not verify_draft(body, lexicon, client_slug, min_words=8):
            return body, f"frame #{idx} of {len(frames)}"
    # Last resort: the fact plus the most inert sentence available.
    body = f"{fact} Worth a look?"
    return body, "all frames failed the gate, used the bare fact"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def draft_message(fact: str, source_url: str, client_slug: str,
                  recipient: Optional[dict] = None, ask: str = DEFAULT_ASK,
                  use_model: bool = True, attempts: int = 2,
                  _generate=None) -> dict:
    """Draft one message from one verified fact.

    Returns a dict:
        text        the message a human will review
        method      "model" or "template"
        verified    True only when a model draft passed verify_draft()
        reason      why the template lane was used, when it was
        rejected    every model attempt that failed, with its exact violations
        tokens      {"prompt","completion","total"} actually billed
        model       provider:model id that answered
        voice       client slug used
        source_url  the URL a human clicks to check the fact

    NEVER raises on model trouble. A dead model degrades to the template lane.
    """
    recipient = recipient or {}
    profile = client_voice._load_profile(client_slug)
    slug = profile.get("slug") or client_slug
    fact = normalize_fact(fact)
    lexicon = build_lexicon(fact, profile, recipient, source_url)

    result = {
        "text": None, "method": None, "verified": False, "reason": None,
        "rejected": [], "tokens": {"prompt": 0, "completion": 0, "total": 0},
        "model": None, "voice": slug, "source_url": source_url,
        "fact": fact,
    }

    if use_model:
        gen = _generate
        if gen is None:
            try:
                gen = _load_router().generate
            except ModelUnavailable as e:
                gen = None
                result["reason"] = f"model lane unavailable: {e}"
        if gen is not None:
            system = build_system_prompt(profile)
            user = build_user_prompt(fact, recipient, profile, ask)
            for attempt in range(attempts):
                try:
                    res = gen(MODEL_ALIAS, user, system=system,
                              temperature=TEMPERATURE + 0.1 * attempt,
                              max_tokens=MAX_TOKENS)
                except Exception as e:
                    result["reason"] = f"model call raised: {str(e)[:120]}"
                    break
                if not isinstance(res, dict) or "error" in res:
                    err = (res or {}).get("error", "no response")
                    result["reason"] = f"model error: {str(err)[:140]}"
                    break
                usage = res.get("usage") or {}
                result["tokens"]["prompt"] += int(usage.get("prompt_tokens") or 0)
                result["tokens"]["completion"] += int(usage.get("completion_tokens") or 0)
                result["model"] = res.get("model")
                candidate = (res.get("output") or "").strip()
                try:
                    candidate = normalize_typography(candidate)
                except AssertionError:
                    result["rejected"].append(
                        {"attempt": attempt + 1, "text": candidate,
                         "violations": ["typography could not be normalized safely"]})
                    continue
                candidate = re.sub(r'^"|"$', "", candidate).strip()
                problems = verify_draft(candidate, lexicon, slug)
                if not problems:
                    result["text"] = candidate
                    result["method"] = "model"
                    result["verified"] = True
                    break
                result["rejected"].append({"attempt": attempt + 1,
                                           "text": candidate,
                                           "violations": problems})
            else:
                result["reason"] = (
                    f"all {attempts} model drafts failed verification"
                    if result["rejected"] else result["reason"])
            if result["text"] is None and result["rejected"] and not result["reason"]:
                result["reason"] = f"all {attempts} model drafts failed verification"
    else:
        result["reason"] = "model lane switched off by caller (--no-model)"

    result["tokens"]["total"] = (result["tokens"]["prompt"]
                                 + result["tokens"]["completion"])

    if result["text"] is None:
        body, note = deterministic_draft(fact, profile, recipient, slug)
        result["text"] = body
        result["method"] = "template"
        result["verified"] = False
        result["template_note"] = note
        if not result["reason"]:
            result["reason"] = "model lane not used"

    return result


def check_collisions(drafts: List[dict]) -> dict:
    """Byte-identical and near-identical body detection, per client.

    The old lane picked 1 of 4 skeletons by sha1(id) % 4, so collisions were
    not a risk, they were arithmetic: any 5 leads for one client had to repeat.
    """
    by_client: Dict[str, List[dict]] = {}
    for d in drafts:
        by_client.setdefault(d.get("voice") or "_", []).append(d)

    report = {"exact": [], "near": [], "per_client": {}}
    for slug, rows in by_client.items():
        exact: Dict[str, List[str]] = {}
        for d in rows:
            exact.setdefault(d["text"], []).append(str(d.get("lead_id") or "?"))
        dupes = {k: v for k, v in exact.items() if len(v) > 1}
        for body, ids in dupes.items():
            report["exact"].append({"voice": slug, "ids": ids, "text": body})

        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = _tokens(rows[i]["text"]), _tokens(rows[j]["text"])
                if not a or not b:
                    continue
                jac = len(a & b) / len(a | b)
                if jac >= 0.85 and rows[i]["text"] != rows[j]["text"]:
                    report["near"].append({
                        "voice": slug, "jaccard": round(jac, 3),
                        "ids": [str(rows[i].get("lead_id")),
                                str(rows[j].get("lead_id"))]})
        report["per_client"][slug] = {
            "drafts": len(rows),
            "unique_bodies": len({d["text"] for d in rows}),
        }
    report["ok"] = not report["exact"]
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_leads(limit: int) -> List[dict]:
    os.environ.setdefault("ENV_FILE", r"C:\Users\wjack\ghl-cli\.env")
    import requests
    from db import load_env
    env = load_env()
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY (set ENV_FILE)")
    r = requests.get(f"{url}/rest/v1/candidates",
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     params={"identity": "eq.verified",
                             "personalization": "not.is.null",
                             "select": "id,title,place_name,category,website,"
                                       "personalization,personalization_source",
                             "order": "id.asc", "limit": str(limit)},
                     timeout=60)
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=10)
    # Clients are data, never hardcoded. Default comes from the environment or
    # from whatever voice files happen to exist.
    ap.add_argument("--voice", default=os.environ.get("SONAR_VOICE"),
                    help="client slug from ingest/voices/ (see --list-voices)")
    ap.add_argument("--list-voices", action="store_true")
    ap.add_argument("--no-model", action="store_true",
                    help="force the deterministic lane")
    ap.add_argument("--attempts", type=int, default=2)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--show-regexes", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="offline verifier checks, no network")
    a = ap.parse_args()

    if a.show_regexes:
        for nm, val in sorted(globals().items()):
            if isinstance(val, re.Pattern):
                print(f"{nm:22} {val.pattern!r}")
        for nm, lst in (("UNVERIFIABLE", _UNVERIFIABLE_RES),
                        ("PREAMBLE", _PREAMBLE_RES)):
            for rx in lst:
                print(f"{nm:22} {rx.pattern!r}")
        return

    if a.list_voices:
        for s in client_voice.list_clients():
            print(s)
        return

    if a.self_test:
        import test_draft_message  # noqa
        return test_draft_message.main()

    if not a.voice:
        sys.exit("Need --voice <slug> (or SONAR_VOICE). Available: "
                 + ", ".join(client_voice.list_clients()))

    leads = _load_leads(a.limit)
    if not leads:
        sys.exit("No verified leads carrying a personalization.")

    drafts = []
    for lead in leads:
        d = draft_message(lead["personalization"],
                          lead.get("personalization_source") or "",
                          a.voice, recipient=lead,
                          use_model=not a.no_model, attempts=a.attempts)
        d["lead_id"] = lead["id"]
        d["lead_name"] = lead.get("title")
        drafts.append(d)

    if a.json:
        print(json.dumps({"drafts": drafts,
                          "collisions": check_collisions(drafts)},
                         indent=1, ensure_ascii=False))
        return

    tot_p = tot_c = 0
    for d in drafts:
        print(f"--- [{d['lead_id']}] {d['lead_name']}  ({d['method']}"
              f"{', VERIFIED' if d['verified'] else ''})")
        print(f"    fact: {d['fact']}")
        print(f"    src : {d['source_url']}")
        print(f"    >>> {d['text']}")
        if d.get("reason"):
            print(f"    note: {d['reason']}")
        for rj in d.get("rejected", []):
            print(f"    REJECTED attempt {rj['attempt']}: "
                  f"{'; '.join(rj['violations'])}")
        t = d["tokens"]
        tot_p += t["prompt"]
        tot_c += t["completion"]
        print(f"    tokens: {t['prompt']} in / {t['completion']} out"
              f"   model={d['model']}")
        print()

    col = check_collisions(drafts)
    print("COLLISIONS:", "none" if col["ok"] else col["exact"])
    for slug, s in col["per_client"].items():
        print(f"  {slug}: {s['unique_bodies']} unique of {s['drafts']} drafts")
    if col["near"]:
        print("  near-duplicates (jaccard >= 0.85):", col["near"])
    n = len(drafts)
    print(f"\nTOKENS: {tot_p} in / {tot_c} out over {n} drafts "
          f"= {(tot_p + tot_c) / n:.0f} total per draft")
    mm = sum(1 for d in drafts if d["method"] == "model")
    print(f"METHOD: {mm} model-written and verified, {n - mm} deterministic template")


if __name__ == "__main__":
    main()
