# ===== Prowl Reddit-ingestion config =====
# Tunable knobs for the overnight spot builder. Edit this, not the engine.

# --- What to search -------------------------------------------------------
# Subreddits worth sweeping for real abandoned / urbex / cool-place posts.
# Order does not matter; the engine hits each with every QUERY below.
SUBREDDITS = [
    "urbanexploration", "AbandonedPorn", "abandoned", "ExplorationHere",
    "Damnthatsinteresting", "texas", "Dallas", "fortworth", "Denton",
    "houston", "Austin", "sanantonio", "urbexbunker",
]

# Search terms. Keep them broad; the location + category logic filters later.
QUERIES = [
    "abandoned", "urbex", "ghost town", "abandoned building", "abandoned hotel",
    "abandoned factory", "ruins", "rooftop", "tunnel", "abandoned house",
]

# --- Where we care about (region gate) ------------------------------------
# Bounding box: candidates whose geocoded pin falls outside are dropped.
# Default = Texas. Widen to the continental US by swapping the numbers below.
REGION_NAME = "Texas"
REGION_BBOX = {  # (south, west, north, east)
    "south": 25.83, "west": -106.65, "north": 36.50, "east": -93.51,
}
# Continental US (uncomment to go nationwide once Texas is proven):
# REGION_NAME = "USA"
# REGION_BBOX = {"south": 24.40, "west": -124.85, "north": 49.40, "east": -66.90}

# Append this to vague place strings before geocoding, to keep results local.
GEOCODE_HINT = "Texas, USA"

# --- Category classification ----------------------------------------------
# First matching keyword (checked against title + selftext, lowercased) wins.
CATEGORY_KEYWORDS = [
    ("tunnel",    ["tunnel", "drain", "storm drain", "underground", "catacomb"]),
    ("rooftop",   ["rooftop", "roof top", "on the roof", "skyline from"]),
    ("abandoned", ["abandoned", "urbex", "ghost town", "ruins", "derelict",
                   "decayed", "forgotten", "empty building", "old hospital",
                   "old factory", "abandoned hotel", "abandoned house"]),
]
DEFAULT_CATEGORY = "abandoned"

# --- Quality gates --------------------------------------------------------
MIN_UPVOTES = 15          # skip low-signal posts
MIN_TITLE_LEN = 12        # skip junk titles
SORTS = ["top", "hot"]    # search sort orders to sweep
TIME_FILTER = "all"       # top-of: all | year | month
PER_QUERY_LIMIT = 50      # posts per subreddit-query-sort (Reddit max 100)

# --- Politeness / rate limits ---------------------------------------------
REDDIT_SLEEP = 1.2        # seconds between Reddit API calls (~50/min, under cap)
GEOCODE_SLEEP = 1.1       # Nominatim asks for <= 1 request/sec
USER_AGENT = "prowl-spot-ingest/1.0 (by /u/prowlapp; contact wjackwing1@gmail.com)"

# --- Social harvest (TikTok / Instagram links found inside Reddit) ---------
# Reddit posts constantly link out to TikTok/IG. We pull those links and attach
# them as embeds — legal (public URL + sanctioned embed), never scraped media.
HARVEST_SOCIAL = True
VALIDATE_TIKTOK = True     # confirm each TikTok link is live via public oEmbed (adds a call)
TIKTOK_OEMBED = "https://www.tiktok.com/oembed"
MAX_EMBEDS = 4             # cap embeds per spot (1 reddit + up to a few social)
SOCIAL_SLEEP = 0.8         # seconds between oEmbed validation calls

# --- TikTok hashtag discovery (EXPERIMENTAL, fragile, ToS-gray) ------------
# Standalone discovery path used by tiktok_ingest.py. Unlike the Reddit social
# harvest above (which only reads TikTok links already posted to Reddit), this
# actively discovers TikTok videos by hashtag. TikTok has no official public
# search API, so discovery leans on an unofficial best-effort method (yt-dlp)
# and can be blocked by TikTok anti-bot at any time. It is embed-only: it never
# downloads a single video or image, it only stores the public video URL and
# validates it through the same public oEmbed endpoint used above.
# The durable clean path is the official TikTok Research API, not this.
TIKTOK_HASHTAGS = [
    "abandoneddallas", "urbextexas", "dallasabandoned", "abandonedtexas",
    "urbexdallas", "texasurbex", "abandonedfortworth", "houstonurbex",
]

# Videos to pull per hashtag page before stopping (kept small on purpose —
# this is a fragile path, do not hammer it).
TIKTOK_PER_HASHTAG = 25

# Politeness sleeps for the TikTok path.
TIKTOK_DISCOVER_SLEEP = 3.0   # seconds between hashtag pages (go slow, avoid blocks)
TIKTOK_VALIDATE_SLEEP = 0.8   # seconds between oEmbed validation calls

# Minimum like count to keep a discovered video (mirrors MIN_UPVOTES on Reddit).
TIKTOK_MIN_LIKES = 0          # 0 = keep all; raise once volume is proven

# --- Output ---------------------------------------------------------------
CANDIDATES_FILE = "candidates.jsonl"   # one JSON spot-candidate per line
SEEN_FILE = "seen_ids.txt"             # reddit post ids already processed (dedupe across runs)
TIKTOK_SEEN_FILE = "seen_tiktok_ids.txt"  # tiktok video ids already processed (dedupe across runs)
