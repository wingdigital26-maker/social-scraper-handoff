#!/usr/bin/env python3
"""
Recover real businesses from the 'unresolved' pool left by identity_gate.py.

WHY THIS EXISTS. identity_gate resolves a candidate against ONE source: the
cached Google Maps scrape. Maps fast-mode returns only ~16-20 listings per city
(715 across 45 cities), so a business that Maps simply did not return is filed
'unresolved' — held back, not deleted, because absence from a partial scrape is
weak evidence. 305 rows sit there. "Frisco Roofing LLC" has a DFW phone and a
matching domain and is obviously real; it is stuck purely because Maps missed
it.

This adds a SECOND, independent identity source and promotes only on positive
evidence.

THE SOURCE THAT WON: the business's own website.
    A business that (a) owns a domain that is demonstrably its own name and
    (b) publishes a DFW-area-code phone number on that site is a real business
    operating in DFW. Both halves are needed. Domain ownership alone proves
    nothing about location (ameliaislandroofing.com is Florida,
    millenniumconstructiontn.com is Tennessee — both correctly stay held). A
    DFW phone on a site you cannot prove they own is just a phone number on
    somebody's page.

WHAT WAS TESTED AND REJECTED:
  * OpenStreetMap / Overpass. Every public Overpass endpoint was 429/500/504
    during testing, and OSM barely maps roofing contractors at all (they are
    service businesses, not storefronts). Nominatim free-text is worse than
    useless here: querying "Frisco Roofing LLC" confidently returns "Elevated
    Roofing, LLC" — a DIFFERENT company. That is precisely the confidently-wrong
    failure the gate exists to prevent.
  * A deeper / name-targeted Maps pass. Google Maps does not honour a
    business-name query in fast mode; querying five specific Dallas roofers by
    name returned fourteen generic nearby listings and none of the five. A full
    extra scrape produced 11 listings not already cached and resolved 0 of the
    305. The corpus would need hundreds of runs to move the number.

WHY THE OWNERSHIP CHECK IS STRICTER HERE THAN audit_prospect._owns_site.
    _owns_site accepts a page whose <title> merely contains every distinctive
    word of the name. On this data that promoted "McKinney & Sons Roofing and
    Construction" to www.mckinneytexas.org — the CITY OF MCKINNEY's official
    site, which naturally carries "McKinney" in its title and a 972 number.
    That single row would have been a false positive shipped into a sales call.
    So this file requires EITHER the registrable domain to be the business name
    (the strong branch of _owns_site), OR the page <title> to contain the whole
    normalized business name, and rejects government / directory / social hosts
    outright.

Anything not proven stays 'unresolved'. Nothing is ever demoted or deleted —
this file only ever moves rows unresolved -> verified.

    python recover_unresolved.py --dry-run --limit 40
    python recover_unresolved.py --dry-run --find-sites --limit 20
    python recover_unresolved.py --limit 50
"""
import argparse
import pathlib
import re
import sys
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import audit_prospect as A
from db import load_env

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"}
FETCH_TIMEOUT = 15
CONTACT_PATHS = ("", "/contact", "/contact-us")

# Hosts that can carry a business's name and a local phone without being that
# business: city halls, chambers, directories, social profiles, review sites.
# mckinneytexas.org is in here for a reason — see the module docstring.
BAD_HOST = re.compile(
    r"(^|\.)(gov|mil)$|"
    r"(cityof|texas\.org$|tx\.us$)|"
    r"(yelp|bbb\.org|angi|angieslist|homeadvisor|thumbtack|houzz|porch|"
    r"nextdoor|manta|yellowpages|yellowbook|superpages|facebook|instagram|"
    r"tiktok|linkedin|twitter|x\.com|youtube|pinterest|indeed|glassdoor|"
    r"chamberofcommerce|networx|buildzoom|expertise|birdeye|nicelocal|"
    r"mapquest|foursquare|alignable|bark\.com|wikipedia)", re.I)

BAD_TITLE = re.compile(
    r"(official website|city of|town of|chamber of commerce|"
    r"municipal|\bcity hall\b|find a contractor|top \d+ |best \d+ |"
    r"directory|near you|reviews of )", re.I)

# Reused from the gate so a doorway shell cannot be laundered back in.
DOORWAY = re.compile(r"(roofingpro|roofing-pro|prosroofing|roofers?near|"
                     r"bestroofers?|top\d+roof)", re.I)

PHONE_RE = re.compile(r"\(?(\d{3})\)?[\s.‑–-]?(\d{3})[\s.‑–-]?(\d{4})")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

