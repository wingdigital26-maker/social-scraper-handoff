#!/usr/bin/env python3
"""
Trade vocabulary — how people actually ASK for a trade, and how to tell that a
search result is really about it.

WHY THIS EXISTS
  Sonar Watch searched one generic intent list ("anyone recommend", "looking for
  a", ...) glued to the client's trade name, then kept a result only if
  trade.split()[0] appeared in the snippet. For "roofing" that mostly survives:
  people say "roof" and "roofer" and the stem matches often enough that Jackson
  Roofing accumulated drafts. For "junk removal" it is fatal. The literal word
  "junk" is industry vocabulary, not customer vocabulary. Customers write "need
  to get rid of", "haul away", "cleanout", "dump run", "someone to take". A live
  run on 2026-08-25 pulled 25 real Nextdoor results for Hero's Junk Removal and
  the single-word gate rejected 25 of 25 — zero drafts, which is exactly what
  the outbound table shows.

  Two corrections come out of that, and both live here:
    1. Ask the way customers ask, per trade, not with one generic list.
    2. Judge relevance on a vocabulary of the trade, not on one word — and judge
       it against the URL too, because Reddit results come back from the index
       with the title "Link to reddit.com" and no snippet at all, while the URL
       slug still spells out the post title
       (/comments/.../cb_wants_you_to_haul_away_old_wood_and_pay_them/).

WHAT IT DOES NOT DO
  No AI, no network, no state. Pure data plus three small functions, so the
  watcher stays deterministic and free.

    intent_queries("junk removal", "Dallas", ["same day"]) -> ['"haul away" ...]
    relevance_terms("junk removal")                        -> ['junk', 'haul', ...]
    is_relevant("junk removal", title, body, url)          -> bool
"""
from __future__ import annotations

import re

__all__ = ["intent_queries", "relevance_terms", "is_relevant", "canonical_trade",
           "TRADES", "GENERIC_ASKS", "GENERIC_CONFIRM"]

# Generic ways anyone asks to hire anyone. These stay — they are real — but they
# are now a supplement to trade-specific phrasing rather than the whole strategy.
GENERIC_ASKS = [
    '"anyone recommend"', '"can anyone recommend"', '"looking for a"',
    '"any recommendations for"', '"who do you use for"', '"need someone to"',
    '"in need of"', '"does anyone know a"', '"need a good"', '"best company for"',
    # Complaint-shaped: unhappy with their current provider is a switch waiting
    # to happen, and these people are the easiest to win.
    '"terrible experience with"', '"never showed up"', '"still waiting on"',
    '"ripped me off"', '"looking to switch"',
]

# Words that say "a person is talking about hiring somebody", used as a weak
# fallback confirmation for a trade we have no vocabulary for.
GENERIC_CONFIRM = [
    "recommend", "recommendation", "quote", "estimate", "hire", "hiring",
    "contractor", "company", "service", "looking for", "need someone",
    "who do you use", "any suggestions",
]

# Spelling variants and near-synonyms a client's scrape_niche might arrive as.
_ALIASES = {
    "junk": "junk removal", "junk hauling": "junk removal", "hauling": "junk removal",
    "junk haul": "junk removal", "junk removal and hauling": "junk removal",
    "rubbish removal": "junk removal", "trash removal": "junk removal",
    "debris removal": "junk removal", "cleanouts": "junk removal",
    "roof": "roofing", "roofer": "roofing", "roofing contractor": "roofing",
    "roof repair": "roofing", "roofing and gutters": "roofing",
    "ac": "hvac", "a/c": "hvac", "air conditioning": "hvac", "heating": "hvac",
    "heating and air": "hvac", "heating and cooling": "hvac", "air": "hvac",
    "hvac repair": "hvac", "furnace": "hvac",
    "plumber": "plumbing", "plumbing repair": "plumbing", "drain cleaning": "plumbing",
    "electrician": "electrical", "electric": "electrical",
    "electrical contractor": "electrical", "electrical repair": "electrical",
    "landscaper": "landscaping", "landscape": "landscaping", "lawn": "landscaping",
    "lawn care": "landscaping", "lawn service": "landscaping", "yard work": "landscaping",
    "tree service": "landscaping", "lawn maintenance": "landscaping",
}


