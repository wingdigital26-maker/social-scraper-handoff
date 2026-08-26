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


# ------------------------------------------------------- outcome taxonomy ---
# Every backend attempt ends in exactly one of these. The whole point is that
# "we got nothing" is never one undifferentiated bucket: a video with captions
# switched off is a permanent, normal, correct zero, while a blocked IP or an
# uninstalled library is an operator problem that must be shouted about.
#
#   ok                 -- captions retrieved
#   no_captions        -- YouTube answered: this video has no captions
#   video_gone         -- video deleted/private/never existed; nothing to fetch
#   blocked            -- YouTube refused US: IP ban, rate limit, bot check.
#                         Common on datacenter IPs such as GitHub Actions.
#   unreachable        -- DNS/timeout/transport failure; retry later
#   missing_dependency -- the backend is not installed in THIS environment.
#                         Not a YouTube verdict at all: nothing was attempted.
#   code_error         -- our bug: unexpected exception, bad parse
_PERMANENT = {"no_captions", "video_gone"}
_TRANSIENT = {"blocked", "unreachable"}
_ENVIRONMENT = {"missing_dependency"}

# ----------------------------------------------- method 1: transcript api ----
# Errors that mean "YouTube answered, there really are no captions".
_NO_CAPTION_NAMES = {
    "TranscriptsDisabled", "NoTranscriptFound", "NotTranslatable",
    "TranslationLanguageNotAvailable",
}
# Errors that mean YouTube actively refused this client. On a hosted runner
# this is the expected failure: YouTube blocks datacenter address ranges.
_BLOCKED_NAMES = {
    "IpBlocked", "RequestBlocked", "PoTokenRequired", "AgeRestricted",
    "VideoUnplayable",
}
# Errors that mean "we could not get a usable answer" -- never call these none.
_UNREACHABLE_NAMES = {
    "YouTubeRequestFailed", "YouTubeDataUnparsable",
    "FailedToCreateConsentCookie", "ConnectionError", "Timeout",
    "ReadTimeout", "ConnectTimeout", "RequestException", "SSLError",
}
# InvalidVideoId / VideoUnavailable = the video does not exist. Nothing to
# retry and nothing to fetch: that is a definitive "no transcript here".
_GONE_NAMES = {"InvalidVideoId", "VideoUnavailable"}

# Text fingerprints for blocks, used when the exception class is unhelpful
# (the library wraps a lot of things in generic errors).
_BLOCK_HINTS = (
    "too many requests", "http error 429", "sign in to confirm",
    "not a bot", "blocking requests from your ip", "ip has been blocked",
    "captcha", "consent", "requests from your network",
)


def _classify_exc(e):
    """(outcome, detail) for one exception raised by a transcript backend."""
    kind = type(e).__name__
    msg = " ".join(str(e).split())[:200]
    if kind in _NO_CAPTION_NAMES:
        return "no_captions", kind
    if kind in _GONE_NAMES:
        return "video_gone", kind
    if kind in _BLOCKED_NAMES:
        return "blocked", kind
    low = str(e).lower()
    if any(h in low for h in _BLOCK_HINTS):
        return "blocked", f"{kind}: {msg}"
    if kind in _UNREACHABLE_NAMES:
        return "unreachable", f"{kind}: {msg}"
    return "unreachable", f"{kind}: {msg}"


def _attempt(backend, outcome, detail="", tries=1):
    return {"backend": backend, "outcome": outcome, "detail": detail,
            "tries": tries}


