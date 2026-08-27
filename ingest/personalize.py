#!/usr/bin/env python3
"""
Find ONE real, checkable fact per verified lead — or store nothing.

Wing's own record is the reason this file exists: 133 cold emails to generic
role inboxes produced zero replies, while a single researched email to a named
person closed. A fact-check of the previous drafts found 19 of 23 carrying the
literal string "category: balm, no complaint quoted" — a mail-merge blast in a
personalization costume. This module refuses to produce that.

For each candidates row with identity='verified' it fetches the business's OWN
public pages and looks for something that is true of THAT business and nobody
else, then writes:

    personalization         short, human-readable, TRUE observation
    personalization_source  the exact URL a human can open to check it

Every stored fact carries an evidence string that was literally found in the
text of the fetched page. If no such fact can be grounded, personalization is
left NULL. A null is the correct answer — it tells Jack that lead needs manual
research before it is worth sending to. There is no category-level fallback,
because the fallback IS the bug.

This module makes NO model call of any kind. Every fact below is a regex match
plus a count, a subtraction, or a set difference. There is no API key to leak,
no token cost per lead, and no surface on which a model can invent a detail —
which is what makes it safe to run across the whole candidate table at once.

    python personalize.py --dry-run --limit 12    # research + print, write nothing
    python personalize.py --limit 20              # research + store
    python personalize.py --recheck               # redo rows already looked at
    python personalize.py --show-regexes          # repr() every pattern, then exit
"""
import argparse
import datetime
import html as html_mod
import json
import pathlib
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from db import load_env
from audit_prospect import sb_request

# Windows consoles default to cp1252 and business names routinely contain
# symbols the codec cannot encode; printing one would kill the whole run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
FETCH_TIMEOUT = 20
THIS_YEAR = datetime.date.today().year

# Cities Wing sells into. Used only to notice that a business NAMES a city on
# its own site while having no page for it — never to invent a service area.
DFW_CITIES = [
    "Allen", "Arlington", "Bedford", "Carrollton", "Cedar Hill", "Cleburne",
    "Colleyville", "Coppell", "Dallas", "Denton", "DeSoto", "Duncanville",
    "Euless", "Flower Mound", "Fort Worth", "Frisco", "Garland", "Grapevine",
    "Grand Prairie", "Haltom City", "Highland Park", "Hurst", "Irving",
    "Keller", "Lancaster", "Lewisville", "Little Elm", "Mansfield",
    "McKinney", "Mesquite", "Midlothian", "North Richland Hills", "Plano",
    "Prosper", "Richardson", "Rockwall", "Rowlett", "Sachse", "Southlake",
    "The Colony", "Trophy Club", "University Park", "Waxahachie", "Weatherford",
    "Wylie",
]

# Manufacturer / trade credentials. A business either publishes one of these
# verbatim or it does not — there is nothing to infer.
CERTIFICATIONS = [
    "GAF Master Elite", "Master Elite", "GAF Certified", "GAF Presidential",
    "Owens Corning Platinum", "Owens Corning Preferred", "Platinum Preferred",
    "CertainTeed SELECT ShingleMaster", "CertainTeed ShingleMaster",
    "SELECT ShingleMaster", "Malarkey Emerald", "Atlas Pro Plus",
    "IKO Shield Pro", "TAMKO Pro", "DaVinci Masterpiece",
    "Velux Certified", "Haag Certified", "RCAT", "North Texas Roofing Contractors",
]

# Specific roof systems. Generic "roof repair" is true of every roofer and is
# deliberately absent from this list.
NICHE_SERVICES = [
    "standing seam", "metal roof", "slate roof", "cedar shake", "tile roof",
    "TPO", "EPDM", "PVC roofing", "modified bitumen", "built-up roof",
    "spray foam roof", "solar shingle", "solar panel", "skylight",
    "gutter guard", "seamless gutter", "copper gutter", "attic ventilation",
    "radiant barrier", "storm damage", "hail damage", "commercial roof",
]

# Awards and standings a business either prints verbatim or does not. Same
# rule as CERTIFICATIONS: literal substring, never inferred.
AWARDS = [
    "Super Service Award", "Angi Super Service", "Best of Houzz",
    "Best of HomeAdvisor", "Neighborhood Favorite", "Torch Award",
    "Best of Dallas", "Best of Fort Worth", "Best of Denton",
    "Best of Plano", "Best of Frisco", "Best of McKinney",
    "Contractor of the Year", "Top Rated Local", "Angie's List",
    "Three Best Rated", "Readers' Choice", "Readers Choice",
]

