#!/usr/bin/env python3
"""
Deep-research a known list of businesses — social profiles + site + SEO + gaps.

Different entry point from social_discover.py: that one FINDS unknown prospects
by niche+city. This one takes businesses you already have (a call sheet, a CSV,
an export) and researches each BY NAME:

    social profiles  -> Instagram / TikTok / Facebook / LinkedIn company page
    their website    -> blog? service pages? how many pages? SSL? email?
    search position  -> where they rank for "{niche} {city}"
    reputation       -> rating, review count, complaint themes
    the pitch angle  -> need_score + a ranked gap list

Zero API cost: public search index + their own public website. No logins.

    python research_leads.py --in ../leads/callsheet30.json
    python research_leads.py --in x.json --workers 3 --out ../leads/researched.json
"""
import argparse
import json
import pathlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import audit_prospect as A

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Where a business's social presence lives, and how to recognise a real profile
# URL for each (as opposed to a post, a hashtag page, or platform chrome).
SOCIAL = {
    "instagram": (r"instagram\.com/([A-Za-z0-9._]{2,40})/?$", "site:instagram.com"),
    "tiktok":    (r"tiktok\.com/@([A-Za-z0-9._-]{2,40})",     "site:tiktok.com"),
    "facebook":  (r"facebook\.com/([A-Za-z0-9._-]{2,60})/?$", "site:facebook.com"),
    "linkedin":  (r"linkedin\.com/company/([A-Za-z0-9._-]{2,60})", "site:linkedin.com/company"),
}
SOCIAL_JUNK = ("/p/", "/reel/", "/explore", "/hashtag", "/posts", "/photos",
               "/videos", "/events", "/groups", "/pages", "/sharer", "/login",
               "/help", "/legal", "/privacy", "/directory", "/jobs")


def find_socials(name, city):
    """Locate this business's own profile on each platform, via the index."""
    out = {}
    for platform, (pattern, op) in SOCIAL.items():
        rx = re.compile(pattern)
        hit = None
        for r in A.search(f'{op} "{name}" {city}', 6):
            url = (r.get("href") or "").split("?")[0]
            if any(j in url for j in SOCIAL_JUNK):
                continue
            m = rx.search(url)
            if not m:
                continue
            handle = m.group(1)
            # Guard against grabbing an unrelated account that merely ranked:
            # require a distinctive word of the business name in the handle,
            # or the business name in the result title.
            toks = A._tokens(name)
            hn = A._norm(handle)
            title = A._norm(r.get("title") or "")
            if toks and not any(t in hn for t in toks):
                if not (A._norm(name)[:12] and A._norm(name)[:12] in title):
                    continue
            # Near-miss guard. "South Industrial Electric" matched a Facebook
            # page for "SOUTHERN Industrial Electric" — a different company.
            # Token containment cannot separate those, so anything short of a
            # close match is handed to a human instead of asserted as fact.
            nn = A._norm(name)
            ratio = (min(len(nn), len(hn)) / max(len(nn), len(hn), 1))
            confident = nn == hn or (nn in hn or hn in nn) and ratio >= 0.9
            hit = {"handle": handle, "url": url,
                   "confidence": "confirmed" if confident else "verify"}
            break
        if hit:
            out[platform] = hit
    return out


def research(lead):
    name, city, niche = lead["name"], lead["city"], lead["niche"]
    print(f"  researching {name} ({city})")

    socials = find_socials(name, city)
    site = A.crawl_site(lead["website"]) if lead.get("website") else {"website": None}
    rep = A.google_reputation(name, city)
    rank = A.seo_rank(name, niche, city, lead.get("website"))

    a = {**site, **rep, "seo_rank": rank}
    a["site_read"] = bool(site.get("reachable"))
    a["phone"] = lead.get("phone") or A.clean_phone(site.get("phone"))
    a["website"] = lead.get("website") or site.get("website")
    if not a.get("site_read"):
        for k in ("has_blog", "has_service_pages", "page_count", "ssl_ok"):
            a[k] = None
    need, gaps = A.need_score(a)

    # No social presence at all is itself a sellable gap for a local trade.
    if not socials:
        gaps.append("no social presence found")
    elif "instagram" not in socials and "facebook" not in socials:
        gaps.append("no Instagram or Facebook presence found")

    out = {**lead,
           "socials": socials,
           "social_count": len(socials),
           "seo_rank": rank,
           "has_blog": a.get("has_blog"),
           "has_service_pages": a.get("has_service_pages"),
           "page_count": a.get("page_count"),
           "ssl_ok": a.get("ssl_ok"),
           "email": a.get("email"),
           "site_read": a.get("site_read"),
           "gmb_rating": a.get("gmb_rating"),
           "gmb_reviews": a.get("gmb_reviews"),
           "bad_review_themes": a.get("bad_review_themes"),
           "need_score": need,
           "gaps": gaps}
    unsure = [p for p, v in socials.items() if v.get("confidence") == "verify"]
    if unsure:
        out["gaps"] = gaps + [f"CHECK: {', '.join(unsure)} profile may belong to a similarly named business"]
    print(f"      need {need} | rank {rank or '-'} | socials {sorted(socials) or 'none'}"
          f"{' (verify: ' + ','.join(unsure) + ')' if unsure else ''} | blog {a.get('has_blog')}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", default="")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    leads = json.loads(pathlib.Path(args.inp).read_text(encoding="utf-8"))
    if args.limit:
        leads = leads[:args.limit]
    out_path = pathlib.Path(args.out or (pathlib.Path(args.inp).with_name(
        pathlib.Path(args.inp).stem + "_researched.json")))

    print(f"Researching {len(leads)} leads (socials + site + SEO + reputation)\n")
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(research, l): l for l in leads}
        for f in as_completed(futures):
            try:
                results.append(f.result())
            except Exception as e:
                l = futures[f]
                print(f"      ERROR on {l['name']}: {str(e)[:100]}")
                results.append({**l, "error": str(e)[:200]})

    results.sort(key=lambda r: (r.get("need_score") or 0), reverse=True)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nWrote {len(results)} researched leads -> {out_path}")

    withsoc = sum(1 for r in results if r.get("social_count"))
    noblog = sum(1 for r in results if r.get("has_blog") is False)
    print(f"  {withsoc}/{len(results)} have a findable social profile")
    print(f"  {noblog}/{len(results)} have no blog")


if __name__ == "__main__":
    main()
