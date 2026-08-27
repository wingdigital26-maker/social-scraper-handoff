# PC-OFF MAP: what genuinely cannot run when Jack's PC is off

Written 2026-08-26. Every number below came from a command run or a log read on
that date, and the source is named next to it. Where something was not measured,
it says "not measured" rather than guessing.

This document is deliberately the negative half of the picture. Three other
agents are moving what CAN move. This is the list of what cannot, so no lane
quietly goes green while producing nothing.

---

## 1. The capability table

Verdict key: **yes** (works PC-off today), **partial** (runs but degraded or
half the job is missing), **no** (does not work PC-off).

| Capability | PC-off? | Blocker | Evidence | Cost to fix |
|---|---|---|---|---|
| Sonar discovery (search-index queries) | **partial, heavily degraded** | Search backends soft-block datacenter IPs. Empty HTTP 200 pages are indistinguishable from a genuine empty result | Same 24-query script, same day. Runner run 33004154921: 49 results, 7 productive, 17 empty. Runner run 32979332336: 30 results, 3 productive, 15 hard errors. Local run of the identical script: 171 results, 23 productive, 0 errors, 0 empty | Residential or mobile proxy pool, roughly 30 to 80 USD a month for a small rotating plan. Not priced against a specific vendor here, so treat that band as an estimate, not a measurement. Alternative is accepting roughly 3.5x less discovery yield in the cloud |
| Sonar watch lane (`sonar-watch`) | **partial** | Same search block. It is the same `ddgs` path | Run 33000149740 (2026-08-26 18:31 UTC) completed **failure**. Runs at 13:45 and 02:25 succeeded | Same as above. Nothing else fixes it |
| Google Maps identity verification | **no** | `gosom google-maps-scraper` is a Windows-only .exe at `C:\Users\wjack\github-tools\gosom-google-maps-scraper\bin\` (versions 1.16.3 and 1.17.1, both `windows-amd64.exe`). `identity_gate.py` explicitly tolerates its absence in cloud (line 263 comment: "NOT an error in the cloud, where the Windows-only gosom binary cannot run") | Of 93 `verified` candidates in Supabase, **59 were proven by a Maps match** and **34 by their own website**. So 63% of all verification evidence disappears in the cloud | Either accept 63% fewer verifiable leads, or pay for a Maps data API. Not priced here |
| Identity gating of cloud-discovered rows | **no, and it is already failing silently** | Cloud discovery writes rows, nothing gates them | Supabase `candidates`, grouped by `discovered_at`: 2026-08-21 had 69 rows with 18 verified; 2026-08-22 had 857 rows with 69 verified; 2026-08-25 had 62 rows with 6 verified; **2026-08-26 has 75 rows and all 75 have `identity` = NULL** | Needs a Linux-runnable identity path. Not built. See recommendation 2 |
| Claude CLI scheduled tasks (dispatch, prospector, heros-content) | **no, and they do not work with the PC ON either** | Local OAuth session expired and cannot be renewed headlessly | `claude -p "say OK"` returns `Failed to authenticate: OAuth session expired and could not be refreshed`. `WingDigital-Agent-dispatch` last run 2026-08-26 07:45, rc **0x1**. `WingDigital-Agent-heros-content` is **Disabled**, last run **11/30/1999** (never), rc 0x41303 | Jack logs in interactively once. Free. But it fixes only the PC-ON case, see section 3 |
| Claude judgment work generally | **yes, via cloud routines only** | Cloud routines run in Claude's own sandbox, which has **no outbound web egress** and no authorized GHL MCP | Documented in memory `wingos-cloud-agent-migration`. Live routines: Wing OS cloud patrol, Sentinel cloud, Content engines cloud | Already the working pattern: Actions does the network work, the routine does the judgment |
| `prospects.db` (the local SQLite pool) | **no** | 3.9 MB file on Jack's disk at `C:\Users\wjack\ghl-cli\prospects.db`, 6,793 rows. **98 Python files** under `ghl-cli` reference it by name | `ls -la` and `grep -rln "prospects.db" *.py \| wc -l` = 98 | Nothing to fix directly. The cloud copy exists; the sync is what is broken, next row |
| Local to cloud prospect sync | **no, and the gap is wider than documented** | `wing-outreach-cloud/tools/copy_local_to_supabase.py` uses `INSERT ... ON CONFLICT (id) DO NOTHING`, so arming and enrichment never reach rows already in the cloud | Measured today. Local: 1,406 rows with `trade='b2b'` and an email; 1,230 of those in status new/enriching. Cloud: 298 and **123**. Row counts match almost exactly (6,793 local vs 6,786 cloud), so the rows are there and the **values** are stale. The documented 818 vs 90 gap has become **1,230 vs 123** | `ghl-cli/sync_armed_to_supabase.py` was written 2026-08-21 and **has never been run**. Zero money. Needs Jack's promote decision, per the b2b-lead-find staging protocol |
| B2B cold email sending | **yes mechanically, but zero output today** | Runs fine in the cloud. It is paused by Jack's own flag | `outreach-sender` run 32998486097, green in 19s: `outreach_state.paused=TRUE (cloud STOP flag) - Wing sending PAUSED, exiting`. It has run green roughly every 90 minutes all day | Nothing to fix. It is a deliberate switch. See section 2 |
| B2B refill / prospecting (local task) | **no** | `WingRefill-Auto` shells to `python b2b_refill_auto.py` in `C:\Users\wjack\ghl-cli` | Task state Running, last run 2026-08-26 12:47, rc **0x41301** (still running). `WingDigital-B2B-Prospector` last run 2026-08-24, rc **0x8007042B** (process terminated unexpectedly) | A cloud `prospector.yml` exists in wing-outreach-cloud. Whether it is at parity was **not measured** |
| Vault content (the notes themselves) | **yes, mirrored** | None for reading | `Jacks Ai Brain 2.0` is a git repo pushed to private `wingdigital26-maker/wing-os-vault`. Working tree has 1 dirty path. Latest commit `420b54b vault auto-sync 2026-08-26 14:14`, and `origin/master` is at the same commit | Nothing |
| Vault *freshness* | **no** | The mirror is pushed by `WingVaultSync`, a **local** scheduled task running `push_vault.ps1` every 20 minutes | Task last run 2026-08-26 14:09, rc 0x0, next 14:29. `vault_sync.log` shows a real failure at 13:49: `PULL-FAIL (rebase)`, `PULL-FAIL (merge)`, `FAIL git push exit 128`, then recovery at 14:09 | PC off means the vault freezes at its last push. Cloud agents that write to the vault repo directly are unaffected; anything Jack writes in Obsidian is invisible to the cloud until the PC returns |
| Vault size / OneDrive | **yes for the mirror** | 153 MB local folder, but the mirror is git, not OneDrive | `du -sh` = 153M | Nothing |
| Client content publishing (Renewal, Jackson) | **partial, and it failed on its live run** | Publishing itself is cloud-capable, but the last scheduled run errored on live verification | `wing-content-factory` `publish` run 32734057496, 2026-08-24 13:40 UTC, **failure**: three `ERROR: live verify https://renewalhealth.life/blog/... page 404, hero 404`, exit code 2. The only other run is a manual dispatch on 2026-08-22 that succeeded | Debug the publish path. Effort, not money. Until then the weekly cadence is not actually running |
| Hero's Junk weekly content | **no** | Its scheduled task is Disabled and has never once run | `WingDigital-Agent-heros-content`, State **Disabled**, Last Run **11/30/1999**, rc 0x41303 | Rebuild on the content-factory pattern, or accept that this lane does not exist |
| Wing OS watchdog / heartbeat alerts | **yes** | None | `wing-digital-os` `watchdog` workflow running green on schedule roughly every 30 to 60 minutes all day, last 32998237771 at 18:10 UTC | Nothing. This is the one lane that is genuinely, verifiably PC-off |
| Supabase reachability from cloud | **yes** | None | Probe run 33004154921: `Supabase -> HTTP 200` | Nothing |
| Prospect website fetching from cloud | **yes** | None | Probe run 33004154921: `SITE_FETCH_OK=3/3` | Nothing |
| Reddit ingest (`nightly-ingest`) | **no** | The cron is **commented out** in the workflow, pending Reddit API keys | `.github/workflows/nightly-ingest.yml`: `#   - cron: "0 8 * * *"` with a note to add secrets and uncomment | Free Reddit API key. Jack only |
| TikTok / Instagram media | **no** | Datacenter IPs are exactly what these platforms block, and the OSM-sourced spots bypass the oEmbed path entirely | Not re-measured today. Recorded in memory `whatsthemove-social-scraper` | Not priced |
| GHL-dependent lanes (CRM, pipelines, reply inbox, booking) | **n/a, gone** | GHL fully retired 2026-08-22 | Memory `ghl-locations-not-active` | These have **no replacement**. PC-off is not the issue; they do not exist |