def canonical_trade(trade: str) -> str:
    """Normalize whatever is in crm_clients.scrape_niche to a vocabulary key."""
    t = re.sub(r"[^a-z0-9/& ]+", " ", (trade or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^(residential|commercial|local|professional|affordable)\s+", "", t)
    t = re.sub(r"\s+(services|service|co|company|llc|inc)$", "", t).strip()
    if t in TRADES:
        return t
    if t in _ALIASES:
        return _ALIASES[t]
    # A multi-word niche like "roofing and siding" — take the first key we hit.
    for key in TRADES:
        if key in t:
            return key
    for alias, key in _ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", t):
            return key
    return t


# Per-trade vocabulary.
#   asks    — how a normal person writes the request. Quoted so the index treats
#             them as phrases; these ARE the search, the trade name is optional.
#   subject — the object of the ask, appended to generic phrases so
#             "need someone to" is not fishing the whole internet.
#   confirm — words whose presence means the result really is about this trade.
#             Substring-matched against title + snippet + URL slug.
TRADES: dict[str, dict[str, list[str]]] = {
    "junk removal": {
        "asks": [
            '"get rid of"', '"haul away"', '"hauled away"', '"need to get rid of"',
            '"someone to haul"', '"who can haul"', '"junk removal"',
            '"estate cleanout"', '"garage cleanout"', '"house cleanout"',
            '"dump run"', '"take it to the dump"', '"clean out my"',
            '"someone to take"', '"pick up my old"', '"remove old furniture"',
            '"hoarder cleanout"', '"foreclosure cleanout"', '"bulk trash pickup"',
            '"cheapest way to dispose of"', '"appliance removal"',
            '"shed demolition"', '"hot tub removal"',
        ],
        "subject": [
            "old furniture", "old couch", "mattress", "appliances", "garage junk",
            "yard debris", "construction debris", "moving leftovers",
        ],
        "confirm": [
            "junk", "haul", "hauling", "hauled", "cleanout", "clean out",
            "clean-out", "declutter", "decluttering", "dump", "landfill",
            "dispose", "disposal", "get rid of", "take away", "remove old",
            "removal", "bulk trash", "debris", "old furniture", "couch",
            "sofa", "mattress", "appliance", "dumpster", "scrap", "clear out",
            "pick up", "pickup", "trash", "garbage", "estate sale leftovers",
        ],
    },
    "roofing": {
        "asks": [
            '"roof leak"', '"leaking roof"', '"need a new roof"', '"roof replacement"',
            '"missing shingles"', '"shingles blew off"', '"hail damage"',
            '"storm damage to my roof"', '"roof inspection"', '"who to call for a roof"',
            '"roofer recommendation"', '"reroof"', '"water stain on my ceiling"',
            '"roof estimate"', '"insurance claim roof"', '"gutter and roof"',
        ],
        "subject": ["roof", "shingles", "roof leak", "gutters", "hail damage"],
        "confirm": [
            "roof", "roofer", "roofing", "shingle", "shingles", "reroof",
            "re-roof", "gutter", "flashing", "soffit", "fascia", "attic leak",
            "ceiling leak", "hail", "storm damage", "leak", "leaking",
            "tile roof", "metal roof", "underlayment",
        ],
    },
    "hvac": {
        "asks": [
            '"ac not working"', '"ac stopped working"', '"no cold air"',
            '"air conditioner not cooling"', '"need a new ac"', '"ac repair"',
            '"heater not working"', '"no heat"', '"furnace not working"',
            '"hvac recommendation"', '"ac unit replacement"', '"thermostat not"',
            '"ac is blowing warm"', '"who to call for ac"', '"freon leak"',
            '"ac making noise"', '"quote for a new system"',
        ],
        "subject": ["ac", "air conditioner", "furnace", "heater", "hvac system"],
        "confirm": [
            "hvac", "a/c", " ac ", "ac unit", "air condition", "aircon",
            "furnace", "heater", "heating", "cooling", "thermostat", "freon",
            "refrigerant", "condenser", "compressor", "ductwork", "duct",
            "mini split", "no cold air", "no heat", "blowing warm", "heat pump",
        ],
    },
    "plumbing": {
        "asks": [
            '"water leak"', '"pipe burst"', '"burst pipe"', '"clogged drain"',
            '"drain is backed up"', '"toilet keeps running"', '"no hot water"',
            '"water heater"', '"low water pressure"', '"sewer smell"',
            '"plumber recommendation"', '"need a plumber"', '"slab leak"',
            '"garbage disposal broken"', '"faucet dripping"', '"sewer line"',
        ],
        "subject": ["water heater", "drain", "toilet", "pipe", "sewer line"],
        "confirm": [
            "plumb", "plumber", "plumbing", "pipe", "pipes", "drain", "clog",
            "clogged", "toilet", "faucet", "sink", "water heater", "tankless",
            "sewer", "septic", "leak", "leaking", "water pressure", "sump pump",
            "garbage disposal", "shower", "slab leak", "backed up", "repipe",
        ],
    },
    "electrical": {
        "asks": [
            '"breaker keeps tripping"', '"no power to"', '"outlet not working"',
            '"need an electrician"', '"electrician recommendation"',
            '"panel upgrade"', '"lights flickering"', '"rewire"',
            '"install a ceiling fan"', '"ev charger install"', '"add an outlet"',
            '"knob and tube"', '"burning smell from outlet"', '"generator install"',
        ],
        "subject": ["electrical panel", "outlet", "breaker", "wiring", "ev charger"],
        "confirm": [
            "electric", "electrical", "electrician", "breaker", "outlet",
            "receptacle", "panel", "wiring", "rewire", "wire", "voltage",
            "amp", "circuit", "gfci", "ev charger", "ceiling fan", "light fixture",
            "flickering", "no power", "generator", "sub panel", "knob and tube",
        ],
    },
    "landscaping": {
        "asks": [
            '"lawn service recommendation"', '"someone to mow"', '"mow my lawn"',
            '"yard cleanup"', '"tree removal"', '"trim my trees"',
            '"landscaper recommendation"', '"need a landscaper"', '"sod install"',
            '"sprinkler not working"', '"irrigation repair"', '"weeds taking over"',
            '"flower bed"', '"stump removal"', '"leaf removal"', '"retaining wall"',
        ],
        "subject": ["lawn", "yard", "trees", "sprinkler system", "flower beds"],
        "confirm": [
            "lawn", "landscap", "landscaper", "landscaping", "mow", "mowing",
            "yard", "grass", "sod", "turf", "tree", "trees", "trim", "trimming",
            "prune", "stump", "hedge", "shrub", "mulch", "flower bed", "garden",
            "sprinkler", "irrigation", "weeds", "leaves", "leaf", "edging",
            "retaining wall", "hardscape",
        ],
    },
}


# Surfaces that match a trade vocabulary but are never a person asking to hire.
# Every one of these was observed in a live 2026-08-25 run: Nextdoor's indexed
# corpus is dominated by its For Sale & Free marketplace and its own marketing
# blog, and both are dense with words like "pick up", "get rid of", "free".
# Relevance alone would wave them through, so they are excluded by shape.
_NOISE_TEXT = [
    "for sale & free", "for sale and free", "make an offer", "nextdoor blog",
    "how to start a", "tips to", "checklist", "grow your business",
    "marketing for", "local deals", "stay connected with your favorite",
    "neighbors have what you need", "free items posted daily",
    "advertise on nextdoor", "business booms",
]
_NOISE_URL = [
    "/for-sale-and-free", "/pages/", "/blog", "business.nextdoor",
    "/products/", "/marketing/", "/ads",
]
# A price tag in the title means it is a listing, not a request.
_PRICE = re.compile(r"(?:^|\s)(?:for\s+)?\$\s?\d", re.I)


def is_noise(title: str = "", body: str = "", url: str = "") -> bool:
    """True if this result is a marketplace listing or platform marketing page."""
    text = f"{title or ''} {body or ''}".lower()
    u = (url or "").lower()
    if any(n in text for n in _NOISE_TEXT):
        return True
    if any(n in u for n in _NOISE_URL):
        return True
    return bool(_PRICE.search(title or ""))


def _uniq(seq):
    seen, out = set(), []
    for x in seq:
        k = x.strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out


def intent_queries(trade: str, city: str = "", extra_terms=None) -> list[str]:
    """Search phrases tuned for `trade`, localized to `city`.

    Returns query bodies WITHOUT a site: operator — the caller prepends
    `site:nextdoor.com` etc., so one vocabulary serves every platform. Ordered
    best-first: trade-native asks come before the generic hire-anyone phrasing,
    so a caller that truncates the list keeps the highest-yield queries.
    """
    extra_terms = [t.strip() for t in (extra_terms or []) if t and t.strip()]
    key = canonical_trade(trade)
    voc = TRADES.get(key)
    city = (city or "").strip()
    label = key if voc else (trade or "").strip()

    qs: list[str] = []
    if voc:
        # 1. How customers actually say it. These carry the intent by themselves,
        #    so they do not need the trade name bolted on — which is the whole
        #    point: "need to get rid of my old couch" never says "junk".
        for ask in voc["asks"]:
            qs.append(f"{ask} {city}".strip())
        # 2. Generic hire-phrases pinned to a concrete object of the ask.
        for g in GENERIC_ASKS[:8]:
            for subj in voc["subject"][:3]:
                qs.append(f"{g} {subj} {city}".strip())
        # 3. The trade name itself, still worth asking — some people do use it.
        for g in GENERIC_ASKS[:6]:
            qs.append(f"{g} {label} {city}".strip())
    else:
        # Unknown trade: no vocabulary, but still produce usable queries by
        # combining every generic ask with the trade name as written.
        for g in GENERIC_ASKS:
            qs.append(f"{g} {label} {city}".strip())

    # 4. Client-supplied scrape_terms, one query each, so an operator can steer
    #    the crawl without a code change.
    for t in extra_terms[:4]:
        qs.append(f'"{t}" {city}'.strip() if " " in t else f"{t} {label} {city}".strip())

    return _uniq(q for q in qs if q)


def relevance_terms(trade: str) -> list[str]:
    """Words whose presence means a result is genuinely about this trade.

    Replaces the crude `trade.split()[0] in text` check. Lowercase, intended for
    substring matching against title + snippet + URL slug together.
    """
    key = canonical_trade(trade)
    voc = TRADES.get(key)
    if voc:
        return _uniq(voc["confirm"])
    # Unknown trade: every word of the trade name plus the generic hire signals.
    words = [w for w in re.split(r"[^a-z0-9]+", (trade or "").lower())
             if len(w) > 2 and w not in {"and", "the", "for", "llc", "inc"}]
    return _uniq(words + GENERIC_CONFIRM)


def is_relevant(trade: str, title: str = "", body: str = "", url: str = "") -> bool:
    """True if this result looks like it is about `trade`.

    The URL is included deliberately. The public index frequently returns Reddit
    hits with the title "Link to reddit.com" and no snippet, while the URL slug
    still carries the post title verbatim. Dropping those on an empty snippet
    throws away the single richest platform in the run.
    """
    if is_noise(title, body, url):
        return False
    slug = re.sub(r"[^a-z0-9]+", " ", (url or "").lower())
    blob = f"{title or ''} {body or ''} {slug}".lower()
    blob = re.sub(r"\s+", " ", f" {blob} ")
    return any(t in blob for t in relevance_terms(trade))
