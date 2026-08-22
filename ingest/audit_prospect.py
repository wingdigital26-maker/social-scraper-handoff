#!/usr/bin/env python3
"""
Prospect audit engine — research every discovered business, ZERO API COST.

For each prospect found by social_discover.py, this answers the questions that
decide whether they are worth a call:

    who are they    -> name, website, phone, email, city
    where do they rank -> position for "{niche} {city}" in the search index
    what are people saying -> Google rating + review count + bad-review themes
    what is missing -> blog? service pages? SSL? thin site? no contact info?
    how badly do they need us -> need_score 0-1, the ranking signal for the queue

Everything is free: the public search index plus plain HTTP fetches of the
business's own public website. No paid APIs, no logins, no platform scraping.

    python audit_prospect.py                    # audit unaudited rows in Supabase
    python audit_prospect.py --limit 5          # small test batch
    python audit_prospect.py --local            # audit candidates.enriched.jsonl instead
    python audit_prospect.py --dry-run          # research + print, write nothing
"""
import argparse
import json
import pathlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests

try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        sys.exit("Needs the search client:  pip install ddgs")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from db import load_env

# Windows consoles default to cp1252 and business names routinely contain
# symbols and emoji it cannot encode. Without this, printing a single
# prospect name raises UnicodeEncodeError and kills the whole run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
SEARCH_SLEEP = 2.0
FETCH_TIMEOUT = 20

# Platform/aggregator domains that are never the business's own website.
NOT_A_WEBSITE = re.compile(
    r"(facebook|instagram|tiktok|linkedin|twitter|x|youtube|yelp|bbb|angi|"
    r"thumbtack|houzz|nextdoor|mapquest|yellowpages|google|birdeye|porch|"
    r"homeadvisor|indeed|glassdoor)\.", re.I)

