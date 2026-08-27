#!/usr/bin/env python3
"""ai_qualify.py -- free-model lead qualifier for scraped social/web candidates.

Python found volume. This file is the judgment pass: a FREE worker model
(via ghl-cli/llm_router.py) reads each candidate and decides whether it is a
real person who wants this service, in this client's market, right now.
Qualified candidates are ranked best-first. Nothing here sends, DMs, posts,
or contacts anyone -- it only reads and judges.

Import and call qualify_candidates(candidates, client_context) from another
module, or run standalone:

    python ai_qualify.py --in candidates.jsonl --out verdicts.jsonl \
        --trade "junk removal" --cities Dallas Fort Worth --services hauling cleanout

Guardrails (do not weaken these):
  - A verdict with no exact quote from the candidate's own text is UNUSABLE
    and is forced to qualify=false. This is the guard against invented reasons.
  - If the model is unreachable, rate limited, or returns unparseable output,
    the candidate comes back status="not_assessed", never qualified. A
    not_assessed candidate is NOT the same as a rejected one -- it just means
    nobody has actually looked yet.
  - Every candidate keeps a stable identity (its URL). Once a URL has a
    verdict on disk, a rerun reuses it instead of re-spending a model call.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ROUTER_DIR = os.environ.get("LLM_ROUTER_DIR", r"C:\Users\wjack\ghl-cli")
if ROUTER_DIR not in sys.path:
    sys.path.insert(0, ROUTER_DIR)

try:
    import llm_router  # noqa: E402
    ROUTER_IMPORT_ERROR = None
except Exception as e:  # router missing or broken -- fail soft, not silent
    llm_router = None
    ROUTER_IMPORT_ERROR = str(e)

DEFAULT_CACHE = HERE / "ai_qualify_cache.jsonl"
MODEL_ALIAS = os.environ.get("AI_QUALIFY_MODEL", "json")  # tight/cheap JSON worker

SYSTEM_PROMPT = """You are a strict lead qualifier for a local service business. \
You are shown ONE social/forum post and a client's market description. Decide \
whether the poster is a real person currently wanting to HIRE this exact \
service, in this client's actual market. You must be skeptical by default.

FAIL the post (qualify=false) if:
- it is outside the client's stated cities/region (a Dallas client does not
  care about a post in Oklahoma City, or any city not near their market)
- the poster is asking a marketing/advertising/business question, not asking
  for the service itself (e.g. "how do I run Google Ads for my company" is
  NOT a lead for a junk removal company)
- the poster is a competitor, a business advertising itself, or someone
  SELLING rather than buying
- it is news, general discussion, or a question with no request for service
- you cannot find a specific phrase in the post that supports qualifying it

PASS the post (qualify=true) only if a real individual is describing a need
for this service, in this market, and you can quote their own words as proof.

You must respond with ONLY a single JSON object, no prose, no markdown fences:
{
  "qualify": true or false,
  "confidence": 0.0 to 1.0,
  "reason": "one short sentence",
  "quote": "exact substring copied character-for-character from the post, or empty string if none"
}
The "quote" field MUST be copied verbatim from the post text given to you. If
you cannot find real supporting words in the post, set qualify to false and
quote to "".
"""

USER_TEMPLATE = """CLIENT CONTEXT
trade: {trade}
service area (cities): {cities}
services offered: {services}

CANDIDATE POST
source: {source}
section/subreddit: {section}
url: {url}
title: {title}
body:
{body}

