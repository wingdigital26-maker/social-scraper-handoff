"""Push approved candidates into GoHighLevel as tagged contacts.

Run after a review session (or on a schedule). For every candidate with
status approved or sent that hasn't been pushed yet, it creates a GHL
contact via the ghl CLI, tagged 'social-lead' + the source platform, with
the post URL as the contact source. Then marks the row ghl_pushed so it
never double-pushes.

    python queue/ghl_push.py           # push everything pending
    python queue/ghl_push.py --dry-run # show what would be pushed
"""
import argparse
import pathlib
import subprocess
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "ingest"))
from db import load_env  # same env loading as the rest of the pipeline


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    env = load_env()
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY")
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json", "Prefer": "return=minimal"}

    r = requests.get(f"{url}/rest/v1/candidates", headers=headers, timeout=30,
                     params={"status": "in.(approved,sent)", "ghl_pushed": "is.false",
                             "select": "id,source,author,title,place_name,url"})
    r.raise_for_status()
    rows = r.json()
    if not rows:
        print("Nothing pending."); return

    pushed = failed = 0
    for c in rows:
        author = (c.get("author") or "unknown").lstrip("@u/")
        name = f"{author} ({c['source']})"
        cmd = ["ghl", "contacts", "create", "--name", name,
               "--tag", "social-lead", "--tag", f"social-{c['source']}",
               "--source", c.get("url") or f"{c['source']} post"]
        if args.dry_run:
            print("DRY:", " ".join(cmd)); continue
        res = subprocess.run(cmd, capture_output=True, text=True, shell=True)
        if res.returncode != 0:
            failed += 1
            print(f"  ! GHL create failed for #{c['id']}: {res.stderr.strip()[:150]}")
            continue
        requests.patch(f"{url}/rest/v1/candidates", headers=headers,
                       params={"id": f"eq.{c['id']}"}, json={"ghl_pushed": True},
                       timeout=30)
        pushed += 1
        print(f"  + {name} -> GHL")

    print(f"\npushed {pushed}, failed {failed}, of {len(rows)} pending")


if __name__ == "__main__":
    main()
