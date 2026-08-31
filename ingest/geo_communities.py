#!/usr/bin/env python3
"""
geo_communities.py - verified DFW subreddit registry for the Sonar ingest pipeline.

This module is a REGISTRY, not a scraper. It answers one question: which
subreddits are real, are the TEXAS ones, and are worth polling for
Dallas-Fort Worth local leads.

Why this file exists separately from the collector:

  A subreddit name is not a location. r/Arlington and r/arlingtonva are
  VIRGINIA. r/Richardson could plausibly be a surname sub. A collector that
  maps subreddit -> city by string match will happily file a Virginia post
  as a Texas lead, and nothing downstream can tell, because by then the post
  looks exactly like a real one. So the mapping lives here, every entry was
  confirmed by reading the feed's own title and subtitle and its actual post
  titles, and entries carry the date they were verified.

  Only VERIFIED_TEXAS entries are pollable. Everything else is recorded with
  the reason it was rejected, because "there is no usable sub for Sachse" is
  a finding about coverage, not a gap to hide. A registry that silently
  contains only the winners cannot tell you what it is missing.

Provenance, and why "unknown" is a real answer:

  For a city sub like r/plano, the subreddit IS the location, so
  location_text is Plano with provenance "subreddit". For a metro sub like
  r/Dallas or r/FortWorth, the poster may live anywhere in the metro, and
  "Dallas" would be a guess dressed as data. Those entries have city=None,
  and the collector writes the metro name into location_text ONLY as a
  scope label with provenance "metro_unknown_city". A reader must be able to
  tell a source-derived location from a guess.

Measurements (posts_per_day, subscribers) were taken from the live
/new/.rss feed on the verified_on date. posts_per_day is derived from the
timespan the 25-item feed window covers, so it is a real observed rate for
that window, not an estimate. It moves. Re-run tools/probe to refresh it.
"""
from __future__ import annotations

import json
from pathlib import Path

REGISTRY_PATH = Path(__file__).with_name("geo_communities.json")


def load_registry(path: Path | None = None) -> dict:
    p = path or REGISTRY_PATH
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def pollable(registry: dict | None = None) -> list[dict]:
    """Entries confirmed to be the Texas/DFW community and safe to poll."""
    reg = registry or load_registry()
    return [e for e in reg["communities"] if e["status"] == "verified_texas"]


def rejected(registry: dict | None = None) -> list[dict]:
    """Entries deliberately NOT polled, each with the reason. Coverage truth."""
    reg = registry or load_registry()
    return [e for e in reg["communities"] if e["status"] != "verified_texas"]


def by_subreddit(name: str, registry: dict | None = None) -> dict | None:
    n = name.lower()
    for e in (registry or load_registry())["communities"]:
        if e["subreddit"].lower() == n:
            return e
    return None


def select(cities: list[str] | None = None, registry: dict | None = None) -> list[dict]:
    """Pollable entries, optionally narrowed to named cities.

    A city name that matches nothing is the caller's error and is returned so
    the collector can say so out loud. Asking for Sachse and silently getting
    a metro sub instead is the failure this returns unmatched names to avoid.
    """
    reg = registry or load_registry()
    ok = pollable(reg)
    if not cities:
        return ok
    want = {c.strip().lower() for c in cities if c.strip()}
    sel, matched = [], set()
    for e in ok:
        names = {(e.get("city") or "").lower(), (e.get("metro_label") or "").lower()}
        names |= {a.lower() for a in e.get("aliases", [])}
        hit = want & (names - {""})
        if hit:
            sel.append(e)
            matched |= hit
    return sel, sorted(want - matched)


def summary_table(registry: dict | None = None) -> str:
    reg = registry or load_registry()
    rows = ["subreddit                city              scope   status            "
            "subs      posts/day  verified",
            "-" * 104]
    for e in sorted(reg["communities"],
                    key=lambda x: (x["status"] != "verified_texas",
                                   -(x.get("posts_per_day") or 0))):
        subs = e.get("subscribers")
        rows.append("{:<24} {:<17} {:<7} {:<17} {:<9} {:<10} {}".format(
            "r/" + e["subreddit"],
            e.get("city") or "-",
            e.get("scope") or "-",
            e["status"],
            f"{subs:,}" if isinstance(subs, int) else "-",
            e.get("posts_per_day") if e.get("posts_per_day") is not None else "-",
            e.get("verified_on") or "-"))
    return "\n".join(rows)


if __name__ == "__main__":
    r = load_registry()
    print(summary_table(r))
    ok = pollable(r)
    total = sum(e.get("posts_per_day") or 0 for e in ok)
    print(f"\n{len(ok)} pollable / {len(r['communities'])} evaluated. "
          f"Observed total {total:.1f} posts/day across the pollable set.")
    print("Post volume is NOT lead volume. This is the raw firehose the "
          "categorizer has to judge, not a count of leads.")