PHONE_RE = re.compile(r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")

# Template/demo placeholders that look like real numbers. A prospect list is
# worthless if it sends someone dialling 469-000-0000, which a Frisco site
# really did publish.
FAKE_PHONE = re.compile(r"^(?:\d{3})?(?:000\d{4}|1234567|0000000|9999999|"
                        r"55501\d{2}|1111111|123456\d)$")


def clean_phone(raw):
    """Return a real-looking phone, or None. Rejects placeholders outright."""
    if not raw:
        return None
    d = re.sub(r"\D", "", raw)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10:
        return None
    if FAKE_PHONE.match(d) or len(set(d)) <= 2:
        return None
    if d[3:] == "0000000" or d[3:6] in ("000", "555"):
        return None
    return raw.strip()

# DFW-area area codes. A prospect whose phone sits outside these is probably a
# same-name business in another state — caught for real with an Oklahoma
# "Litz Roofing" (405) and an India-hosted "Sunaura Solar" (.in) surfacing in a
# Plano search. Flagged, not deleted: the human decides.
DFW_AREA_CODES = {"214", "469", "972", "817", "682", "945", "940", "430", "903"}
FOREIGN_TLD = re.compile(r"\.(in|uk|au|ca|pk|ph|ng|de|fr|ru|cn|br|za)$", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
RATING_RE = re.compile(r"([0-5][.,]\d)\s*(?:out of 5|stars?|★|/\s*5)", re.I)
REVIEWS_RE = re.compile(r"([\d,]{1,7})\s*(?:google\s*)?reviews?", re.I)

# Bad-review themes worth flagging — these are what a prospect is losing jobs over.
COMPLAINT_THEMES = {
    "no callback": ["never called back", "no call back", "wouldn't return", "never returned my call",
                    "no response", "never heard back", "ghosted"],
    "late/no-show": ["never showed", "no show", "didn't show up", "hours late", "kept rescheduling"],
    "quality": ["leaked", "leaking", "shoddy", "poor workmanship", "had to redo", "did it wrong",
                "damaged my"],
    "pricing": ["overcharged", "hidden fee", "price went up", "overpriced", "quoted me"],
    "communication": ["never answered", "couldn't reach", "no communication", "wouldn't respond"],
}


def search(query, limit=6):
    try:
        with DDGS() as d:
            out = list(d.text(query, max_results=limit))
        time.sleep(SEARCH_SLEEP)
        return out
    except Exception as e:
        print(f"      search failed: {str(e)[:70]}")
        return []


def sb_request(method, url, *, retries=4, **kw):
    """Supabase call with backoff. A single transient blip used to kill a
    whole sweep step mid-run, so every DB call goes through here."""
    delay = 2
    for attempt in range(retries):
        try:
            r = requests.request(method, url, timeout=45, **kw)
            if r.status_code < 500:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)[:80]
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    print(f"      DB call failed after {retries} tries: {last}")
    return None


# ------------------------------------------------------------ who are they ---
# Words too generic to prove a domain belongs to a business. "4X Construction
# Group LLC" was matched to mansfieldgroup.net purely because both contain
# "group" — a distinctive token has to be something only this company uses.
STOPWORDS = {"roofing", "roof", "hvac", "plumbing", "the", "and", "llc", "inc",
             "co", "company", "services", "service", "of", "greater", "tx",
             "texas", "construction", "contractors", "contractor",
             "group", "pros", "pro", "team", "solutions", "partners", "systems",
             "associates", "enterprises", "industries", "brothers", "bros",
             "sons", "home", "homes", "exteriors", "remodeling", "restoration",
             "builders", "building", "general", "quality", "best", "top",
             "local", "american", "usa", "dfw", "metroplex", "north", "south",
             "east", "west", "your", "expert", "experts", "master", "premier"}


def _tokens(name):
    return [w for w in re.split(r"\W+", (name or "").lower())
            if len(w) > 2 and w not in STOPWORDS]


_CC_SECOND = {"co", "com", "net", "org", "gov", "ac"}   # e.g. co.uk, com.au


def _registrable(host):
    """'shop.example.co.uk' -> 'example'. Good-enough eTLD+1 without a PSL."""
    labels = (host or "").lower().split(":")[0].strip(".").split(".")
    if len(labels) < 2:
        return labels[0] if labels else ""
    if len(labels) >= 3 and labels[-2] in _CC_SECOND and len(labels[-1]) == 2:
        return labels[-3]
    return labels[-2]


def _owns_site(name, host, html):
    """Is this site really this business? Requires positive evidence.

    Without this the finder happily returns whatever ranked first — it once
    matched a roofer to ultimate-guitar.com. A wrong website poisons every
    downstream signal (rank, blog, contact) and would put a false claim in a
    sales call, so an unverified site is treated as no site at all.
    """
    n = _norm(name)
    # Compare against the REGISTRABLE domain, not the full host. Doorway spam
    # hides the business name in a subdomain on a shared domain — caught for
    # real with "bannerroofingconstructionllc.discoveredats.com". A business
    # that owns its web presence owns the domain itself.
    h = _norm(_registrable(host))
    if n and h and (n in h or h in n):
        short, long_ = sorted((len(n), len(h)))
        # Length guard: "roofingdallas" must not match "metalroofingdallas",
        # which is a different company with a superset domain.
        if short / long_ >= 0.8:
            return True
    if not html:
        return False
    # Body text is not enough — directory pages list dozens of businesses and
    # would match everyone. Require the name in the page's own <title>.
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    title = _norm(m.group(1) if m else "")
    if not title or not n:
        return False
    # A title match alone is not enough: spam/doorway domains mirror a real
    # business name in their <title> to game search. Caught for real —
    # "roofingcontractors.faculty.bio" titled itself "Bumble Roofing of Greater
    # Dallas" while the real site was bumbleroofing.com. Require at least one
    # distinctive word of the name to appear in the host itself.
    toks_h = _tokens(name)
    if toks_h and not any(t in h for t in toks_h):
        return False
    if not toks_h and n and n not in h:
        # Name is all generic words (e.g. "M&R Roofing"): with nothing
        # distinctive to check, a title match alone is not enough evidence.
        return False
    if n in title:
        # Same length guard as the domain check: a generic name like
        # "roofingdallas" sits inside "metalroofingdallas", a different company.
        first = re.split(r"[|\-–—:]", m.group(1))[0] if m else ""
        return len(n) / max(len(_norm(first)) or len(title), 1) >= 0.8
    toks = _tokens(name)
    return bool(toks) and all(_norm(t) in title for t in toks)


def find_website(name, city, is_person=False):
    """Find and VERIFY the business's own site. Returns None unless confirmed.

    For PERSON profiles (LinkedIn /in/) this returns None outright. Personal
    names are not distinctive enough to match a domain safely — searching
    "Brian Eddy" surfaced brianeddymd.com, a psychiatrist in Connecticut, and
    attached it to a Dallas roofing prospect. A person is a contact AT a
    company, not a business whose website we can infer from their name.
    """
    if is_person:
        return None
    seen_hosts = set()
    for q in (f'"{name}" {city} official website', f'{name} {city}'):
        for r in search(q, 8):
            href = r.get("href") or ""
            parts = urlparse(href)
            host = parts.netloc
            if not host or host in seen_hosts or NOT_A_WEBSITE.search(host):
                continue
            seen_hosts.add(host)
            base = f"{parts.scheme or 'https'}://{host}"
            # Cheap check first: does the domain itself look like the business?
            if _owns_site(name, host, None):
                return base
            try:
                page = requests.get(base, headers=UA, timeout=FETCH_TIMEOUT)
                if page.status_code < 400 and _owns_site(name, host, page.text):
                    return base
            except Exception:
                continue
    return None


def crawl_site(url):
    """Fetch homepage + one contact page. Returns site signals, all free."""
    sig = {"website": url, "ssl_ok": None, "phone": None, "email": None,
           "has_blog": None, "has_service_pages": None, "page_count": None,
           "reachable": False, "title": None}
    try:
        r = requests.get(url, headers=UA, timeout=FETCH_TIMEOUT, allow_redirects=True)
    except Exception as e:
        sig["error"] = str(e)[:80]
        return sig
    if r.status_code >= 400:
        sig["error"] = f"HTTP {r.status_code}"
        return sig

    html = r.text
    sig["reachable"] = True
    sig["ssl_ok"] = r.url.startswith("https")
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    sig["title"] = m.group(1).strip()[:120] if m else None

    links = set(re.findall(r'href=["\']([^"\']+)["\']', html))
    internal = {urljoin(url, l) for l in links
                if not l.startswith(("mailto:", "tel:", "#", "javascript:"))}
    internal = {l for l in internal if urlparse(l).netloc == urlparse(url).netloc}
    sig["page_count"] = len(internal)
    sig["has_blog"] = any(re.search(r"/blog|/news|/articles|/resources", l, re.I) for l in internal)
    sig["has_service_pages"] = any(
        re.search(r"/service|/repair|/replace|/install|/residential|/commercial", l, re.I)
        for l in internal)

    phones = PHONE_RE.findall(html)
    sig["phone"] = next((c for c in (clean_phone(x) for x in phones) if c), None)
    emails = [e for e in EMAIL_RE.findall(html)
              if not re.search(r"\.(png|jpg|jpeg|gif|svg|webp|css|js)$", e, re.I)
              and "sentry" not in e.lower() and "example" not in e.lower()]
    sig["email"] = emails[0] if emails else None

    # A contact page often holds the email when the homepage doesn't.
    if not sig["email"]:
        contact = next((l for l in internal if re.search(r"/contact", l, re.I)), None)
        if contact:
            try:
                c = requests.get(contact, headers=UA, timeout=FETCH_TIMEOUT)
                more = [e for e in EMAIL_RE.findall(c.text)
                        if not re.search(r"\.(png|jpg|jpeg|gif|svg|webp|css|js)$", e, re.I)]
                if more:
                    sig["email"] = more[0]
                if not sig["phone"]:
                    p = PHONE_RE.findall(c.text)
                    sig["phone"] = next((c for c in (clean_phone(x) for x in p) if c), None)
            except Exception:
                pass
    return sig


# --------------------------------------------------- google maps (free bin) ---
# Reuses the gosom google-maps-scraper binary already on this machine (free, no
# key). Scraped ONCE PER NICHE+CITY and cached, then every prospect is matched
# against that cache — one ~60s scrape instead of one per business.
MAPS_BIN_DIR = pathlib.Path(r"C:\Users\wjack\github-tools\gosom-google-maps-scraper\bin")
MAPS_CACHE_DIR = pathlib.Path(__file__).resolve().parent / ".maps_cache"
# fast-mode needs a geo anchor; add cities as the pipeline expands.
CITY_COORDS = {
    "dallas": (32.7767, -96.797), "fort worth": (32.7555, -97.3308), "arlington": (32.7357, -97.1081),
    "grand prairie": (32.7459, -96.9978), "irving": (32.814, -96.9489), "garland": (32.9126, -96.6389),
    "mesquite": (32.7668, -96.5992), "coppell": (32.9546, -96.99), "farmers branch": (32.9268, -96.8961),
    "carrollton": (32.9756, -96.89), "plano": (33.0198, -96.6989), "frisco": (33.1507, -96.8236),
    "mckinney": (33.1972, -96.6398), "allen": (33.1032, -96.6706), "richardson": (32.9483, -96.7299),
    "denton": (33.2148, -97.1331), "lewisville": (33.0462, -96.9942), "flower mound": (33.0146, -97.0969),
    "grapevine": (32.9343, -97.0781), "euless": (32.8371, -97.0819), "bedford": (32.844, -97.1431),
    "hurst": (32.8235, -97.1706), "north richland hills": (32.8343, -97.2289),
    "haltom city": (32.7996, -97.2692), "mansfield": (32.5632, -97.1417),
    "cedar hill": (32.5885, -96.9561), "desoto": (32.5896, -96.857), "lancaster": (32.5921, -96.7561),
    "duncanville": (32.6518, -96.9083), "rockwall": (32.9312, -96.4597), "addison": (32.9618, -96.8292),
    "cleburne": (32.3476, -97.3867), "waxahachie": (32.3865, -96.8483), "burleson": (32.5421, -97.3208),
    "keller": (32.9346, -97.2289), "southlake": (32.9412, -97.1342), "wylie": (33.0151, -96.5389),
    "murphy": (33.0151, -96.613), "sachse": (32.9762, -96.5952), "the colony": (33.089, -96.8861),
    "little elm": (33.1626, -96.9375), "prosper": (33.2362, -96.8011), "celina": (33.3245, -96.7847),
    "anna": (33.3487, -96.5486), "forney": (32.7482, -96.4719), "midlothian": (32.4832, -96.9944),
    # non-DFW, kept for when the pipeline expands beyond the metro
    "houston": (29.7604, -95.3698), "austin": (30.2672, -97.7431),
    "san antonio": (29.4241, -98.4936),
}


def _maps_bin():
    if not MAPS_BIN_DIR.exists():
        return None
    bins = sorted(MAPS_BIN_DIR.glob("google_maps_scraper-*windows-amd64.exe"))
    return bins[-1] if bins else None


def maps_cache(niche, city):
    """All Maps businesses for one niche+city, scraped once and cached to disk."""
    import subprocess, tempfile
    key = re.sub(r"\W+", "_", f"{niche}_{city}".lower()).strip("_")
    MAPS_CACHE_DIR.mkdir(exist_ok=True)
    cached = MAPS_CACHE_DIR / f"{key}.json"
    if cached.exists():
        try:
            return json.loads(cached.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    binary = _maps_bin()
    coords = CITY_COORDS.get(city.lower())
    if not binary or not coords:
        if not binary:
            print("      (no Maps binary found — skipping Maps enrichment)")
        else:
            print(f"      (no coords for {city} — add to CITY_COORDS for Maps data)")
        return []

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="maps_"))
    (tmp / "q.txt").write_text(f"{niche} {city}\n", encoding="utf-8")
    out = tmp / "out.json"
    print(f"      scraping Google Maps for '{niche} {city}' (once, then cached)...")
    try:
        subprocess.run(
            [str(binary), "-input", str(tmp / "q.txt"), "-results", str(out),
             "-json", "-c", "1", "-exit-on-inactivity", "90s",
             "-fast-mode", "-geo", f"{coords[0]},{coords[1]}", "-zoom", "12"],
            cwd=str(tmp), timeout=600,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"      Maps scrape failed: {str(e)[:70]}")
        return []
    if not out.exists():
        return []
    try:
        data = json.loads(out.read_text(encoding="utf-8", errors="ignore") or "[]")
    except json.JSONDecodeError:
        return []
    recs = data if isinstance(data, list) else [data]
    cached.write_text(json.dumps(recs), encoding="utf-8")
    print(f"      cached {len(recs)} Maps businesses for {city}")
    return recs


