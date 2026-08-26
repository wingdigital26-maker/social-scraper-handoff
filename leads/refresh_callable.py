#!/usr/bin/env python3
"""Rebuild callable93.json from live Supabase.

The published sheet is only as honest as the moment it was generated. Four of
its personalization facts turned out to be wrong (a proximity window reaching
across a footer into an unrelated nav), and one was stale. Those were corrected
in the database, so the sheet has to be regenerated from the database rather
than patched by hand.

    ENV_FILE="$HOME/ghl-cli/.env" python refresh_callable.py
"""
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "ingest"))
import audit_prospect as A          # noqa: E402
from db import load_env             # noqa: E402

FIELDS = ("id,title,place_name,phone,website,contact_name,contact_title,"
          "contact_email,personalization,personalization_source,gmb_rating,"
          "gmb_reviews,seo_rank,need_score,audit_gaps,identity_reason")

env = load_env()
url, key = env["SUPABASE_URL"], env["SUPABASE_SERVICE_KEY"]
h = {"apikey": key, "Authorization": f"Bearer {key}"}

rows = A.sb_request("GET", f"{url}/rest/v1/candidates", headers=h,
                    params={"select": FIELDS, "identity": "eq.verified",
                            "limit": "500"}).json()
print(f"verified rows: {len(rows)}")

# A sheet you dial from must not list the same number twice. Keep the row
# carrying the most to say on the call.
def richness(x):
    return (bool(x.get("contact_name")), bool(x.get("personalization")),
            x.get("need_score") or 0)

by_phone, no_phone = {}, 0
for r in rows:
    d = re.sub(r"\D", "", r.get("phone") or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10:
        no_phone += 1
        continue
    if d not in by_phone or richness(r) > richness(by_phone[d]):
        by_phone[d] = r

out = []
for r in by_phone.values():
    # A Maps match confirms the city and takes the phone off the place record.
    # A website match proves only that the company is real. The sheet says which.
    r["proof"] = "website" if "own website" in (r.get("identity_reason") or "") \
                 else "maps"
    r.pop("identity_reason", None)
    out.append(r)

(HERE / "callable93.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
facts = sum(1 for x in out if x.get("personalization"))
named = sum(1 for x in out if x.get("contact_name"))
print(f"  dropped {no_phone} with no usable phone, "
      f"{len(rows) - no_phone - len(out)} duplicate numbers")
print(f"  wrote {len(out)}: {facts} with a fact, {named} with a named owner, "
      f"{sum(1 for x in out if x['proof'] == 'maps')} Maps-proven")
