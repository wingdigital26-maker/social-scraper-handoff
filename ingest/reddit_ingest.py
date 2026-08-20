#!/usr/bin/env python3
"""
Prowl overnight spot builder — Reddit edition.

Pulls real posts from Reddit's OFFICIAL API (not scraping), extracts and
geocodes their locations, dedupes, classifies, and writes candidate spots to
a review-staging file. Each candidate carries the Reddit post as an embed, so
nothing is copied — we only ever link back to the original poster.

Flow:  reddit API -> extract place -> geocode -> region gate -> dedupe ->
       classify -> candidates.jsonl   (then a human reviews + promotes)

Secrets (never hardcoded) live in C:\\Users\\wjack\\ghl-cli\\.env :
    REDDIT_CLIENT_ID=...
    REDDIT_CLIENT_SECRET=...
See REDDIT-KEY-GUIDE.md for the 10-minute setup.

Usage:
    python reddit_ingest.py                 # full run, writes candidates.jsonl
    python reddit_ingest.py --limit 40      # stop after ~40 new candidates (test)
    python reddit_ingest.py --dry-run       # search + geocode, write nothing
"""
import os, sys, re, json, time, base64, argparse, pathlib

try:
    import requests
except ImportError:
    sys.exit("This needs the 'requests' package.  Run:  pip install requests")

import config as C

HERE = pathlib.Path(__file__).resolve().parent
ENV_PATH = pathlib.Path(r"C:\Users\wjack\ghl-cli\.env")


# ---------------------------------------------------------------- secrets ---
def load_env():
    """Read KEY=VALUE lines from the local .env (never from the synced vault)."""
    vals = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    # environment overrides file
    for k in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"):
        if os.environ.get(k):
            vals[k] = os.environ[k]
    return vals


# ------------------------------------------------------------- reddit api ---
def reddit_token(cid, secret):
    """App-only OAuth (client_credentials) — read access to public listings, no user login."""
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        headers={"Authorization": f"Basic {auth}", "User-Agent": C.USER_AGENT},
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    if r.status_code != 200:
        sys.exit(f"Reddit auth failed ({r.status_code}): {r.text[:200]}\n"
                 f"Check REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET in {ENV_PATH}")
    return r.json()["access_token"]


def reddit_search(token, sub, query, sort, limit):
    r = requests.get(
        f"https://oauth.reddit.com/r/{sub}/search",
        headers={"Authorization": f"Bearer {token}", "User-Agent": C.USER_AGENT},
        params={"q": query, "restrict_sr": 1, "sort": sort,
                "t": C.TIME_FILTER, "limit": limit, "type": "link"},
        timeout=30,
    )
    time.sleep(C.REDDIT_SLEEP)
    if r.status_code == 429:
        print("   rate-limited, backing off 30s"); time.sleep(30); return []
    if r.status_code != 200:
        print(f"   search {sub}/{query}/{sort} -> {r.status_code}"); return []
    return [c["data"] for c in r.json().get("data", {}).get("children", [])]


# --------------------------------------------------------- location logic ---
# A light gazetteer boosts matching for the big Texas metros; anything else
# falls through to Nominatim with the "Texas, USA" hint.
TX_CITIES = [
    "Dallas", "Fort Worth", "Arlington", "Plano", "Irving", "Garland", "Frisco",
    "McKinney", "Denton", "Mesquite", "Grand Prairie", "Carrollton", "Richardson",
    "Houston", "San Antonio", "Austin", "El Paso", "Waco", "Killeen", "Tyler",
    "Abilene", "Amarillo", "Lubbock", "Midland", "Odessa", "Beaumont", "Galveston",
    "Corpus Christi", "Wichita Falls", "San Angelo", "Mineral Wells", "Glen Rose",
    "Thurber", "Cedar Hill", "Copper Canyon", "Terrell", "Ennis", "Cleburne",
]
_CITY_RE = re.compile("|".join(rf"\b{re.escape(c)}\b" for c in TX_CITIES), re.I)
# "... in Waco, TX" / "near Denton Texas" style
_PLACE_RE = re.compile(r"\b(?:in|near|at|outside|around)\s+([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){0,3})", )