# Offers that give a call an opening line. Literal text only.
OFFERS = [
    "financing available", "financing options", "0% financing",
    "no interest", "no money down", "flexible financing",
    "lifetime warranty", "lifetime workmanship warranty",
    "workmanship warranty", "labor warranty", "50-year warranty",
    "50 year warranty", "25-year warranty", "25 year warranty",
    "10-year workmanship", "golden pledge", "system plus",
    "free inspection", "free estimate", "free roof inspection",
    "emergency service", "24/7 emergency", "same day service",
]

MONTHS = ("january february march april may june july august september "
          "october november december").split()
MONTH_RE = re.compile(
    r"\b(" + "|".join(MONTHS) + r")\w*\.?\s+(\d{1,2},?\s+)?(19|20)\d{2}\b", re.I)
ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
SINCE_RE = re.compile(r"\bsince\s+((?:19|20)\d{2})\b", re.I)
YEARS_RE = re.compile(
    r"\b(?:over\s+|more\s+than\s+)?(\d{2,3})\s*\+?\s*years?\s+"
    r"(?:of\s+)?(?:experience|in\s+business|serving|of\s+service|"
    r"in\s+the\s+roofing|of\s+roofing)", re.I)
TAG_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)

# --- deterministic finders added 2026-08-27 --------------------------------
# Every one of these is a literal read. Run --show-regexes to see repr() of
# each; this file has previously shipped a pattern containing a literal 0x08
# byte and an "email" pattern that matched "slick-carousel@1.8.1", so the
# reprs are printable on demand rather than trusted by eye.

# A footer copyright. visible_text() unescapes &copy; to © before this runs.
COPYRIGHT_RE = re.compile(
    r"(?:©|\(c\)|copyright)\s*(?:©\s*)?"
    r"((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)\d{2}))?", re.I)

# "family owned and operated", optionally with a founding year.
FAMILY_RE = re.compile(
    r"\bfamily[\s–-]?(?:owned|run|operated)"
    r"(?:\s*(?:and|&)\s*operated)?"
    r"(?:\s+since\s+((?:19|20)\d{2}))?", re.I)

# A countable production claim: "3,000 roofs installed", "500+ homes served".
VOLUME_RE = re.compile(
    r"\b((?:\d{1,3},)?\d{3,6}|\d{2,3})\s*\+?\s+"
    r"(roofs|homes|houses|customers|clients|projects|jobs|properties|families|"
    r"buildings|businesses)\s+"
    r"(installed|completed|served|serviced|replaced|repaired|roofed|helped|"
    r"protected)\b", re.I)

# A crew-size claim. Capped at 3-4 digits so a phone number or a ZIP cannot
# be read as a headcount.
CREW_RE = re.compile(
    r"\b(?:team|crew|staff)\s+of\s+(?:over\s+|more\s+than\s+)?(\d{1,4})\b"
    r"|\b(\d{1,3})\s+(?:full[\s-]time\s+)?"
    r"(?:crews|trucks|technicians|installers|employees)\b", re.I)

# tel: links, read out of the RAW html rather than the visible text, so a
# number rendered as an image or an icon-only button still counts.
TEL_HREF_RE = re.compile(r'href=["\']\s*tel:([^"\']+)["\']', re.I)
DIGITS_RE = re.compile(r"\D+")

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
SERVICE_AREA_RE = re.compile(
    r"(service\s+areas?|areas?\s+we\s+serve|area\s+served|we\s+serve|"
    r"proudly\s+serv|serving\s+|communities\s+we|surrounding\s+"
    r"(area|communit|town|cit))", re.I)

# --- guards on the service-area reader -------------------------------------
# Being NEAR the words "service areas" is not the same as BEING in the list.
# Each of these three was a wrong fact sitting in the database, found by
# re-reading the stored source pages, and each would have been said out loud
# to the business it was wrong about.