---

## 2. What silently produces nothing when the PC is off

These are the dangerous ones. They run. They go green. They deliver nothing or
near-nothing, and no alert fires.

**1. Sonar cloud discovery, in the empty slices.** The 2026-08-26 08:37 UTC
`sonar-daily` run (32948667720) completed **success** in 7m18s. Reading its log:
the first discover slice produced 48 candidates. The next three slices each
printed `dup : 0 | kept : 0`. Three of four slices produced literally zero, and
the run is still marked green. Because a blocked query and a genuinely empty
query look identical (HTTP 200, empty page, `ddgs` raises "No results found"),
there is no signal anywhere that says "you were blocked."

**2. Identity verification of everything the cloud discovers.** All 75 rows
discovered on 2026-08-26 have `identity` = NULL. They were never gated, because
the gate needs a Windows binary. On days where the gate did run, only 6 to 18
percent of rows came out `verified` and 40 to 50 percent came out
`not_a_business`. So an ungated cloud batch is not "93 good leads pending
review", it is a pile that is roughly half junk with no marker saying which
half. Downstream anything that treats a row as a lead is now working from
unverified data and will not know it.

**3. The B2B send-ready pool.** Local shows 1,230 armed and emailable rows. The
cloud sender's own eligibility query returns **123**. The sender is looking at
one tenth of the pool that the local database, the vault snapshots, and any
dashboard reading `prospects.db` will report. Adding more scraping does nothing
about this. This is the exact failure documented on 2026-08-21 and it has grown,
not shrunk.