def _via_api(vid, attempts=3):
    """Returns (result_dict | None, attempt_record).

    The attempt record always says what happened, even on success, so the
    caller can log a real diagnosis instead of "failed via none".
    """
    name = "youtube-transcript-api"
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as e:
        return None, _attempt(name, "missing_dependency",
                              f"not installed ({e}); pip install youtube-transcript-api",
                              tries=0)

    delay = 2
    last = ("unreachable", "no attempt made")
    for attempt in range(attempts):
        try:
            fetched = YouTubeTranscriptApi().fetch(vid, languages=list(LANGS))
        except Exception as e:
            outcome, detail = _classify_exc(e)
            last = (outcome, detail)
            if outcome in _PERMANENT:
                # Definitive answer from YouTube -- do not retry, but let
                # yt-dlp have a shot at captions the API cannot see.
                return None, _attempt(name, outcome, detail, attempt + 1)
            if attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
            continue
        try:
            segments, parts = [], []
            for sn in fetched:
                t = float(getattr(sn, "start", 0.0) or 0.0)
                txt = (getattr(sn, "text", "") or "").replace("\n", " ").strip()
                if not txt:
                    continue
                segments.append({"t": round(t, 2), "text": txt})
                parts.append(txt)
        except Exception as e:
            # Parsing our own result failed -> that is our bug, not YouTube's.
            return None, _attempt(name, "code_error",
                                  f"{type(e).__name__}: {str(e)[:160]}", attempt + 1)
        if not parts:
            return None, _attempt(name, "no_captions",
                                  "API returned an empty caption track", attempt + 1)
        return (_finish(" ".join(parts), segments, name),
                _attempt(name, "ok", f"{len(segments)} segments", attempt + 1))

    return None, _attempt(name, last[0], last[1], attempts)


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


_GONE_HINTS = ("video unavailable", "is not a valid url", "incomplete youtube id",
               "private video", "does not exist", "removed by the uploader",
               "has been terminated", "account associated with this video")


def _ytdlp_installed():
    """True if `python -m yt_dlp` can actually run in THIS interpreter.

    Checked separately because a missing module makes yt-dlp exit non-zero
    exactly like a YouTube failure would -- which is how a completely
    uninstalled fallback masqueraded as a network problem for weeks.
    """
    try:
        import importlib.util
        return importlib.util.find_spec("yt_dlp") is not None
    except Exception:
        return False


def _via_ytdlp(vid, attempts=2):
    """Returns (result | None, attempt_record)."""
    name = "yt-dlp"
    if not _ytdlp_installed():
        return None, _attempt(name, "missing_dependency",
                              "yt_dlp module not importable; pip install yt-dlp",
                              tries=0)

    url = f"https://www.youtube.com/watch?v={vid}"
    delay = 3
    last = ("unreachable", "no attempt made")
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
                r = None
                last = ("unreachable", "yt-dlp timed out after 180s")
            except Exception as e:
                return None, _attempt(name, "code_error",
                                      f"could not launch: {type(e).__name__}: {str(e)[:140]}",
                                      attempt + 1)

            files = sorted(pathlib.Path(td).glob("*.vtt"))
            if files:
                raw = files[0].read_text(encoding="utf-8", errors="replace")
                segments = parse_vtt(raw)
                if segments:
                    return (_finish(" ".join(s["text"] for s in segments),
                                    segments, name),
                            _attempt(name, "ok", f"{len(segments)} segments",
                                     attempt + 1))
                last = ("code_error", f"VTT written but parsed to 0 segments ({files[0].name})")
                return None, _attempt(name, *last, tries=attempt + 1)

            if r is not None:
                err = " ".join(((r.stderr or "") + " " + (r.stdout or "")).split())
                low = err.lower()
                if any(s in low for s in _GONE_HINTS):
                    return None, _attempt(name, "video_gone", err[:200], attempt + 1)
                if any(h in low for h in _BLOCK_HINTS):
                    return None, _attempt(name, "blocked", err[:200], attempt + 1)
                if r.returncode == 0:
                    # Reached YouTube fine, it just has no subtitles here.
                    return None, _attempt(name, "no_captions",
                                          "yt-dlp exited 0 with no subtitle file",
                                          attempt + 1)
                last = ("unreachable", f"exit {r.returncode}: {err[:200]}" if err
                        else f"exit {r.returncode} with no output")

            if attempt < attempts - 1:
                time.sleep(delay)
                delay *= 2
    return None, _attempt(name, last[0], last[1], attempts)