def _norm(s):
    return re.sub(r"\b(llc|inc|co|company|the|and|of|greater|tx|texas)\b|\W", "",
                  (s or "").lower())


def match_business(name, recs):
    """Find this prospect inside the cached Maps results (fuzzy, conservative)."""
    n = _norm(name)
    if not n or not recs:
        return None
    for r in recs:                                   # exact normalized match first
        if _norm(r.get("title")) == n:
            return r
    # Containment only when the two names are close in length. Without this,
    # "roofingdallas" wrongly matches "lowcostroofingdallas" and we would
    # attribute another company's rating and phone number to this prospect.
    best, best_len = None, 0
    for r in recs:
        t = _norm(r.get("title"))
        if not t or len(t) < 6:
            continue
        if t in n or n in t:
            short, long_ = sorted((len(t), len(n)))
            if short / long_ < 0.75:      # too different to be the same business
                continue
            if len(t) > best_len:
                best, best_len = r, len(t)
    return best


# ------------------------------------------------------- what people say -----
def google_reputation(name, city):
    """Rating + review count + complaint themes, read from public search snippets.

    Deliberately index-based: Google Maps blocks direct scraping, but rating and
    review text surface in indexed snippets from Google/Yelp/BBB listings.
    """
    out = {"gmb_rating": None, "gmb_reviews": None, "bad_review_themes": None}
    blob = []
    key = _norm(name)
    for q in (f'"{name}" {city} reviews rating', f'"{name}" {city} complaints'):
        for r in search(q, 6):
            text = f"{r.get('title', '')} {r.get('body', '')}"
            blob.append(text)
            # Only trust numbers from a snippet that actually names THIS business.
            # Otherwise we scrape a competitor's rating out of a listicle and
            # hand Jack a wrong number on a sales call.
            if not key or key[:14] not in _norm(text):
                continue
            if out["gmb_rating"] is None:
                m = RATING_RE.search(text)
                if m:
                    try:
                        out["gmb_rating"] = float(m.group(1).replace(",", "."))
                    except ValueError:
                        pass
            if out["gmb_reviews"] is None:
                m = REVIEWS_RE.search(text)
                if m:
                    try:
                        n_rev = int(m.group(1).replace(",", ""))
                        out["gmb_reviews"] = n_rev if n_rev < 100000 else None
                    except ValueError:
                        pass
    joined = " ".join(blob).lower()
    hits = [theme for theme, phrases in COMPLAINT_THEMES.items()
            if any(p in joined for p in phrases)]
    out["bad_review_themes"] = ", ".join(hits) if hits else None
    return out


