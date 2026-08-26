#!/usr/bin/env python3
"""
Relevance gate for Sonar Watch — scores a search hit before it becomes a draft.

WHY THIS EXISTS
  watch_social.py files every hit that contains the trade word. That means a
  roofing company's own Nextdoor business page, a 2019 thread, and a person who
  asked for a roofer this morning all land in the CRM looking identical. A human
  opening that queue has to re-do the filtering by hand, which is the work the
  watcher was supposed to do.

  This module ranks and rejects. It answers one question about each hit: is a
  real person, near the client, asking right now for the thing the client sells?

FOUR THINGS IT LOOKS AT, plus two kill switches:
    demand shape   a question beats a statement
    trade match    does the post talk about this trade at all
    recency        a dead thread is a dead lead, and replying looks like a bot
    geography      right metro, or nothing

    KILL: the post is a business advertising itself (the single most common
          false positive on a site:nextdoor.com search)
    KILL: platform chrome, login walls, directories, aggregator listicles

HARD RULES
  Pure Python, no AI, no network. Unit-testable offline, deterministic.
  A date or a location is NEVER invented. Unknown scores neutral and the
  uncertainty is written into `reasons` so a human sees it.

  score_hit(...) -> {"score": float 0-1, "reasons": [str],
                     "reject": bool, "reject_reason": str|None,
                     "components": {...}}
"""
import datetime as _dt
import re

__all__ = ["score_hit", "DFW_CITIES", "WEIGHTS"]

# ---------------------------------------------------------------------------
# Weights. They sum to 1.0 so `score` is directly readable as a 0-1 confidence.
#
# Recency and demand-shape carry the most because they are what separate a live
# lead from a technically-relevant page. Trade match is cheap to satisfy (the
# caller already keyword-filtered) so it earns less. Geography is mostly handled
# by the hard out-of-state reject, so its soft weight is the smallest.
# ---------------------------------------------------------------------------
WEIGHTS = {
    "recency": 0.30,
    "intent": 0.28,
    "trade": 0.24,
    "geo": 0.18,
}
NEUTRAL = 0.5   # what an honestly-unknown signal scores

# A thread older than this is not a lead at any weight — the person hired
# somebody eighteen months ago. It never reaches the queue.
DEAD_THREAD_DAYS = 545

# Weighted-sum alone let a perfect-but-ancient post out-rank a fresh one,
# because three strong signals drowned one zero. Age therefore also SCALES the
# final score instead of only contributing to it: a stale hit can never sit
# above a fresh hit of the same quality. Unknown dates (rec == NEUTRAL) land
# mid-scale, which is the honest answer.
def _staleness_multiplier(rec):
    return 0.55 + 0.45 * rec

# ---------------------------------------------------------------------------
# Geography. DFW is one labor market: a Plano roofer works Frisco and Richardson
# without blinking, so the whole metro counts as in-market for a DFW client.
# ---------------------------------------------------------------------------
DFW_CITIES = {
    "dallas", "fort worth", "ft worth", "arlington", "plano", "frisco",
    "mckinney", "allen", "richardson", "irving", "garland", "carrollton",
    "denton", "lewisville", "flower mound", "grapevine", "southlake",
    "colleyville", "keller", "mansfield", "grand prairie", "euless", "bedford",
    "hurst", "coppell", "rowlett", "wylie", "murphy", "sachse", "prosper",
    "celina", "little elm", "the colony", "addison", "farmers branch",
    "university park", "highland park", "rockwall", "mesquite", "desoto",
    "duncanville", "cedar hill", "lancaster", "waxahachie", "midlothian",
    "burleson", "north richland hills", "haltom city", "watauga", "saginaw",
    "weatherford", "granbury", "cleburne", "corinth", "argyle", "trophy club",
    "roanoke", "justin", "aubrey", "anna", "melissa", "princeton", "forney",
    "terrell", "greenville", "sherman", "denison", "dfw", "metroplex",
}
TEXAS_HINTS = {"texas", " tx ", ", tx", "(tx", "tx)", "north texas", "dfw"}

# Every state but Texas. A post anchored to one of these is somebody else's lead.
_OTHER_STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming",
]
_STATE_NAME_RE = re.compile(r"\b(" + "|".join(_OTHER_STATES) + r")\b", re.I)
# Abbreviations only count in "City, ST" position — bare "OR"/"IN"/"ME"/"OK" are
# ordinary English words and matching them loose would reject half the corpus.
_STATE_ABBR_RE = re.compile(
    r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|"
    r"MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|UT|VT|VA|"
    r"WA|WV|WI|WY)\b"
)
# A handful of big non-Texas cities that show up without their state attached.
_OTHER_BIG_CITIES = re.compile(
    r"\b(seattle|portland|denver|phoenix|atlanta|chicago|boston|miami|"
    r"brooklyn|manhattan|philadelphia|detroit|nashville|charlotte|"
    r"los angeles|san diego|san francisco|san jose|las vegas|tampa|orlando|"
    r"minneapolis|milwaukee|pittsburgh|cleveland|columbus|indianapolis|"
    r"kansas city|st louis|salt lake city|albuquerque|tucson|sacramento)\b",
    re.I,
)