# 1. The city is the business's OWN postal address. Ahlers Roofing prints
#    "Address: 2333 Minnis Drive Haltom City, Texas 76117" in a footer that
#    also carries a SERVICE AREAS nav — Haltom City is NOT in that nav. The
#    stored fact told a company headquartered in Haltom City that it had no
#    Haltom City page. A city followed by a state and a ZIP is an address.
ADDRESS_TAIL_RE = re.compile(r"^\s*,?\s*(tx|texas)\b\.?,?\s*\d{5}", re.I)
#    The street-number branch is deliberately anchored on a real street SUFFIX.
#    An earlier draft of it was just `\d{2,6}\s+[\w.]+{0,4}$`, which happily
#    matched the tail of "...972-332-1766 Service Areas " and would have thrown
#    away a CORRECT fact -- the same too-loose-regex mistake that once let
#    "slick-carousel@1.8.1" into this database as an email address.
ADDRESS_LEAD_RE = re.compile(
    r"(address|located at|visit us|headquarters|mailing)\s*:?\s*$"
    r"|\b\d{2,6}\s+[A-Za-z][\w.]*(\s+[A-Za-z][\w.]*){0,3}\s+"
    r"(st|street|ave|avenue|rd|road|dr|drive|blvd|boulevard|ln|lane|way|"
    r"pkwy|parkway|ct|court|hwy|highway|ste|suite|cir|circle|trl|trail)"
    r"\.?,?\s*$", re.I)

# 2. The city is only half of a METRO name. "Dallas-Fort Worth", "Dallas / Ft
#    Worth Metroplex" — naming the metroplex is not listing Fort Worth (or
#    Dallas) as a served city. Two stored facts came from this alone.
METRO_RE = re.compile(
    r"(dallas|dfw)\s*[-/–]?\s*(fort|ft\.?)\s*worth|"
    r"(fort|ft\.?)\s*worth\s*[-/–]\s*dallas|"
    r"d\.?f\.?w\.?\s*metroplex|the\s+metroplex", re.I)

# 3. The city is a LANDMARK, not a service area: "if you are in the DFW area
#    east of Dallas, chances are we can help" — the cities they actually cover
#    are listed immediately after, and Dallas is not among them.
LANDMARK_LEAD_RE = re.compile(
    r"\b(east|west|north|south|northeast|northwest|southeast|southwest|"
    r"outside|near|nearby|around|beyond|toward|towards|just)\s+(of\s+)?$", re.I)


def _is_real_service_area_mention(text, low, m):
    """True only if this occurrence of a city is genuinely a listed service area.

    `m` is a match of the city inside `text`. Returns False for the three
    confirmed false-positive shapes above. A False here means "not proven",
    which correctly costs us a fact rather than shipping a wrong one.
    """
    before = text[max(0, m.start() - 40): m.start()]
    after = text[m.end(): m.end() + 20]

    # own postal address
    if ADDRESS_TAIL_RE.match(after):
        return False
    if ADDRESS_LEAD_RE.search(before):
        return False

    # half of a metro name
    for mm in METRO_RE.finditer(text):
        if mm.start() <= m.start() and m.end() <= mm.end():
            return False

    # a landmark to navigate by
    if LANDMARK_LEAD_RE.search(before):
        return False

    # the phrase that makes it a service area must be CLOSE, not merely on the
    # page. 200 characters reached across a whole footer into an unrelated nav.
    window = low[max(0, m.start() - 120): m.end() + 120]
    return bool(SERVICE_AREA_RE.search(window))


# --------------------------------------------------------------- fetching ---
def fetch(url):
    """GET a page. Returns (final_url, html) or (None, None). Never raises."""
    if not url:
        return None, None
    if not url.startswith("http"):
        url = "https://" + url
    try:
        r = requests.get(url, headers=UA, timeout=FETCH_TIMEOUT, allow_redirects=True)
        ctype = r.headers.get("content-type", "")
        if r.ok and "html" in ctype.lower() and r.text:
            return r.url, r.text
    except Exception:
        pass
    return None, None


def visible_text(raw):
    """Strip a page down to the words a human would actually read."""
    if not raw:
        return ""
    raw = TAG_RE.sub(" ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html_mod.unescape(raw)
    return re.sub(r"\s+", " ", raw).strip()


def internal_links(raw, base):
    """Every same-host link on the page, as (path, anchor_text)."""
    if not raw:
        return []
    host = urlparse(base).netloc.replace("www.", "")
    out = []
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>',
                         raw, re.S | re.I):
        href, anchor = m.group(1).strip(), visible_text(m.group(2))
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base, href)
        if urlparse(full).netloc.replace("www.", "") != host:
            continue
        out.append((urlparse(full).path.lower(), anchor, full))
    return out


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ------------------------------------------------------------ the finders ---
# Each finder returns a dict {fact, source, evidence, kind} or None.
# `evidence` MUST be a substring of text that was actually fetched — that is
# what makes the claim checkable rather than generated.