def extract_place(title, selftext):
    """Return (place_string, confidence 0-1) or (None, 0)."""
    text = f"{title}\n{selftext or ''}"
    m = _CITY_RE.search(text)
    if m:
        return m.group(0), 0.9                      # known city named outright
    m = _PLACE_RE.search(title)
    if m:
        cand = m.group(1).strip(" .")
        if len(cand) >= 3 and cand.lower() not in ("the", "this", "here", "a"):
            return cand, 0.5                         # "in <Somewhere>" phrasing
    if re.search(r"\b(TX|Texas)\b", text):
        return C.REGION_NAME, 0.2                    # only region-level signal
    return None, 0.0


_geo_cache = {}
def geocode(place):
    """Nominatim (OpenStreetMap) — free, no key. Returns (lat, lng) or None."""
    key = place.lower()
    if key in _geo_cache:
        return _geo_cache[key]
    q = place if place.lower().endswith(("texas", "usa")) else f"{place}, {C.GEOCODE_HINT}"
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            headers={"User-Agent": C.USER_AGENT},
            params={"q": q, "format": "json", "limit": 1, "countrycodes": "us"},
            timeout=30,
        )
        time.sleep(C.GEOCODE_SLEEP)
        arr = r.json() if r.status_code == 200 else []
        out = (float(arr[0]["lat"]), float(arr[0]["lon"])) if arr else None
    except Exception as e:
        print(f"   geocode error for {place!r}: {e}"); out = None
    _geo_cache[key] = out
    return out


def in_region(lat, lng):
    b = C.REGION_BBOX
    return b["south"] <= lat <= b["north"] and b["west"] <= lng <= b["east"]


# ------------------------------------------------- social link harvesting ---
# Find TikTok / Instagram links people shared in a Reddit post, and turn them
# into embeds. We only capture the public URL — the app renders it the
# sanctioned embed way. No scraping, no media download.
_TIKTOK_RE = re.compile(r"https?://(?:www\.|vm\.|m\.)?tiktok\.com/[^\s)\"'>]+", re.I)
_INSTA_RE  = re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/[A-Za-z0-9_\-]+", re.I)


def _clean_url(u):
    return u.split("?")[0].rstrip("/").rstrip(".,)")


def validate_tiktok(url):
    """Public oEmbed check — confirms the video is live and returns author. No key."""
    try:
        r = requests.get(C.TIKTOK_OEMBED, params={"url": url},
                         headers={"User-Agent": C.USER_AGENT}, timeout=20)
        time.sleep(C.SOCIAL_SLEEP)
        if r.status_code == 200 and r.json().get("html"):
            return r.json().get("author_name")
    except Exception:
        pass
    return None


def harvest_social(post):
    """Return a list of extra embed dicts from TikTok/IG links in the post."""
    if not C.HARVEST_SOCIAL:
        return []
    blob = " ".join(str(post.get(k, "")) for k in
                    ("url", "url_overridden_by_dest", "selftext", "title"))
    out, seen = [], set()
    for u in _TIKTOK_RE.findall(blob):
        u = _clean_url(u)
        if u in seen:
            continue
        seen.add(u)
        if C.VALIDATE_TIKTOK and validate_tiktok(u) is None:
            continue                       # dead/private link — skip
        out.append({"type": "tiktok", "url": u})
    for u in _INSTA_RE.findall(blob):
        u = _clean_url(u)
        if u in seen:
            continue
        seen.add(u)
        out.append({"type": "instagram", "url": u})   # client embed.js validates on render
    return out


# ---------------------------------------------------------- classify/build ---
def classify(title, selftext):
    text = f"{title} {selftext or ''}".lower()
    for cat, words in C.CATEGORY_KEYWORDS:
        if any(w in text for w in words):
            return cat
    return C.DEFAULT_CATEGORY