# ---------------------------------------------------------------------------
# Demand shape. Someone ASKING is the only moment worth a reply.
# ---------------------------------------------------------------------------
_ASK_STRONG = re.compile(
    r"(anyone\s+(know|recommend|have|used)|can\s+anyone\s+recommend|"
    r"any\s+recommendations?|looking\s+for\s+(a|an|someone)|"
    r"need\s+(a|an|someone|help)|in\s+need\s+of|who\s+(do|did)\s+you\s+use|"
    r"recommendations?\s+for|any\s+suggestions?|asking\s+for\s+a\s+friend|"
    r"does\s+anyone\s+know|hoping\s+(for|to\s+find)|trying\s+to\s+find|"
    r"where\s+(can|should)\s+i|has\s+anyone\s+used|advice\s+on\s+(hiring|finding))",
    re.I,
)
_ASK_SOFT = re.compile(
    r"(recommend|suggestions?|referral|quote|estimate\s+for|help\s+with|"
    r"thoughts\s+on|worth\s+it|how\s+much\s+(should|does))", re.I)
_SWITCH = re.compile(
    r"(terrible\s+experience|never\s+showed\s+up|still\s+waiting\s+on|"
    r"ripped\s+me\s+off|looking\s+to\s+switch|ghosted\s+me|"
    r"stopped\s+answering|bad\s+experience\s+with)", re.I)
_URGENT = re.compile(
    r"\b(asap|urgent|emergency|today|tomorrow|this\s+week|leak|leaking|"
    r"no\s+ac|no\s+heat|flood|storm\s+damage|burst)\b", re.I)
_FIRST_PERSON = re.compile(r"\b(i|i'm|im|my|we|we're|our)\b", re.I)

# ---------------------------------------------------------------------------
# Kill switch 1: a business advertising itself.
# ---------------------------------------------------------------------------
# URL shapes that ARE a company page. Any one of these alone is enough.
_BIZ_URL = [
    (re.compile(r"nextdoor\.com/pages/", re.I), "Nextdoor business page URL (/pages/)"),
    (re.compile(r"nextdoor\.com/pages_directory", re.I), "Nextdoor business directory URL"),
    (re.compile(r"nextdoor\.com/business", re.I), "Nextdoor business URL"),
    (re.compile(r"facebook\.com/pages/", re.I), "Facebook business page URL"),
    (re.compile(r"/(business|biz)[-_]?(profile|listing|page)s?/", re.I), "business-listing URL"),
    (re.compile(r"yelp\.com/biz/", re.I), "Yelp business listing"),
    (re.compile(r"(mapquest|manta|chamberofcommerce|yellowpages|superpages)\.com", re.I),
     "business directory domain"),
]
# Ad copy. Individually a person could type one of these; two is a sales pitch.
_BIZ_PHRASES = [
    (re.compile(r"verified\s+by\s+nextdoor", re.I), '"Verified by Nextdoor" badge'),
    (re.compile(r"\bfaves?\s+from\s+neighbors?\b", re.I), "Nextdoor faves badge"),
    (re.compile(r"\b\d+\s*(\+\s*)?(reviews?|recommendations?|ratings?|faves?)\b", re.I),
     "review-count language"),
    (re.compile(r"\b\d(\.\d)?\s*(star|out of 5)\b", re.I), "star-rating language"),
    (re.compile(r"\b(call|contact|text)\s+us\b", re.I), '"call us" CTA'),
    (re.compile(r"free\s+(estimate|quote|inspection|consultation)", re.I), "free-estimate CTA"),
    (re.compile(r"licensed\s+(and|&)\s+insured", re.I), '"licensed and insured"'),
    (re.compile(r"family\s+owned", re.I), '"family owned"'),
    (re.compile(r"(serving|proudly\s+serving)\s+[a-z ]{3,25}\s+(since|for)\s+", re.I),
     '"serving X since Y"'),
    (re.compile(r"(request|get)\s+(a\s+)?(free\s+)?(quote|estimate|bid)", re.I), "quote CTA"),
    (re.compile(r"\b24/?7\b.{0,20}(service|available|emergency)", re.I), "24/7 service claim"),
    (re.compile(r"\bwe\s+(offer|specialize|provide|install|repair|serve)\b", re.I),
     '"we offer/specialize" vendor voice'),
    (re.compile(r"(financing\s+available|satisfaction\s+guaranteed|"
                r"no\s+obligation|workmanship\s+warranty)", re.I), "sales-pitch boilerplate"),
    (re.compile(r"\b(llc|inc\.?|co\.)\s*[-|·]", re.I), "company-name-and-tagline title shape"),
]