# ------------------------------------------------------------- public api ----
def describe(res: dict) -> str:
    """One-line human diagnosis of a fetch_transcript result."""
    head = (f"{res['status']} via {res['source']} "
            f"({len(res['text'])} chars, {len(res['segments'])} segments)")
    reason = res.get("reason")
    if res["status"] in ("ok", "too_long"):
        return head
    return f"{head} — {reason}: {res.get('detail', '')}".rstrip(" —:")


def attempt_lines(res: dict):
    """Per-backend log lines: what was tried, and what each one said."""
    out = []
    for a in res.get("attempts", []):
        detail = a.get("detail") or ""
        tries = a.get("tries", 1)
        suffix = f" after {tries} tries" if tries > 1 else ("" if tries else " (not attempted)")
        out.append(f"{a['backend']}: {a['outcome']}{suffix}"
                   + (f" — {detail}" if detail else ""))
    return out


def fetch_transcript(video_url_or_id: str) -> dict:
    """Return {text, status, segments, source, reason, detail, attempts}.

    status: ok | none | failed | too_long
      ok       -- captions retrieved
      too_long -- captions retrieved but truncated to MAX_CHARS
      none     -- YouTube answered; this video has no captions (or is gone).
                  PERMANENT and normal. Safe to stop retrying.
      failed   -- we did not get an answer we can trust. TRANSIENT or an
                  environment problem. Retry later; never record as "none".

    `reason` explains a non-ok status in one word, and is the field that makes
    the difference actionable:
      no_captions | video_gone         -> nothing is broken, this is the truth
      blocked                          -> YouTube refused this IP (datacenter)
      unreachable                      -> network/transport failure
      missing_dependency               -> no backend is even installed here
      code_error                       -> our bug
      bad_url                          -> we were handed an unparseable url

    Never invents or approximates a transcript.
    """
    vid = video_id(video_url_or_id)
    if not vid:
        return {"text": "", "status": "failed", "segments": [], "source": "none",
                "reason": "bad_url", "detail": f"unparseable video url/id: {video_url_or_id!r}",
                "attempts": []}

    attempts = []
    for backend in (_via_api, _via_ytdlp):
        res, rec = backend(vid)
        attempts.append(rec)
        if res:
            res["reason"] = "ok"
            res["detail"] = ""
            res["attempts"] = attempts
            return res

    outcomes = [a["outcome"] for a in attempts]

    def pick(outcome):
        return next((a for a in attempts if a["outcome"] == outcome), None)

    # Precedence matters. An environment/transport problem ALWAYS outranks a
    # "no captions" verdict from the other backend, because a wrong `none` is
    # written to the DB permanently and the video is never retried.
    if all(o in _ENVIRONMENT for o in outcomes):
        chosen, status = pick("missing_dependency"), "failed"
    elif "blocked" in outcomes:
        chosen, status = pick("blocked"), "failed"
    elif "code_error" in outcomes:
        chosen, status = pick("code_error"), "failed"
    elif "unreachable" in outcomes:
        chosen, status = pick("unreachable"), "failed"
    elif "video_gone" in outcomes:
        chosen, status = pick("video_gone"), "none"
    elif "no_captions" in outcomes:
        chosen, status = pick("no_captions"), "none"
    else:
        chosen, status = None, "failed"

    reason = chosen["outcome"] if chosen else "unknown"
    detail = chosen["detail"] if chosen else "no backend produced a verdict"
    if reason == "missing_dependency":
        detail = ("no transcript backend is installed in this environment "
                  "(" + "; ".join(a["detail"] for a in attempts if a["detail"]) + ")")
    return {"text": "", "status": status, "segments": [], "source": "none",
            "reason": reason, "detail": detail, "attempts": attempts}


