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
                     "verdict": "ok"|"unresolved_location"|"reject",
                     "components": {...}}

THREE VERDICTS, NOT TWO. `reject` answers "is this wrong". It cannot answer
"is this unproven", and conflating the two is how r/Roofing item 1vb2zkg
scored 0.910. See UNRESOLVED_LOCATION_CAP below.
"""
import datetime as _dt
import re

__all__ = ["score_hit", "DFW_CITIES", "WEIGHTS", "UNRESOLVED_LOCATION_CAP"]

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

# ---------------------------------------------------------------------------
# UNRESOLVED LOCATION. A local-services lead with no resolvable geography.
#
# WHY. Re-scoring the six Jackson Roofing drafts already sitting in Supabase,
# five were rejected on geography (Buckeye AZ, antioch--ca, r/akron,
# r/milwaukee, r/saskatoon). The sixth scored 0.910:
#
#   r/Roofing on Reddit: "Hail Storm came through town 2 days ago. Roofers
#   swarmed the town. Do I need a new roof based on this random sample"
#
# The demand is real — genuine hail urgency, a genuine homeowner question. The
# problem is that "town" is every town on earth. r/Roofing is a TRADE
# subreddit, so the subreddit check has nothing to conflict with, and NEUTRAL
# geography silently meant "no problem" instead of "unverified". That is the
# same wildcard bug as region="Texas", pointing the other way.
#
# The fix is NOT a banned-subreddit list — r/Roofing today, r/HVAC and
# r/Plumbing tomorrow, and the list rots. We detect the absence of any
# resolvable place, generically, wherever it comes from.
#
# These hits are NOT junk. Following identity_gate.py's house rule, unresolved
# means unproven, not wrong: they get their own verdict so a human can check
# the thread's actual location, and they are capped below watch_social's
# MIN_RELEVANCE (0.35) so nothing can auto-file one as a ready-to-send reply
# claiming "we do roofing around Plano". The pre-cap number is preserved in
# components["score_if_located"] so no information is destroyed.
# ---------------------------------------------------------------------------
UNRESOLVED_LOCATION_CAP = 0.30

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

# A configured "city" that is really a whole state or metro. Brilliant
# Fulfillment's row literally says "Texas", and that string alone let San
# Marcos (200 miles from DFW) read as an exact target-city match worth geo=1.0.
# Texas is 800 miles wide. For a local-services lead, statewide means "we do
# not know the city", NOT "anywhere in Texas is fine".
_STATEWIDE_REGIONS = {
    "", "tx", "texas", "north texas", "central texas", "east texas",
    "west texas", "south texas", "dfw", "dfw metroplex", "metroplex",
    "statewide", "nationwide", "usa", "us", "united states",
}

# A "City, TX" label — the shape Nextdoor puts in every post title and the
# shape people type in a snippet. Used to catch in-state-but-out-of-market.
_CITY_ST_TX = re.compile(r"\b([A-Za-z][A-Za-z .']{2,28}?),\s*(?:TX|Texas)\b", re.I)

# Words that appear in a "City, TX" capture but are not the city name.
_CITY_STOPWORDS = {"in", "near", "around", "from", "to", "at", "the", "of",
                   "here", "we", "us", "i", "you", "everyone", "neighbors",
                   "neighbours", "hello", "hi", "thanks", "today", "tomorrow"}

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
# Terms trade_vocab hands us for EVERY trade (its GENERIC_CONFIRM list) plus the
# obvious near-misses. They prove somebody said a hiring-shaped word; they prove
# nothing about the trade. Duplicated here on purpose — this module imports
# nothing from the project so it stays unit-testable standalone.
#
# WHY. For niche "health & beauty DTC" the vocabulary is
# ['health','beauty','dtc','recommend','recommendation','quote','estimate',
#  'hire','hiring','contractor','company','service','looking for', ...].
# The San Marcos intro post's only vocabulary hit was "recommendation", which
# bought it trade=0.65 for a post that never mentions the trade at all.
_GENERIC_TERMS = {
    "recommend", "recommends", "recommended", "recommendation", "recommendations",
    "quote", "quotes", "estimate", "estimates", "hire", "hiring", "hired",
    "contractor", "contractors", "company", "companies", "service", "services",
    "business", "businesses", "looking for", "need someone", "who do you use",
    "any suggestions", "suggestion", "suggestions", "referral", "referrals",
}

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
# Intent floor. Absence of evidence is not evidence of demand.
#
# WHY. Run 32976099694 filed "Hello! - San Marcos, TX | Nextdoor" as a
# client-ready draft. It is a neighbourhood introduction post. Nothing in it
# expresses a need for anything, let alone for the client's trade. It survived
# because `intent` merely DEFAULTED to 0.25 and the other three components
# (trade, geo, recency) carried it over the 0.35 bar. A statement scored 0.72
# on that arithmetic — twice the threshold — purely for being on-topic.
#
# So demand is now a GATE, not a weight. There must be positive evidence that
# somebody is asking for something. Notice this costs nothing in recall: every
# genuine lead shape ("anyone recommend", "looking for", "need someone to",
# "who does", "quotes for", a question mark, a complaint about an incumbent,
# an emergency) already trips one of the patterns below.
# ---------------------------------------------------------------------------

# Platform and location decoration that public-index titles carry. Stripped so
# the greeting test can anchor on the real title. "Hello! - San Marcos, TX |
# Nextdoor" is a REAL observed string; the whole title is the word "Hello!".
_PLATFORM_TAIL = re.compile(
    r"\s*[|]\s*(nextdoor|reddit|facebook|instagram|tiktok|x|twitter)\s*$", re.I)
_LOC_TAIL = re.compile(r"\s*[-–—]\s*([A-Za-z][A-Za-z .']{1,28}),\s*([A-Za-z]{2})\s*$")

# A title that is nothing but a salutation. No ask can hide in one word.
_GREETING_ONLY = re.compile(
    r"^[\s\W]*(hello|hi|hey|howdy|greetings|good\s+(morning|afternoon|evening)|"
    r"welcome|happy\s+\w+|hello\s+(neighbors?|neighbours?|everyone|all|there))"
    r"[\s!.,?–—-]*$", re.I)

# "I just moved here / new to the neighborhood / introducing myself". These
# posts very often DO contain a soft ask ("any recommendations for the area?"),
# which is why a soft-ask check alone did not save us. A general request for
# neighbourhood tips is not demand for the client's service, so an intro post
# is rejected even when it asks something. This is a hard reject on purpose.
_INTRO_POST = re.compile(
    r"\b(new\s+(to\s+the\s+(area|neighborhood|neighbourhood|community)|"
    r"here|neighbor|neighbour|resident)|just\s+moved\s+(here|in|to|into)|"
    r"introduc(e|ing)\s+(myself|ourselves)|wanted\s+to\s+(say|introduce)\s+"
    r"(hi|hello|myself)|saying\s+hello|first\s+post\s+here|"
    r"glad\s+to\s+be\s+here|nice\s+to\s+meet\s+everyone)\b", re.I)

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
    # Marketplace surfaces. Someone selling a sofa is the opposite of someone
    # who needs one hauled away, and the category landing pages are not posts
    # at all. Both were reaching the junk-removal queue.
    (re.compile(r"nextdoor\.com/(for_sale_and_free|forsale|marketplace)", re.I),
     "Nextdoor For Sale & Free marketplace"),
    # The platform's own marketing and editorial keeps surviving every other
    # gate — "Small Moments, Big Ripples: Neighbor Stories From Nextdoor" was
    # the sole survivor of a 39-result run.
    (re.compile(r"(blog|about|business|help)\.nextdoor\.com", re.I),
     "Nextdoor corporate/editorial page"),
    (re.compile(r"nextdoor\.com/(blog|press|newsroom|stories)", re.I),
     "Nextdoor editorial content"),
    (re.compile(r"/(marketplace|classifieds|for-sale|forsale)(/|$)", re.I),
     "classifieds/marketplace surface"),
    (re.compile(r"facebook\.com/marketplace", re.I), "Facebook Marketplace"),
    (re.compile(r"(craigslist|offerup|letgo|mercari)\.", re.I), "resale marketplace"),
]

# Titles that mark a listing rather than a request. "$200" plus "for sale"
# is someone selling; a category page has no specific ask at all.
_MARKETPLACE_TITLE = [
    # Platform editorial dressed as a neighbour post.
    (re.compile(r"(stories|moments)\s+from\s+nextdoor", re.I),
     "Nextdoor editorial, not a neighbour"),
    (re.compile(r"nextdoor\s+(100|blog|newsroom|guide)", re.I),
     "Nextdoor corporate content"),
    (re.compile(r"^\s*for sale\s*(&|and)\s*free", re.I),
     "marketplace category page, not a post"),
    (re.compile(r"for sale.{0,30}nextdoor", re.I), "marketplace listing"),
    (re.compile(r"for\s+\$\s?\d", re.I), "item listed for a price, a seller not a asker"),
    (re.compile(r"(free|curb ?alert).{0,20}(pick ?up|porch|curb)", re.I),
     "giveaway listing, not a service request"),
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

# Geography hides in the URL, not the text. A reddit post is anchored by its
# SUBREDDIT (/r/akron/ is Ohio, /r/saskatoon/ is Canada) and a Nextdoor post by
# its slug (/austin--tx/). The old check only read title+snippet, so six
# Jackson Roofing drafts were aimed at Arizona, California, Ohio, Wisconsin and
# Saskatchewan — for a Dallas roofer. A place name in the URL is the single
# most reliable location signal available and it was being ignored.
_LOCAL_SUBS = {
    "dfw", "dallas", "fortworth", "plano", "frisco", "mckinney", "allen",
    "richardson", "denton", "arlington", "irving", "garland", "texas", "austin",
    "houston", "sanantonio",
}
_SUB_RE = re.compile(r"reddit\.com/r/([A-Za-z0-9_]+)", re.I)
_ND_SLUG_RE = re.compile(r"nextdoor\.com/[a-z-]*/?([a-z][a-z-]+)--([a-z]{2})", re.I)


def _looks_like_city(name):
    """Cheap guard so a captured 'City, TX' is plausibly a place name."""
    n = name.strip().lower()
    if not n or len(n) < 3 or len(n) > 28:
        return False
    words = n.split()
    if not words or len(words) > 3:
        return False
    return not any(w in _CITY_STOPWORDS for w in words)


def _out_of_metro_tx_city(name):
    """True when `name` is a real Texas city label that is NOT in the DFW metro.

    THE SAN MARCOS RULE. The old geography check had exactly two verdicts:
    "some other state" (reject) or "Texas is mentioned" (geo 0.65, keep). There
    was no third case for "Texas, but the wrong end of it", so San Marcos —
    a three-hour drive past the southern edge of any DFW service area — scored
    as in-market. Texas being one state does not make it one labor market.
    """
    n = " ".join(name.strip().lower().split())
    if not _looks_like_city(n):
        return False
    if n in DFW_CITIES:
        return False
    # "north texas", "texas" etc. are regions, not cities: not a conflict.
    if n in _STATEWIDE_REGIONS:
        return False
    return True


def _url_location_conflict(url_low, city_l):
    """Return a reason string when the URL itself anchors somewhere else."""
    m = _ND_SLUG_RE.search(url_low)
    if m:
        slug_city, slug_state = m.group(1).replace("-", " "), m.group(2).lower()
        if slug_state != "tx":
            return f"Nextdoor slug says {slug_city.title()}, {slug_state.upper()}"
        # In Texas but not in the metro. The slug is the most authoritative
        # location a Nextdoor URL carries, so it outranks any city name that
        # happens to appear in the body text.
        if slug_city != city_l and _out_of_metro_tx_city(slug_city):
            return (f"Nextdoor slug says {slug_city.title()}, TX — a Texas city "
                    f"outside the DFW metro")
    m = _SUB_RE.search(url_low)
    if m:
        sub = m.group(1).lower()
        # Only judge subs that look like a place. A topical sub (r/roofing,
        # r/homeimprovement) carries no location and must not be rejected.
        generic = {"roofing", "homeimprovement", "diy", "construction", "askacontractor",
                   "realestate", "homeowners", "smallbusiness", "entrepreneur", "ecommerce",
                   "3pl", "logistics", "supplychain", "amazonfba", "shopify"}
        if sub not in generic and sub not in _LOCAL_SUBS and len(sub) > 3:
            return f"subreddit r/{m.group(1)} is not a DFW community"
    return None


def _label_location_conflict(title, text, city_l, in_market):
    """Sibling of _url_location_conflict for the 'City, TX' labels in the TEXT.

    A /p/ Nextdoor permalink carries no slug, so the URL check has nothing to
    read — but the public-index TITLE still ends in the post's neighbourhood:
    "Hello! - San Marcos, TX | Nextdoor". That label is the only geography the
    result has, and it was being read as "Texas is mentioned, good enough".

    Only fires when NO in-metro city appears anywhere. A post that says
    "moving from San Marcos to Plano" keeps its DFW anchor and is not rejected.
    """
    if in_market:
        return None
    labels = []
    m = _LOC_TAIL.search(_PLATFORM_TAIL.sub("", title or ""))
    if m and m.group(2).lower() == "tx":
        labels.append(m.group(1))
    labels += _CITY_ST_TX.findall(text or "")
    for name in labels:
        if _out_of_metro_tx_city(name) and name.strip().lower() != city_l:
            return (f"'{name.strip().title()}, TX' is a Texas city outside the "
                    f"DFW service area")
    return None


_ZIP_RE = re.compile(r"\b\d{5}(-\d{4})?\b")
_COUNTY_RE = re.compile(r"\b[A-Z][a-z]+\s+County\b")
_ANY_CITY_ST_RE = re.compile(r"\b[A-Za-z][A-Za-z .']{2,28}?,\s*[A-Z]{2}\b")


def _any_place_named(text, url_low):
    """Did ANYTHING in this hit name a place — any place, anywhere?

    Deliberately generic and deliberately permissive: it is asked only after
    every out-of-market check has already passed, so its job is to separate
    "a location exists and is merely ambiguous" from "there is no location in
    this document at all". It must never be a list of subreddits to ban —
    r/Roofing must fail this for the same structural reason r/HVAC and
    r/Plumbing will, namely that a trade sub carries no geography.
    """
    if _ANY_CITY_ST_RE.search(text) or _COUNTY_RE.search(text):
        return True
    if _ZIP_RE.search(text):
        return True
    if _ND_SLUG_RE.search(url_low):
        return True
    m = _SUB_RE.search(url_low)
    if m and m.group(1).lower() in _LOCAL_SUBS:
        return True   # e.g. r/austin — a real place, just not our metro
    return False


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

    def out(score, reject=False, reject_reason=None, comps=None, verdict=None):
        return {
            "score": round(max(0.0, min(1.0, score)), 3),
            "reasons": reasons,
            "reject": reject,
            "reject_reason": reject_reason,
            "verdict": verdict or ("reject" if reject else "ok"),
            "components": comps or {},
        }

    # -- kill switch: junk surfaces -----------------------------------------
    for rx, why in _JUNK_URL:
        if rx.search(url_low):
            reasons.append(f"junk surface: {why}")
            return out(0.0, True, f"Junk surface: {why}")
    for rx, why in _MARKETPLACE_TITLE:
        if rx.search(title or ""):
            reasons.append(f"marketplace surface: {why}")
            return out(0.0, True, f"Marketplace listing, not a service request: {why}")
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
    city_l = city.lower().strip()
    # A statewide/metro-wide "city" is an unknown city, not a match-anything
    # wildcard. Without this, city="Texas" made every Texas post an exact
    # target-city hit worth geo 1.0 — including San Marcos.
    city_is_region = city_l in _STATEWIDE_REGIONS
    if city_is_region and city_l:
        reasons.append(f"configured region '{city}' is statewide, not a city — "
                       f"judging by DFW metro membership instead")
    city_named = bool(city_l) and not city_is_region and city_l in hay
    in_market = []
    if city_named:
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

    # The URL is checked even when the text mentioned a DFW city, because a
    # neighbour in Akron can still say the word "Dallas".
    url_conflict = _url_location_conflict(url_low, city_l)
    if url_conflict:
        reasons.append(url_conflict)
        return out(0.0, True, f"Out of market: {url_conflict}")

    label_conflict = _label_location_conflict(title, text, city_l, in_market)
    if label_conflict:
        reasons.append(label_conflict)
        return out(0.0, True, f"Out of market: {label_conflict}")

    # geo_resolved answers a different question from geo: not "how good is the
    # location" but "did we find one at all". The old code had no way to say
    # "nowhere" as distinct from "somewhere mediocre", so nowhere scored 0.5.
    geo_resolved = True
    if city_named:
        geo = 1.0
        reasons.append(f"target city '{city}' appears")
    elif in_market:
        geo = 0.85
        reasons.append(f"nearby DFW city appears: {sorted(set(in_market))[0]}")
    elif texas_named:
        geo = 0.65
        reasons.append("Texas named but no specific in-market city")
    elif _any_place_named(text, url_low):
        # Some place was named and it survived every out-of-market check. Not
        # provably ours, not provably theirs. Honestly neutral.
        geo = NEUTRAL
        geo_resolved = True
        reasons.append("a place is named but it resolves to neither DFW nor an "
                       "excluded market — scored neutral, not guessed")
    else:
        geo = NEUTRAL
        geo_resolved = False
        reasons.append("NO location resolvable in title, snippet or URL — "
                       "the post could be anywhere on earth")

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
    specific = [t for t in matched if t not in _GENERIC_TERMS]
    if not trade_named and not specific:
        # Only generic hiring words matched. That is a shape, not a subject.
        if matched:
            reasons.append("only generic hire words matched: " + ", ".join(matched[:4]))
        return out(0.0, True,
                   f"No trade relevance: nothing trade-specific for '{trade}' appears"
                   + (f" (only generic terms: {', '.join(matched[:3])})" if matched else ""))
    if trade_named and matched:
        tr = 1.0
    elif trade_named:
        tr = 0.75
    elif specific:
        tr = 0.55 + min(0.3, 0.1 * len(specific))
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
    # INTENT FLOOR. Runs before scoring, because no amount of trade/geo/recency
    # should be able to manufacture demand that the post does not express.
    core_title = _LOC_TAIL.sub("", _PLATFORM_TAIL.sub("", title)).strip()
    if _GREETING_ONLY.match(core_title):
        reasons.append(f"title is a bare greeting ('{core_title}') — no need expressed")
        return out(0.0, True,
                   f"No demand: the whole title is a greeting ('{core_title}'), "
                   f"nobody is asking for anything")
    if _INTRO_POST.search(text):
        reasons.append("neighbourhood introduction post — general chat, not a service request")
        return out(0.0, True,
                   "No demand: introduction / 'new to the area' post. Any ask in it "
                   "is for general neighbourhood tips, not for this trade")

    ask_strong = _ASK_STRONG.search(text)
    switch = _SWITCH.search(text)
    ask_soft = _ASK_SOFT.search(text)
    urgent = _URGENT.search(text)
    question = "?" in title or "?" in snippet
    if not (ask_strong or switch or ask_soft or urgent or question):
        reasons.append("no request language and no question anywhere — a statement")
        return out(0.0, True,
                   "No demand: nothing in the title or snippet expresses a need "
                   "(no ask phrase, no question, no complaint, no urgency)")

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

    # -- unresolved location -------------------------------------------------
    # Held back, NOT discarded. The demand may be perfectly real; what is
    # missing is proof it is OUR demand. Deleting it would repeat the mistake
    # identity_gate.py exists to avoid — swapping confidently-wrong data for
    # confidently-deleted data.
    if not geo_resolved:
        comps["geo_resolved"] = False
        comps["score_if_located"] = round(min(1.0, score), 3)
        reasons.append(
            f"demand is strong ({comps['score_if_located']:.3f}) but unplaceable — "
            f"held at {UNRESOLVED_LOCATION_CAP} for human location check, not sent")
        return out(min(score, UNRESOLVED_LOCATION_CAP), False, None, comps,
                   verdict="unresolved_location")
    comps["geo_resolved"] = True

    return out(score, False, None, comps)


if __name__ == "__main__":  # tiny manual probe
    import json as _json
    print(_json.dumps(score_hit(
        "Anyone recommend a good roofer in Plano?",
        "Posted 2 days ago. We had hail damage last week and I need someone to look at it.",
        "https://nextdoor.com/p/abc123", "roofing", "Plano",
        ["roof", "shingle", "hail"]), indent=2))