# ------------------------------------------------------------- do they rank --
def seo_rank(name, niche, city, website):
    """Position for '{niche} {city}'. None = not in the first page of results."""
    if not website:
        return None
    host = urlparse(website).netloc.replace("www.", "")
    for i, r in enumerate(search(f"{niche} {city}", 15), start=1):
        if host and host in (r.get("href") or ""):
            return i
    return None


# --------------------------------------------------------------- the model ---
def need_score(a, is_person=False):
    """How badly does this business need what Wing Digital sells? 0 = fine, 1 = desperate.

    Every component is a real, sellable gap. Weights favour the problems Wing
    actually fixes: invisibility in search, no content engine, reputation drag.
    """
    # A person (LinkedIn /in/) is a decision-maker to contact, not a business
    # to audit. Scoring them on "no website" or "no blog" would be nonsense and
    # would float them above real businesses in the queue.
    if is_person:
        return None, ["person profile — decision-maker contact, audit their company instead"]

    gaps, pts, total = [], 0.0, 0.0

    def add(weight, bad, label):
        nonlocal pts, total
        total += weight
        if bad:
            pts += weight
            gaps.append(label)

    # Only judge ranking when we actually have a site to look for. "No website
    # found" is its own gap below — counting it as "not ranking" too would
    # double-penalize and would be a guess, not a finding.
    if a.get("website"):
        add(0.25, a.get("seo_rank") is None, "not ranking for their main keyword")
    add(0.10, a.get("website") is None, "no website found")

    # Only judge site content when we actually READ the site. A 403/timeout
    # means unknown, not missing — scoring it as a gap would invent a weakness
    # and put a false claim in Jack's mouth on the call.
    if a.get("site_read"):
        add(0.20, a.get("has_blog") is False, "no blog / no content engine")
        add(0.15, a.get("has_service_pages") is False, "no service pages")
        add(0.10, (a.get("page_count") or 0) < 12, "very thin website")
        add(0.05, a.get("ssl_ok") is False, "no SSL")
        add(0.05, not a.get("phone") and not a.get("email"), "no contact info on site")
    elif a.get("website"):
        gaps.append("site could not be read (blocked or down) — verify by hand")

    # Geography sanity — surfaced as a warning so nobody calls the wrong state.
    host = urlparse(a.get("website") or "").netloc
    if host and FOREIGN_TLD.search(host.split(":")[0]):
        gaps.append("WARNING: non-US website domain — likely a different company")
    ph = a.get("phone") or ""
    digits = re.sub(r"\D", "", ph)
    if len(digits) >= 10:
        code = digits[-10:-7]
        if code not in DFW_AREA_CODES:
            gaps.append(f"WARNING: {code} area code is outside DFW — verify this is the right company")

    rating = a.get("gmb_rating")
    add(0.05, rating is not None and rating < 4.3, f"rating {rating}" if rating else "low rating")
    reviews = a.get("gmb_reviews")
    add(0.05, reviews is not None and reviews < 25, f"only {reviews} reviews" if reviews else "few reviews")
    if a.get("bad_review_themes"):
        gaps.append(f"complaints: {a['bad_review_themes']}")

    return round(pts / total, 3) if total else None, gaps