def _ev(evidence, text):
    """Guard: only let a fact through if its evidence is really on the page."""
    return bool(evidence) and evidence.lower() in text.lower()


def find_stale_blog(pages, row):
    """Their blog exists and its newest dated post is old. Read off the page."""
    for url, raw in pages.items():
        path = urlparse(url).path.lower()
        if not re.search(r"/(blog|news|articles|insights)", path):
            continue
        text = visible_text(raw)
        clean = []
        for m in list(MONTH_RE.finditer(text)) + list(ISO_DATE_RE.finditer(text)):
            lit = m.group(0).strip()
            ym = re.search(r"(?:19|20)\d{2}", lit)
            if ym:
                clean.append((int(ym.group(0)), lit))
        if not clean:
            continue
        newest_year, newest_lit = max(clean, key=lambda t: t[0])
        if newest_year >= THIS_YEAR - 1 or newest_year < 2005:
            continue
        if not _ev(newest_lit, text):
            continue
        return {
            "kind": "stale_blog",
            "fact": f"The newest dated post on your blog is {newest_lit} — "
                    f"the page has been sitting untouched for "
                    f"{THIS_YEAR - newest_year}+ years.",
            "evidence": newest_lit,
            "source": url,
        }
    return None


def find_service_area_gap(pages, row, links):
    """They name a city on their own site but publish no page for it.

    Absence only means something on a site with a real navigation, so this
    refuses to fire on a site whose links we could barely read.
    """
    if len(links) < 8:
        return None
    home_url = next(iter(pages))
    text = visible_text(pages[home_url])
    low = text.lower()
    paths = " ".join(p for p, _a, _f in links)
    anchors = " ".join(a.lower() for _p, a, _f in links)
    own_city = (row.get("place_name") or "").strip()

    named = []
    for city in DFW_CITIES:
        if city.lower() == own_city.lower():
            continue
        # The city has to sit in an actual service-area sentence. A loose
        # "serv" test matched "Customer Service" in a testimonial byline and
        # turned a reviewer's home town into an invented service-area claim —
        # exactly the fabricated personalization this file exists to prevent.
        # _is_real_service_area_mention adds the three guards that a re-audit
        # of the stored facts proved were still missing: the business's own
        # postal address, a metro compound name, and a landmark reference.
        # \b on BOTH sides: without the leading one "Allen" matches inside
        # "McAllen", a different Texas city 500 miles away.
        for m in re.finditer(r"\b" + re.escape(city) + r"\b", text, re.I):
            if _is_real_service_area_mention(text, low, m):
                named.append((city, text[max(0, m.start() - 60): m.end() + 60].strip()))
                break

    # One stray hit proves nothing. A real service-area list names several
    # towns, and that is the only shape we are willing to read as a claim.
    if len(named) < 2:
        return None

    missing = [(c, ctx) for c, ctx in named
               if slug(c) not in paths and c.lower() not in anchors]
    if not missing:
        return None
    # One city, the first one they named, so the observation stays concrete.
    city, ctx = missing[0]
    if not _ev(city, text):
        return None
    extra = ""
    if len(missing) > 1:
        extra = f" ({len(missing)} of the cities you list have no page at all)"
    return {
        "kind": "service_area_gap",
        "fact": f"You list {city} as a service area on your homepage, but there "
                f"is no {city} page anywhere on the site{extra} — nothing for "
                f"someone in {city} to find.",
        "evidence": city,
        "source": home_url,
    }


