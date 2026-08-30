#!/usr/bin/env python3
"""
Reddit source -- read-only search via Reddit's official free application-only
OAuth API (client_credentials grant). Follows SOURCE-CLI-CONTRACT.md exactly.

WHY THIS EXISTS
  Direct scraping of Reddit (the site, the .json endpoints, old.reddit, and
  through the r.jina.ai reader proxy) all returned 403 as of 2026-08-27.
  Reddit's own API is free, legitimate, and requires no user login for
  read-only search: an app registered as "script" type gets a client id and
  secret, which is exchanged for a short-lived bearer token via the
  client_credentials grant. That is what this tool does.

AUTH
  Needs REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET, loaded from the
  environment or from C:\\Users\\wjack\\ghl-cli\\.env if present there.
  To get credentials:
    1. Go to https://www.reddit.com/prefs/apps
    2. Click "create app" / "create another app"
    3. Choose type "script"
    4. Put anything for name/redirect uri (e.g. http://localhost:8080)
    5. Copy the client id (under the app name) and the "secret" field
    6. Add to C:\\Users\\wjack\\ghl-cli\\.env :
         REDDIT_CLIENT_ID=xxxxxxxxxxxxxx
         REDDIT_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxx

READ ONLY. No posting, no commenting, no voting, no DMs, no user login. This
tool only ever calls read-only search endpoints with an app-only token.

RATE LIMITING
  Reddit's app-only limit is roughly 100 queries/minute. This tool sleeps
  between requests and additionally honors the X-Ratelimit-Remaining and
  X-Ratelimit-Reset response headers, backing off when Reddit says to.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SOURCE = "reddit"
UA = "WingDigitalResearch/1.0 (by /u/wingdigital, contact: wjackwing1@gmail.com)"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SEARCH_URL = "https://oauth.reddit.com/search"
SUBREDDIT_SEARCH_URL = "https://oauth.reddit.com/r/{sub}/search"
MIN_SLEEP = 0.7  # keeps us well under 100 req/min even without header info

ENV_FILE = Path(r"C:\Users\wjack\ghl-cli\.env")

CREDENTIAL_HELP = """\
Reddit search is not configured: no REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET found.

Reddit blocks plain scraping (403 on direct, .json, old.reddit, and reader
proxies) but offers a FREE official API for exactly this use case. To enable it:

  1. Go to https://www.reddit.com/prefs/apps
  2. Click "create app" / "create another app"
  3. Choose app type "script"
  4. Name it anything; for the redirect uri put http://localhost:8080
  5. After creating it, copy:
       - the client id (short string under the app name)
       - the "secret" field
  6. Add both to C:\\Users\\wjack\\ghl-cli\\.env :
       REDDIT_CLIENT_ID=your_client_id_here
       REDDIT_CLIENT_SECRET=your_secret_here
  7. Re-run this tool.

No user login is required. This uses the client_credentials (app-only) grant,
which is read-only and cannot post, comment, vote, or message as any account.
"""

# Generic topic subreddits relevant to local-lead style searches (junk
# removal, moving, estate cleanouts, etc). Not client-specific -- these are
# always searched in addition to whatever --subreddits / --cities resolve to.
TOPIC_SUBREDDITS = [
    "declutter",
    "estatesales",
    "moving",
    "HomeImprovement",
]


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ, without
    overwriting anything already set in the real environment."""
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError as exc:
        print(f"reddit_cli: could not read {path}: {exc}", file=sys.stderr)


def get_credentials() -> tuple[str, str] | None:
    load_env_file(ENV_FILE)
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    return client_id, client_secret


def get_token(client_id: str, client_secret: str) -> str | None:
    try:
        resp = requests.post(
            TOKEN_URL,
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": UA},
            timeout=20,
        )
    except requests.RequestException as exc:
        print(f"reddit_cli: token request failed: {exc}", file=sys.stderr)
        return None

    if resp.status_code != 200:
        print(
            f"reddit_cli: token request rejected, status {resp.status_code}: "
            f"{resp.text[:300]}",
            file=sys.stderr,
        )
        return None

    try:
        data = resp.json()
    except ValueError:
        print("reddit_cli: token response was not valid JSON", file=sys.stderr)
        return None

    token = data.get("access_token")
    if not token:
        print(f"reddit_cli: token response had no access_token: {data}", file=sys.stderr)
        return None
    return token


def respect_rate_limit(resp: requests.Response) -> None:
    """Sleep according to Reddit's rate limit headers, falling back to a
    fixed minimum delay when headers are absent."""
    remaining = resp.headers.get("X-Ratelimit-Remaining")
    reset = resp.headers.get("X-Ratelimit-Reset")
    if remaining is not None and reset is not None:
        try:
            remaining_f = float(remaining)
            reset_f = float(reset)
            if remaining_f <= 1:
                # Out of budget for this window, wait it out.
                time.sleep(min(reset_f, 60.0))
                return
            # Spread remaining requests evenly across the remaining window.
            pace = reset_f / max(remaining_f, 1.0)
            time.sleep(max(MIN_SLEEP, min(pace, 5.0)))
            return
        except ValueError:
            pass
    time.sleep(MIN_SLEEP)


def resolve_subreddits(cities: list[str], explicit: list[str]) -> list[str]:
    subs: list[str] = []
    for s in explicit:
        s = s.strip()
        if s and s not in subs:
            subs.append(s)
    for city in cities:
        slug = "".join(ch for ch in city if ch.isalnum())
        if slug and slug not in subs:
            subs.append(slug)
    for topic in TOPIC_SUBREDDITS:
        if topic not in subs:
            subs.append(topic)
    return subs


