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
ENV_PATH = pathlib.Path(os.environ.get("ENV_FILE", HERE.parent / ".env"))

# Emoji / em-dashes in the run log must never be the thing that kills a run
# on a cp1252 Windows console.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ------------------------------------------------------- failure vocabulary ---
# House rule: zero yield is a HARD FAILURE. But "nothing came back" has three
# genuinely different causes and they must never share an exit code, because
# only one of them is fixable by Jack and only one of them is normal.
#
#   EXIT_CONFIG  (2) a credential/config is missing. Nothing was even attempted.
#   EXIT_BLOCKED (3) the platform answered, and its answer was "no". Auth
#                    rejection, rate limit, TLS reset, or HTTP 200 + empty page
#                    from a datacenter IP. NOT a data problem.
#   EXIT_ZERO    (4) the platform genuinely answered with real results, and
#                    after filtering none of them were new/usable.
#
# Exit 0 now means, and only means, "rows were produced".
EXIT_OK, EXIT_CONFIG, EXIT_BLOCKED, EXIT_ZERO = 0, 2, 3, 4


def _banner(kind, lines):
    bar = "=" * 72
    print(f"\n{bar}\n{kind}\n{bar}")
    for ln in lines:
        print(ln)
    print(bar)


def fail_config(what, how):
    """A missing credential is a CONFIGURATION problem. It must never be
    allowed to look like 'no data found'."""
    _banner("FAIL: MISSING CONFIGURATION - nothing was attempted",
            [f"Missing: {what}", "", "How to fix:", how])
    sys.exit(EXIT_CONFIG)


def fail_blocked(platform, evidence):
    _banner(f"FAIL: {platform.upper()} IS REFUSING THIS HOST - not a data shortage",
            [evidence, "",
             "Zero rows here means the source said no, NOT that the region is quiet.",
             "Nothing was written. No rows were invented."])
    sys.exit(EXIT_BLOCKED)


def fail_zero(platform, evidence):
    _banner(f"FAIL: {platform.upper()} ANSWERED BUT YIELDED ZERO NEW ROWS",
            [evidence, "",
             "The source responded normally, so this is a genuine empty harvest",
             "(everything already seen, or filtered out) - not a block.",
             "Still a hard failure: a nightly lane that writes nothing is broken."])
    sys.exit(EXIT_ZERO)


