# DM / email lane for Hero's Junk Removal and Jackson Roofing — findings

## 1. Compliance finding first, because it settles half the question

Hero's Junk Removal's recorded contract scope (from
`Jacks Ai Brain 2.0/wiki/clients/heros-junk-removal.md`, Scope section):

> SEO and content, plus social and email tracking. **No outbound sending on
> their behalf** — Wing does not cold-email for Hero's.

That is unambiguous. Hero's did not sign up for outbound contact of any kind,
DM, reply, or email. Building or running a "DM or reply to people asking for
junk removal" lane for Hero's is outside what they agreed to. This should not
happen for Hero's without going back to Jack and, likely, back to the client
to change scope. It is not a judgment call, it is a documented no.

Jackson Roofing has no equivalent "no outbound" line found in its client
page, so the discussion below (reachability, handle gap, recommendation)
applies to Jackson only unless Hero's scope changes.

## 2. Reachability matrix: what contact is actually possible

The pipeline (`ingest/watch_social.py`) finds these posts by running search
queries like `site:reddit.com/r/plano roofer` or `site:nextdoor.com ...`
against a general search index, not through either platform's own API. That
matters for what's reachable:

| Channel | Public in-thread reply | Private DM |
|---|---|---|
| Reddit | Possible with a normal logged-in Reddit account, no API key needed to post a comment through the website. Against no platform rule by itself if the comment is genuinely on-topic and not spam. | Requires a Reddit account (authenticated) and, if done at any volume/automation, Reddit's Data API Terms govern it. Reddit's terms and community norms treat unsolicited commercial DMs as spam/solicitation, which can get an account suspended. I could not fetch Reddit's current API/policy pages from this environment to quote the exact clause, so treat "unsolicited commercial DM = against the rules" as **very likely true but unverified against the live document** — Jack should confirm by reading Reddit's Data API Terms and site-wide rules before anyone sends one. |
| Nextdoor | Possible only through a real, verified Nextdoor account tied to a real address in that neighborhood (Nextdoor requires address/identity verification to post at all). No API key path exists for Wing to post as the business from outside. | Nextdoor has no public API for sending direct messages at all — confirmed by the codebase audit ("no DM capability anywhere in this codebase... no Nextdoor... messaging"). Any DM would have to be done manually, by a human, logged into a real Nextdoor account, which is itself against the spirit of an unsolicited commercial outreach. |

Bottom line: there is no API-based DM capability for either platform today.
The only channel that doesn't require an authenticated human clicking around
inside someone's account is a public in-thread reply, and even that needs a
real logged-in account to post.

## 3. The handle gap

Every row in `outbound` has `recipient` = the post title and
`recipient_handle` = NULL. This is not a bug, it's what the data source
provides. `watch_social.py` gets its hits from search-engine results (title,
URL, snippet) — see the fields it builds at lines ~705-745: `recipient`,
`recipient_url`, `evidence_url`, `subject`, `body`. There is no `author`,
`username`, or `handle` field anywhere in that dict, and no fetch of the
actual Reddit/Nextdoor page happens in the pipeline today.

Could it be captured? Conceptually, yes, but only with a change that isn't
built: the pipeline would need to actually fetch the post page itself (not
just the search snippet) and parse out the author username from the page
HTML/JSON. For Reddit this is straightforward in principle since author is
always in the page data. For Nextdoor it's harder because Nextdoor gates
most content behind login, so an unauthenticated fetch likely can't see the
poster's name at all. This is a "would need to build a page-fetch + parse
step" gap, not implemented here, not implementing it now per instructions.

## 4. Email

Social posters on Reddit or Nextdoor do not expose an email address in the
post, the profile, or the search snippet. Reddit accounts are pseudonymous
by design and don't show email. Nextdoor profiles are gated behind login and
don't surface email to outsiders either. There is no scraped or scrapable
email address anywhere in this data path.

Plainly: **email is not a real option for this lane.** "DM or email" as
phrased can't both be satisfied from Reddit/Nextdoor post data. If email
outreach is wanted, it needs a different source entirely (e.g. a real lead
form, not a scraped social post).

## 5. Recommended lane

For Jackson Roofing (Hero's excluded per section 1):

- **A public, helpful, on-topic reply in the thread is the only lane that is
  both realistically permitted and likely to work.** It requires a real
  logged-in account, is visible and low-risk if it reads as genuinely
  helpful rather than salesy, and doesn't touch platform messaging systems
  that either don't have a public API (Nextdoor) or treat unsolicited
  commercial contact as spam (Reddit, likely, unverified exact clause).
- An unsolicited DM on either platform risks the account it's sent from —
  Reddit shadowbans/suspends accounts for spammy DMs, and Nextdoor DMs would
  have to come from a real neighbor account, which isn't something Wing
  should be operating on Jackson's behalf without Jackson's own explicit,
  verified account.
- Practical next step if Jack wants to move on this: keep the current
  `status: needs_location_check` / drafted-reply pattern already in the
  pipeline, route it to a human (Jack or Jackson's team) who posts the reply
  from a real, owned account, and drop DM entirely from the plan unless a
  real messaging API becomes available later.
