#!/usr/bin/env python3
"""
Sonar Watch telemetry — makes a watch run visible after it ends.

WHY THIS EXISTS
  Run 32976099694 (2026-08-26) reported "success" while producing one draft from
  sixty queries, and nothing in the database recorded that it had run at all:
  crm_clients.last_scraped_at was NULL for all four clients. A green check with
  no row behind it is indistinguishable from a job that never fired. This module
  writes the two things that make freshness checkable:

    1. crm_clients.last_scraped_at  — "this client was looked at, at this time"
    2. watch_runs                   — one row per client per run, with the
                                      query/result/kept/throttled counters, so a
                                      collapse in yield is visible as a trend
                                      instead of being re-derived from CI logs.

  A client that was SKIPPED still gets both writes, with status='skipped' and the
  reason. Skipping silently is how Northcomm burned queries on "work in (no
  cities set)" for a month without anyone noticing.

SCHEMA (already applied to the Sonar Supabase project klzmpjregrcxumaxfsug;
re-runnable, and reproduced here so the table can be rebuilt from source):

    alter table public.crm_clients add column if not exists last_scraped_at timestamptz;

    create table if not exists public.watch_runs (
      id            bigserial primary key,
      ran_at        timestamptz not null default now(),
      client        text not null,
      client_slug   text,
      status        text not null default 'ok',   -- ok | skipped | off | throttled | error
                                                   -- 'off'     = channels='none', deliberately disabled
                                                   -- 'skipped' = misconfigured, somebody must fix it
      skip_reason   text,
      platforms     text,
      queries       int not null default 0,
      results       int not null default 0,
      kept          int not null default 0,
      rejected      int not null default 0,
      throttled     int not null default 0,
      empty_queries int not null default 0,
      errors        int not null default 0,
      unresolved_location int not null default 0,
      dup           int not null default 0,
      no_intent     int not null default 0,
      low_score     int not null default 0
    );
    -- Migration for tables created before the drop-reason columns existed
    -- (idempotent, safe to re-run):
    alter table public.watch_runs add column if not exists unresolved_location int not null default 0;
    alter table public.watch_runs add column if not exists dup int not null default 0;
    alter table public.watch_runs add column if not exists no_intent int not null default 0;
    alter table public.watch_runs add column if not exists low_score int not null default 0;
    create index if not exists watch_runs_client_ran_at_idx
      on public.watch_runs (client, ran_at desc);
    alter table public.watch_runs enable row level security;

  RLS is on with no policy, which means anon/authenticated cannot read it and the
  service key (which bypasses RLS) can. The watcher is a server-side job, so that
  is the correct posture — add a policy only if a dashboard needs to read it.

NO FABRICATION. Every function here writes only numbers the caller measured. If a
write fails it says so on stdout and returns False; it never pretends to have
recorded a run, because a false freshness timestamp is worse than a NULL one.
"""
from __future__ import annotations

import datetime as _dt

import requests

__all__ = ["utcnow", "record_run", "mark_scraped"]

TIMEOUT = 30


def utcnow() -> str:
    """ISO-8601 UTC, the format Postgres timestamptz round-trips cleanly."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _headers(key: str) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def mark_scraped(url: str, key: str, slug: str, when: str | None = None) -> bool:
    """Stamp crm_clients.last_scraped_at for one client, keyed by slug.

    crm_clients has no id column — slug is the natural key — so the filter is on
    slug. A client with no slug cannot be stamped, and that is reported rather
    than silently skipped.
    """
    if not slug:
        print("      telemetry: client has no slug, cannot stamp last_scraped_at")
        return False
    try:
        r = requests.patch(
            f"{url}/rest/v1/crm_clients",
            headers=_headers(key),
            params={"slug": f"eq.{slug}"},
            json={"last_scraped_at": when or utcnow()},
            timeout=TIMEOUT,
        )
        if not r.ok:
            print(f"      telemetry: last_scraped_at FAILED {r.status_code} {r.text[:160]}")
            return False
        return True
    except Exception as e:  # network, DNS, timeout
        print(f"      telemetry: last_scraped_at ERROR {type(e).__name__}: {e}")
        return False


def record_run(url: str, key: str, row: dict) -> bool:
    """Insert one watch_runs row. `row` must already carry measured counters."""
    payload = {
        "ran_at": row.get("ran_at") or utcnow(),
        "client": row.get("client") or "(unknown)",
        "client_slug": row.get("client_slug"),
        "status": row.get("status") or "ok",
        "skip_reason": row.get("skip_reason"),
        "platforms": row.get("platforms"),
        "queries": int(row.get("queries") or 0),
        "results": int(row.get("results") or 0),
        "kept": int(row.get("kept") or 0),
        "rejected": int(row.get("rejected") or 0),
        "throttled": int(row.get("throttled") or 0),
        "empty_queries": int(row.get("empty_queries") or 0),
        "errors": int(row.get("errors") or 0),
        "unresolved_location": int(row.get("unresolved_location") or 0),
        "dup": int(row.get("dup") or 0),
        "no_intent": int(row.get("no_intent") or 0),
        "low_score": int(row.get("low_score") or 0),
    }
    try:
        r = requests.post(f"{url}/rest/v1/watch_runs",
                          headers=_headers(key), json=payload, timeout=TIMEOUT)
        if not r.ok and r.status_code == 400 and "column" in r.text.lower():
            # The live table predates the newest drop-reason columns (PostgREST
            # rejects unknown keys with PGRST204). Losing the whole row over
            # them would be worse than losing the new buckets, so retry once
            # without them — and say so, because the fix is running the
            # documented ALTER TABLE migration above.
            missing = [k for k in ("dup", "no_intent", "low_score",
                                   "unresolved_location") if k in payload]
            print(f"      telemetry: watch_runs rejected new columns "
                  f"({r.text[:120]}); retrying without {missing} — run the "
                  f"schema migration in watch_telemetry.py's docstring")
            for k in missing:
                payload.pop(k, None)
            r = requests.post(f"{url}/rest/v1/watch_runs",
                              headers=_headers(key), json=payload,
                              timeout=TIMEOUT)
        if not r.ok:
            print(f"      telemetry: watch_runs insert FAILED "
                  f"{r.status_code} {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"      telemetry: watch_runs insert ERROR {type(e).__name__}: {e}")
        return False