Decide qualify/confidence/reason/quote for this candidate now. Reply with the JSON object only."""


def _candidate_id(cand):
    url = (cand.get("url") or cand.get("source_url") or "").strip()
    if url:
        return url
    # no URL at all -- hash title+body so we still dedupe something stable
    raw = (cand.get("title", "") + "|" + (cand.get("body") or cand.get("snippet") or ""))
    return "nourl:" + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


def _candidate_text(cand):
    return (cand.get("body") or cand.get("snippet") or cand.get("text") or "").strip()


def _load_cache(cache_path):
    cache = {}
    p = Path(cache_path)
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                cid = row.get("candidate_id")
                if cid:
                    cache[cid] = row
            except Exception:
                continue
    return cache


def _append_cache(cache_path, row):
    with open(cache_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _parse_verdict(raw_output, post_text):
    """Parse model JSON and enforce the quote guarantee. Returns a dict or None
    (None means unparseable -> caller treats as not_assessed)."""
    if raw_output is None:
        return None
    text = raw_output.strip()
    # strip accidental markdown fences some free models still add
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    # tolerate leading/trailing prose by grabbing the first {...} block
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except Exception:
        return None

    qualify = bool(data.get("qualify", False))
    try:
        confidence = float(data.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(data.get("reason") or "").strip()
    quote = str(data.get("quote") or "").strip()

    # THE GUARD: a qualify=true verdict must carry a quote that actually
    # appears in the candidate's own text. No exact match -> forced unqualified.
    quote_verified = bool(quote) and quote in post_text
    if qualify and not quote_verified:
        qualify = False
        reason = (reason + " [downgraded: quote not verified in source text]").strip()

    return {
        "qualify": qualify,
        "confidence": confidence,
        "reason": reason or ("no reason given" if not qualify else reason),
        "quote": quote if quote_verified else "",
        "quote_verified": quote_verified,
    }


def qualify_one(candidate, client_context, model_alias=MODEL_ALIAS):
    """Call the free model for a single candidate. Returns a verdict dict.

    status is one of: "qualified", "unqualified", "not_assessed".
    "not_assessed" is used whenever the model could not be reached or its
    output could not be parsed -- it is never collapsed into "unqualified".
    """
    post_text = _candidate_text(candidate)
    cid = _candidate_id(candidate)

    if llm_router is None:
        return {
            "candidate_id": cid,
            "status": "not_assessed",
            "qualify": False,
            "confidence": 0.0,
            "reason": f"llm_router.py not importable: {ROUTER_IMPORT_ERROR}",
            "quote": "",
            "calls_made": 0,
        }

    if not post_text:
        return {
            "candidate_id": cid,
            "status": "not_assessed",
            "qualify": False,
            "confidence": 0.0,
            "reason": "candidate has no body/snippet text to assess",
            "quote": "",
            "calls_made": 0,
        }

    prompt = USER_TEMPLATE.format(
        trade=client_context.get("trade", "unspecified"),
        cities=", ".join(client_context.get("cities", []) or ["unspecified"]),
        services=", ".join(client_context.get("services", []) or ["unspecified"]),
        source=candidate.get("source", ""),
        section=candidate.get("subreddit") or candidate.get("section") or "",
        url=candidate.get("url") or candidate.get("source_url") or "",
        title=candidate.get("title", ""),
        body=post_text[:4000],
    )

    res = llm_router.generate(
        model_alias, prompt, system=SYSTEM_PROMPT,
        temperature=0.1, max_tokens=400, force_json=True, retries=2,
    )

    if "error" in res:
        return {
            "candidate_id": cid,
            "status": "not_assessed",
            "qualify": False,
            "confidence": 0.0,
            "reason": f"model unreachable: {res['error'][:200]}",
            "quote": "",
            "calls_made": 1,
            "model": res.get("model", model_alias),
        }

    verdict = _parse_verdict(res.get("output"), post_text)
    if verdict is None:
        return {
            "candidate_id": cid,
            "status": "not_assessed",
            "qualify": False,
            "confidence": 0.0,
            "reason": "model output was unparseable JSON",
            "quote": "",
            "calls_made": 1,
            "model": res.get("model", model_alias),
            "raw_output": (res.get("output") or "")[:500],
        }

    return {
        "candidate_id": cid,
        "status": "qualified" if verdict["qualify"] else "unqualified",
        "qualify": verdict["qualify"],
        "confidence": verdict["confidence"],
        "reason": verdict["reason"],
        "quote": verdict["quote"],
        "calls_made": 1,
        "model": res.get("model", model_alias),
    }


def qualify_candidates(candidates, client_context, cache_path=DEFAULT_CACHE,
                        model_alias=MODEL_ALIAS, use_cache=True):
    """Qualify a list of candidate dicts against client_context.

    client_context: {"trade": str, "cities": [str], "services": [str]}

    Returns (ranked_results, stats). ranked_results is the full input list of
    candidates, each merged with its verdict, sorted so qualified-and-most-
    confident comes first, then unqualified, then not_assessed last. Rerunning
    with the same cache_path skips any candidate URL already verdicted.
    """
    cache = _load_cache(cache_path) if use_cache else {}
    results = []
    calls_made = 0
    t0 = time.time()

    for cand in candidates:
        cid = _candidate_id(cand)
        if use_cache and cid in cache:
            verdict = cache[cid]
        else:
            verdict = qualify_one(cand, client_context, model_alias=model_alias)
            calls_made += verdict.get("calls_made", 0)
            if use_cache:
                _append_cache(cache_path, verdict)
                cache[cid] = verdict
        merged = dict(cand)
        merged.update(verdict)
        results.append(merged)

    def sort_key(r):
        status_rank = {"qualified": 0, "unqualified": 1, "not_assessed": 2}.get(r["status"], 3)
        return (status_rank, -r.get("confidence", 0.0))

    results.sort(key=sort_key)

    stats = {
        "total": len(candidates),
        "qualified": sum(1 for r in results if r["status"] == "qualified"),
        "unqualified": sum(1 for r in results if r["status"] == "unqualified"),
        "not_assessed": sum(1 for r in results if r["status"] == "not_assessed"),
        "model_calls_made": calls_made,
        "elapsed_seconds": round(time.time() - t0, 1),
    }
    return results, stats


def _load_candidates_file(path):
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix == ".jsonl" or "\n{" in text.strip()[:2] + text.strip()[1:2] or (
        text.strip().startswith("{") and "\n" in text.strip()
    ):
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        if rows:
            return rows
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


def main():
    ap = argparse.ArgumentParser(description="Free-model lead qualifier")
    ap.add_argument("--in", dest="in_path", required=True, help="candidates JSON or JSONL file")
    ap.add_argument("--out", dest="out_path", help="write ranked verdicts JSONL here")
    ap.add_argument("--trade", default="unspecified")
    ap.add_argument("--cities", nargs="*", default=[])
    ap.add_argument("--services", nargs="*", default=[])
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    ap.add_argument("--model", default=MODEL_ALIAS)
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args()

    candidates = _load_candidates_file(a.in_path)
    ctx = {"trade": a.trade, "cities": a.cities, "services": a.services}
    results, stats = qualify_candidates(
        candidates, ctx, cache_path=a.cache, model_alias=a.model,
        use_cache=not a.no_cache,
    )

    print(f"[ai_qualify] {stats['total']} candidates -> "
          f"{stats['qualified']} qualified, {stats['unqualified']} unqualified, "
          f"{stats['not_assessed']} not_assessed "
          f"({stats['model_calls_made']} model calls, {stats['elapsed_seconds']}s)",
          file=sys.stderr)

    if a.out_path:
        with open(a.out_path, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"[ai_qualify] wrote {a.out_path}", file=sys.stderr)
    else:
        for r in results:
            if r["status"] == "qualified":
                print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
