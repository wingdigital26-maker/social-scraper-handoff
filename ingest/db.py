"""Push enriched candidates into Supabase and log the run.

Keeps the ingest engines untouched: they still write jsonl, this bridges
jsonl -> hosted DB. Dedupe is the DB's unique (source, source_id) constraint.

Env (real env vars, or a .env file at repo root / $ENV_FILE):
    SUPABASE_URL=https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY=...

    python db.py                          # push candidates.enriched.jsonl
    python db.py --in candidates.jsonl    # push raw (unscored) candidates
"""
import argparse
import datetime
import json
import os
import pathlib
import sys

import requests

HERE = pathlib.Path(__file__).resolve().parent


def load_env():
    vals = dict(os.environ)
    env_file = pathlib.Path(os.environ.get("ENV_FILE", HERE.parent / ".env"))
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return vals


def to_row(c: dict) -> dict:
    """Map a candidate jsonl record onto the candidates table columns."""
    posted = c.get("created_utc")
    return {
        "source": c.get("source", "reddit"),
        "source_id": str(c.get("id") or c.get("source_id") or ""),
        "url": next((e["url"] for e in c.get("embeds", []) if e.get("type") == c.get("source", "reddit")), None)
               or c.get("url"),
        "title": c.get("title") or c.get("name"),
        "body": c.get("desc"),
        "author": c.get("author"),
        "place_name": c.get("place"),
        "lat": c.get("lat"),
        "lng": c.get("lng"),
        "loc_confidence": c.get("location_confidence"),
        "category": c.get("category"),
        "upvotes": c.get("upvotes"),
        "embeds": c.get("embeds", []),
        "score": c.get("score"),
        "intent": c.get("intent"),
        "draft_reply": c.get("draft_reply"),
        "posted_at": (datetime.datetime.fromtimestamp(posted, datetime.timezone.utc).isoformat()) if posted else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(HERE / "candidates.enriched.jsonl"))
    ap.add_argument("--source", default="reddit", help="source label for the run_log row")
    args = ap.parse_args()

    env = load_env()
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY")
    headers = {
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=ignore-duplicates,return=representation",
    }

    path = pathlib.Path(args.inp)
    if not path.exists():
        sys.exit(f"No input file: {path}")
    rows = [to_row(json.loads(l)) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if r["source_id"]]

    inserted = 0
    for i in range(0, len(rows), 200):  # batch to keep payloads small
        r = requests.post(f"{url}/rest/v1/candidates?on_conflict=source,source_id",
                          headers=headers, json=rows[i:i + 200], timeout=60)
        if r.status_code not in (200, 201):
            requests.post(f"{url}/rest/v1/run_log", headers=headers, timeout=30, json={
                "source": args.source, "found": len(rows), "inserted": inserted,
                "error": f"{r.status_code}: {r.text[:300]}"})
            sys.exit(f"Insert failed ({r.status_code}): {r.text[:300]}")
        inserted += len(r.json())

    requests.post(f"{url}/rest/v1/run_log", headers=headers, timeout=30,
                  json={"source": args.source, "found": len(rows), "inserted": inserted})
    print(f"pushed {len(rows)} candidates, {inserted} new (rest were dupes)")


if __name__ == "__main__":
    main()
