"""Free, keyless YouTube transcript fetcher for the creator-intel lane.

intel_watch.py stores only the RSS description of each new video. That is not
enough to turn a video into a concrete suggestion for Wing's systems -- for
that we need what the video ACTUALLY SAYS. This module gets the captions.

Two free methods, tried in order:
  1. youtube-transcript-api  (pip install youtube-transcript-api)
     Cleanest: real timestamped segments, one HTTP call, no download.
  2. yt-dlp                  (already on this machine, see tiktok_ingest.py)
     Writes auto-subs/subs as VTT into a temp dir; we parse the VTT.

The single most important behaviour here is the distinction between:
    none    -> YouTube answered and this video genuinely has no captions
    failed  -> we could not reach YouTube (DNS blip, block, timeout)
This machine has intermittent DNS failures. Recording a network error as
"no captions" would permanently mark a perfectly good video as uncaptioned
and it would never be retried, so every ambiguous case degrades to `failed`.

    ENV_FILE=C:/Users/wjack/ghl-cli/.env python intel_transcript.py --url <url>
    ENV_FILE=C:/Users/wjack/ghl-cli/.env python intel_transcript.py --backfill 5
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

import requests

# Windows consoles default to cp1252 and creator video titles routinely carry
# emoji and smart quotes it cannot encode -- printing one would kill the run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = pathlib.Path(__file__).resolve().parent

MAX_CHARS = 120_000  # past this we still return text, but flag it too_long
LANGS = ("en", "en-US", "en-GB")


# --------------------------------------------------------------------- env ---
def load_env():
    """Same pattern as db.py: real env vars, then a .env file ($ENV_FILE)."""
    vals = dict(os.environ)
    env_file = pathlib.Path(os.environ.get("ENV_FILE", HERE.parent / ".env"))
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return vals


def sb_request(method, url, *, retries=4, **kw):
    """Supabase call with backoff (copied from audit_prospect.py).

    The network here is flaky enough that a plain requests call WILL
    intermittently fail, and a single blip must not kill a backfill.
    """
    delay = 2
    last = "unknown"
    for attempt in range(retries):
        try:
            r = requests.request(method, url, timeout=45, **kw)
            if r.status_code < 500:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)[:80]
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    print(f"      DB call failed after {retries} tries: {last}")
    return None


# ------------------------------------------------------------ video ids ------
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def video_id(url_or_id: str):
    """Pull the 11-char video id out of any YouTube URL form, or pass an id."""
    s = (url_or_id or "").strip()
    if _ID_RE.match(s):
        return s
    for pat in (r"[?&]v=([A-Za-z0-9_-]{11})",
                r"/shorts/([A-Za-z0-9_-]{11})",
                r"/embed/([A-Za-z0-9_-]{11})",
                r"/live/([A-Za-z0-9_-]{11})",
                r"youtu\.be/([A-Za-z0-9_-]{11})"):
        m = re.search(pat, s)
        if m:
            return m.group(1)
    return None


def _finish(text, segments, source):
    """Apply the length cap. Truncated text is still returned so downstream
    can use the start of a very long video rather than getting nothing."""
    if not text.strip():
        return {"text": "", "status": "none", "segments": [], "source": source}
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS]
        segs = [s for s in segments if s.get("t", 0) >= 0][:0] or []
        # keep only the segments that fall inside the kept text
        kept, run = [], 0
        for s in segments:
            run += len(s.get("text", "")) + 1
            if run > MAX_CHARS:
                break
            kept.append(s)
        return {"text": cut, "status": "too_long", "segments": kept or segs,
                "source": source}
    return {"text": text, "status": "ok", "segments": segments, "source": source}


# ----------------------------------------------- method 1: transcript api ----
# Errors that mean "YouTube answered, there really are no captions".
_NO_CAPTION_NAMES = {
    "TranscriptsDisabled", "NoTranscriptFound", "NotTranslatable",
    "TranslationLanguageNotAvailable",
}
# Errors that mean "we could not get a usable answer" -- never call these none.
_UNREACHABLE_NAMES = {
    "IpBlocked", "RequestBlocked", "PoTokenRequired", "YouTubeRequestFailed",
    "YouTubeDataUnparsable", "FailedToCreateConsentCookie", "AgeRestricted",
    "VideoUnplayable",
}
# InvalidVideoId / VideoUnavailable = the video does not exist. Nothing to
# retry and nothing to fetch: that is a definitive "no transcript here".
_GONE_NAMES = {"InvalidVideoId", "VideoUnavailable"}


def _via_api(vid, attempts=3):
    """Returns (result_dict | None, definitive_status | None).

    None/None means "this method could not decide" -> fall through to yt-dlp.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None, None

    delay = 2
    last_kind = None
    for attempt in range(attempts):
        try:
            fetched = YouTubeTranscriptApi().fetch(vid, languages=list(LANGS))
            segments, parts = [], []
            for sn in fetched:
                t = float(getattr(sn, "start", 0.0) or 0.0)
                txt = (getattr(sn, "text", "") or "").replace("\n", " ").strip()
                if not txt:
                    continue
                segments.append({"t": round(t, 2), "text": txt})
                parts.append(txt)
            return _finish(" ".join(parts), segments, "youtube-transcript-api"), None
        except Exception as e:
            kind = type(e).__name__
            last_kind = kind
            if kind in _NO_CAPTION_NAMES or kind in _GONE_NAMES:
                # Definitive answer from YouTube -- do not retry, but let
                # yt-dlp have a shot at captions the API cannot see.
                return None, "none"
            # Anything else (DNS, timeout, block, unknown) is unreachable.
            if attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
    print(f"      transcript-api unreachable ({last_kind})")
    return None, "failed"