def iso(ts) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OSError, OverflowError):
        return None


def make_record(post: dict, query: str, client: str | None) -> dict:
    permalink = post.get("permalink")
    url = f"https://www.reddit.com{permalink}" if permalink else post.get("url") or None
    title = post.get("title")
    body = post.get("selftext") or None
    author = post.get("author")
    author_handle = author if author and author != "[deleted]" else None
    subreddit = post.get("subreddit")
    location_text = f"r/{subreddit}" if subreddit else None
    posted_at = iso(post.get("created_utc"))

    return {
        "source": SOURCE,
        "platform": "reddit",
        "url": url,
        "title": title if title else None,
        "body": body,
        "author_handle": author_handle,
        "location_text": location_text,
        "posted_at": posted_at,
        "event_date": None,
        "query": query,
        "client": client,
    }


# Set whenever the Reddit API refuses a search request (403/429/other non-200).
# main() reads it so a run where EVERY request was refused exits 2 (BLOCKED)
# instead of exiting 0 with zero records — an invisible throttle otherwise.
_ANY_BLOCKED = False


def search_subreddit_or_all(
    token: str,
    query: str,
    subreddit: str | None,
    limit: int,
) -> list[dict]:
    headers = {"User-Agent": UA, "Authorization": f"bearer {token}"}
    params = {
        "q": query,
        "limit": min(limit, 100),
        "sort": "new",
        "restrict_sr": "true" if subreddit else "false",
    }
    url = SUBREDDIT_SEARCH_URL.format(sub=subreddit) if subreddit else SEARCH_URL

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=20)
    except requests.RequestException as exc:
        print(f"reddit_cli: search request failed for {url}: {exc}", file=sys.stderr)
        return []

    global _ANY_BLOCKED
    if resp.status_code == 403:
        _ANY_BLOCKED = True
        print(f"reddit_cli: 403 from Reddit API on {url} (query={query!r})", file=sys.stderr)
        return []
    if resp.status_code == 429:
        _ANY_BLOCKED = True
        print(f"reddit_cli: 429 rate limited on {url}, backing off", file=sys.stderr)
        respect_rate_limit(resp)
        return []
    if resp.status_code != 200:
        _ANY_BLOCKED = True
        print(
            f"reddit_cli: unexpected status {resp.status_code} from {url}: {resp.text[:200]}",
            file=sys.stderr,
        )
        respect_rate_limit(resp)
        return []

    respect_rate_limit(resp)

    try:
        data = resp.json()
    except ValueError:
        print(f"reddit_cli: non-JSON response from {url}", file=sys.stderr)
        return []

    children = data.get("data", {}).get("children", [])
    return [c.get("data", {}) for c in children if c.get("data")]


def main() -> int:
    parser = argparse.ArgumentParser(description="Reddit source (official free API)")
    parser.add_argument("--query", action="append", default=[], help="repeatable, or comma separated")
    parser.add_argument("--cities", default="", help="comma separated")
    parser.add_argument("--client", default=None)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--since", type=int, default=None, help="freshness window in days")
    parser.add_argument("--json", action="store_true", default=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--subreddits", default="", help="comma separated, explicit subreddits")
    parser.add_argument("--all-reddit", action="store_true", help="also search all of Reddit, not just resolved subreddits")
    args = parser.parse_args()

    queries: list[str] = []
    for q in args.query:
        queries.extend(part.strip() for part in q.split(",") if part.strip())
    if not queries:
        print("reddit_cli: no --query given, nothing to search", file=sys.stderr)
        return 1

    creds = get_credentials()
    if creds is None:
        print(CREDENTIAL_HELP, file=sys.stderr)
        return 2

    client_id, client_secret = creds
    token = get_token(client_id, client_secret)
    if token is None:
        print("reddit_cli: could not obtain an access token, treating as blocked", file=sys.stderr)
        return 2

    cities = [c.strip() for c in args.cities.split(",") if c.strip()]
    explicit_subs = [s.strip() for s in args.subreddits.split(",") if s.strip()]
    subreddits = resolve_subreddits(cities, explicit_subs)

    since_cutoff = None
    if args.since is not None:
        since_cutoff = time.time() - (args.since * 86400)

    emitted = 0
    seen_urls: set[str] = set()

    for query in queries:
        targets: list[str | None] = list(subreddits)
        if args.all_reddit or not subreddits:
            targets.append(None)  # None means search all of Reddit

        for sub in targets:
            if emitted >= args.limit:
                break
            posts = search_subreddit_or_all(token, query, sub, args.limit - emitted)
            for post in posts:
                if emitted >= args.limit:
                    break
                created = post.get("created_utc")
                if since_cutoff is not None and created is not None:
                    try:
                        if float(created) < since_cutoff:
                            continue
                    except ValueError:
                        pass
                record = make_record(post, query, args.client)
                if not record["url"]:
                    continue
                if record["url"] in seen_urls:
                    continue
                seen_urls.add(record["url"])
                # Records are ALWAYS emitted, dry-run included. Per
                # SOURCE-CLI-CONTRACT.md dry-run only skips side effects, and
                # run_pipeline always passes --dry-run and counts stdout lines
                # — suppressing output here made every reddit run read as
                # EMPTY. This CLI has no side effects to skip.
                print(json.dumps(record))
                emitted += 1

    if emitted == 0:
        if _ANY_BLOCKED:
            print("reddit_cli: 0 records, all requests were blocked", file=sys.stderr)
            return 2
        print(
            f"reddit_cli: 0 records for queries={queries} subreddits={subreddits} "
            f"since={args.since}. No matches found in the searched window/subreddits.",
            file=sys.stderr,
        )
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
