#!/usr/bin/env python3
"""categorize_raw.py -- free-model categorizer for leads_raw.

Collection and loading are done (see load_raw.py). This is the judgment step:
it reads unjudged rows (category IS NULL) from Supabase leads_raw, asks a free
worker model (via ghl-cli/llm_router.py) to sort each one into
consumer_lead / partner / competitor / noise, and writes the verdict back.

Reuses ai_qualify.py's proven quote guarantee: a consumer_lead or partner
verdict REQUIRES a verbatim substring quote from the row's own title/body, or
it is downgraded. Never weaken that guard.

Freshness is computed in PYTHON, not trusted to the model: event_date in the
past can never be urgency 'now'. This is the exact lesson that cost a day --
an 18 day stale post passed every filter because nothing checked its age.

Usage:
    python categorize_raw.py --client "Hero's Junk Removal" --limit 20
    python categorize_raw.py --client "Hero's Junk Removal" --limit 20 --confirm
    python categorize_raw.py --client "Hero's Junk Removal" --limit 20 --confirm --rejudge

--dry-run behavior is the DEFAULT (mirrors load_raw.py's --confirm gate):
a bare run judges nothing in the database. Pass --confirm to write.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

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
except Exception as e:
    llm_router = None
    ROUTER_IMPORT_ERROR = str(e)

MODEL_ALIAS = os.environ.get("AI_CATEGORIZE_MODEL", "json")

ALLOWED_CATEGORIES = {"consumer_lead", "partner", "competitor", "noise"}
ALLOWED_URGENCY = {"now", "dated", "someday"}
ACTIONABLE = {"consumer_lead", "partner"}

# Credentials live in the OS repo's env file, same pattern as load_raw.py.
ENV_CANDIDATES = [
    Path(r"C:\Users\wjack\wing-digital-os\.env.local"),
    Path(__file__).resolve().parents[2] / "wing-digital-os" / ".env.local",
]

SYSTEM_PROMPT = """You are a strict categorizer for a local service business's \
incoming social/web posts. You are shown ONE post plus its dates and a client's \
market description. Sort it into exactly one category:

- consumer_lead: a real INDIVIDUAL who personally wants to HIRE this service.
- partner: a BUSINESS that needs this service repeatedly, not a one-off. \
Example: an estate sale company that runs sales most weekends needs a hauler \
most weekends. Treat a real repeat-need business as a partner, not a failed \
lead, and often a better one than a one-time consumer.
- competitor: someone offering or advertising the SAME service.
- noise: anything else -- unrelated items, general discussion, no real need.

You must be skeptical by default. Categorize consumer_lead or partner ONLY if \
you can quote the poster's own words as proof.

For consumer_lead the quote must show a real, specific personal need to hire.

For partner, the quote only needs to show this post IS a listing for a \
repeat-need business doing the kind of event/job that generates this service's \
work over and over -- for example an estate sale company's name, or the fact \
the post is an estate sale / liquidation / moving-sale listing with a business \
running it. You do NOT need the business to explicitly ask for hauling; the \
nature of their recurring business (estate sales, moving companies, property \
managers, etc.) IS the evidence. If the post is just an individual giving away \
or selling one item with no business running it, that is noise or consumer_lead \
context, not partner.

If you cannot find real supporting words for either, use competitor or noise \
instead.

Also decide urgency:
- "now": they want this immediately, no future date implied.
- "dated": a specific future date/event is given (e.g. an estate sale date).
- "someday": vague future interest, no real date.
- null: cannot be determined.
Do NOT do date arithmetic yourself with confidence -- if dates are given in the
post, you may reference them, but the caller will enforce freshness in code.

You must respond with ONLY a single JSON object, no prose, no markdown fences:
{
  "category": "consumer_lead" or "partner" or "competitor" or "noise",
  "urgency": "now" or "dated" or "someday" or null,
  "confidence": 0.0 to 1.0,
  "reason": "one short sentence",
  "quote": "exact substring copied character-for-character from the title or body, or empty string if none"
}
The "quote" field MUST be copied verbatim from the text given to you. If you \
cannot find real supporting words, set quote to "".
"""

USER_TEMPLATE = """CLIENT CONTEXT
trade: {trade}
service area (cities): {cities}
services offered: {services}

POST
platform: {platform}
url: {url}
title: {title}
body:
{body}