def find_niche_service_gap(pages, row, links):
    """They advertise a specific system on the homepage with no page for it."""
    if len(links) < 8:
        return None
    home_url = next(iter(pages))
    text = visible_text(pages[home_url])
    paths = " ".join(p for p, _a, _f in links)
    anchors = " ".join(a.lower() for _p, a, _f in links)
    for svc in NICHE_SERVICES:
        if svc.lower() not in text.lower():
            continue
        if slug(svc) in paths or svc.lower() in anchors:
            continue
        # A term that appears once in passing is not a service they sell.
        n = len(re.findall(re.escape(svc), text, re.I))
        if n < 2:
            continue
        return {
            "kind": "service_gap",
            "fact": f'Your homepage mentions "{svc}" {n} times, but none of the '
                    f"{len(links)} links on it goes to a page about it.",
            "evidence": svc,
            "source": home_url,
        }
    return None


def find_self_claim(pages, row):
    """Something they publish about themselves: a founding year, a tenure
    claim, or a named certification. Verbatim or not at all.

    These stay pure observation. An earlier draft tacked on "that credential
    is not showing up in local search" and "none of it is written down
    anywhere Google can read" — neither was checked, and one of them was
    provably false for a prospect with a 169-page site. Say only what was read.
    """
    for url, raw in pages.items():
        text = visible_text(raw)
        for cert in CERTIFICATIONS:
            if cert.lower() in text.lower():
                if not _ev(cert, text):
                    continue
                return {
                    "kind": "certification",
                    "fact": f'Your site lists "{cert}" among your '
                            f"certifications.",
                    "evidence": cert,
                    "source": url,
                }
        m = SINCE_RE.search(text)
        if m:
            year = int(m.group(1))
            if 1900 < year <= THIS_YEAR:
                lit = m.group(0)
                if _ev(lit, text):
                    return {
                        "kind": "years",
                        "fact": f'Your site says "{lit}", which puts you '
                                f"{THIS_YEAR - year} years in.",
                        "evidence": lit,
                        "source": url,
                    }
        m = YEARS_RE.search(text)
        if m:
            lit = m.group(0).strip()
            if _ev(lit, text):
                return {
                    "kind": "years",
                    "fact": f'Your site says "{lit}".',
                    "evidence": lit,
                    "source": url,
                }
    return None


def find_insecure_site(pages, row):
    """Their site is served over plain http://. The browser says so out loud.

    Read off the FINAL url after redirects, so a site that quietly upgrades to
    https is not accused. Nothing is inferred: either the address bar shows
    http:// and the "Not secure" chip, or it does not.
    """
    home_url = next(iter(pages))
    if not home_url.lower().startswith("http://"):
        return None
    host = urlparse(home_url).netloc
    return {
        "kind": "insecure_site",
        "fact": f"Your site still loads over plain http:// — Chrome shows "
                f'"Not secure" in the address bar next to {host}.',
        "evidence": None,          # url-derived, not page-text-derived
        "source": home_url,
    }


def find_stale_copyright(pages, row):
    """The footer copyright year has stopped moving.

    Only the newest copyright year on the page is used, and only when it is at
    least two years behind — a site touched last December is not stale in
    January, and being wrong about that is worse than saying nothing.
    """
    for url, raw in pages.items():
        text = visible_text(raw)
        years = []
        for m in COPYRIGHT_RE.finditer(text):
            lit = m.group(0).strip()
            y = int(m.group(2) or m.group(1))
            if 1990 < y <= THIS_YEAR:
                years.append((y, lit))
        if not years:
            continue
        newest, lit = max(years, key=lambda t: t[0])
        if newest >= THIS_YEAR - 1:
            continue
        if not _ev(lit, text):
            continue
        return {
            "kind": "stale_copyright",
            "fact": f'The copyright line in your footer still reads "{lit}" — '
                    f"{THIS_YEAR - newest} years out of date.",
            "evidence": lit,
            "source": url,
        }
    return None


def find_family_owned(pages, row):
    """They say "family owned" on their own site. Quoted verbatim."""
    for url, raw in pages.items():
        text = visible_text(raw)
        m = FAMILY_RE.search(text)
        if not m:
            continue
        lit = m.group(0).strip()
        if not _ev(lit, text):
            continue
        year = m.group(1)
        if year and 1900 < int(year) <= THIS_YEAR:
            return {
                "kind": "family_owned",
                "fact": f'Your site says "{lit}", which puts you '
                        f"{THIS_YEAR - int(year)} years in.",
                "evidence": lit,
                "source": url,
            }
        # Deliberately neutral wording. Norman Roofing's page reads "a
        # family-owned atmosphere for our trade partners" — the phrase is
        # unarguably on the page, but "the business IS family owned" is a
        # reading, not a reading-off. Say only that the phrase is there.
        return {
            "kind": "family_owned",
            "fact": f'Your site uses the phrase "{lit}".',
            "evidence": lit,
            "source": url,
        }
    return None