# ------------------------------------------------------ method 2: yt-dlp -----
_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->")
_TAG = re.compile(r"<[^>]+>")


def _vtt_secs(m):
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000.0


def parse_vtt(raw: str):
    """VTT -> [{t, text}], de-duplicated.

    YouTube auto-caption VTT is a rolling display: each cue repeats the
    previous line plus one new one. Naive parsing triples the transcript,
    so every line is emitted only once, in order.
    """
    segments, seen, cur_t = [], set(), None
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE")):
            continue
        m = _TS.search(line)
        if m:
            cur_t = _vtt_secs(m)
            continue
        if "-->" in line or line.isdigit():
            continue
        txt = _TAG.sub("", line).strip()
        txt = txt.replace("&nbsp;", " ").replace("&amp;", "&") \
                 .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
        txt = re.sub(r"\s+", " ", txt).strip()
        if not txt or txt in seen:
            continue
        seen.add(txt)
        segments.append({"t": round(cur_t, 2) if cur_t is not None else 0.0, "text": txt})
    return segments


def _via_ytdlp(vid, attempts=2):
    """Returns (result | None, definitive_status | None)."""
    url = f"https://www.youtube.com/watch?v={vid}"
    delay = 3
    for attempt in range(attempts):
        with tempfile.TemporaryDirectory(prefix="ytsub_") as td:
            cmd = [
                sys.executable, "-m", "yt_dlp", url,
                "--skip-download",
                "--write-subs", "--write-auto-subs",
                "--sub-langs", "en.*,en",
                "--sub-format", "vtt",
                "--convert-subs", "vtt",
                "-o", str(pathlib.Path(td) / "%(id)s.%(ext)s"),
                "--no-warnings", "--no-playlist",
            ]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=180)
            except subprocess.TimeoutExpired:
                print("      yt-dlp: timed out")
                r = None
            except Exception as e:
                print(f"      yt-dlp: could not launch ({e})")
                return None, "failed"

            files = sorted(pathlib.Path(td).glob("*.vtt"))
            if files:
                raw = files[0].read_text(encoding="utf-8", errors="replace")
                segments = parse_vtt(raw)
                if segments:
                    return _finish(" ".join(s["text"] for s in segments),
                                   segments, "yt-dlp"), None

            err = ((r.stderr if r else "") or "").lower()
            # yt-dlp reached YouTube and YouTube said the video is gone/private:
            # definitive, nothing to retry.
            if any(s in err for s in ("video unavailable", "is not a valid url",
                                      "incomplete youtube id", "private video",
                                      "does not exist", "removed by the uploader",
                                      "unavailable")):
                return None, "none"
            # Reached YouTube fine, it just has no subtitles for this video.
            if r is not None and r.returncode == 0:
                return None, "none"
            if attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
    return None, "failed"