**4. The B2B cold email sender itself.** Green every 90 minutes, all day, every
day. `outreach_state.paused=TRUE`, exits in under 20 seconds, sends zero. Jack
paused it deliberately on 2026-08-16, so this is correct behavior, but a green
check mark next to "outreach-sender" is currently not evidence that any email
left the building. If the pause is ever forgotten, this lane looks perfectly
healthy while doing nothing for months.

**5. The three Claude CLI scheduled tasks.** `dispatch` exits rc 0x1 every
morning. `heros-content` has never run once in its life. `prospector` last
succeeded on 2026-08-24 but shells to the same expired binary, so its next run
will fail the same way. These fail whether the PC is on or off, which makes them
worse than a PC-off problem, not better.

---

## 3. What only Jack can do, that no agent can

1. **Re-authenticate the Claude CLI.** Run `claude` interactively on the PC and
   complete the OAuth login. No agent can do this: the flow requires a browser
   session and a human at the keyboard, and headless renewal is exactly what the
   error message says is impossible. Free, takes about a minute.
   Important caveat: **this does not make those tasks PC-off.** They are Windows
   scheduled tasks shelling to a local .exe. Fixing the login fixes the PC-ON
   case only. For PC-off, that work has to become a Claude cloud routine, which
   is a rebuild, not a login.
2. **Get free Reddit API keys** and add them as repo secrets, then the
   `nightly-ingest` cron can be uncommented. Free. Blocks the entire Reddit
   ingest lane today.
3. **Buy the sending domain** for the own-SMTP pipe. `smtp_sender.py` was built
   and tested 2026-08-21 and has been blocked on this since. Roughly 10 to 15
   USD a year for the domain itself, plus whatever mailbox hosting costs.
