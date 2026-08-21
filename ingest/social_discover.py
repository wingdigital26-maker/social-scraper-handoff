#!/usr/bin/env python3
"""
Social prospect discovery — TikTok + Instagram + LinkedIn, ZERO API COST.

THE KEY IDEA: we do not scrape the platforms directly. TikTok, Instagram and
LinkedIn all block direct scraping hard (TikTok anti-bot, IG login wall,
LinkedIn bans + litigates). Instead we query the SEARCH INDEX for public
profiles and posts those platforms already let Google index. Same data,
no blocks, no login, no keys, no cost.

What it does:
    niche + city  ->  search index  ->  public profiles/posts on 3 platforms
                  ->  extract handle/name  ->  dedupe  ->  candidates.jsonl
                  ->  (enrich.py scores + drafts)  ->  (db.py -> queue)

HARD RULES (same as the rest of the pipeline):
  1. No login, no cookies, no credentials on any platform. Public index only.
  2. No media downloaded. We store public URLs only.
  3. Nothing auto-sends. Everything lands in the review queue for a human.
  4. LinkedIn: public profile URLs from the search index only. We never log in
     to LinkedIn and never bulk-scrape profile pages — that violates their
     User Agreement and gets accounts permanently banned.

Usage:
    python social_discover.py --niche roofing --city Dallas
    python social_discover.py --niche "warehouse" --city "Fort Worth" --platforms linkedin
    python social_discover.py --niche hvac --city Plano --limit 20 --dry-run
"""
import argparse
import json
import pathlib
import re
import sys
import time

try:
    from ddgs import DDGS
except ImportError:  # older package name
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        sys.exit("Needs the search client:  pip install ddgs")

HERE = pathlib.Path(__file__).resolve().parent
SEEN_FILE = HERE / "seen_social_profiles.txt"
OUT_FILE = HERE / "candidates.jsonl"

SLEEP = 2.0          # between searches, be polite to the index
MAX_PER_QUERY = 25

# Per-platform search recipes. Each yields public, indexed URLs.
PLATFORMS = {
    "tiktok": {
        "queries": [
            'site:tiktok.com "{niche}" {city}',
            'site:tiktok.com/@ {niche} {city}',
        ],
        "profile_re": re.compile(r"tiktok\.com/@([A-Za-z0-9._-]+)"),
    },
    "instagram": {
        "queries": [
            'site:instagram.com {niche} {city}',
            'site:instagram.com "{niche}" {city} contractor',
        ],
        "profile_re": re.compile(r"instagram\.com/([A-Za-z0-9._]+)/?$"),
    },
    "linkedin": {
        "queries": [
            'site:linkedin.com/in {niche} {city}',
            'site:linkedin.com/company {niche} {city}',
        ],
        "profile_re": re.compile(r"linkedin\.com/(?:in|company)/([A-Za-z0-9._-]+)"),
    },
}

# URL fragments that are never a prospect (platform chrome, help pages, etc.)
JUNK = ("/explore", "/directory", "/legal", "/help", "/about", "/privacy",
        "/tags/", "/pulse/", "/jobs/", "/p/", "/reel/", "/video/")


def search(query, limit):
    """Query the public search index. Returns [{title, href, body}]."""
    try:
        with DDGS() as d:
            return list(d.text(query, max_results=limit))
    except Exception as e:
        print(f"   search failed ({str(e)[:80]}) — skipping")
        return []


_BOILERPLATE = re.compile(
    r"\s*(?:[•·|\-]\s*)?(?:Instagram photos and videos?|Instagram|TikTok|LinkedIn)"
    r"(?:\s*photos and videos?)?\s*$", re.I)


def clean_name(title, platform):
    """Pull a human/business name out of the search result title."""
    t = (title or "").replace("�", " ")
    # Cut everything from the platform's own boilerplate onward, truncated or not
    # ("Infinite Roofing · Instagram photos and ..." -> "Infinite Roofing").
    t = re.split(r"\s*(?:Instagram photos|Instagram Photos|on TikTok|on Instagram|on LinkedIn)",
                 t)[0]
    t = re.split(r"\s*[|·—]\s*|\s+-\s+", t)[0].strip()
    t = re.sub(r"\s*\(@[^)]*\)?\s*$", " ", t)       # "Name (@handle)" / truncated "(@hand"
    for _ in range(3):                              # peel repeated platform tails
        new = _BOILERPLATE.sub("", t).strip(" •·-|")
        if new == t:
            break
        t = new
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t[:120] or None