# ------------------------------------------------------------------ audit ----
def audit(c, maps_recs=None):
    name = c.get("title") or c.get("name")
    city = c.get("place_name") or c.get("place") or ""
    niche = c.get("category") or ""
    print(f"  auditing {name} ({city})")

    # Google Maps first — it is authoritative for phone/website/rating/address.
    maps = match_business(name, maps_recs or [])
    maps_out = {}
    if maps:
        maps_out = {"phone": clean_phone(maps.get("phone")),
                    "website": (maps.get("website") or "").split("?")[0] or None,
                    "gmb_rating": maps.get("review_rating") or None,
                    "address": maps.get("address")}
        print(f"      matched on Maps: {maps.get('title')}")

    # LinkedIn /in/ URLs are people; everything else is a business page.
    is_person = "/in/" in (c.get("url") or "") or c.get("prospect_type") == "person"
    website = (c.get("website") or maps_out.get("website")
               or find_website(name, city, is_person=is_person))
    site = crawl_site(website) if website else {"website": None}
    if site.get("error"):
        print(f"      site unreachable: {site['error']}")
    rep = google_reputation(name, city)
    rank = seo_rank(name, niche, city, site.get("website"))

    a = {**site, **rep, "seo_rank": rank}
    a["site_read"] = bool(site.get("reachable"))
    # Maps wins on contact + rating; the crawl fills whatever Maps lacks.
    for k in ("phone", "website", "gmb_rating"):
        if maps_out.get(k):
            a[k] = maps_out[k]
    a.pop("address", None)
    a.pop("reachable", None); a.pop("title", None); a.pop("error", None)
    if not a.get("site_read"):        # do not report unverified content signals
        for k in ("has_blog", "has_service_pages", "page_count", "ssl_ok"):
            a[k] = None
    a["need_score"], gaps = need_score(a, is_person=is_person)
    a["audit_gaps"] = gaps
    a.pop("site_read", None)          # scoring input, not a DB column
    a["audited_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"      need {a['need_score']} | rank {rank} | "
          f"{a.get('gmb_rating')} stars, {a.get('gmb_reviews')} reviews | "
          f"{', '.join(gaps[:3]) or 'no gaps'}")
    return a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--local", action="store_true", help="audit candidates.enriched.jsonl")
    ap.add_argument("--workers", type=int, default=4,
                    help="prospects audited concurrently (1 = serial)")
    args = ap.parse_args()

    if args.local:
        path = pathlib.Path(__file__).resolve().parent / "candidates.enriched.jsonl"
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        rows = rows[:args.limit] if args.limit else rows
        for c in rows:
            audit(c)
        return

    env = load_env()
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json", "Prefer": "return=minimal"}

    params = {"audited_at": "is.null", "status": "eq.new",
              "select": "id,title,place_name,category,website,url",
              "order": "id.asc"}
    if args.limit:
        params["limit"] = str(args.limit)
    r = sb_request("GET", f"{url}/rest/v1/candidates", headers=h, params=params)
    if r is None or not r.ok:
        sys.exit("Could not read the queue from Supabase.")
    rows = r.json()
    if not rows:
        print("Nothing to audit."); return

    print(f"Auditing {len(rows)} prospects (free: Maps + search index + their own site)\n")
    # One Maps scrape per niche+city, shared across every prospect in that group.
    groups = {}
    for c in rows:
        groups.setdefault((c.get("category") or "", c.get("place_name") or ""), []).append(c)

    def do_one(c, recs):
        a = audit(c, recs)
        if args.dry_run:
            return
        sb_request("PATCH", f"{url}/rest/v1/candidates", headers=h,
                   params={"id": f"eq.{c['id']}"}, json=a)

    for (niche, city), group in groups.items():
        recs = maps_cache(niche, city) if niche and city else []
        if args.workers > 1:
            # Each prospect is independent network I/O, so a few in flight at
            # once turns an hours-long pass into minutes. Kept modest so the
            # search index does not start throttling us.
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = [pool.submit(do_one, c, recs) for c in group]
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception as e:
                        print(f"      audit error: {str(e)[:90]}")
        else:
            for c in group:
                do_one(c, recs)
    print("\nDone. Queue is now ranked by need_score.")


if __name__ == "__main__":
    main()