# A 200 response is not the same as a readable page. Three shapes turned up on
# real rows and every one of them was being reported as "owned but shows no
# DFW-area-code phone" -- a sentence that claims we READ the business's site and
# it lacked a phone. We had read nothing:
#   * banner-roofing.com returned a 521-byte sgcaptcha challenge (WE were blocked)
#   * lumenroofing.com and ironsummitroofing.com returned a 342-byte JS redirect
#     to "/lander" -- parked domains, not the business's site at all
# Calling any of those "no phone on their site" is exactly the unknown-reported-
# as-a-finding failure this project keeps having. They are UNREADABLE, and that
# is a different answer that a human can act on.
CAPTCHA_RE = re.compile(r"sgcaptcha|captcha|cf-browser-verification|"
                        r"just a moment|attention required|access denied|"
                        r"enable javascript and cookies", re.I)
PARKED_RE = re.compile(r'location\.href\s*=\s*["\']/?lander|'
                       r"(domain (is )?(for sale|parked))|parkingcrew|sedoparking|"
                       r"afternic|bodis\.com", re.I)
MIN_READABLE_TEXT = 200      # characters of visible text


def readable(html):
    """(ok, reason). Is this actually the business's page, or a wall/stub?

    ORDER MATTERS. An earlier version tested the captcha signature first and
    rejected nemaroofing.com -- a 433KB page full of real content that simply
    embeds reCAPTCHA on its contact form. Matching the bare word "captcha"
    anywhere in a document is the same too-loose-substring mistake as the email
    regex that once matched "slick-carousel@1.8.1". So: if we actually got a
    page's worth of text, we READ it, whatever widgets it happens to load. Only
    a page with no text to speak of gets diagnosed as parked or walled.
    """
    if not html:
        return False, "unreachable"
    if len(strip_html(html).strip()) >= MIN_READABLE_TEXT:
        return True, None
    if PARKED_RE.search(html):
        return False, "parked domain / for-sale lander, not their site"
    if CAPTCHA_RE.search(html):
        return False, "blocked by a bot wall (WE were blocked -- not a finding)"
    return False, "returned almost no readable text (stub/JS-only page)"


def strip_html(html):
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html))


def host_of(website):
    if "//" not in (website or ""):
        website = "https://" + (website or "")
    return urlparse(website).netloc.lower().strip()


def proves_ownership(name, host, html):
    """Stricter than audit_prospect._owns_site. Returns (bool, how).

    Two acceptable proofs, in order of strength:
      domain  the registrable domain IS the business name (_owns_site's own
              strong branch, evaluated with html=None so the weak title
              fallback cannot fire).
      title   the page's <title> contains the WHOLE normalized business name.
              Requiring the whole name — not merely every distinctive token —
              is what stops the City of McKinney from claiming McKinney & Sons.
    """
    if not host or BAD_HOST.search(host) or DOORWAY.search(host):
        return False, None
    if A._owns_site(name, host, None):
        return True, "domain"
    if not html:
        return False, None
    m = TITLE_RE.search(html)
    if not m:
        return False, None
    raw_title = m.group(1)
    if BAD_TITLE.search(raw_title):
        return False, None
    n, t = A._norm(name), A._norm(raw_title)
    # Short normalized names ("mroofing") sit inside too many strings to be
    # evidence on their own.
    if len(n) >= 8 and n and n in t:
        return True, "title"
    return False, None


def fetch_site(host):
    """Homepage + contact page. Returns (combined_html, [paths_fetched])."""
    html, got = "", []
    for scheme in ("https", "http"):
        for path in CONTACT_PATHS:
            try:
                r = requests.get(f"{scheme}://{host}{path}", headers=UA,
                                 timeout=FETCH_TIMEOUT, allow_redirects=True)
            except Exception:
                continue
            if r.status_code >= 400 or not r.text:
                continue
            html += r.text
            got.append(path or "/")
        if html:
            break
    return html, got