DATES (computed, trust these over your own arithmetic)
posted_at: {posted_at}
event_date: {event_date}
today: {today}
days_since_posted: {days_since_posted}
event_date_is_past: {event_date_is_past}
event_date_is_future: {event_date_is_future}

Decide category/urgency/confidence/reason/quote now. Reply with the JSON object only."""


def load_env() -> tuple[str, str]:
    for p in ENV_CANDIDATES:
        if not p.exists():
            continue
        url = key = ""
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("SONAR_SUPABASE_URL="):
                url = line.split("=", 1)[1].strip()
            elif line.startswith("SONAR_SUPABASE_SERVICE_KEY="):
                key = line.split("=", 1)[1].strip()
        if url and key:
            return url, key
    sys.exit("Could not find SONAR_SUPABASE_URL / SONAR_SUPABASE_SERVICE_KEY. "
             "Looked in: " + ", ".join(str(p) for p in ENV_CANDIDATES))


def _parse_dt(raw):
    if not raw:
        return None
    try:
        s = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def freshness_facts(row):
    """Compute all date comparisons in Python. Never trust the model with this."""
    now = datetime.now(timezone.utc)
    posted = _parse_dt(row.get("posted_at"))
    event = _parse_dt(row.get("event_date"))
    days_since_posted = (now - posted).days if posted else None
    event_is_past = (event < now) if event else None
    event_is_future = (event >= now) if event else None
    return {
        "today": now.date().isoformat(),
        "days_since_posted": days_since_posted,
        "event_date_is_past": event_is_past,
        "event_date_is_future": event_is_future,
    }


def _fetch_rows(base_url, key, client, limit, rejudge):
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    params = {
        "select": "*",
        "client": f"eq.{client}",
        "order": "collected_at.asc",
        "limit": str(limit),
    }
    if not rejudge:
        params["category"] = "is.null"
    r = requests.get(f"{base_url}/rest/v1/leads_raw", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _patch_row(base_url, key, row_id, fields):
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    r = requests.patch(f"{base_url}/rest/v1/leads_raw?id=eq.{row_id}",
                        headers=headers, data=json.dumps(fields), timeout=30)
    return r.ok, r.status_code, (r.text or "")[:200]


def _parse_verdict(raw_output, post_text):
    """Parse model JSON and enforce the quote guarantee. Returns dict or None."""
    if raw_output is None:
        return None
    text = raw_output.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except Exception:
        return None

    category = str(data.get("category") or "").strip()
    urgency = data.get("urgency")
    urgency = str(urgency).strip() if urgency else None
    try:
        confidence = float(data.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(data.get("reason") or "").strip()
    quote = str(data.get("quote") or "").strip()

    if category not in ALLOWED_CATEGORIES:
        return None  # unparseable verdict, not a valid category -> not_assessed
    if urgency not in ALLOWED_URGENCY:
        urgency = None

    # THE GUARD: consumer_lead/partner must carry a verbatim, verified quote.
    quote_verified = bool(quote) and quote in post_text
    if category in ACTIONABLE and not quote_verified:
        category = "noise"
        reason = (reason + " [downgraded: quote not verified in source text]").strip()
        urgency = None

    return {
        "category": category,
        "urgency": urgency,
        "confidence": confidence,
        "reason": reason or "no reason given",
        "quote": quote if quote_verified else "",
    }


def categorize_one(row, client_context, model_alias=MODEL_ALIAS):
    post_text = ((row.get("title") or "") + "\n" + (row.get("body") or "")).strip()
    facts = freshness_facts(row)

    if llm_router is None:
        return {"judge_status": "not_assessed", "category": None, "urgency": None,
                "confidence": 0.0, "reason": f"llm_router.py not importable: {ROUTER_IMPORT_ERROR}",
                "quote": None}

    if not (row.get("title") or row.get("body")):
        return {"judge_status": "not_assessed", "category": None, "urgency": None,
                "confidence": 0.0, "reason": "row has no title/body to assess", "quote": None}

    prompt = USER_TEMPLATE.format(
        trade=client_context.get("trade", "unspecified"),
        cities=", ".join(client_context.get("cities", []) or ["unspecified"]),
        services=", ".join(client_context.get("services", []) or ["unspecified"]),
        platform=row.get("platform") or "",
        url=row.get("url") or "",
        title=row.get("title") or "",
        body=(row.get("body") or "")[:4000],
        posted_at=row.get("posted_at"),
        event_date=row.get("event_date"),
        today=facts["today"],
        days_since_posted=facts["days_since_posted"],
        event_date_is_past=facts["event_date_is_past"],
        event_date_is_future=facts["event_date_is_future"],
    )

    res = llm_router.generate(
        model_alias, prompt, system=SYSTEM_PROMPT,
        temperature=0.1, max_tokens=400, force_json=True, retries=2,
    )

    if "error" in res:
        return {"judge_status": "not_assessed", "category": None, "urgency": None,
                "confidence": 0.0, "reason": f"model unreachable: {res['error'][:200]}",
                "quote": None}

    verdict = _parse_verdict(res.get("output"), post_text)
    if verdict is None:
        return {"judge_status": "not_assessed", "category": None, "urgency": None,
                "confidence": 0.0, "reason": "model output was unparseable or invalid category",
                "quote": None}

    # HARD ENFORCEMENT IN PYTHON: an event_date in the past can never be 'now'.
    if facts["event_date_is_past"] and verdict["urgency"] == "now":
        verdict["urgency"] = "someday"
        verdict["reason"] = (verdict["reason"] +
                              " [urgency corrected: event_date is in the past]").strip()

    return {
        "judge_status": "judged",
        "category": verdict["category"],
        "urgency": verdict["urgency"],
        "confidence": verdict["confidence"],
        "reason": verdict["reason"],
        "quote": verdict["quote"] or None,
    }


def main():
    ap = argparse.ArgumentParser(description="Free-model categorizer for leads_raw")
    ap.add_argument("--client", required=True, help="client label, matches leads_raw.client exactly")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--trade", default="unspecified")
    ap.add_argument("--cities", nargs="*", default=[])
    ap.add_argument("--services", nargs="*", default=[])
    ap.add_argument("--model", default=MODEL_ALIAS)
    ap.add_argument("--rejudge", action="store_true",
                    help="re-judge rows that already have a category")
    ap.add_argument("--confirm", action="store_true",
                    help="actually write verdicts to Supabase. Default is dry-run.")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit alias for the default (no writes)")
    a = ap.parse_args()

    base_url, key = load_env()
    rows = _fetch_rows(base_url, key, a.client, a.limit, a.rejudge)

    print(f"[categorize_raw] fetched {len(rows)} rows for client={a.client!r} "
          f"(rejudge={a.rejudge})", file=sys.stderr)

    if not rows:
        print("[categorize_raw] nothing to judge.", file=sys.stderr)
        return 0

    ctx = {"trade": a.trade, "cities": a.cities, "services": a.services}
    updates = []
    counts = {"consumer_lead": 0, "partner": 0, "competitor": 0, "noise": 0, "not_assessed": 0}
    now_iso = datetime.now(timezone.utc).isoformat()

    for row in rows:
        verdict = categorize_one(row, ctx, model_alias=a.model)
        if verdict["judge_status"] == "not_assessed":
            counts["not_assessed"] += 1
            fields = {
                "judge_status": "not_assessed",
                "reason": verdict["reason"],
                "judged_at": now_iso,
            }
        else:
            counts[verdict["category"]] += 1
            fields = {
                "category": verdict["category"],
                "urgency": verdict["urgency"],
                "confidence": verdict["confidence"],
                "reason": verdict["reason"],
                "quote": verdict["quote"],
                "judge_status": "judged",
                "judged_at": now_iso,
            }
        updates.append((row, fields))

    do_write = a.confirm and not a.dry_run

    print(f"[categorize_raw] verdicts -- consumer_lead:{counts['consumer_lead']} "
          f"partner:{counts['partner']} competitor:{counts['competitor']} "
          f"noise:{counts['noise']} not_assessed:{counts['not_assessed']}",
          file=sys.stderr)

    if not do_write:
        print("[categorize_raw] DRY RUN. Nothing was written. Pass --confirm to write.",
              file=sys.stderr)
        for row, fields in updates:
            tag = fields.get("category") or fields["judge_status"]
            print(f"    would update id={row['id']} -> {tag} :: "
                  f"{(row.get('title') or '(no title)')[:60]}", file=sys.stderr)
        return 0

    written = failed = 0
    for row, fields in updates:
        ok, status, body = _patch_row(base_url, key, row["id"], fields)
        if ok:
            written += 1
        else:
            failed += 1
            print(f"[categorize_raw] PATCH failed for id={row['id']} HTTP {status}: {body}",
                  file=sys.stderr)

    print(f"[categorize_raw] wrote {written}, failed {failed}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
