#!/usr/bin/env python3
"""
Competitor / creator intel watcher — ZERO API COST.

Watches the AI/build creators Wing Digital should be learning from and files
their new videos into Supabase so the OS can surface them.

    intel_sources  ->  who we watch (kind, handle, name, channel_url, why, active)
    intel_items    ->  what they published (source_handle, title, url, published_at,
                       summary, takeaway, actionable, status)

Discovery is YouTube's public RSS feed: free, official, keyless, reliable.
    https://www.youtube.com/feeds/videos.xml?channel_id=UC...
The only missing piece is the UC id, which is scraped once out of the public
channel page HTML and then CACHED back into intel_sources.channel_url so every
later run goes straight to RSS. If a handle refuses to resolve we fall back to
the free search index (ddgs) and say so in the row's summary.

We never invent a summary or a takeaway. `summary` is only the feed's own
media:description; `takeaway` stays null and `actionable` false until a human
(or a deliberate later step) fills them in.

    python intel_watch.py                      # all active sources
    python intel_watch.py --dry-run            # research + print, write nothing
    python intel_watch.py --source jackroberts # one source
    python intel_watch.py --limit 5            # cap videos per source
"""
import argparse
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from db import load_env
from audit_prospect import sb_request

# Windows consoles default to cp1252 and video titles routinely contain
# symbols and emoji it cannot encode. Without this, printing a single title
# raises UnicodeEncodeError and kills the whole run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
TIMEOUT = 25
SUMMARY_MAX = 400

NS = {"atom": "http://www.w3.org/2005/Atom",
      "media": "http://search.yahoo.com/mrss/",
      "yt": "http://www.youtube.com/xml/schemas/2015"}

# The canonical link is the ONLY id on a channel page guaranteed to be the
# channel itself. A bare "channelId" regex silently matches the first
# recommended video's owner instead — @jackroberts resolved to Android Central
# that way. externalId (in channelMetadataRenderer) is the safe backup.
class TransientFetchError(Exception):
    """Network could not be reached — distinct from "not found"."""


CANONICAL_RE = re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{20,})"')
CHANNEL_ID_RE = re.compile(r'"externalId"\s*:\s*"(UC[\w-]{20,})"')
UC_IN_URL_RE = re.compile(r"(UC[\w-]{20,})")



def http_get(url, tries=3):
    """GET with backoff, distinguishing a transient failure from a real 404.

    Returns (response, transient_failure). transient_failure=True means we
    could not reach YouTube at all, so the caller must NOT treat it as
    "nothing there" and must NOT fall back to unverified search results.
    """
    delay = 3
    last_transient = False
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            return r, False
        except Exception as e:
            # DNS/connect/read failures are transient; this machine has
            # intermittent DNS dropouts that previously polluted the table.
            last_transient = True
            if attempt < tries - 1:
                time.sleep(delay)
                delay *= 2
    return None, last_transient


# ------------------------------------------------------------- resolution ---
def resolve_channel_id(handle: str, cached: str | None) -> str | None:
    """UC id for a handle. Uses the cached channel_url first so the public page
    is fetched once per source, ever — not once per run."""
    if cached:
        m = UC_IN_URL_RE.search(cached)
        if m:
            return m.group(1)

    handle = handle.lstrip("@")
    for url in (f"https://www.youtube.com/@{handle}",
                f"https://www.youtube.com/@{handle}/videos",
                f"https://www.youtube.com/c/{handle}",
                f"https://www.youtube.com/user/{handle}"):
        r, transient = http_get(url)
        if r is None:
            if transient:
                # Could not reach YouTube. Say so rather than reporting the
                # channel as missing, so the caller can skip instead of
                # falling back to unverified search hits.
                raise TransientFetchError(f"could not reach {url}")
            continue
        if r.status_code != 200:
            continue
        m = CANONICAL_RE.search(r.text) or CHANNEL_ID_RE.search(r.text)
        if m:
            return m.group(1)
    return None


