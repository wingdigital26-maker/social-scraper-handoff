#!/usr/bin/env python3
"""
draft_from_leads.py -- turn a JUDGED lead in leads_raw into a drafted outbound
message sitting in Jack's inbox (the `outbound` table, status='draft').

This is the ONLY new step between "collection judged a lead as actionable"
and "a human reviews a message". It never sends anything. Every message it
writes still needs a human click in the OS before it goes anywhere.

Pipeline this sits in:
    source CLI -> load_raw.py -> leads_raw            (collection, done)
    categorize_raw.py / ai_qualify.py -> leads_raw     (judgement, done)
    draft_from_leads.py -> outbound                    (THIS FILE)
    (human reviews in the OS, some other tool sends)   (not built here)

Design notes, deliberate not incidental:

  * DRY RUN BY DEFAULT. A bare run prints what it would draft and writes
    nothing. --confirm writes.

  * Dedup by evidence_url. Before drafting anything for a client, every
    existing outbound.evidence_url for that client is pulled and any
    leads_raw.url already present there is skipped. Re-running never
    duplicates a draft.

  * The lead's own words are load bearing. `quote` was substring-verified
    against the source post/listing by the judge upstream. If a row has no
    usable quote, or no real contact path, NO draft is written for it. A
    generic message that could have been written without reading the post
    is worse than no message -- an empty queue is a valid, honest result.

  * Two message shapes, because they are two different conversations:
      consumer_lead -> short, human, one job, help with the thing they said.
      partner       -> a standing-relationship pitch to a business that needs
                        this service repeatedly (e.g. an estate sale company
                        running sales most weekends needs a hauler every
                        weekend). References the company's real name and
                        real upcoming date when the row has one.

  * check_voice() from client_voice.py (not modified, only imported) gates
    every generated message before it is queued. No em dashes, no hype
    words, no fake urgency, no pricing claims, no fabricated detail about
    the recipient. A message that fails the gate is dropped, not sent
    anyway with a warning.

  * No client name, city, or trade is hardcoded anywhere below. Everything
    client-specific comes from the leads_raw row or from client_voice's
    per-client profile (voices/*.json), looked up by matching the row's
    `client` string against each profile's display_name.

  * channel/recipient are populated only from what the row actually proves
    the prospect can be reached by: a phone number found in the row's own
    text, or a platform handle the collector captured. Nothing is invented.
    tier is a straight passthrough of the judge's own urgency, never
    re-guessed here.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client_voice as cv  # noqa: E402  (do not modify client_voice.py)

ENV_CANDIDATES = [
    Path(r"C:\Users\wjack\wing-digital-os\.env.local"),
    Path(__file__).resolve().parents[2] / "wing-digital-os" / ".env.local",
]

ACTIONABLE_CATEGORIES = ("consumer_lead", "partner")

URGENCY_RANK = {"high": 0, "urgent": 0, "medium": 1, "normal": 1, "low": 2}

_PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")

# ---------------------------------------------------------------------------
# RECENCY GATE
#
# Until now this file had no age check at all, so a 2016 Reddit thread was as
# draftable as one from this morning. Worse, the only date most rows carried was
# collected_at -- when WE looked, not when the thing happened -- so everything
# read as two days old regardless of true age.
#
# A lead's shelf life is a property of what KIND of lead it is, so the threshold
# is per category and source rather than one global number:
#
#   consumer_lead, 45 days
#       Someone asking "who do you recommend for junk removal" has hired
#       somebody within a few weeks. Messaging them at six weeks is already
#       late; at six months it reads as a bot that cannot tell time. 45 days is
#       deliberately a little generous rather than tight, because every draft is
#       human-reviewed before it goes anywhere, so the cost of a marginal
#       borderline lead is one glance and the cost of over-trimming is a real
#       lost job.
#
#   craigslist consumer_lead, 14 days
#       A "come haul this away" listing is a right-now request against a
#       specific pile of stuff. Craigslist expires its own listings on roughly
#       this cadence for the same reason. After two weeks the pile is gone.
#
#   partner, 365 days
#       An estate-sale operator is not an event, it is a business. A company
#       running sales most weekends still needs a hauler most weekends whether
#       the listing that surfaced them was from March or from last week. Age
#       barely matters here, so the gate exists only to drop genuinely dead
#       operators rather than to enforce freshness.
#
# UNKNOWN AGE MEANS SKIP, NOT PASS.
#   This is the whole point. Letting an undated row through "because we cannot
#   prove it is old" is precisely the fallback that created this bug: unknown
#   silently became fresh. The safe direction is the opposite one. Skipping
#   costs a draft that a human never saw; passing costs Jack's credibility with
#   a real prospect, and the row is never deleted, so any lead skipped this way
#   becomes draftable the moment backfill_posted_at.py resolves its date.
#   --allow-unknown-age exists as a deliberate, explicit override, and drafts
#   produced under it are STAMPED as unknown-age in the row rather than passing
#   as if they were dated.
# ---------------------------------------------------------------------------

MAX_AGE_DAYS = {
    ("consumer_lead", "craigslist"): 14,
    ("consumer_lead", None): 45,
    ("partner", None): 365,
}


def max_age_for(row: dict) -> int | None:
    category = (row.get("category") or "").strip()
    source = (row.get("source") or "").strip().lower() or None
    if (category, source) in MAX_AGE_DAYS:
        return MAX_AGE_DAYS[(category, source)]
    return MAX_AGE_DAYS.get((category, None))


def parse_iso(value) -> datetime | None:
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def effective_date(row: dict):
    """The date describing WHEN this lead is about.

    posted_at, then event_date (an estate sale's operative date is the sale
    itself). collected_at is deliberately absent: it records when we looked, not
    when anything happened, and treating it as a date is the original bug.
    """
    return row.get("posted_at") or row.get("event_date")


def lead_age_days(row: dict, now: datetime | None = None) -> float | None:
    """Age in days, or None when genuinely unknown. Never guesses."""
    dt = parse_iso(effective_date(row))
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def recency_verdict(row: dict, allow_unknown: bool,
                    now: datetime | None = None) -> tuple[bool, str, str]:
    """(ok_to_draft, reason, age_label).

    age_label is written into the draft's personalization so the age -- known or
    unknown -- is visible on the row a human reviews, never silent.
    """
    age = lead_age_days(row, now)
    limit = max_age_for(row)

    if age is None:
        if allow_unknown:
            return True, "", "age UNKNOWN (no publication date on this row)"
        return (False,
                "age UNKNOWN: no posted_at and no event_date, so this lead "
                "cannot be shown to be current. Run backfill_posted_at.py to "
                "resolve it, or pass --allow-unknown-age to draft anyway.",
                "age UNKNOWN")

    # An event_date in the future is not an old lead, it is an upcoming one.
    if age < 0:
        return True, "", f"upcoming in {abs(age):.0f} days"

    if limit is not None and age > limit:
        return (False,
                f"too old: {age:.0f} days, limit for "
                f"{row.get('category')}/{row.get('source')} is {limit} days",
                f"{age:.0f} days old")

    return True, "", f"{age:.0f} days old"


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


def sb_headers(key: str) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def fetch_candidate_leads(url: str, key: str, client: str) -> list[dict]:
    params = {
        "select": "*",
        "client": f"eq.{client}",
        "category": "in.(" + ",".join(ACTIONABLE_CATEGORIES) + ")",
        "limit": 500,
    }
    r = requests.get(f"{url}/rest/v1/leads_raw", headers=sb_headers(key), params=params)
    r.raise_for_status()
    return r.json()


def fetch_drafted_urls(url: str, key: str, client: str) -> set[str]:
    """Every leads_raw url that already produced an outbound row for this
    client, so re-running never duplicates a draft."""
    params = {
        "select": "evidence_url",
        "client": f"eq.{client}",
        "evidence_url": "not.is.null",
        "limit": 5000,
    }
    r = requests.get(f"{url}/rest/v1/outbound", headers=sb_headers(key), params=params)
    r.raise_for_status()
    return {row["evidence_url"] for row in r.json() if row.get("evidence_url")}


def sort_key(row: dict) -> tuple:
    urgency = (row.get("urgency") or "").strip().lower()
    rank = URGENCY_RANK.get(urgency, 3)
    # collected_at is NOT a freshness signal and must never stand in for one.
    # It used to be the fallback here, which is precisely why an ancient post
    # sorted to the top as though it were new: every row's collected_at is a
    # couple of days old, so everything looked equally current. A row with no
    # real date now sorts LAST within its urgency rank instead of masquerading
    # as the freshest thing in the queue.
    #
    # The ordering itself was also backwards: the old key sorted the ISO string
    # ASCENDING while its comment claimed "freshest first", so within an urgency
    # rank the OLDEST post came out on top. Negating the epoch makes the stated
    # intent and the actual behaviour agree.
    dt = parse_iso(effective_date(row))
    if dt is None:
        return (rank, 1, 0.0)
    return (rank, 0, -dt.timestamp())


def find_voice_profile(client_name: str) -> dict | None:
    """Match leads_raw.client against a voices/*.json display_name. Returns
    None (not the generic profile) when nothing matches, so callers can tell
    the difference between "we know this client's voice" and "we don't"."""
    if not client_name:
        return None
    target = client_name.strip().lower()
    for slug in cv.list_clients():
        profile = cv._load_profile(slug)  # cv's own cache/loader, not modified
        if (profile.get("display_name") or "").strip().lower() == target:
            return profile
    return None


def extract_phone(*texts: str | None) -> str | None:
    for t in texts:
        if not t:
            continue
        m = _PHONE_RE.search(t)
        if m:
            return m.group(0).strip()
    return None


def clean_quote(q: str | None) -> str | None:
    if not q:
        return None
    q = q.strip().strip('"').strip()
    return q or None


def build_consumer_draft(row: dict, profile: dict | None) -> tuple[str | None, str | None, str]:
    """Returns (subject, body, personalization) or (None, None, reason)."""
    quote = clean_quote(row.get("quote"))
    if not quote:
        return None, None, "no verified quote on the row"

    trade = (profile or {}).get("trade") or ""
    city = (profile or {}).get("default_city") or (row.get("location_text") or "")
    signoff = (profile or {}).get("signoff") or ""

    if not trade:
        return None, None, "no known trade for this client (no voice profile match)"

    opener = f'Saw what you wrote: "{quote}"'
    if city:
        middle = f" We do {trade} jobs like that around {city}."
    else:
        middle = f" We do {trade} jobs like that."
    close = " Happy to help if you still need it handled, no obligation either way."
    body = opener + middle + close
    if signoff:
        body = body + " " + signoff

    subject = f"About your post: {(row.get('title') or quote)[:60]}"
    reason = (row.get("reason") or "").strip()
    personalization = f'Quote: "{quote}"'
    if reason:
        personalization += f" | Why flagged: {reason}"
    return subject, body, personalization


def build_partner_draft(row: dict, profile: dict | None) -> tuple[str | None, str | None, str]:
    quote = clean_quote(row.get("quote"))
    if not quote:
        return None, None, "no verified quote on the row"

    company = (row.get("author_handle") or "").strip()
    if not company:
        return None, None, "no company name on the row (author_handle empty)"

    trade = (profile or {}).get("trade") or ""
    city = (profile or {}).get("default_city") or (row.get("location_text") or "")
    if not trade:
        return None, None, "no known trade for this client (no voice profile match)"

    event_date = row.get("event_date")

    opener = f'Hi {company}, saw this: "{quote}"'
    if event_date:
        middle = (
            f" Looks like you have a sale coming up on {event_date}. "
            f"We do post-sale {trade} for estate and liquidation sale companies"
        )
    else:
        middle = f" We do post-sale {trade} for estate and liquidation sale companies"
    if city:
        middle += f" around {city}."
    else:
        middle += "."
    close = (
        " Running sales most weekends usually means a hauler is needed most weekends too. "
        "If it would help to have one company on call for every sale instead of "
        "scrambling after each one, glad to talk about that."
    )
    body = opener + middle + close

    subject = f"Hauling partner for {company}"
    reason = (row.get("reason") or "").strip()
    personalization = f'Quote: "{quote}"'
    if reason:
        personalization += f" | Why flagged: {reason}"
    if event_date:
        personalization += f" | Upcoming sale: {event_date}"
    return subject, body, personalization


def slug_of(profile: dict | None) -> str | None:
    return (profile or {}).get("slug")


def source_url(row: dict) -> str | None:
    """The one real, human-openable URL this draft is evidenced by.

    Returns None rather than anything invented. There is no fallback, no
    constructed profile link, no platform search URL. If the row cannot prove
    where it came from, the caller must not write a draft at all -- a message
    Jack cannot click back to its source is a message he cannot trust.
    """
    u = (row.get("url") or "").strip()
    return u if u.startswith("http") else None


def build_draft(row: dict, profile: dict | None,
                allow_unknown_age: bool = False) -> dict | None:
    category = row.get("category")

    # Age is checked FIRST, before any message is composed. A stale lead should
    # not consume a voice-gate pass, and more importantly the skip reason a
    # human reads should be the real one ("too old") rather than whatever
    # incidental thing the drafter happened to trip over afterwards.
    fresh, why, age_label = recency_verdict(row, allow_unknown_age)
    if not fresh:
        return {"skipped": True, "row": row, "reason": why}

    if category == "consumer_lead":
        subject, body, note = build_consumer_draft(row, profile)
    elif category == "partner":
        subject, body, note = build_partner_draft(row, profile)
    else:
        return None

    if not body:
        return {"skipped": True, "row": row, "reason": note}

    # Evidence is not optional. A draft with no source link cannot be reviewed,
    # so it is skipped here rather than written with a NULL (or guessed)
    # evidence_url. This also protects the dedup in fetch_drafted_urls, which
    # only ever sees non-null evidence_url values.
    evidence = source_url(row)
    if not evidence:
        return {"skipped": True, "row": row,
                "reason": "no source url on the leads_raw row (nothing to link back to)"}

    violations = cv.check_voice(body, slug_of(profile))
    if violations:
        return {"skipped": True, "row": row, "reason": "voice gate failed: " + "; ".join(violations)}

    company = (row.get("author_handle") or "").strip() or None
    phone = extract_phone(row.get("body"), row.get("title"))

    if category == "partner":
        channel = "phone" if phone else "web"
        recipient = phone
        recipient_handle = company
    else:
        author_handle = (row.get("author_handle") or "").strip() or None
        channel = "phone" if phone else ("platform_dm" if author_handle else None)
        recipient = phone
        recipient_handle = author_handle

    if not channel:
        return {"skipped": True, "row": row, "reason": "no provable contact path (no phone, no handle)"}

    tier = row.get("urgency")

    # The lead's age travels WITH the draft. A reviewer should never have to go
    # back to leads_raw to find out whether they are about to answer a post from
    # last week or from 2016, and an unknown age says so in words.
    note = f"{note} | Lead age: {age_label}"

    return {
        "skipped": False,
        "row": row,
        "outbound": {
            "client": row.get("client"),
            "channel": channel,
            "direction": "outbound",
            "recipient": recipient,
            "recipient_handle": recipient_handle,
            "recipient_url": evidence if not phone else None,
            "subject": subject,
            "body": body,
            "personalization": note,
            "evidence_url": evidence,
            "status": "draft",
            "tier": tier,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Draft outbound messages from judged leads_raw rows.")
    ap.add_argument("--client", required=True, help="Exact leads_raw.client value.")
    ap.add_argument("--confirm", action="store_true",
                    help="Actually write draft rows. Without this, nothing is written.")
    ap.add_argument("--limit", type=int, default=0, help="Cap how many drafts to create this run.")
    ap.add_argument("--allow-unknown-age", action="store_true",
                    help="Draft leads whose publication date is unknown. OFF by "
                         "default: an undated lead cannot be shown to be current, "
                         "and treating unknown as fresh is the bug this gate "
                         "exists to prevent. Drafts made this way are stamped "
                         "'age UNKNOWN' in their personalization.")
    args = ap.parse_args()

    url, key = load_env()
    profile = find_voice_profile(args.client)

    leads = fetch_candidate_leads(url, key, args.client)
    already = fetch_drafted_urls(url, key, args.client)

    leads = [r for r in leads if r.get("url") not in already]
    leads.sort(key=sort_key)

    drafted, skipped = [], []
    for row in leads:
        result = build_draft(row, profile, allow_unknown_age=args.allow_unknown_age)
        if result is None:
            continue
        if not result["skipped"]:
            drafted.append(result)
            if args.limit and len(drafted) >= args.limit:
                break
        else:
            skipped.append(result)

    stale = sum(1 for s in skipped if s["reason"].startswith("too old"))
    unknown = sum(1 for s in skipped if s["reason"].startswith("age UNKNOWN"))
    print(f"[draft_from_leads] client={args.client!r} candidates={len(leads)} "
          f"would_draft={len(drafted)} skipped={len(skipped)}", file=sys.stderr)
    print(f"[draft_from_leads] recency gate: {stale} skipped as TOO OLD, "
          f"{unknown} skipped as UNKNOWN AGE, "
          f"{len(skipped) - stale - unknown} skipped for other reasons. "
          f"No rows were deleted.", file=sys.stderr)
    for s in skipped:
        row = s["row"]
        print(f"    SKIP id={row.get('id')} category={row.get('category')} "
              f"reason={s['reason']}", file=sys.stderr)

    for d in drafted:
        ob = d["outbound"]
        print("----", file=sys.stderr)
        print(f"    lead_id={d['row'].get('id')} category={d['row'].get('category')} "
              f"tier={ob['tier']} channel={ob['channel']}", file=sys.stderr)
        print(f"    recipient={ob['recipient']!r} recipient_handle={ob['recipient_handle']!r}",
              file=sys.stderr)
        print(f"    subject: {ob['subject']}", file=sys.stderr)
        print(f"    body: {ob['body']}", file=sys.stderr)
        print(f"    personalization: {ob['personalization']}", file=sys.stderr)
        print(f"    evidence_url: {ob['evidence_url']}", file=sys.stderr)

    if not args.confirm:
        print("[draft_from_leads] DRY RUN. Nothing was written. Pass --confirm to write.",
              file=sys.stderr)
        return 0

    if not drafted:
        print("[draft_from_leads] nothing to write.", file=sys.stderr)
        return 0

    payload = [d["outbound"] for d in drafted]
    r = requests.post(
        f"{url}/rest/v1/outbound",
        headers={**sb_headers(key), "Prefer": "return=representation"},
        json=payload,
    )
    r.raise_for_status()
    written = r.json()
    print(f"[draft_from_leads] wrote {len(written)} draft rows to outbound.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