def norm_name(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def dedupe_key(name, lat, lng):
    """Same-ish name within ~1km rounds to one spot."""
    return (norm_name(name)[:24], round(lat, 2), round(lng, 2))


def build_candidate(post, place, conf, lat, lng, cat):
    permalink = "https://www.reddit.com" + post.get("permalink", "")
    title = post.get("title", "").strip()
    embeds = [{"type": "reddit", "url": permalink}] + harvest_social(post)
    embeds = embeds[:C.MAX_EMBEDS]
    social_n = sum(1 for e in embeds if e["type"] in ("tiktok", "instagram"))
    return {
        "source": "reddit",
        "reddit_id": post.get("id"),
        "name": title[:80],
        "cat": cat,
        "lat": round(lat, 5),
        "lng": round(lng, 5),
        "place": place,
        "location_confidence": conf,
        "upvotes": post.get("ups", 0),
        "subreddit": post.get("subreddit"),
        "author": post.get("author"),
        "embeds": embeds,
        "social_embeds": social_n,
        "desc": (post.get("selftext") or title)[:400],
        # Auto-collected -> always needs a human before going live.
        "needs_review": True,
        "legal_status": "unverified",
        "tags": ["auto-ingested", "reddit", f"loc-{'exact' if conf >= 0.9 else 'approx'}"]
               + (["has-video"] if social_n else []),
    }


# ------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="stop after N new candidates")
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    args = ap.parse_args()

    env = load_env()
    cid, secret = env.get("REDDIT_CLIENT_ID"), env.get("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit(f"Missing REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET in {ENV_PATH}\n"
                 f"See REDDIT-KEY-GUIDE.md — it's a 10-minute setup.")

    seen_path = HERE / C.SEEN_FILE
    seen = set(seen_path.read_text().split()) if seen_path.exists() else set()
    run_keys = set()

    print(f"Auth… region={C.REGION_NAME}  subs={len(C.SUBREDDITS)}  queries={len(C.QUERIES)}")
    token = reddit_token(cid, secret)

    cand_path = HERE / C.CANDIDATES_FILE
    out = None if args.dry_run else cand_path.open("a", encoding="utf-8")
    stats = dict(seen_posts=0, no_place=0, geo_fail=0, out_region=0, dup=0, kept=0)

    try:
        for sub in C.SUBREDDITS:
            for q in C.QUERIES:
                for sort in C.SORTS:
                    for post in reddit_search(token, sub, q, sort, C.PER_QUERY_LIMIT):
                        pid = post.get("id")
                        if not pid or pid in seen:
                            continue
                        seen.add(pid); stats["seen_posts"] += 1
                        title = post.get("title", "")
                        if len(title) < C.MIN_TITLE_LEN or post.get("ups", 0) < C.MIN_UPVOTES:
                            continue
                        if post.get("over_18"):
                            continue
                        place, conf = extract_place(title, post.get("selftext"))
                        if not place:
                            stats["no_place"] += 1; continue
                        coords = geocode(place)
                        if not coords:
                            stats["geo_fail"] += 1; continue
                        lat, lng = coords
                        if not in_region(lat, lng):
                            stats["out_region"] += 1; continue
                        cat = classify(title, post.get("selftext"))
                        k = dedupe_key(title, lat, lng)
                        if k in run_keys:
                            stats["dup"] += 1; continue
                        run_keys.add(k)
                        cand = build_candidate(post, place, conf, lat, lng, cat)
                        stats["kept"] += 1
                        vid = f"  +{cand['social_embeds']}📹" if cand["social_embeds"] else ""
                        print(f"  + [{cat}] {cand['name'][:50]}  ({place}, conf {conf}){vid}")
                        if out:
                            out.write(json.dumps(cand, ensure_ascii=False) + "\n"); out.flush()
                        if args.limit and stats["kept"] >= args.limit:
                            raise KeyboardInterrupt
    except KeyboardInterrupt:
        print("\nStopping early (limit reached or interrupted).")
    finally:
        if out:
            out.close()
        if not args.dry_run:
            seen_path.write_text("\n".join(sorted(seen)))

    print("\n=== run summary ===")
    for k, v in stats.items():
        print(f"  {k:12}: {v}")
    if not args.dry_run:
        print(f"\nCandidates appended to {cand_path}")
        print("Next: review them, then run  promote.py  to publish the good ones.")


if __name__ == "__main__":
    main()