class SourceHealth:
    """Is the platform actually answering us?

    Modeled on watch_social.py's IndexHealth (read, deliberately not imported -
    that module owns its own copy). The failure this exists to catch: a backend
    that returns HTTP 200 with an empty body to datacenter IPs. Measured on this
    project: ~3.1 results/query from Jack's machine vs 0.13 from a GitHub hosted
    runner, with no error on either. Locally that is indistinguishable from
    "nothing new this week" unless something counts it.
    """

    def __init__(self, platform):
        self.platform = platform
        self.ok = 0            # answered with >=1 result
        self.empty = 0         # answered 200, zero results
        self.blocked = 0       # explicit refusal: 401/403/429/TLS reset
        self.errors = 0        # anything else that went wrong on the wire
        self.notes = []

    def note(self, status, detail=""):
        setattr(self, status, getattr(self, status) + 1)
        if detail and status != "ok" and len(self.notes) < 8:
            self.notes.append(f"  - {detail}")

    @property
    def attempts(self):
        return self.ok + self.empty + self.blocked + self.errors

    def summary(self):
        return (f"{self.platform}: {self.attempts} requests -> "
                f"{self.ok} answered, {self.empty} empty, "
                f"{self.blocked} refused, {self.errors} errored")

    def verdict(self):
        """'ok' | 'blocked' | 'empty'  — only meaningful when no rows were kept."""
        if self.attempts == 0:
            return "blocked"
        if self.blocked or self.errors >= max(2, self.attempts // 2):
            return "blocked"
        if self.ok == 0:
            # Every single request came back 200-and-empty. A real region is
            # never that consistently silent; this is the soft-block signature.
            return "blocked"
        return "empty"

    def detail(self):
        return "\n".join([self.summary()] + self.notes)


# ---------------------------------------------------------------- secrets ---
REDDIT_KEY_HOWTO = """\
  1. Sign in to Reddit, then open:  https://www.reddit.com/prefs/apps
  2. Scroll to the bottom, click "are you a developer? create an app...".
  3. Fill in exactly:
       name          prowl-spot-ingest
       type          select the "script" radio button   <- NOT web app
       redirect uri  http://localhost:8080
     (description and about url can be left blank)
  4. Click "create app".
  5. On the resulting card, two strings are what you need:
       CLIENT_ID     the short string directly UNDER the app name,
                     just below the words "personal use script"
       CLIENT_SECRET the string labelled "secret"
  6. Store them. Either add these two lines to C:\\Users\\wjack\\ghl-cli\\.env :
       REDDIT_CLIENT_ID=<the short one>
       REDDIT_CLIENT_SECRET=<the secret>
     ...and/or, for the cloud lane, from the repo root run:
       gh secret set REDDIT_CLIENT_ID
       gh secret set REDDIT_CLIENT_SECRET
  7. Verify locally:
       cd ingest && python reddit_ingest.py --dry-run --limit 5
     Success looks like lines starting with "  + [".
  Cost: free. Time: about 10 minutes. No card, no approval wait, no review."""


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
    if r.status_code in (401, 403):
        fail_config(
            "valid REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET (the pair present is "
            f"being REJECTED by Reddit: HTTP {r.status_code} {r.text[:120]})",
            REDDIT_KEY_HOWTO)
    if r.status_code != 200:
        fail_blocked("reddit",
                     f"OAuth token request returned HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()["access_token"]
    except (ValueError, KeyError):
        fail_blocked("reddit",
                     f"OAuth endpoint returned 200 but no access_token: {r.text[:200]}")


def reddit_search(token, sub, query, sort, limit, health=None):
    tag = f"{sub}/{query}/{sort}"
    try:
        r = requests.get(
            f"https://oauth.reddit.com/r/{sub}/search",
            headers={"Authorization": f"Bearer {token}", "User-Agent": C.USER_AGENT},
            params={"q": query, "restrict_sr": 1, "sort": sort,
                    "t": C.TIME_FILTER, "limit": limit, "type": "link"},
            timeout=30,
        )
    except requests.RequestException as e:
        # Transport-level failure (TLS reset, DNS, timeout). This is the shape a
        # network-layer block takes, so it is recorded, never swallowed.
        print(f"   search {tag} -> transport error: {e}")
        if health:
            health.note("blocked", f"{tag}: transport error {type(e).__name__}: {e}")
        return []
    time.sleep(C.REDDIT_SLEEP)
    if r.status_code == 429:
        print("   rate-limited, backing off 30s")
        if health:
            health.note("blocked", f"{tag}: HTTP 429 rate limited")
        time.sleep(30)
        return []
    if r.status_code in (401, 403):
        if health:
            health.note("blocked", f"{tag}: HTTP {r.status_code} (token rejected)")
        print(f"   search {tag} -> {r.status_code}")
        return []
    if r.status_code != 200:
        print(f"   search {tag} -> {r.status_code}")
        if health:
            health.note("errors", f"{tag}: HTTP {r.status_code}")
        return []
    try:
        rows = [c["data"] for c in r.json().get("data", {}).get("children", [])]
    except ValueError:
        if health:
            health.note("errors", f"{tag}: 200 but body was not JSON")
        return []
    if health:
        # HTTP 200 with an empty listing is the datacenter soft-block signature.
        health.note("ok" if rows else "empty",
                    "" if rows else f"{tag}: HTTP 200 with zero results")
    return rows


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
# Nominatim is a second, independent chokepoint: it rate-limits and blocks
# datacenter IPs too. If it goes down, EVERY candidate dies at geo_fail and the
# run looks like "the region was quiet". GEO_HEALTH makes that visible.
GEO_HEALTH = SourceHealth("nominatim")


def geocode(place):
    """Nominatim (OpenStreetMap) — free, no key. Returns (lat, lng) or None.

    Note: a cached miss is NOT re-counted in GEO_HEALTH, so the health numbers
    stay a record of real requests rather than of repeated place strings.
    """
    key = place.lower()
    if key in _geo_cache:
        return _geo_cache[key]
    q = place if place.lower().endswith(("texas", "usa")) else f"{place}, {C.GEOCODE_HINT}"
    out = None
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            headers={"User-Agent": C.USER_AGENT},
            params={"q": q, "format": "json", "limit": 1, "countrycodes": "us"},
            timeout=30,
        )
        time.sleep(C.GEOCODE_SLEEP)
        if r.status_code in (403, 429):
            GEO_HEALTH.note("blocked", f"{place!r}: HTTP {r.status_code} from Nominatim")
            print(f"   geocode BLOCKED for {place!r}: HTTP {r.status_code}")
        elif r.status_code != 200:
            GEO_HEALTH.note("errors", f"{place!r}: HTTP {r.status_code}")
        else:
            arr = r.json()
            if arr:
                out = (float(arr[0]["lat"]), float(arr[0]["lon"]))
                GEO_HEALTH.note("ok")
            else:
                GEO_HEALTH.note("empty", f"{place!r}: no match")
    except requests.RequestException as e:
        GEO_HEALTH.note("blocked", f"{place!r}: transport error {type(e).__name__}: {e}")
        print(f"   geocode transport error for {place!r}: {e}")
    except (ValueError, KeyError, IndexError, TypeError) as e:
        GEO_HEALTH.note("errors", f"{place!r}: bad response shape {e}")
        print(f"   geocode bad response for {place!r}: {e}")
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


OEMBED_HEALTH = SourceHealth("tiktok-oembed")


def validate_tiktok(url):
    """Public oEmbed check. Returns (status, author_name).

    status is one of:
        "live"    oEmbed answered and the video is up  (author_name set)
        "dead"    oEmbed answered and said no such video / not embeddable
        "blocked" we never got an answer: TLS reset, timeout, 403, 429

    This used to be `except Exception: pass; return None`, which collapsed
    "blocked" into "dead". TikTok is currently blocked at the transport layer
    (ConnectionResetError 10054, measured 2026-08-26), so that bare except was
    silently deleting every single TikTok embed harvested off Reddit and
    reporting it as "the links were dead". Three states, three answers.
    """
    try:
        r = requests.get(C.TIKTOK_OEMBED, params={"url": url},
                         headers={"User-Agent": C.USER_AGENT}, timeout=20)
    except requests.RequestException as e:
        OEMBED_HEALTH.note("blocked", f"transport error {type(e).__name__}: {e}")
        return "blocked", None
    time.sleep(C.SOCIAL_SLEEP)
    if r.status_code in (403, 429) or r.status_code >= 500:
        OEMBED_HEALTH.note("blocked", f"HTTP {r.status_code} from oEmbed")
        return "blocked", None
    if r.status_code != 200:
        OEMBED_HEALTH.note("empty", f"HTTP {r.status_code} for {url}")
        return "dead", None
    try:
        body = r.json()
    except ValueError:
        OEMBED_HEALTH.note("errors", "oEmbed returned 200 with non-JSON body")
        return "blocked", None
    if body.get("html"):
        OEMBED_HEALTH.note("ok")
        return "live", body.get("author_name")
    OEMBED_HEALTH.note("empty", f"no embed html for {url}")
    return "dead", None


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
        entry = {"type": "tiktok", "url": u}
        if C.VALIDATE_TIKTOK:
            status, author = validate_tiktok(u)
            if status == "dead":
                continue                   # confirmed dead/private link — drop it
            if status == "blocked":
                # We could not check. Keeping an unverified public URL is honest
                # and reversible; silently deleting it is neither. Marked so a
                # reviewer (and promote.py) can see it was never confirmed.
                entry["validated"] = False
                entry["validation"] = "unchecked-tiktok-blocked"
            else:
                entry["validated"] = True
                if author:
                    entry["author"] = author
        out.append(entry)
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
        "created_utc": post.get("created_utc"),
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
    cid = (env.get("REDDIT_CLIENT_ID") or "").strip()
    secret = (env.get("REDDIT_CLIENT_SECRET") or "").strip()
    missing = [n for n, v in (("REDDIT_CLIENT_ID", cid),
                              ("REDDIT_CLIENT_SECRET", secret)) if not v]
    if missing:
        fail_config(
            f"{' and '.join(missing)}\n"
            f"  Looked in environment variables, and in the .env file at:\n"
            f"    {ENV_PATH}   ({'exists' if ENV_PATH.exists() else 'DOES NOT EXIST'})\n"
            f"  These have never been created. This is why the nightly-ingest\n"
            f"  cron failed at startup every morning from 2026-08-21 onward.",
            REDDIT_KEY_HOWTO)

    seen_path = HERE / C.SEEN_FILE
    seen = (set(seen_path.read_text(encoding="utf-8", errors="ignore").split())
            if seen_path.exists() else set())
    # Ids burned this run. Only ids that reached a PERMANENT decision get
    # persisted; a post dropped because the geocoder was down is transient and
    # must stay retryable, or one bad night poisons those posts forever.
    newly_seen, transient = set(), set()
    run_keys = set()

    print(f"Auth… region={C.REGION_NAME}  subs={len(C.SUBREDDITS)}  queries={len(C.QUERIES)}")
    token = reddit_token(cid, secret)
    health = SourceHealth("reddit")

    cand_path = HERE / C.CANDIDATES_FILE
    out = None if args.dry_run else cand_path.open("a", encoding="utf-8")
    stats = dict(seen_posts=0, no_place=0, geo_fail=0, out_region=0, dup=0, kept=0)

    try:
        for sub in C.SUBREDDITS:
            for q in C.QUERIES:
                for sort in C.SORTS:
                    for post in reddit_search(token, sub, q, sort,
                                              C.PER_QUERY_LIMIT, health):
                        pid = post.get("id")
                        if not pid or pid in seen or pid in newly_seen:
                            continue
                        newly_seen.add(pid); stats["seen_posts"] += 1
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
                            # Transient: retry this post on a future run.
                            transient.add(pid)
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
            # Never burn ids that failed for a transient reason.
            seen_path.write_text("\n".join(sorted(seen | (newly_seen - transient))),
                                 encoding="utf-8")

    print("\n=== run summary ===")
    for k, v in stats.items():
        print(f"  {k:12}: {v}")
    print(f"  {'retryable':12}: {len(transient)} (geocoder down — not marked seen)")
    print("\n=== source health ===")
    print(health.detail())
    if GEO_HEALTH.attempts:
        print(GEO_HEALTH.detail())
    if OEMBED_HEALTH.attempts:
        print(OEMBED_HEALTH.detail())

    if stats["kept"]:
        if not args.dry_run:
            print(f"\nCandidates appended to {cand_path}")
            print("Next: review them, then run  promote.py  to publish the good ones.")
        sys.exit(EXIT_OK)

    # Zero yield. Which of the three kinds is it?
    if health.verdict() == "blocked":
        fail_blocked("reddit", health.detail())
    if GEO_HEALTH.attempts and GEO_HEALTH.verdict() == "blocked":
        fail_blocked("nominatim (geocoder)",
                     "Reddit answered fine, but the GEOCODER refused us, so every\n"
                     "candidate died at the geocode step:\n" + GEO_HEALTH.detail())
    fail_zero("reddit",
              health.detail() +
              f"\n{stats['seen_posts']} new posts examined; none survived the "
              f"filters (no_place={stats['no_place']} geo_fail={stats['geo_fail']} "
              f"out_region={stats['out_region']} dup={stats['dup']}).")


if __name__ == "__main__":
    main()