def feed_for(channel_id: str | None, handle: str):
    """(entries, feed_title) from YouTube RSS. Empty list if the feed is dead."""
    # Only fall back to ?user= when there is no id at all — a legacy username
    # is not guaranteed to belong to the same person as the handle.
    urls = ([f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"]
            if channel_id else
            [f"https://www.youtube.com/feeds/videos.xml?user={handle.lstrip('@')}"])
    for u in urls:
        r, transient = http_get(u)
        if r is None:
            if transient:
                raise TransientFetchError(f"could not reach {u}")
            continue
        if r.status_code != 200 or "<entry" not in r.text:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        title = (root.findtext("atom:title", default="", namespaces=NS) or "").strip()
        return root.findall("atom:entry", NS), title
    return [], ""


def entry_to_row(handle: str, e) -> dict | None:
    vid_url = None
    link = e.find("atom:link", NS)
    if link is not None:
        vid_url = link.get("href")
    vid_id = e.findtext("yt:videoId", default="", namespaces=NS)
    if not vid_url and vid_id:
        vid_url = f"https://www.youtube.com/watch?v={vid_id}"
    title = (e.findtext("atom:title", default="", namespaces=NS) or "").strip()
    if not vid_url or not title:
        return None

    group = e.find("media:group", NS)
    desc = None
    if group is not None:
        raw = (group.findtext("media:description", default="", namespaces=NS) or "").strip()
        if raw:
            desc = raw[:SUMMARY_MAX]

    return {
        "source_handle": handle,
        "title": title,
        "url": vid_url,
        "published_at": e.findtext("atom:published", default=None, namespaces=NS),
        "summary": desc,          # feed's own words, or nothing. Never invented.
        "takeaway": None,         # a human / later step fills this in
        "actionable": False,
        "status": "new",
    }


# ----------------------------------------------------------------- fallback --
def search_fallback(source: dict, limit: int) -> list[dict]:
    """RSS refused this handle. Use the free search index instead and mark the
    rows so nobody mistakes a search hit for a verified feed item."""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            print("      no search client installed (pip install ddgs)")
            return []
    name = source.get("name") or source["handle"]
    rows = []
    try:
        with DDGS() as d:
            hits = list(d.text(f"{name} youtube video", max_results=limit * 2))
    except Exception as ex:
        print(f"      search fallback failed: {str(ex)[:80]}")
        return []
    for h in hits:
        u = h.get("href") or h.get("url") or ""
        if "youtube.com/watch" not in u and "youtu.be/" not in u:
            continue
        rows.append({
            "source_handle": source["handle"],
            "title": (h.get("title") or "").strip(),
            "url": u,
            "published_at": None,
            "summary": "[via search index — RSS resolution failed for this handle; "
                       "date and description unverified]",
            "takeaway": None,
            "actionable": False,
            "status": "new",
        })
        if len(rows) >= limit:
            break
    return rows


# --------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="research + print, write nothing")
    ap.add_argument("--source", help="one handle, instead of every active source")
    ap.add_argument("--limit", type=int, default=15, help="max videos per source")
    args = ap.parse_args()

    env = load_env()
    # Locally the credentials live in the ghl-cli .env; in CI they arrive as
    # real env vars and this file simply is not there.
    if not env.get("SUPABASE_URL"):
        alt = pathlib.Path.home() / "ghl-cli" / ".env"
        if alt.exists():
            for line in alt.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY")
    auth = {"apikey": key, "Authorization": f"Bearer {key}"}
    write = {**auth, "Content-Type": "application/json",
             "Prefer": "resolution=ignore-duplicates,return=representation"}

    q = "intel_sources?select=*&active=eq.true&order=id"
    if args.source:
        q += f"&handle=eq.{args.source}"
    r = sb_request("GET", f"{url}/rest/v1/{q}", headers=auth)
    if r is None or r.status_code != 200:
        sys.exit(f"Could not read intel_sources: {r.status_code if r else 'no response'}")
    sources = r.json()
    if not sources:
        sys.exit("No active intel_sources to watch.")

    total_found = total_new = stats_skipped = 0
    for s in sources:
        handle = s["handle"]
        print(f"\n{s.get('name') or handle}  (@{handle})")

        try:
            cid = resolve_channel_id(handle, s.get("channel_url"))
        except TransientFetchError as e:
            print(f"   SKIPPED — network unreachable ({e}). Not falling back to "
                  f"search: unverified hits attributed to a real creator are worse "
                  f"than no row.")
            stats_skipped += 1
            continue
        if cid:
            was_cached = bool(s.get("channel_url") and UC_IN_URL_RE.search(s["channel_url"]))
            print(f"   channel_id {cid}" + ("  (cached)" if was_cached else "  (resolved from page)"))
        else:
            print("   channel_id unresolved")

        try:
            entries, feed_title = feed_for(cid, handle)
        except TransientFetchError as e:
            print(f"   SKIPPED — feed unreachable ({e}). Not falling back to search.")
            stats_skipped += 1
            continue
        if entries:
            rows = [x for x in (entry_to_row(handle, e) for e in entries[:args.limit]) if x]
            print(f"   RSS ok{f' — {feed_title}' if feed_title else ''}: {len(rows)} videos")
        else:
            print("   RSS failed — falling back to the search index")
            rows = search_fallback(s, args.limit)
            print(f"   search fallback: {len(rows)} videos")
        total_found += len(rows)

        for row in rows[:5]:
            when = (row["published_at"] or "")[:10]
            print(f"      - {when}  {row['title'][:80]}")
        if len(rows) > 5:
            print(f"      ... and {len(rows) - 5} more")

        if args.dry_run:
            # The old dry run printed "would file 0 new" unconditionally because
            # total_new only counted insert responses — so it proved nothing.
            # Ask the DB which of these URLs it already has.
            known = set()
            if rows:
                urls_q = ",".join(f'"{r["url"]}"' for r in rows)
                chk = sb_request("GET", f"{url}/rest/v1/intel_items",
                                 headers=auth,
                                 params={"select": "url", "url": f"in.({urls_q})"})
                if chk is not None and chk.ok:
                    known = {x["url"] for x in chk.json()}
            fresh = [r for r in rows if r["url"] not in known]
            total_new += len(fresh)
            print(f"   would file {len(fresh)} new ({len(rows) - len(fresh)} already known)")
            continue

        # Cache the resolved id so the public page is never fetched again.
        if cid and not (s.get("channel_url") or "").endswith(cid):
            sb_request("PATCH", f"{url}/rest/v1/intel_sources?id=eq.{s['id']}",
                       headers={**auth, "Content-Type": "application/json",
                                "Prefer": "return=minimal"},
                       json={"channel_url": f"https://www.youtube.com/channel/{cid}"})

        if not rows:
            continue
        resp = sb_request("POST", f"{url}/rest/v1/intel_items?on_conflict=url",
                          headers=write, json=rows)
        if resp is None or resp.status_code not in (200, 201):
            print(f"   insert failed: {resp.status_code if resp else 'no response'} "
                  f"{(resp.text[:200] if resp else '')}")
            continue
        new = len(resp.json())
        total_new += new
        print(f"   inserted {new} new (rest already known)")

    verb = "would file" if args.dry_run else "filed"
    print(f"\n{len(sources)} source(s), {total_found} videos found, {verb} {total_new} new.")


if __name__ == "__main__":
    main()
