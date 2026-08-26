#!/usr/bin/env python3
"""
Client config linter — catches scraper configs in crm_clients that cannot work,
or that work badly, BEFORE a run burns queries producing nothing.

WHY THIS EXISTS
  Every active client's config was misconfigured in a different way and nothing
  caught it. A client with no cities still runs. A client whose scrape_cities
  says "Texas" silently loses geographic filtering, and a San Marcos TX post got
  filed as a lead for a DFW-area client. A client whose channels list holds a
  channel the watcher does not watch will never match anything. None of that
  raises an error anywhere — the run just reports "0 drafts", which reads as
  "no demand this week" rather than "this client is broken".

READ-ONLY. This tool reads crm_clients and prints. It never writes.

AUTHORITY
  The list of channels the watcher can search is read live from
  watch_social.PLATFORMS, and the set it actually searches in a normal run is
  read from that module's --platforms default. Nothing here assumes a channel
  list of its own, so this file cannot drift from the watcher.

  Trade phrasing is checked against trade_vocab.TRADES, which is the module that
  encodes the fact that demand language differs from trade language ("need to
  get rid of", not "junk removal").

NEVER FABRICATES. When a client's config is empty the linter says it is not
configured and needs a decision from the business owner. It never proposes a
plausible-sounding niche or city list.

NO CLIENT NAMES ARE HARDCODED. Every rule is generic and derived from the data.

    ENV_FILE="$HOME/ghl-cli/.env" python client_config_lint.py
    ENV_FILE="$HOME/ghl-cli/.env" python client_config_lint.py --json

Exit code 0 = no ERROR findings. 1 = at least one client cannot possibly work.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from db import load_env                    # noqa: E402
import watch_social                        # noqa: E402  authoritative channel list
import trade_vocab                         # noqa: E402  authoritative trade phrasing

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ERROR = "ERROR"
WARNING = "WARNING"

# ---------------------------------------------------------------------------
# Authoritative facts pulled from the watcher itself, never restated here.
# ---------------------------------------------------------------------------

def watcher_channels() -> set[str]:
    """Every channel watch_social.py knows how to build a query for."""
    return set(watch_social.PLATFORMS)


def watcher_default_channels() -> set[str]:
    """DEPRECATED. Kept only so the header can report it.

    This used to be the real source of truth: watch_social.py ignored
    crm_clients.channels and searched whatever --platforms defaulted to. Since
    2026-08-26 the watcher reads channels per client and --platforms is a debug
    override defaulting to empty, so this returns an empty set and nothing
    lints against it.
    """
    # Read the default out of the watcher's own argparse definition rather than
    # copying the value here, so this cannot drift from what actually runs.
    import inspect
    src = inspect.getsource(watch_social.main)
    for line in src.splitlines():
        if "--platforms" in line and "default=" in line:
            tail = line.split("default=", 1)[1]
            quote = '"' if '"' in tail else "'"
            if quote in tail:
                val = tail.split(quote)[1]
                return {p.strip() for p in val.split(",") if p.strip()}
    return set()


# Channels that exist in the watcher's query table but whose real-world yield is
# known to be near zero. Facebook groups — where the demand actually is — have
# been closed to programmatic access since the Groups API was killed in Apr 2024
# (recorded in watch_social.py's own module docstring); a site: search only
# reaches public pages.
DEGRADED_CHANNELS = {
    "facebook": "Facebook groups have been walled since the Apr 2024 Groups-API "
                "shutdown; a site: search only reaches public pages, so yield is "
                "near zero (see watch_social.py module docstring)",
}

# Channels built for local consumer neighborhood demand. Pointing a business
# that sells to other businesses at these is a category error.
CONSUMER_NEIGHBORHOOD_CHANNELS = {"nextdoor"}

# Words that, appearing in a niche, mean this client sells to other BUSINESSES.
# Deliberately narrow: it only fires on terms that are unambiguously trade-side,
# so a false positive is unlikely. Anything vaguer is left un-flagged rather
# than guessed at.
B2B_NICHE_MARKERS = (
    "3pl", "third-party logistics", "third party logistics", "fulfillment",
    "wholesale", "distribution", "distributor", "b2b", "warehousing",
    "freight", "manufacturer", "manufacturing", "saas", "logistics",
)

# US states + DC. A value here is a STATE, not a city, and the watcher will
# happily build "... in Texas" queries that match anywhere in the state.
US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia", "washington dc", "d.c.",
}
STATE_ABBRS = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}
# "New York" is both. Flagging it as a state would be wrong as often as right,
# so it is reported as AMBIGUOUS rather than as a defect.
STATE_CITY_COLLISIONS = {"new york", "oklahoma city", "kansas city", "washington"}


def csv(val) -> list[str]:
    """Split a comma-joined config field exactly the way the watcher does."""
    return [x.strip() for x in (val or "").split(",") if x.strip()]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def lint_client(c: dict, supported: set[str], defaults: set[str]) -> list[dict]:
    f: list[dict] = []

    def add(sev, field, problem, fix):
        f.append({"severity": sev, "field": field, "problem": problem, "fix": fix})

    niche = (c.get("scrape_niche") or "").strip()
    cities = csv(c.get("scrape_cities"))
    terms = csv(c.get("scrape_terms"))
    channels = [x.lower() for x in csv(c.get("channels"))]
    configured = any([niche, cities, terms, channels])

    # RULE -1 — the deliberate off-switch, checked before everything else.
    # channels='none' means a human decided this client has no social lead
    # watching. That is a settled decision, not a defect, and it must never be
    # reported as one: a linter that keeps flagging a choice Jack already made
    # trains him to ignore the linter. watch_social.py honours the same sentinel
    # and spends zero queries on these clients.
    if channels == ["none"]:
        return []

    # RULE 0 — nothing configured at all. Do not guess what it should be.
    if not configured:
        add(ERROR, "scrape_niche/scrape_cities/scrape_terms/channels",
            "active client with no scraper config at all — every run iterates "
            "this client and produces nothing",
            "not configured - needs a decision from Jack on what this client "
            "sells and where. Do NOT infer it; until then set active=false so "
            "runs stop iterating it.")
        # The remaining field rules would just restate this. Still check the
        # run-evidence rule below, then return.
        if not c.get("last_scraped_at"):
            add(WARNING, "last_scraped_at",
                "null — no evidence the watcher has ever processed this client",
                "have the watcher stamp last_scraped_at on every client it "
                "iterates, so 'no leads' is distinguishable from 'never ran'.")
        return f

    # RULE 1 — no niche. watch_social falls back to the literal string "work",
    # which searches for nothing real.
    if not niche:
        add(ERROR, "scrape_niche",
            "null/empty — watch_social falls back to the placeholder trade "
            "'work', so every query searches for a word no customer writes",
            "set scrape_niche to the trade this client sells, ideally one of "
            "the vocabularies in trade_vocab.TRADES: "
            + ", ".join(sorted(trade_vocab.TRADES)))

    # RULE 2 — no cities. The watcher loops `cities or [""]`, so it still runs,
    # just with no place in the query.
    if not cities:
        add(ERROR, "scrape_cities",
            "null/empty — the watcher still runs this client but with no "
            "location in any query, so results are nationwide noise",
            "set scrape_cities to a comma-separated list of the actual cities "
            "this client serves.")

    # RULE 3 — a STATE where cities belong. This is a confirmed live failure:
    # a San Marcos TX post was filed as a lead for a client whose
    # scrape_cities said "Texas".
    for city in cities:
        low = city.lower().strip()
        if low in STATE_CITY_COLLISIONS:
            add(WARNING, "scrape_cities",
                f"'{city}' is both a city and a state name — cannot tell which "
                "was meant",
                "if the state was meant, replace it with the specific cities "
                "served; if the city was meant, no change needed.")
        elif low in US_STATES or low in STATE_ABBRS:
            add(ERROR, "scrape_cities",
                f"'{city}' is a STATE, not a city — geographic filtering is "
                "effectively disabled and far-away posts qualify as local leads",
                "replace it with the specific cities served. A statewide value "
                "is how an out-of-metro post gets filed as a local lead.")

    # RULE 4 — channels the watcher cannot search at all.
    unknown = [ch for ch in channels if ch not in supported]
    for ch in unknown:
        add(ERROR, "channels",
            f"'{ch}' is not a channel watch_social.py can search — "
            f"it supports only: {', '.join(sorted(supported))}",
            f"remove '{ch}' from channels and set a channel the watcher "
            f"actually searches ({', '.join(sorted(defaults))} are the ones a "
            "scheduled run covers). If this client is genuinely an "
            "email-outreach client and not a social-watch client, that belongs "
            "in a different system, not in the watcher's config.")

    # RULE 5 removed 2026-08-26. It checked each client's channels against the
    # watcher's --platforms default, which was the real source of truth back
    # when watch_social.py ignored crm_clients.channels entirely. The watcher
    # now reads channels per client and --platforms is a debug override with an
    # empty default, so "not in the default run set" is true of every client and
    # means nothing. Keeping it would have fired a false ERROR on Jackson and
    # Hero's forever.
    known = [ch for ch in channels if ch in supported]

    # RULE 6 — channels that are supported but known-degraded.
    for ch in known:
        if ch in DEGRADED_CHANNELS:
            add(WARNING, "channels", f"'{ch}': {DEGRADED_CHANNELS[ch]}",
                f"treat '{ch}' as a bonus, never as a client's only channel; "
                "measure its actual yield before relying on it.")

    # RULE 7 — a business that sells to businesses aimed at a consumer
    # neighborhood channel. Heuristic, and only fires on unambiguous markers.
    if niche:
        hit = next((m for m in B2B_NICHE_MARKERS if m in niche.lower()), None)
        if hit:
            mismatched = sorted(set(known) & CONSUMER_NEIGHBORHOOD_CHANNELS)
            if mismatched:
                add(WARNING, "channels/scrape_niche",
                    f"niche '{niche}' reads as B2B (matched '{hit}') but "
                    f"channels include {mismatched}, which carry local CONSUMER "
                    "demand — neighbors do not post asking for a 3PL. "
                    "Heuristic: confirm the business model before acting",
                    "point B2B clients at channels where businesses ask "
                    "(reddit trade subs) or accept that this client is not a "
                    "social-watch fit and say so, rather than running it dry.")

    # RULE 8 — no client-specific terms. Not fatal: trade_vocab supplies real
    # customer phrasing. Fatal only if the trade has no vocabulary either.
    canon = trade_vocab.canonical_trade(niche) if niche else ""
    has_vocab = canon in trade_vocab.TRADES
    if not terms:
        if niche and has_vocab:
            add(WARNING, "scrape_terms",
                f"null — queries fall back to trade_vocab's generic phrasing "
                f"for '{canon}'; nothing client-specific is searched",
                "add 2-5 comma-separated phrases customers actually type for "
                "this client. trade_vocab.intent_queries() folds them in.")
        elif niche:
            add(ERROR, "scrape_terms",
                f"null AND trade '{niche}' has no vocabulary in "
                "trade_vocab.TRADES — queries fall back to generic asks glued "
                "to the trade name, which is exactly the failure trade_vocab "
                "was written to fix (a live run rejected 25 of 25 real results "
                "that way)",
                f"either add a TRADES entry for '{canon or niche}' in "
                "trade_vocab.py, or set scrape_terms to real customer phrasing "
                "for this client.")

    # RULE 9 — trade with no vocabulary, even when terms exist.
    if niche and not has_vocab and terms:
        add(WARNING, "scrape_niche",
            f"'{niche}' does not map to any trade_vocab.TRADES vocabulary "
            "(relevance falls back to generic confirmation terms, which lets "
            "off-topic results through)",
            f"add a TRADES entry for '{canon or niche}' in trade_vocab.py.")

    # RULE 10 — no evidence the watcher ever ran.
    if not c.get("last_scraped_at"):
        add(WARNING, "last_scraped_at",
            "null — no evidence the watcher has ever processed this client, so "
            "'zero leads' cannot be distinguished from 'never ran'",
            "have the watcher stamp last_scraped_at on every client it "
            "iterates, and alert when it goes stale.")

    return f


def fetch_active_clients(env) -> list[dict]:
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY "
                 "(set ENV_FILE to the .env holding them)")
    r = requests.get(f"{url}/rest/v1/crm_clients", timeout=30,
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     params={"active": "is.true", "select": "*"})
    if not r.ok:
        sys.exit(f"crm_clients read failed: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client", help="lint one client by name or slug")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    env = load_env()
    clients = fetch_active_clients(env)
    if args.client:
        q = args.client.lower()
        clients = [c for c in clients
                   if (c.get("name") or "").lower() == q
                   or (c.get("slug") or "").lower() == q]
    clients.sort(key=lambda c: (c.get("name") or c.get("slug") or "").lower())

    supported = watcher_channels()
    defaults = watcher_default_channels()

    results = []
    for c in clients:
        results.append({
            "name": c.get("name"), "slug": c.get("slug"),
            "findings": lint_client(c, supported, defaults),
        })

    n_err = sum(1 for r in results for x in r["findings"] if x["severity"] == ERROR)

    if args.json:
        print(json.dumps({"clients": results, "errors": n_err}, indent=2))
        return 1 if n_err else 0

    print("=" * 78)
    print("CLIENT SCRAPER CONFIG LINT  (read-only; writes nothing)")
    print(f"active clients: {len(results)}")
    print(f"watcher supports channels : {', '.join(sorted(supported))}")
    print(f"channel source of truth   : crm_clients.channels (per client)")
    print("=" * 78)

    for r in results:
        errs = [x for x in r["findings"] if x["severity"] == ERROR]
        warns = [x for x in r["findings"] if x["severity"] == WARNING]
        status = "FAIL" if errs else ("WARN" if warns else "OK")
        print(f"\n{r['name']}  [{r['slug']}]   {status}"
              f"   {len(errs)} error(s), {len(warns)} warning(s)")
        if not r["findings"]:
            print("    no findings")
        for x in errs + warns:
            print(f"    [{x['severity']:7}] {x['field']}")
            print(f"        problem: {x['problem']}")
            print(f"        fix    : {x['fix']}")

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    w = max([len(r["name"] or "") for r in results] + [12])
    print(f"{'CLIENT'.ljust(w)}  {'STATUS':6}  {'ERR':>3}  {'WARN':>4}  TOP ISSUE")
    print("-" * 78)
    for r in results:
        errs = [x for x in r["findings"] if x["severity"] == ERROR]
        warns = [x for x in r["findings"] if x["severity"] == WARNING]
        status = "FAIL" if errs else ("WARN" if warns else "OK")
        top = (errs + warns)
        head = f"{top[0]['field']}: {top[0]['problem'][:44]}" if top else "-"
        print(f"{(r['name'] or '').ljust(w)}  {status:6}  {len(errs):>3}  "
              f"{len(warns):>4}  {head}")
    print("-" * 78)
    print(f"{n_err} ERROR finding(s) across {len(results)} active client(s).")
    print("ERROR = cannot possibly produce a lead.  WARNING = will work badly.")
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