def find_award(pages, row):
    """A named award or standing they publish. Literal substring, like certs."""
    for url, raw in pages.items():
        text = visible_text(raw)
        for award in AWARDS:
            m = re.search(re.escape(award), text, re.I)
            if m:
                # Quote the page's own casing: T-Rock prints "TOP RATED
                # LOCAL®", and a human scanning for the quoted string should
                # find exactly what we quoted.
                lit = m.group(0)
                return {
                    "kind": "award",
                    "fact": f'Your site mentions "{lit}".',
                    "evidence": lit,
                    "source": url,
                }
    return None


def find_volume_claim(pages, row):
    """A number they publish about their own output or crew."""
    for url, raw in pages.items():
        text = visible_text(raw)
        m = VOLUME_RE.search(text)
        if m:
            lit = re.sub(r"\s+", " ", m.group(0).strip())
            if _ev(lit, text):
                return {
                    "kind": "volume_claim",
                    "fact": f'Your site claims "{lit}".',
                    "evidence": lit,
                    "source": url,
                }
        m = CREW_RE.search(text)
        if m:
            lit = re.sub(r"\s+", " ", m.group(0).strip())
            if _ev(lit, text):
                return {
                    "kind": "crew_claim",
                    "fact": f'Your site says "{lit}".',
                    "evidence": lit,
                    "source": url,
                }
    return None


def find_offer(pages, row):
    """Financing, warranty or free-inspection language they already publish.

    Counted, because an offer that appears once in a footer and an offer the
    whole site is built around are different conversations.
    """
    for url, raw in pages.items():
        text = visible_text(raw)
        for offer in OFFERS:
            found = re.findall(re.escape(offer), text, re.I)
            if not found:
                continue
            offer, n = found[0], len(found)   # the page's own casing
            # A phrase that appears once, usually in a footer, is not an offer
            # the business is built around and makes a limp opening line. Same
            # threshold the niche-service finder already uses.
            if n < 2:
                continue
            return {
                "kind": "offer",
                "fact": f'Your page uses the phrase "{offer}" '
                        f"{n} time{'s' if n != 1 else ''}.",
                "evidence": offer,
                "source": url,
            }
    return None


def find_phone_mismatch(pages, row):
    """Two different phone numbers linked on the same page.

    Read from tel: hrefs only. A tel: link is unambiguously a phone number —
    a bare digit run in body text could be a licence number, a ZIP, or a year,
    and this file has been burned before by a pattern that was almost right.
    The claim made is only what is literally there: two different numbers.
    """
    home_url, home_raw = next(iter(pages.items()))
    seen = []
    for m in TEL_HREF_RE.finditer(home_raw):
        d = DIGITS_RE.sub("", m.group(1))
        if len(d) == 11 and d.startswith("1"):
            d = d[1:]
        if len(d) != 10:
            continue
        if d not in seen:
            seen.append(d)
    if len(seen) < 2:
        return None
    raw_digits = DIGITS_RE.sub("", home_raw)
    if not all(d in raw_digits for d in seen[:2]):
        return None
    shown = ["({}) {}-{}".format(d[:3], d[3:6], d[6:]) for d in seen[:2]]
    extra = f" (and {len(seen) - 2} more)" if len(seen) > 2 else ""
    return {
        "kind": "phone_mismatch",
        "fact": f"Your homepage links {len(seen)} different phone numbers — "
                f"{shown[0]} and {shown[1]}{extra}. Every one of them is a "
                f"separate number Google has to reconcile with your listing.",
        "evidence": None,          # html-derived, verified against raw digits
        "source": home_url,
    }


def find_duplicate_title(pages, row):
    """Two of their pages ship the identical <title>.

    Visible in the browser tab, so a human can confirm it by opening both
    URLs. Requires the title to be non-trivial so a one-word placeholder does
    not produce a fussy non-observation.
    """
    titles = {}
    for url, raw in pages.items():
        m = TITLE_RE.search(raw or "")
        if not m:
            continue
        t = re.sub(r"\s+", " ", html_mod.unescape(re.sub(r"<[^>]+>", " ",
                                                         m.group(1)))).strip()
        if len(t) < 12:
            continue
        titles.setdefault(t, []).append(url)
    for t, urls in titles.items():
        if len(urls) < 2:
            continue
        return {
            "kind": "duplicate_title",
            "fact": f'{len(urls)} of your pages share one browser-tab title, '
                    f'"{t}" — including {urls[0]} and {urls[1]}, which Google '
                    f"reads as the same page twice.",
            "evidence": None,      # html-derived, checked in the browser tab
            "source": urls[1],
        }
    return None


