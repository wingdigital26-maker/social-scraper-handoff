#!/usr/bin/env python3
"""
Craigslist source -- read-only search of Craigslist's household/free and
gigs/labor sections, wired into watch_social.py's same geo + intent gates.

WHY THIS EXISTS
  Craigslist is a measured winner for junk removal (roughly 31 leads per run,
  far ahead of the whole social sweep combined). This module hits Craigslist's
  own search endpoint directly -- it does NOT go through the DDGS/duckduckgo
  index at all, so it is immune to the soft-block behavior watch_social.py's
  IndexHealth was built to catch.

READ ONLY. No login, no posting, no contacting anyone. Every result's href IS
the real Craigslist posting URL -- nothing here fabricates a link, a title,
or a date.

SECTIONS SEARCHED
  hsh  for-sale > household items (people wanting stuff hauled / sold cheap)
  zip  free stuff (the single richest junk-removal surface on Craigslist)
  lbg  gigs > labor (people asking for help hauling / moving / cleanout labor)

CITY -> SUBDOMAIN
  Craigslist is organized by metro, not by city, so a client's own configured
  scrape_cities has to resolve to a Craigslist metro subdomain
  (dallas.craigslist.org covers Dallas, Fort Worth, Plano, ... all at once).
  This table is general DFW geography, not any one client's config -- it
  never invents a city that was not already in that client's scrape_cities;
  a city with no entry here is simply not searchable on Craigslist and is
  skipped, never guessed at.
"""
from __future__ import annotations

import re
import time

import requests

UA = "Mozilla/5.0 (compatible; WingDigitalResearch/1.0; contact: wjackwing1@gmail.com)"
# Matches watch_social.SLEEP's pacing philosophy: this is a background job,
# not an interactive one, so it goes deliberately slow against one host.
SLEEP = 5.0

SECTIONS = {
    "household": "hsh",   # for-sale > household items
    "free":      "zip",   # free stuff
    "labor":     "lbg",   # gigs > labor
}

# General DFW metro geography. Not client-specific -- any client whose
# scrape_cities names one of these gets mapped to the metro that actually
# serves it on Craigslist.
_METRO_SUBDOMAIN = {
    "dallas": "dallas", "fort worth": "dallas", "arlington": "dallas",
    "plano": "dallas", "mckinney": "dallas", "allen": "dallas",
    "frisco": "dallas", "richardson": "dallas", "garland": "dallas",
    "irving": "dallas", "denton": "dallas", "grapevine": "dallas",
    "carrollton": "dallas", "lewisville": "dallas", "mesquite": "dallas",
    "waco": "waco", "austin": "austin", "houston": "houston",
    "san antonio": "sanantonio", "oklahoma city": "oklahomacity",
}


def subdomain_for_city(city: str) -> str:
    """Craigslist metro subdomain for `city`, or "" if unmapped."""
    key = " ".join((city or "").strip().lower().split())
    return _METRO_SUBDOMAIN.get(key, "")


def subdomains_for_cities(cities) -> set:
    """The set of Craigslist subdomains that count as 'in this client's
    metro', derived only from the client's own configured cities -- same
    pattern as watch_social.allowed_subreddits for Reddit."""
    return {s for s in (subdomain_for_city(c) for c in cities) if s}


def geo_gate(subdomain_queried: str, allowed_subdomains: set):
    """(ok, subdomain, reason).

    Craigslist listing permalinks come back as www.craigslist.org/view/d/...
    with NO subdomain in the URL itself (unlike Reddit, where the index can
    hand back a post from a subreddit the query never asked for) -- the
    metro scoping happens server-side on Craigslist's end, driven entirely by
    which {subdomain}.craigslist.org we queried. So the gate here checks the
    subdomain THIS CODE chose to query, not something parsed back out of the
    result -- and it is a hard backstop against a caller passing an
    unconfigured subdomain, not a defense against index leakage the way
    watch_social.geo_gate_reddit is.
    """
    sub = (subdomain_queried or "").lower()
    if not sub:
        return False, "", "no craigslist subdomain was queried"
    if sub not in allowed_subdomains:
        return False, sub, (
            f"craigslist subdomain {sub}.craigslist.org is not in this client's "
            f"configured metro (allowed: "
            f"{', '.join(sorted(allowed_subdomains)) or 'none configured'})"
        )
    return True, sub, ""


def _clean_phrase(phrase: str) -> str:
    """Strip the quoting watch_social's phrase vocabulary uses for DDGS
    (site: search needs `"exact phrase"`; Craigslist's own query box does not
    use quotes the same way and treats them literally as characters)."""
    return (phrase or "").strip().strip('"')


_RESULT_RE = re.compile(
    r'<li class="cl-static-search-result"[^>]*title="(?P<t1>[^"]*)"[^>]*>\s*'
    r'<a href="(?P<href>[^"]+)">\s*'
    r'<div class="title">(?P<t2>.*?)</div>.*?'
    r'<div class="location">\s*(?P<loc>[^<]*?)\s*</div>',
    re.S,
)


def _parse_html(html_text: str):
    """Craigslist's no-JS static search results page: real <li
    class="cl-static-search-result"> entries, each with a real permalink
    (the /view/d/... URL IS the posting) and the location text Craigslist
    itself attached to that specific listing. Nothing here is invented --
    listings not present in the page are simply not returned.

    Discovered live 2026-08-27: Craigslist's RSS endpoint (format=rss) 403s
    every request regardless of User-Agent, but this plain HTML search page
    answers 200 with real, parseable results. This is why the source scrapes
    HTML instead of using the documented RSS feed.
    """
    out = []
    if not html_text:
        return out
    for m in _RESULT_RE.finditer(html_text):
        href = m.group("href").strip()
        title = re.sub(r"\s+", " ", m.group("t2") or m.group("t1") or "").strip()
        loc = re.sub(r"\s+", " ", m.group("loc") or "").strip()
        if href:
            out.append({"href": href, "title": title, "body": loc})
    return out


def search(subdomain: str, section_key: str, phrase: str, limit: int = 20, tries: int = 2):
    """One Craigslist search. Returns (status, results):
      "ok"        results is a non-empty list of {"href","title","body"}
      "empty"     the search answered, nothing matched
      "throttled" Craigslist refused (403/429/503) after retries
      "error"     bad input, network failure, or unparseable response

    Same three-way status shape as watch_social.search() so results plug into
    the identical funnel/telemetry accounting. `body` carries the listing's
    own location text (e.g. "McKinney"), which is real signal the geo gate
    and relevance scorer can both use -- not filler.
    """
    cat = SECTIONS.get(section_key)
    query = _clean_phrase(phrase)
    if not cat or not subdomain or not query:
        return "error", []
    api_url = f"https://{subdomain}.craigslist.org/search/{cat}"
    params = {"query": query, "sort": "date"}
    delay = 3
    for attempt in range(tries):
        try:
            resp = requests.get(api_url, params=params, timeout=20,
                                headers={"User-Agent": UA})
            time.sleep(SLEEP)
            if resp.status_code in (403, 429, 503):
                if attempt < tries - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                return "throttled", []
            if not resp.ok:
                return "error", []
            items = _parse_html(resp.text)
            if not items:
                return "empty", []
            return "ok", items[:limit]
        except requests.RequestException:
            if attempt < tries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            return "throttled", []
    return "error", []