# ---------------------------------------------------------------------------
# Kill switch 2: junk surfaces.
# ---------------------------------------------------------------------------
_JUNK_URL = [
    (re.compile(r"/(login|signin|sign_in|signup|register|auth)\b", re.I), "login wall"),
    (re.compile(r"(help|support)\.[a-z]+\.com", re.I), "help-center page"),
    (re.compile(r"/(help|support|faq|about|privacy|terms|legal|careers|press|"
                r"advertise|settings|preferences)(/|$)", re.I), "platform chrome page"),
    (re.compile(r"/(explore|hashtag|tags?|topics?|directory|categories|category)/", re.I),
     "hashtag/explore/directory page"),
    (re.compile(r"nextdoor\.com/(city|neighborhood)/", re.I), "Nextdoor city index page"),
    (re.compile(r"reddit\.com/r/[^/]+/?$", re.I), "subreddit landing page, not a post"),
    (re.compile(r"reddit\.com/r/[^/]+/(wiki|about|top|new|hot)(/|$)", re.I),
     "subreddit index page, not a post"),
    (re.compile(r"[?&](q|query|search)=", re.I), "search-results page"),
    (re.compile(r"(angi|angieslist|homeadvisor|thumbtack|houzz|porch|"
                r"buildzoom|networx|bbb)\.(com|org)", re.I), "lead-aggregator site"),
]
_LISTICLE = [
    (re.compile(r"\b(top|best)\s+\d{1,2}\b", re.I), '"top N / best N" listicle'),
    (re.compile(r"\b\d{1,2}\s+best\b", re.I), '"N best" listicle'),
    (re.compile(r"\bbest\s+[a-z ]{3,30}\s+(in|near)\s+", re.I), '"best X in Y" roundup'),
    (re.compile(r"\b(near\s+me)\b.{0,40}\b(20\d\d)\b", re.I), '"near me" SEO page'),
    (re.compile(r"\b(cost\s+guide|price\s+guide|how\s+much\s+does\s+it\s+cost\s+to)\b", re.I),
     "cost-guide content page"),
    (re.compile(r"\b(ultimate|complete)\s+guide\b", re.I), "guide content page"),
    (re.compile(r"/(blog|articles?|guides?|resources)/", re.I), "blog/article URL"),
]

# ---------------------------------------------------------------------------
# Dates. Only ever READ, never guessed.
# ---------------------------------------------------------------------------
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MONTH_RE = ("jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
             "jul(?:y)?|aug(?:ust)?|sep(?:t)?(?:ember)?|oct(?:ober)?|"
             "nov(?:ember)?|dec(?:ember)?")