4. **Decide on promoting the 1,230 armed rows** to cloud send-ready. Per the
   b2b-lead-find staging protocol this is deliberately Jack's call, which is why
   `sync_armed_to_supabase.py` was written and never run.
5. **Unpause outreach**, or confirm it stays paused. Right now it is a silent
   no-op wearing a green check.

---

## 4. Recommendations, ranked, with real numbers

**1. Run `sync_armed_to_supabase.py --confirm`, or accept a sender that sees 10%
of the pool.** Cost: zero dollars, about five minutes, plus Jack's promote
decision. Payoff: the cloud eligible pool goes from 123 toward 1,230. This is
the single highest-value item on the list and it is already built. Nothing else
in this document has a better ratio.

**2. Stop pretending cloud-discovered rows are verified. Mark them.** Cost: a
small code change, zero dollars. Any row with `identity` = NULL that came from a
cloud run should be visibly flagged as ungated everywhere it surfaces, and
should never enter a call queue. Right now the 75 rows from today are
indistinguishable in shape from a verified row. Longer term the honest options
are: (a) run the identity gate locally as a nightly catch-up whenever the PC is
on, accepting that it is a PC-tethered step, or (b) pay for a Maps data API.
Option (a) costs nothing and is what I would do. Option (b) was not priced.

**3. Accept that the Maps identity leg needs the PC. Do not try to move it.**
Cost: an accepted quality loss. 59 of 93 verified candidates were proven by a
Maps match. There is no Linux build of that binary in the tree, the code already
has a documented cloud-skip path, and the honest answer is that this lane is
PC-bound until somebody pays for a data source. Say so out loud rather than
letting a cloud run imply verification it did not do.

**4. Either fix Sonar's cloud search yield with proxies, or downgrade what the
cloud lane claims to do.** Measured today, three times: the runner gets 3 to 7
productive queries out of 24 where the same script on the PC gets 23 out of 24.
That is not a tuning problem, it is an IP problem. A rotating residential proxy
plan is the only real fix and would run in the tens of dollars per month (band
not verified against a vendor). If Jack does not want to pay, the right move is
to say plainly that cloud Sonar is a top-up and the PC is the real discovery
engine, and to size expectations at roughly one third to one seventh of local
yield.

**5. Fix the `wing-content-factory` publish failure before trusting the weekly
cadence.** Cost: debugging effort, zero dollars. Its one and only live scheduled
run (2026-08-24) failed with three `page 404, hero 404` live-verify errors. The
health gate did its job and refused to lie, which is good, but the practical
state is that PC-off publishing has not yet succeeded on a schedule even once.

**6. Re-authenticate the Claude CLI, then decide the fate of the three tasks.**
Cost: free. Two of the three (`dispatch`, `prospector`) are worth keeping as
PC-on tools. `heros-content` has never run in its entire existence and should
either be rebuilt on the cloud content-factory pattern or deleted, not left
Disabled where it looks like a capability that exists.

**7. Accept that vault freshness is PC-bound.** Cost: an accepted limitation.
The vault *content* is safely mirrored and the cloud can read it fine. But the
push is a local task every 20 minutes, so with the PC off the cloud reads a
frozen snapshot. That is fine for a weekend and not fine for a week. Anything a
cloud agent needs to act on must be written by the cloud into the vault repo
directly, never assumed to have been synced up from Obsidian.

---

## 5. What I could not measure

- TikTok and Instagram reachability from a runner was not re-tested today. The
  claim that datacenter IPs are blocked there comes from prior memory, not from
  a measurement I ran.
- Whether `wing-outreach-cloud/.github/workflows/prospector.yml` is at feature
  parity with the local `b2b_refill_auto.py` lane. Not measured.
- Actual proxy pricing. The band quoted in recommendation 4 is an estimate and
  is labelled as such.
- Three other agents were actively editing workflows in `social-scraper-handoff`,
  `wing-outreach-cloud` and `wing-digital-os` while this was written. Everything
  above reflects the state read on 2026-08-26 between roughly 19:10 and 19:30
  UTC and may have moved since.