def find_review_standing(row):
    """Their Google review standing, from the count the audit pass stored.

    Stated as a BAND, never as an exact count, and this is not fussiness. The
    stored count is a snapshot: row 70 (Proficient Roofing) carries 29, and an
    independent check on 2026-08-27 found third-party directories reporting 30
    for the same listing. Review counts move. A fact that says "shows 29
    reviews" is therefore wrong the first week somebody leaves one, and the
    five-second test — open the source, see the claim — fails. "Fewer than 30"
    survives the drift it was written to survive.

    High counts are dropped entirely. Wing sells review generation; a roofer
    with 155 reviews is not a lead for it, so an exact-count claim there would
    carry all of the drift risk and none of the sales value.
    """
    n = row.get("gmb_reviews")
    if n is None:
        return None
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    name = (row.get("title") or "").strip()
    city = (row.get("place_name") or "").strip()
    if not name:
        return None
    src = ("https://www.google.com/maps/search/"
           + requests.utils.quote(f"{name} {city}".strip()))

    if n == 0:
        return {
            "kind": "reviews",
            "fact": "Your Google Business listing has no reviews on it at all.",
            "evidence": "0",
            "source": src,
        }
    for band in (10, 30):
        if n < band:
            return {
                "kind": "reviews",
                "fact": f"Your Google listing is still sitting under {band} "
                        f"reviews.",
                "evidence": str(n),
                "source": src,
            }
    return None


def find_bad_rank(row):
    """Their position for their own main term, only when it is genuinely poor.

    Read off the live results page by the audit pass; the source URL is the
    same search anyone can re-run.
    """
    rank = row.get("seo_rank")
    niche = (row.get("category") or "roofing").strip()
    city = (row.get("place_name") or "").strip()
    if not rank or not city or rank < 8:
        return None
    q = f"{niche} {city}"
    return {
        "kind": "rank",
        "fact": f'Your site comes up at position {rank} for "{q}".',
        "evidence": str(rank),
        "source": "https://duckduckgo.com/?q=" + requests.utils.quote(q),
    }


# ------------------------------------------------------------- the driver ---
CANDIDATE_PATHS = ["", "/about", "/about-us", "/services", "/blog", "/news",
                   "/service-areas", "/areas-we-serve"]


def gather(website, links_from_home=None):
    """Fetch the homepage plus a few of their own pages worth reading."""
    pages, links = {}, []
    home_url, home_raw = fetch(website)
    if not home_raw:
        return pages, links
    pages[home_url] = home_raw
    links = internal_links(home_raw, home_url)

    wanted = []
    for path, anchor, full in links:
        if re.search(r"/(about|blog|news|articles|service-area|areas-we-serve|"
                     r"locations?)(/|$)", path):
            if full not in pages and full not in wanted:
                wanted.append(full)
    for full in wanted[:4]:
        u, raw = fetch(full)
        if raw:
            pages[u] = raw
        time.sleep(0.4)
    return pages, links


# The wording of every fact is written here, in Python, by hand. An earlier
# version of this file could optionally hand each fact to a worker model to
# "tighten the wording"; that path is gone. It was the only LLM dependency in
# the whole lead pipeline outside intel_propose.py, it cost a call per lead at
# a table size of 1,224, and a rewriter that is allowed to touch a sentence is
# a rewriter that can drop a qualifier. There is no model in this module.


