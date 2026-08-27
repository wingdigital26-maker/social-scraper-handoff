# Source CLI contract

Every source is its own CLI tool. One tool, one platform, no shared state. They
compose because they all emit the SAME record shape on stdout as JSONL, which
matches the `leads_raw` table in the Sonar Supabase project.

The point of separate tools: when one platform breaks or gets blocked, exactly
one tool fails and says so. Nothing else changes, and the failure is visible
instead of showing up as a quiet zero somewhere downstream.

## The record

One JSON object per line on stdout. Nothing else goes to stdout, ever. Logs,
progress, and errors go to stderr so `tool ... > out.jsonl` is always clean.

```json
{
  "source":        "craigslist",
  "platform":      "craigslist",
  "url":           "https://...",
  "title":         "HELP NEEDED GARAGE CLEANOUT",
  "body":          "full text if available, else the snippet the source gave",
  "author_handle": null,
  "location_text": "McKinney",
  "posted_at":     "2026-08-27T14:02:00Z",
  "event_date":    null,
  "query":         "cleanout",
  "client":        "Hero's Junk Removal"
}
```

### Field rules, all of them load bearing

- `url` is REQUIRED and must be a real permalink. No record without one. A row
  that cannot be checked by a human is not evidence.
- `posted_at` is when the SOURCE says it was posted. If the source does not say,
  it is `null`. **Never substitute the collection time.** Defaulting this is
  exactly how an 18 day old cleanout job looked like a live lead on 2026-08-27.
- `event_date` is for things dated in the FUTURE, like an estate sale or a
  permitted job. A future date is inherently fresh, which is why these sources
  are worth more than classifieds.
- `title` and `body` carry the subject's own words. Do not clean, summarize,
  translate, or "improve" them. A later AI pass must quote them verbatim to
  qualify a lead, so altering them corrupts the evidence.
- Anything unknown is `null`. Never `""`, never `0`, never a placeholder.

## CLI shape

Every tool supports:

```
--query TEXT        repeatable, or comma separated
--cities TEXT       comma separated, from the client's config. Never invented.
--client TEXT       label written onto each record
--limit N           max records emitted
--since DAYS        freshness window. Tools that CANNOT filter by date must say
                    so on stderr rather than silently ignoring the flag.
--json              emit JSONL to stdout (default)
--dry-run           do the fetches, emit records, write NOTHING anywhere
```

Exit codes:
- `0` ran, whatever the yield, including zero results
- `2` the source refused us (403, 429, captcha, login wall). This is NOT the
  same as finding nothing and must never be reported as an empty result.
- `1` bad input or a real crash

Anything that returns zero rows must print to stderr WHY: no matches, blocked,
or rate limited. A silent zero is the failure mode this contract exists to stop.

## Reachability, measured 2026-08-27

Do not spend time re-testing the closed ones. All of these were checked live.

| Source | Direct | Via r.jina.ai | Verdict |
|---|---|---|---|
| Reddit | 403 | 403 | closed to scraping, OPEN via official free API |
| TikTok | connection reset | 403 | closed, no legitimate search API |
| Instagram | login wall | n/a | closed, Graph API only for owned accounts |
| Nextdoor | blocked | 403 | closed, no API and no crawlable index |
| Craigslist | 200 | n/a | OPEN |
| estatesales.net | 200 | n/a | OPEN, carries future dated sales |
| Dallas Open Data | 200 | n/a | OPEN, public records |

Reddit and Nextdoor CONTENT remains reachable indirectly through a web search
index using `site:` queries. That is a different tool from scraping the host.

## Rules that apply to every tool

- Read only. No tool sends, posts, comments, DMs, or logs in as anyone.
- Never fabricate a record, a URL, an author, or a date.
- Be a polite client: real User-Agent, real delays, respect robots and rate
  limits. If a source says no, the answer is exit 2, not a workaround.
- No em dashes in output or comments.
- Never hardcode a client name, city, or trade. Everything comes from flags.
