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

    python personalize.py --dry-run --limit 12    # research + print, write nothing
    python personalize.py --limit 20              # research + store
    python personalize.py --recheck               # redo rows already looked at
"""
import argparse
import datetime
import html as html_mod
import json
import os
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
SERVICE_AREA_RE = re.compile(
    r"(service\s+areas?|areas?\s+we\s+serve|area\s+served|we\s+serve|"
    r"proudly\s+serv|serving\s+|communities\s+we|surrounding\s+"
    r"(area|communit|town|cit))", re.I)


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
        for m in re.finditer(re.escape(city), text, re.I):
            window = low[max(0, m.start() - 200): m.end() + 200]
            if SERVICE_AREA_RE.search(window):
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


def rephrase(fact, evidence):
    """Optional: let a FREE worker model tighten the wording.

    The model may only rewrite a fact that is already grounded. If its output
    drops the verbatim evidence, or drifts in length, the original wins. The
    model is never allowed to be the source of a fact.
    """
    router = os.environ.get("LLM_ROUTER_PATH", r"C:\Users\wjack\ghl-cli\llm_router.py")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("llm_router", router)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = mod.generate(
            "seo",
            "Rewrite this observation as one plain sentence a contractor would "
            "read without cringing. Keep every fact and keep the quoted text "
            f'"{evidence}" exactly as-is. No compliments, no adjectives, no '
            f"greeting. Output only the sentence.\n\n{fact}",
            temperature=0.3, max_tokens=120)
        text = (out or "").strip().strip('"')
        if text and evidence.lower() in text.lower() and 20 < len(text) < 320:
            return text
    except Exception as e:
        print(f"      rephrase skipped: {str(e)[:70]}")
    return fact


def personalize(row):
    """Return (fact, source, kind, evidence); fact is None when nothing is grounded."""
    website = row.get("website")
    if not website:
        return None, None, "no website to read — needs manual research", None

    pages, links = gather(website)
    if not pages:
        return None, None, "site could not be fetched — needs manual research", None

    finders = [
        lambda: find_stale_blog(pages, row),
        lambda: find_service_area_gap(pages, row, links),
        lambda: find_niche_service_gap(pages, row, links),
        lambda: find_self_claim(pages, row),
        lambda: find_bad_rank(row),
    ]
    for f in finders:
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
    ap.add_argument("--rephrase", action="store_true",
                    help="let the free worker model tighten wording (facts unchanged)")
    args = ap.parse_args()

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
        if fact and args.rephrase and evidence:
            fact = rephrase(fact, evidence)
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


if __name__ == "__main__":
    main()