def finders_for(pages, row, links):
    """The ordered finder list. First hit wins, so the strongest fact leads.

    Order matters and is deliberate: the four original page-gap finders and
    the self-claim finder run first and unchanged, so nothing that used to be
    found changes shape. Everything after them only ever fires on a lead that
    would otherwise have been a NULL.
    """
    return [
        lambda: find_stale_blog(pages, row),
        lambda: find_service_area_gap(pages, row, links),
        lambda: find_niche_service_gap(pages, row, links),
        lambda: find_self_claim(pages, row),
        # --- added 2026-08-27, all deterministic, all NULL-fillers ---
        lambda: find_insecure_site(pages, row),
        lambda: find_stale_copyright(pages, row),
        lambda: find_family_owned(pages, row),
        lambda: find_award(pages, row),
        lambda: find_volume_claim(pages, row),
        lambda: find_phone_mismatch(pages, row),
        lambda: find_duplicate_title(pages, row),
        lambda: find_offer(pages, row),
        lambda: find_bad_rank(row),
        lambda: find_review_standing(row),
    ]


def _row_only_facts(row, why):
    """No page was read. The row itself may still carry numbers the audit pass
    stored, and restating a stored number is not inventing one. If it carries
    nothing either, NULL is the honest answer."""
    for f in (lambda: find_bad_rank(row), lambda: find_review_standing(row)):
        hit = f()
        if hit:
            return hit["fact"], hit["source"], hit["kind"], hit["evidence"]
    return None, None, why, None


def personalize(row):
    """Return (fact, source, kind, evidence); fact is None when nothing is grounded."""
    website = row.get("website")
    if not website:
        return _row_only_facts(row, "no website to read — needs manual research")

    pages, links = gather(website)
    if not pages:
        return _row_only_facts(
            row, "site could not be fetched — needs manual research")

    for f in finders_for(pages, row, links):
        try:
            hit = f()
        except Exception as e:
            print(f"      finder error: {str(e)[:70]}")
            continue
        if hit:
            return hit["fact"], hit["source"], hit["kind"], hit["evidence"]
    return (None, None,
            "nothing specific found on their site — needs manual research", None)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="research + print, write nothing")
    ap.add_argument("--limit", type=int, help="only this many prospects")
    ap.add_argument("--recheck", action="store_true",
                    help="redo rows that already have a personalization")
    ap.add_argument("--show-regexes", action="store_true",
                    help="print repr() of every pattern in this file, then exit")
    args = ap.parse_args()

    if args.show_regexes:
        for nm, val in sorted(globals().items()):
            if isinstance(val, re.Pattern):
                print(f"{nm:22} {val.pattern!r}")
        return

    env = load_env()
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json", "Prefer": "return=minimal"}

    params = {
        "identity": "eq.verified",
        "select": "id,title,place_name,category,website,seo_rank,has_blog,"
                  "has_service_pages,page_count,audit_gaps,gmb_rating,gmb_reviews",
        "order": "id.asc",
    }
    if not args.recheck:
        params["personalization"] = "is.null"
    if args.limit:
        params["limit"] = str(args.limit)

    r = sb_request("GET", f"{url}/rest/v1/candidates", headers=h, params=params)
    if r is None or not r.ok:
        sys.exit("Could not read verified prospects from Supabase.")
    rows = r.json()
    if not rows:
        print("Nothing to personalize.")
        return

    print(f"Reading {len(rows)} verified prospects' own websites.")
    print("A null is a real answer: it means that lead needs manual research.\n")

    found = 0
    for row in rows:
        name = row.get("title") or f"#{row['id']}"
        fact, source, kind, evidence = personalize(row)
        if fact:
            found += 1
            print(f"[{row['id']}] {name}  ({kind})")
            print(f"      {fact}")
            print(f"      source: {source}\n")
        else:
            print(f"[{row['id']}] {name}  -> NULL ({kind})\n")

        if args.dry_run:
            continue
        patch = {"personalization": fact, "personalization_source": source}
        sb_request("PATCH", f"{url}/rest/v1/candidates", headers=h,
                   params={"id": f"eq.{row['id']}"}, json=patch)

    nulls = len(rows) - found
    print(f"\n{found} of {len(rows)} got a real, sourced fact. {nulls} are NULL.")
    if nulls > found:
        print("More than half need manual research before they are worth emailing.")

    # A run that read N sites and grounded nothing is a broken run, not a quiet
    # one. Exiting 0 there lets a dead network or a changed page shape look like
    # "these leads just have nothing to say about them" forever.
    if rows and found == 0:
        print(f"\nFAIL: read {len(rows)} site(s) and grounded 0 facts. "
              f"That is a failure, not a result — check network and page fetching.")
        sys.exit(1)


if __name__ == "__main__":
    main()
