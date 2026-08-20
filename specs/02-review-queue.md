# Spec 2: scored review queue

Goal: stop dumping raw candidates into a file nobody reads. Score every candidate, surface the best ones in a simple approve/edit/skip UI, and draft a templated (non-AI) reply for each so the human's job is one click, not composition.

Depends on spec 1 (candidates live in Supabase).

## Scoring

Pure Python, runs at ingest time (or as a backfill pass). Written to `candidates.score`.

```
score = w1 * engagement_velocity + w2 * keyword_strength + w3 * recency + w4 * location_confidence
```

- **engagement_velocity** = upvotes (or likes) / days_since_posted, capped and log-scaled: `min(log1p(upvotes / max(days, 0.5)), 5) / 5`
- **keyword_strength** = fraction of high-intent keywords hit. Two keyword tiers in config: HOT ("where is", "anyone know", "how do I get", "looking for") and NORMAL (the existing category keywords). HOT hits count double. HOT keywords are the marketing signal: a person asking a question is a warm lead, a photo dump is content.
- **recency** = `exp(-days_since_posted / 14)` so a 2-week-old post has ~37% of a fresh one's recency value
- **location_confidence** = already computed by the geocoder, 0 to 1

Starting weights: `w1=0.35, w2=0.35, w3=0.20, w4=0.10`. Keep them in config.py; tune by eye after a week of real data.

## Reply drafting (no AI)

Template engine with slot-filling. `ingest/drafter.py`:

- 5-6 reply skeletons per intent type (question / showcase / complaint), rotated by `hash(source_id) % len(templates)` so consecutive replies never look identical.
- Slots filled from the candidate row: `{place_name}`, `{category}`, `{detail}` (first matched keyword phrase from the post body).
- Example skeleton: `"{place_name} is a solid pick. If you're into {category} spots around there, there are a couple most people miss. Happy to share."`
- Output goes to a `draft_reply` text column on the candidate. Anything the drafter can't fill cleanly gets `draft_reply = null` and still shows in the queue (human writes it).
- Hard rule: drafts are never auto-sent. The queue is the only exit.

```sql
alter table candidates add column draft_reply text;
alter table candidates add column intent text; -- question | showcase | complaint
```

## Queue UI

Keep it stupid simple, phase 1 is one page:

- **Option A (fastest):** a single HTML page + Supabase JS client, hosted anywhere static (Vercel, GitHub Pages behind the repo). Lists candidates where `status='new'` ordered by `score desc`, shows title, link, score breakdown, and the draft reply in an editable textarea. Three buttons: **Approve** (status='approved', saves edited draft), **Skip** (status='rejected'), **Open post** (new tab).
- **Option B:** a route inside the existing Wing OS dashboard (already on Vercel + Supabase, auth already handled). Same table, same three buttons.

Phase 1 rule: **Approve does not send anything.** It marks the row approved with the final reply text. The human posts it manually from their own account (copy button next to the draft). This keeps every account safe while we learn what reply styles land.

Phase 2 (only after phase 1 proves volume): approved Reddit replies could send via the Reddit API (allowed, rate-limited, from an account with real karma). TikTok/IG replies stay manual or move to official-API opt-in flows. Never scripted-login sending.

## Daily flow once live

1. Nightly: ingest runs (spec 1), scorer + drafter run right after, queue fills.
2. Morning: open queue, work top 20 by score, approve/edit/skip. Ten minutes.
3. Approved replies get posted; `status` flips to `sent` when done.
4. Weekly: look at which templates got responses, cut the losers, add two new ones.

## Definition of done

- [ ] Scorer implemented, scores visible in Supabase, ordering looks sane by eye
- [ ] HOT keyword tier catching question-type posts (spot-check 20)
- [ ] Drafter filling templates for >60% of candidates without null
- [ ] Queue page live: list, edit, approve, skip all working
- [ ] One full week of the daily flow completed to validate the loop