_ISO_RE = re.compile(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b")
_URL_DATE_RE = re.compile(r"/(20\d\d)/(\d{1,2})(?:/(\d{1,2}))?/")
_MDY_RE = re.compile(r"\b(" + _MONTH_RE + r")\.?\s+(\d{1,2}),?\s+(20\d\d)\b", re.I)
_DMY_RE = re.compile(r"\b(\d{1,2})\s+(" + _MONTH_RE + r")\.?\s+(20\d\d)\b", re.I)
_MY_RE = re.compile(r"\b(" + _MONTH_RE + r")\.?\s+(20\d\d)\b", re.I)
_SLASH_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b")
# "3 days ago", "2 yr. ago", "· 7h ago", "posted 5 hours ago"
_AGO_RE = re.compile(
    r"\b(\d{1,3})\s*(second|sec|s|minute|min|m|hour|hr|h|day|d|week|wk|w|"
    r"month|mo|year|yr|y)s?\.?\s+ago\b", re.I)
_AGO_WORD_RE = re.compile(r"\b(today|yesterday|just now|an hour ago|a day ago)\b", re.I)
_AGO_UNIT_DAYS = {
    "second": 0, "sec": 0, "s": 0, "minute": 0, "min": 0, "m": 0,
    "hour": 0, "hr": 0, "h": 0, "day": 1, "d": 1, "week": 7, "wk": 7, "w": 7,
    "month": 30, "mo": 30, "year": 365, "yr": 365, "y": 365,
}


def _safe_date(y, m, d):
    try:
        return _dt.date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


def _parse_date(text, now):
    """Return (date, how_it_was_found) or (None, None). Never guesses."""
    m = _ISO_RE.search(text)
    if m:
        d = _safe_date(m.group(1), m.group(2), m.group(3))
        if d:
            return d, "ISO date in text"
    m = _URL_DATE_RE.search(text)
    if m:
        d = _safe_date(m.group(1), m.group(2), m.group(3) or 15)
        if d:
            return d, "date in URL path"
    m = _MDY_RE.search(text)
    if m:
        d = _safe_date(m.group(3), _MONTHS[m.group(1)[:3].lower()], m.group(2))
        if d:
            return d, "written date"
    m = _DMY_RE.search(text)
    if m:
        d = _safe_date(m.group(3), _MONTHS[m.group(2)[:3].lower()], m.group(1))
        if d:
            return d, "written date"
    m = _SLASH_RE.search(text)
    if m:
        d = _safe_date(m.group(3), m.group(1), m.group(2))
        if d:
            return d, "numeric date"
    m = _MY_RE.search(text)
    if m:
        # No day given. Mid-month is the least-wrong anchor and it is disclosed.
        d = _safe_date(m.group(2), _MONTHS[m.group(1)[:3].lower()], 15)
        if d:
            return d, "month-and-year only (day unknown, assumed mid-month)"
    m = _AGO_RE.search(text)
    if m:
        days = int(m.group(1)) * _AGO_UNIT_DAYS[m.group(2).lower()]
        return now - _dt.timedelta(days=days), "relative age in text"
    m = _AGO_WORD_RE.search(text)
    if m:
        w = m.group(1).lower()
        days = 0 if w in ("today", "just now", "an hour ago") else 1
        return now - _dt.timedelta(days=days), "relative age in text"
    return None, None


def _recency_score(age_days):
    """Steep decay. Under a week is gold; past a year it is archaeology."""
    if age_days < 0:
        return NEUTRAL          # a future date is a parse artifact, not a signal
    for limit, val in ((3, 1.0), (7, 0.95), (14, 0.85), (30, 0.7),
                       (60, 0.5), (90, 0.35), (180, 0.2), (365, 0.08)):
        if age_days <= limit:
            return val
    return 0.0


# ---------------------------------------------------------------------------
def score_hit(title, snippet, url, trade, city, relevance_terms,
              published_hint=None, now=None):
    """Score one search hit 0-1 and decide whether it may become a draft.

    relevance_terms is passed in by the caller (trade_vocab builds it). This
    module deliberately does not import it, so it can be tested standalone.

    `now` exists only so tests are deterministic; production leaves it None.
    """
    title = title or ""
    snippet = snippet or ""
    url = url or ""
    trade = (trade or "").strip()
    city = (city or "").strip()
    terms = [t.lower().strip() for t in (relevance_terms or []) if t and t.strip()]
    now = now or _dt.date.today()

    text = f"{title} {snippet}"
    low = text.lower()
    url_low = url.lower()
    reasons = []

    def out(score, reject=False, reject_reason=None, comps=None):
        return {
            "score": round(max(0.0, min(1.0, score)), 3),
            "reasons": reasons,
            "reject": reject,
            "reject_reason": reject_reason,
            "components": comps or {},
        }

    # -- kill switch: junk surfaces -----------------------------------------
    for rx, why in _JUNK_URL:
        if rx.search(url_low):
            reasons.append(f"junk surface: {why}")
            return out(0.0, True, f"Junk surface: {why}")
    for rx, why in _LISTICLE:
        if rx.search(text) or rx.search(url_low):
            reasons.append(f"listicle/content page: {why}")
            return out(0.0, True, f"Aggregator or listicle content, not a person: {why}")

    # -- kill switch: a business advertising itself -------------------------
    for rx, why in _BIZ_URL:
        if rx.search(url_low):
            reasons.append(f"business page: {why}")
            return out(0.0, True, f"Business page, not a person asking: {why}")
    biz_hits = [why for rx, why in _BIZ_PHRASES if rx.search(text)]
    if len(biz_hits) >= 2:
        reasons.append("business page: " + "; ".join(biz_hits))
        return out(0.0, True,
                   "Business advertising itself: " + "; ".join(biz_hits[:3]))
    if biz_hits:
        reasons.append(f"one vendor-ish phrase present ({biz_hits[0]}), not enough to reject")

    # -- geography -----------------------------------------------------------
    hay = f"{low} {url_low}"
    city_l = city.lower()
    in_market = []
    if city_l and city_l in hay:
        in_market.append(city)
    for c in DFW_CITIES:
        if c in hay and c != city_l:
            in_market.append(c)
    texas_named = any(t in f" {low} " for t in TEXAS_HINTS)

    other_state = None
    if not in_market and not texas_named:
        m = _STATE_NAME_RE.search(text)
        if m:
            other_state = m.group(1).title()
        else:
            m = _STATE_ABBR_RE.search(text)
            if m:
                other_state = m.group(1)
            else:
                m = _OTHER_BIG_CITIES.search(text)
                if m:
                    other_state = f"{m.group(1).title()} (out of metro)"
    if other_state:
        reasons.append(f"location points to {other_state}, not the DFW metro")
        return out(0.0, True, f"Out of market: post is anchored to {other_state}")

    if city_l and city_l in hay:
        geo = 1.0
        reasons.append(f"target city '{city}' appears")
    elif in_market:
        geo = 0.85
        reasons.append(f"nearby DFW city appears: {sorted(set(in_market))[0]}")
    elif texas_named:
        geo = 0.65
        reasons.append("Texas named but no specific in-market city")
    else:
        geo = NEUTRAL
        reasons.append("no location found in title, snippet or URL — scored neutral, not guessed")

    # -- recency -------------------------------------------------------------
    when, how = (None, None)
    if published_hint:
        when, how = _parse_date(str(published_hint), now)
        if when:
            how = f"{how} (from published_hint)"
    if not when:
        when, how = _parse_date(f"{text} {url}", now)
    if when:
        age = (now - when).days
        rec = _recency_score(age)
        reasons.append(f"dated {when.isoformat()} ({age}d old, {how}) — recency {rec:.2f}")
        if age > DEAD_THREAD_DAYS:
            reasons.append("thread is long dead; replying to it would read as a bot")
            return out(0.0, True,
                       f"Dead thread: dated {when.isoformat()}, {age} days old "
                       f"(cutoff {DEAD_THREAD_DAYS})")
    else:
        rec = NEUTRAL
        reasons.append("no date found in title, snippet or URL — recency scored neutral, not guessed")

    # -- trade relevance -----------------------------------------------------
    trade_words = [w for w in re.split(r"\W+", trade.lower()) if len(w) > 2]
    matched = sorted({t for t in terms if t and t in low})
    trade_named = any(w in low for w in trade_words) if trade_words else False
    if trade_named and matched:
        tr = 1.0
    elif trade_named:
        tr = 0.75
    elif matched:
        tr = 0.55 + min(0.3, 0.1 * len(matched))
    else:
        tr = 0.0
    if tr == 0.0:
        reasons.append("no trade word or vocabulary term present")
        return out(0.0, True,
                   f"No trade relevance: neither '{trade}' nor any relevance term appears")
    bits = []
    if trade_named:
        bits.append(f"trade '{trade}' named")
    if matched:
        bits.append(f"vocab hits: {', '.join(matched[:4])}")
    reasons.append("trade match — " + "; ".join(bits))

    # -- demand shape --------------------------------------------------------
    intent = 0.25
    if _ASK_STRONG.search(text):
        intent = 1.0
        reasons.append("explicit ask ('anyone recommend' / 'looking for' shape)")
    elif _SWITCH.search(text):
        intent = 0.8
        reasons.append("complaint about a current provider — a switch waiting to happen")
    elif "?" in title:
        intent = 0.65
        reasons.append("question in the title")
    elif _ASK_SOFT.search(text):
        intent = 0.5
        reasons.append("soft ask language")
    else:
        reasons.append("statement, not a request — low demand signal")
    if _FIRST_PERSON.search(text) and intent >= 0.5:
        intent = min(1.0, intent + 0.05)
        reasons.append("written in first person (a neighbor, not a brand)")

    comps = {"recency": rec, "intent": round(intent, 3), "trade": tr, "geo": geo}
    score = sum(WEIGHTS[k] * v for k, v in comps.items())
    score *= _staleness_multiplier(rec)
    comps["staleness_multiplier"] = round(_staleness_multiplier(rec), 3)

    if _URGENT.search(text):
        score = min(1.0, score + 0.05)
        reasons.append("urgency language — bump +0.05")

    return out(score, False, None, comps)


if __name__ == "__main__":  # tiny manual probe
    import json as _json
    print(_json.dumps(score_hit(
        "Anyone recommend a good roofer in Plano?",
        "Posted 2 days ago. We had hail damage last week and I need someone to look at it.",
        "https://nextdoor.com/p/abc123", "roofing", "Plano",
        ["roof", "shingle", "hail"]), indent=2))