def dfw_phones(text):
    """DFW-area-code numbers printed on the page.

    NANP area codes are geographically assigned and a business publishes its
    own reachable number, so this is locality evidence you can act on. Toll-free
    is deliberately NOT accepted here: an 800 number proves nothing about where
    the company is, and locality is the whole question.
    """
    out = []
    for m in PHONE_RE.finditer(text):
        if m.group(1) in A.DFW_AREA_CODES and m.group(2) != "000":
            out.append(f"({m.group(1)}) {m.group(2)}-{m.group(3)}")
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def recover(row, find_sites=False):
    """Return (promote_bool, reason, extra_patch, debug_note)."""
    name = (row.get("title") or "").strip()
    website = row.get("website") or ""

    if not website and find_sites:
        # find_website already refuses person profiles and applies its own
        # ownership check; whatever it returns is re-checked below anyway.
        website = A.find_website(name, row.get("place_name") or "DFW") or ""
        if website:
            row["_found_site"] = website

    if not website:
        return False, None, {}, "no website to verify"

    host = host_of(website)
    if not host or BAD_HOST.search(host) or DOORWAY.search(host):
        return False, None, {}, f"host {host} is a directory/social/gov host"

    html, paths = fetch_site(host)
    if not html:
        return False, None, {}, f"{host} unreachable"

    # Do not derive any finding about the business from a page we did not read.
    ok, why = readable(html)
    if not ok:
        return False, None, {}, f"{host} {why}"

    owned, how = proves_ownership(name, host, html)
    if not owned:
        return False, None, {}, f"{host} not provably owned by '{name}'"

    phones = dfw_phones(strip_html(html))
    if not phones:
        return False, None, {}, f"{host} owned but shows no DFW-area-code phone"

    reason = (f"verified by own website: {host} is provably this business "
              f"({how} match) and publishes DFW phone(s) {', '.join(phones[:3])} "
              f"[source: business website, pages {','.join(paths)}]")
    patch = {"website": f"https://{host}"}
    # Only FILL a missing phone, never overwrite one. A page can carry several
    # DFW numbers (Zenith Roofing publishes three) and the first one scraped is
    # not necessarily the main line — good enough as locality evidence, not good
    # enough to replace a number someone may already have dialled.
    if not (row.get("phone") or "").strip():
        patch["phone"] = A.clean_phone(phones[0])
    return True, reason, patch, f"PROMOTE via {how}: {phones[:2]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--find-sites", action="store_true",
                    help="for rows with no website, search for one first "
                         "(slow, DuckDuckGo rate-limited)")
    args = ap.parse_args()

    env = load_env()
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json", "Prefer": "return=minimal"}

    params = {"select": "id,title,place_name,category,source,url,website,phone",
              "identity": "eq.unresolved", "order": "id.asc"}
    if not args.find_sites:
        # Rows with no website have nothing for this source to check.
        params["website"] = "not.is.null"
    if args.limit:
        params["limit"] = str(args.limit)

    r = A.sb_request("GET", f"{url}/rest/v1/candidates", headers=h, params=params)
    if r is None or not r.ok:
        sys.exit("could not read candidates")
    rows = r.json()
    if not rows:
        print("Nothing to recover.")
        return

    print(f"Checking {len(rows)} unresolved rows against their own websites"
          f"{' (DRY RUN)' if args.dry_run else ''}\n")

    promoted, held, unreachable = 0, 0, 0
    for row in rows:
        ok, reason, patch, note = recover(row, find_sites=args.find_sites)
        if note.endswith("unreachable"):
            unreachable += 1
        tag = "PROMOTE" if ok else "hold   "
        print(f"[{tag}] {row['id']:>5}  {row['title'][:44]:<44} {note}")
        if not ok:
            held += 1
            continue
        promoted += 1
        print(f"          evidence: {reason}")
        if not args.dry_run:
            patch.update({"identity": "verified", "identity_reason": reason,
                          "identity_checked_at": "now()"})
            A.sb_request("PATCH", f"{url}/rest/v1/candidates", headers=h,
                         params={"id": f"eq.{row['id']}"}, json=patch)

    print(f"\n=== recovery ===\n  promoted to verified {promoted}\n"
          f"  still unresolved     {held}")
    print("\nPromotion needs BOTH proven domain ownership AND a DFW-area-code "
          "phone on the page. Unproven rows stay unresolved — nothing deleted, "
          "nothing demoted.")

    # Promoting nothing is a NORMAL, correct outcome here -- most unresolved rows
    # genuinely cannot be proven, and holding them is the point of the file. So
    # zero promotions is not a failure. Every site being unreachable IS: that is
    # this machine's network, and it would otherwise look like 50 businesses all
    # failing the evidence test.
    if rows and unreachable == len(rows):
        print(f"\nFAIL: all {len(rows)} sites were unreachable — that is a local "
              f"network/DNS failure, not evidence about these businesses.")
        sys.exit(1)


if __name__ == "__main__":
    main()