# ------------------------------------------------------------- public api ----
def fetch_transcript(video_url_or_id: str) -> dict:
    """Return {text, status, segments, source}.

    status: ok | none | failed | too_long
      ok       -- captions retrieved
      too_long -- captions retrieved but truncated to MAX_CHARS
      none     -- YouTube answered; this video has no captions (or is gone)
      failed   -- we could not reach YouTube; retry this video later
    Never invents or approximates a transcript.
    """
    vid = video_id(video_url_or_id)
    if not vid:
        return {"text": "", "status": "failed", "segments": [],
                "source": "none", "error": "unparseable video url/id"}

    res, verdict = _via_api(vid)
    if res:
        return res
    api_verdict = verdict  # 'none' | 'failed' | None (lib missing)

    res, verdict = _via_ytdlp(vid)
    if res:
        return res

    # Only say "no captions" when at least one method definitively said so and
    # neither method reported an unreachable network. A `failed` anywhere wins.
    if api_verdict == "failed" or verdict == "failed":
        status = "failed"
    elif "none" in (api_verdict, verdict):
        status = "none"
    else:
        status = "failed"
    return {"text": "", "status": status, "segments": [], "source": "none"}


# ---------------------------------------------------------------- backfill ---
def backfill(n, dry_run=False):
    env = load_env()
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json"}

    r = sb_request("GET", f"{url}/rest/v1/intel_items", headers=h, params={
        "select": "id,title,url,source_handle",
        "transcript": "is.null",
        "url": "ilike.*youtu*",
        "order": "published_at.desc",
        "limit": str(n)})
    if r is None or not r.ok:
        sys.exit("Could not read intel_items from Supabase.")
    rows = r.json()
    if not rows:
        print("Nothing to backfill -- every intel_items row already has a transcript.")
        return

    print(f"Backfilling transcripts for {len(rows)} intel_items rows\n")
    tally = {}
    for row in rows:
        title = (row.get("title") or "")[:70]
        print(f"  [{row['id']}] {title}")
        res = fetch_transcript(row.get("url") or "")
        tally[res["status"]] = tally.get(res["status"], 0) + 1
        print(f"      {res['status']} via {res['source']} "
              f"({len(res['text'])} chars, {len(res['segments'])} segments)")
        if dry_run:
            continue
        patch = {"transcript_status": res["status"]}
        if res["status"] in ("ok", "too_long"):
            patch["transcript"] = res["text"]
        elif res["status"] == "none":
            # Mark it so it isn't re-attempted forever. Empty string, not null,
            # so the `transcript is null` filter stops picking it up.
            patch["transcript"] = ""
        else:
            # failed -> leave transcript NULL so a later run retries it.
            pass
        pr = sb_request("PATCH", f"{url}/rest/v1/intel_items", headers=h,
                        params={"id": f"eq.{row['id']}"}, json=patch)
        if pr is None or not pr.ok:
            print(f"      WARNING: write-back failed "
                  f"({pr.status_code if pr is not None else 'no response'})")
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="YouTube url or 11-char video id")
    ap.add_argument("--backfill", type=int, metavar="N",
                    help="fetch transcripts for up to N intel_items rows with transcript is null")
    ap.add_argument("--dry-run", action="store_true", help="backfill without writing to Supabase")
    ap.add_argument("--json", action="store_true", help="--url: dump the full result as JSON")
    args = ap.parse_args()

    if args.url:
        res = fetch_transcript(args.url)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return
        print(f"status : {res['status']}")
        print(f"source : {res['source']}")
        print(f"chars  : {len(res['text'])}   segments: {len(res['segments'])}")
        if res["segments"][:1]:
            s = res["segments"][0]
            print(f"first  : [{s['t']}s] {s['text'][:80]}")
        print("-" * 60)
        print(res["text"][:400])
        return

    if args.backfill:
        backfill(args.backfill, dry_run=args.dry_run)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