def backend_report() -> str:
    """Which backends this environment can actually use. Printed up front so a
    dependency-less runner is obvious in the first line of the log."""
    bits = []
    try:
        import youtube_transcript_api  # noqa: F401
        bits.append("youtube-transcript-api=yes")
    except ImportError:
        bits.append("youtube-transcript-api=MISSING")
    bits.append(f"yt-dlp={'yes' if _ytdlp_installed() else 'MISSING'}")
    return "  ".join(bits)


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

    print(f"Backfilling transcripts for {len(rows)} intel_items rows")
    print(f"backends available: {backend_report()}\n")
    tally, reasons, chars = {}, {}, 0
    for row in rows:
        title = (row.get("title") or "")[:70]
        print(f"  [{row['id']}] {title}")
        res = fetch_transcript(row.get("url") or "")
        tally[res["status"]] = tally.get(res["status"], 0) + 1
        if res["status"] not in ("ok", "too_long"):
            reasons[res["reason"]] = reasons.get(res["reason"], 0) + 1
        chars += len(res["text"])
        print(f"      {describe(res)}")
        for line in attempt_lines(res):
            print(f"        - {line}")
        if dry_run:
            continue
        patch = {"transcript_status": res["status"]}
        if res["status"] in ("ok", "too_long"):
            patch["transcript"] = res["text"]
        elif res["status"] == "none":
            # Mark it so it isn't re-attempted forever. Empty string, not null,
            # so the `transcript is null` filter stops picking it up.
            # Only reached for no_captions / video_gone -- a genuine, permanent
            # YouTube answer. A block or a missing library never lands here.
            patch["transcript"] = ""
        else:
            # failed -> leave transcript NULL so a later run retries it.
            pass
        pr = sb_request("PATCH", f"{url}/rest/v1/intel_items", headers=h,
                        params={"id": f"eq.{row['id']}"}, json=patch)
        if pr is None or not pr.ok:
            print(f"      WARNING: write-back failed "
                  f"({pr.status_code if pr is not None else 'no response'})")

    got = tally.get("ok", 0) + tally.get("too_long", 0)
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    if reasons:
        print("failure reasons: " + "  ".join(f"{k}={v}" for k, v in sorted(reasons.items())))
    print(f"transcript chars fetched: {chars}")

    # ZERO-YIELD IS A FAILURE, NOT A QUIET SUCCESS.
    # This project has been burned by a step exiting 0 having produced nothing.
    # If we attempted rows and not one transcript came back, and the reason is
    # anything other than "these videos genuinely have no captions", the step
    # must go red so the operator sees it.
    if got == 0:
        env_or_transient = sum(v for k, v in reasons.items()
                               if k not in ("no_captions", "video_gone"))
        summary = "  ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
        if env_or_transient:
            print(f"\nZERO-YIELD FAILURE: {len(rows)} video(s) attempted, 0 transcripts "
                  f"retrieved, and {env_or_transient} of them failed for a reason that is "
                  f"NOT 'this video has no captions' ({summary}).")
            if reasons.get("missing_dependency"):
                print("  -> No transcript backend is installed in this environment. "
                      "Install them where this runs:\n"
                      "     pip install youtube-transcript-api yt-dlp")
            if reasons.get("blocked"):
                print("  -> YouTube refused this IP. Hosted CI runners (GitHub Actions) "
                      "sit in datacenter ranges that YouTube blocks; there is no free fix "
                      "from a hosted runner. Run this leg locally or via a residential "
                      "egress, and treat these rows as unfetched (they stay NULL and retry).")
            print("  Downstream proposals from these videos would be based on NO "
                  "transcript. That is not a clean zero.")
            sys.exit(1)
        print(f"\n0 transcripts retrieved, but all {len(rows)} video(s) genuinely have no "
              f"captions ({summary}). That is a real, permanent zero -- not an error.")
    return


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
        print(f"backends: {backend_report()}")
        print(f"status : {res['status']}")
        print(f"reason : {res.get('reason')}")
        if res.get("detail"):
            print(f"detail : {res['detail']}")
        print(f"source : {res['source']}")
        print(f"chars  : {len(res['text'])}   segments: {len(res['segments'])}")
        for line in attempt_lines(res):
            print(f"  tried  {line}")
        if res["segments"][:1]:
            s = res["segments"][0]
            print(f"first  : [{s['t']}s] {s['text'][:80]}")
        print("-" * 60)
        print(res["text"][:400])
        # Non-zero exit on a non-permanent failure so callers/CI can react.
        if res["status"] == "failed":
            sys.exit(2)
        return

    if args.backfill:
        backfill(args.backfill, dry_run=args.dry_run)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