_JUNK_TITLE = re.compile(r"^(link to |https?://|www\.)|^(instagram|tiktok|linkedin)\.?com?$", re.I)


def prettify_handle(handle):
    """roofingdallas -> Roofingdallas; new_view_roofing -> New View Roofing."""
    return " ".join(w.capitalize() for w in re.split(r"[._-]+", handle) if w) or handle


def best_name(title, handle, platform):
    n = clean_name(title, platform)
    if not n or _JUNK_TITLE.match(n) or len(n) < 3:
        return prettify_handle(handle)
    return n


def build(platform, handle, url, title, snippet, niche, city):
    is_person = "/in/" in url
    return {
        "source": platform,
        "id": f"{platform}:{handle}",
        "name": best_name(title, handle, platform),
        "title": best_name(title, handle, platform),
        "author": handle,
        "place": city,
        "category": niche,
        "cat": niche,
        "location_confidence": 0.6,          # city came from our query, not geocoding
        "lat": None, "lng": None,
        "upvotes": 0,                        # no public engagement number from the index
        "prospect_type": "person" if is_person else "business",
        "embeds": [{"type": platform, "url": url}],
        "desc": (snippet or "")[:400],
        "needs_review": True,
        "legal_status": "public-index",
        "tags": ["auto-discovered", platform, f"niche-{niche.replace(' ', '-')}",
                 f"city-{city.replace(' ', '-')}"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", required=True, help='e.g. roofing, hvac, "warehouse operations"')
    ap.add_argument("--city", required=True, help='e.g. Dallas, "Fort Worth"')
    ap.add_argument("--platforms", default="tiktok,instagram,linkedin")
    ap.add_argument("--limit", type=int, default=0, help="stop after N new prospects")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    wanted = [p.strip() for p in args.platforms.split(",") if p.strip() in PLATFORMS]
    if not wanted:
        sys.exit(f"No valid platforms. Choose from: {', '.join(PLATFORMS)}")

    seen = set(SEEN_FILE.read_text().split()) if SEEN_FILE.exists() else set()
    out = None if args.dry_run else OUT_FILE.open("a", encoding="utf-8")
    stats = dict(results=0, junk=0, dup=0, kept=0)

    print(f"Discovering '{args.niche}' in {args.city} across: {', '.join(wanted)}")
    print("(search-index only — no platform login, no scraping of walled pages)\n")

    try:
        for platform in wanted:
            spec = PLATFORMS[platform]
            for tmpl in spec["queries"]:
                q = tmpl.format(niche=args.niche, city=args.city)
                print(f"[{platform}] {q}")
                for r in search(q, MAX_PER_QUERY):
                    stats["results"] += 1
                    url = (r.get("href") or "").split("?")[0]
                    m = spec["profile_re"].search(url)
                    if not m or any(j in url for j in JUNK):
                        stats["junk"] += 1
                        continue
                    handle = m.group(1)
                    key = f"{platform}:{handle}"
                    if key in seen:
                        stats["dup"] += 1
                        continue
                    seen.add(key)
                    cand = build(platform, handle, url, r.get("title", ""),
                                 r.get("body", ""), args.niche, args.city)
                    stats["kept"] += 1
                    print(f"  + [{platform}] {cand['name'][:45]}  @{handle}")
                    if out:
                        out.write(json.dumps(cand, ensure_ascii=False) + "\n")
                        out.flush()
                    if args.limit and stats["kept"] >= args.limit:
                        raise KeyboardInterrupt
                time.sleep(SLEEP)
    except KeyboardInterrupt:
        print("\nStopping early (limit reached or interrupted).")
    finally:
        if out:
            out.close()
        if not args.dry_run:
            SEEN_FILE.write_text("\n".join(sorted(seen)))

    print("\n=== run summary ===")
    for k, v in stats.items():
        print(f"  {k:10}: {v}")
    if stats["kept"] and not args.dry_run:
        print(f"\nAppended to {OUT_FILE}")
        print("Next:  python enrich.py  &&  python db.py --source social")


if __name__ == "__main__":
    main()
