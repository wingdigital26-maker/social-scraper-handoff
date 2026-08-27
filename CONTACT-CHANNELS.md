# CONTACT-CHANNELS

How Wing Digital can actually contact a lead once Sonar has found one.

Researched 2026-08-26. Every policy claim below carries a URL and the date it was checked.
Where I could not reach a primary source, the line says "not verified" and says what I tried.
Nothing here is guessed.

---

## 1. Headline verdict

Jack's prior was **correct on all three counts**. Cold DM automation on Instagram, Facebook
and TikTok is structurally blocked, not merely hard. The block is in the API surface itself,
so there is no "apply for more access" path. Anything that reaches a stranger's DM inbox at
scale has to be unofficial automation driving a logged-in account, and the account being
driven would be the client's, not Wing's.

The good news is that the legitimate channels are not the leftovers. Public reply plus phone
plus a small volume of researched named-person email is a better system than cold DM ever was,
and it is the one Sonar was already designed for.

---

## 2. Verdict table

| Channel | Possible? | Permitted? | Cost | Risk | Citation |
|---|---|---|---|---|---|
| Instagram DM, cold | No | No | n/a | Client IG account and connected Page | Meta: "Only after an Instagram user has sent your app user's Instagram professional account a message can your app send a message to the Instagram user." [IG Messaging API](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api) checked 2026-08-26 |
| Instagram DM, reply to inbound | Yes | Yes, 24h | Free | Low | Same page: "Your app has 24 hours to respond to any message sent from an Instagram user to your app user." Human agent tag extends this for a real person's reply |
| Facebook Messenger DM, cold | No | No | n/a | Client Page | Meta: "Businesses have up to 24 hours to respond to a user." Outside it you need tags, Sponsored Messages or One-Time Notifications. [Messenger policy overview](https://developers.facebook.com/docs/messenger-platform/policy/policy-overview/) checked 2026-08-26 |
| Facebook Sponsored Messages | Yes | Yes, but not cold | Ad spend | Low | Meta: "Sponsored Messages allow businesses to reengage with people who have an **open conversation** with their Page in Messenger." No prior conversation means no Sponsored Message. Same page, checked 2026-08-26 |
| Facebook message tags outside 24h | Yes | Yes, non-promotional only | Free | Messaging restrictions | Meta: tags are "for a set of approved use cases" and use "outside of approved use cases may result in restrictions on your ability to send messages." Message Tags "may not be used to send promotional content, including... deals, offers, coupons, and discounts." Same page, checked 2026-08-26 |
| TikTok DM | No | No | n/a | Client TikTok account | TikTok's developer products are Display API, Content Posting API, Research API and Data Portability API. No send-message endpoint exists in any of them. The only DM surface is Data Portability read-only export scopes (`portability.directmessages.single` / `.ongoing`), which export the authorising user's own data and currently cover EEA and UK users only. **Partially verified**: developers.tiktok.com refused every fetch from this environment (ECONNRESET on five attempts, curl blocked by sandbox, browser navigation denied), so this rests on search-indexed excerpts of TikTok's own docs rather than a page I read end to end. Treat as high confidence, not certainty |
| Unofficial DM automation, any platform | Yes, technically | **No** | Tool fees | **Client account ban** | See section 3 |
| Business email, named person, from website | Yes | Yes, with CAN-SPAM compliance | Free scrape, domain cost | Domain reputation | See section 4 |
| Role inbox (info@, sales@) | Yes | Yes | Free | Wasted sends, deliverability | See section 5 |
| Website contact form | Yes, on unprotected forms | Grey. Site ToS varies | Free | Low legal, poor deliverability | See section 6 |
| Phone | Yes | Yes, B2B calls are TSR-exempt | Jack's time | Low | 16 CFR 310.6(b)(7) exempts "Telephone calls between a telemarketer and any business to induce the purchase of goods or services... by the business" [Cornell LII](https://www.law.cornell.edu/cfr/text/16/310.6) checked 2026-08-26 |
| Public reply to a public ask | Yes | Yes | Free | Community norms only | See section 7 |
| LinkedIn scraping or automated messaging | Yes, technically | **No** | n/a | Account ban plus litigation | LinkedIn User Agreement 8.2.13 forbids "bots or other unauthorized automated methods to access the Services, add or download contacts, send or redirect messages" [LinkedIn UA](https://www.linkedin.com/legal/user-agreement) checked 2026-08-26. See section 8 |

---

## 3. DO NOT BUILD

Each item here would either get an account banned or break a law. Named specifically, because
in most cases the account at risk is **the client's**, not Wing's.

### 3.1 Instagram or Facebook DM automation through an unofficial path

This means Selenium, Playwright, a mobile private API, a session cookie, or any paid
"IG DM sender" tool. All of them work by driving a logged-in account.

Whose account is at risk: **the client's Instagram professional account, and the Facebook Page
linked to it.** On Meta those two are joined. Losing the IG account can take the Page with it,
which takes the ad account and the review history with it. For a junk removal company whose
lead flow runs through that Page, that is a business-ending event caused by Wing.

The official API forecloses the cold case explicitly. Meta: "Only after an Instagram user has
sent your app user's Instagram professional account a message can your app send a message to
the Instagram user" ([source](https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/messaging-api), checked 2026-08-26). The paid escape hatch does not
help either, because Sponsored Messages only reach people "who have an open conversation with
their Page in Messenger" ([source](https://developers.facebook.com/docs/messenger-platform/policy/policy-overview/), checked 2026-08-26).

There is no cold path. Do not build one.

### 3.2 TikTok DM automation

No API exists to send a DM, so any implementation is browser or private-API automation of a
logged-in account. TikTok's Community Guidelines prohibit "using automation to register or
operate accounts in bulk" and prohibit using "bots, scripts, or other means to distribute
content or interactions in bulk"
([TikTok Community Guidelines, Integrity and Authenticity](https://www.tiktok.com/community-guidelines/en/integrity-authenticity), checked 2026-08-26 via search excerpt; the page itself
was not fetchable from this environment).

Whose account is at risk: **the client's TikTok account**, including any Business Account
status and any content that is currently ranking.

### 3.3 LinkedIn scraping, connection blasting, or automated InMail

LinkedIn's User Agreement forbids both the scraping and the messaging:

- 8.2.2: "Develop, support or use software, devices, scripts, robots or any other means or
  processes (such as crawlers, browser plugins and add-ons or any other technology) to scrape
  or copy the Services"
- 8.2.13: "Use bots or other unauthorized automated methods to access the Services, add or
  download contacts, send or redirect messages, create, comment on, like, share, or re-share
  posts, or otherwise drive inauthentic engagement"
- 8.2.4: forbids copying or distributing information obtained from the Services "without the
  consent of the content owner"

([LinkedIn User Agreement](https://www.linkedin.com/legal/user-agreement), checked 2026-08-26.)

And LinkedIn enforces it in court. In hiQ Labs v. LinkedIn the court found hiQ breached the
User Agreement, and the case ended in December 2022 with a consent judgment: a permanent
injunction against scraping, deletion of all scraped data and derived code, and 500,000 USD in
damages ([Morgan Lewis](https://www.morganlewis.com/blogs/sourcingatmorganlewis/2022/12/linkedin-v-hiq-landmark-data-scraping-suit-provides-guidance-to-data-scrapers-and-web-operators),
[Privacy World](https://www.privacyworld.blog/2022/12/linkedins-data-scraping-battle-with-hiq-labs-ends-with-proposed-judgment/), both checked 2026-08-26). These are law firm
commentaries, not the docket itself, which is a secondary source. The direction of the outcome
is not in dispute across them.

The 2019 Ninth Circuit ruling that people quote as "scraping is legal" was only about the
Computer Fraud and Abuse Act. It never blessed breaching the contract, and the contract claim
is the one LinkedIn won.

Whose account is at risk: whichever account does the scraping or the messaging. If Wing runs it
from Jack's own LinkedIn, that is Jack's professional identity. If Wing runs it from a client's,
it is theirs. Sonar's existing design already refuses to touch LinkedIn directly and instead
reads only what LinkedIn lets Google index. That decision is correct and should not be
"improved".

### 3.4 Fake or burner social accounts to do any of the above

Explicitly banned by LinkedIn 8.2.1 ("Create a false identity on LinkedIn, misrepresent your
identity") and it is the exact conduct that produced the hiQ damages award. It also converts a
terms breach into something a plaintiff can call deception.

### 3.5 Any CAPTCHA solving service, or any automation of a logged-in area of a third-party site

Out of scope by Jack's own rule and a bad idea independently. If a form is behind a CAPTCHA,
the operator has stated they do not want machine submissions. Respect it and use another
channel.

### 3.6 Emailing without CAN-SPAM compliance

Commercial email must have accurate header and From information, a non-deceptive subject line,
a clear disclosure that it is an advertisement, a valid physical postal address, and a clear
working opt-out that is honoured promptly. Each violating email is exposed to a civil penalty
of up to 53,088 USD ([FTC CAN-SPAM Act: A Compliance Guide for Business](https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business), checked
2026-08-26). **Partial verification note**: ftc.gov returned HTTP 403 to my direct fetch, so
these requirements come from FTC-hosted search results rather than a page I rendered. The rule
text itself is at 16 CFR Part 316. Before the first real send, read the FTC page in a browser
and confirm the current penalty figure, which is inflation-adjusted annually.

`smtp_sender.py` already appends an unsubscribe footer and checks suppression before every
send. The physical postal address and the advertisement disclosure need to be confirmed present
in the template before the first live send. That is a five minute check, not a build.

### 3.7 Cold-emailing an existing Wing client

Already prevented. `smtp_sender.py` suppression matches on domain as well as address
(`is_suppressed` falls through to the domain), so one entry blocks every mailbox at a client.
Keep it. Never trim the suppression file to raise send volume.

---

## 4. EMAIL: how well does the website mining actually work

`ingest/contact_find.py` is the right design and it is not the problem.

### What it does

It crawls the homepage plus up to six about, team, staff and contact pages, and records only
addresses that literally appear in bytes it downloaded. It never constructs `firstname@domain`
from a name. It rejects addresses whose domain is not the business's own, respects robots.txt,
retries network failures rather than recording them as "no email", and refuses to attach a
person's name to an address when the match is ambiguous. Those constraints are why the numbers
look bad, and they are correct. A fabricated address bounces, and bounces on a cold domain are
exactly what kills the domain.

### The real numbers, read from the file today

From `leads/callable93.json`, 86 rows:

| Field | Count |
|---|---|
| Rows | 86 |
| With a phone number | 86 (100%) |
| With a website | 64 (74%) |
| With any email at all | 17 (20%) |
| Role inbox (info@, sales@, help@) | 15 |
| Genuinely a named person's mailbox | 1 (`firstname@examplerooferC.com`, Ralph Harris) |
| Business Gmail, not a person | 1 (`examplecity.roofer@gmail.com`) |
| With a named human contact of any kind | 8 |

So the honest yield is roughly one usable named-person email per 86 leads. Jack's "2 personal"
figure is one real person plus one business Gmail that the classifier labels `unknown` rather
than personal, which is the classifier being honest.

### Why so few

Four reasons, in order of size:

1. **Small trades genuinely do not publish an owner email.** They publish a phone number and a
   form, on purpose, because the phone is how they get jobs. There is no address on the page to
   find. This is most of the gap and no amount of engineering fixes it.
2. **26% have no website at all** in this set, so there is nothing to mine.
3. **Obfuscation and JS rendering.** The miner handles `mailto:`, `&#64;`, and "name [at]
   domain", but not addresses injected by JavaScript after page load or drawn into an image.
4. **The crawl budget is seven pages** and stops at about/team/contact URL patterns. Owner
   names on trade sites often live on a blog post or a careers page.

### What would actually move the number

Ranked by yield per unit of effort. All of these are legitimate.

1. **Facebook Page "About" and Google Business Profile.** Local trades often list an email on
   their Facebook Page even when the website has none. Public data, no login needed if it is
   indexed. Realistically the single biggest lift for the local-services lane.
2. **State business registrations.** In Texas, entity records and registered-agent filings
   frequently name the owner, and sometimes carry an address. This turns "no name" into "name",
   which is what makes a phone call land.
3. **Render JavaScript on the ~20% of sites that return no address but do have a contact page.**
   The repo already has Scrapling available for this. Modest lift, bounded cost.
4. **Accept that the answer is often "phone".** For a 100-lead local sheet, the correct
   expectation is 1 to 3 named emails and 100 phone numbers. Design the outreach around that
   fact instead of fighting it.

What would **not** help: buying a verification or enrichment tool that guesses patterns. It
produces addresses that were never published, which is exactly what `contact_find.py` was
written to refuse, and the bounces land on a domain with no reputation.

---

## 5. ROLE ADDRESSES: is the "never cold-email info@" policy right?

**Mostly right, and it should be softened rather than kept absolute.**

The evidence in the codebase is strong. `contact_find.py`'s own header records 133 sends to
role inboxes with 0 replies, against one researched email to a named founder that closed a
deal. That is a real signal from Wing's own send history, not a rule of thumb.

The tradeoffs, honestly:

**Against role inboxes**
- Zero for 133 in Wing's own data.
- Role inboxes are the most spam-filtered addresses a company has, and they are frequently
  seeded into spam traps. On a cold domain during warmup, a run of role-inbox sends with no
  opens is the fastest way to teach the filters that the domain sends unwanted mail.
- The reader is usually a receptionist or an office manager with no authority to buy, and no
  incentive to forward.

**For role inboxes**
- They are the only address that exists for most local businesses.
- For B2B, `sales@` and `info@` at a 3PL are actually monitored by people whose job is to
  respond to inbound, which is a materially different situation from `info@` at a five-person
  roofing company.
- A role inbox is a legitimate published business contact. Emailing it is not wrong, it is
  just usually ineffective.

**Recommended policy change**: keep the ban for the local-services lane, where it is backed by
data. For the B2B lane, allow `sales@` at target companies, but only after the domain is warm,
only as a fallback when no named contact exists, capped at a small fraction of daily volume,
and tracked separately so the reply rate is measurable rather than assumed. If it comes back
zero for fifty, close it for good.

Never send to `noreply@`, `postmaster@`, `abuse@`, or `webmaster@` under any circumstances.
Those are the trap addresses.

---

## 6. WEBSITE CONTACT FORMS

**Can they be filled programmatically?** Technically yes for a plain form, no for anything
behind a CAPTCHA, and a CAPTCHA is off the table by rule.

**Should Wing build this?** No, not as an automated channel. Reasons:

1. **Off the table wherever there is a CAPTCHA.** Modern reCAPTCHA v3 and Turnstile are
   invisible and score-based, so there is often no visible challenge to detect, and the
   submission simply gets scored as a bot and silently dropped. Wing would have no idea the
   message never arrived. That is worse than not sending, because the pipeline reports success.
2. **Deliverability is not actually better.** A contact-form submission arrives as an email
   from the site's own form handler. If it reads as a sales pitch it goes to the same place a
   cold email would, minus the ability to track opens, minus a reply-to thread the prospect
   can just hit reply on.
3. **Terms.** Many sites' terms restrict automated submission. There is no single citation here
   because it is per-site, so I will not claim a general legal rule. **Not verified as a
   bright-line prohibition**; the honest statement is that it is a per-site contract question,
   which makes it unreviewable at scale.
4. **It burns the one channel that was working.** A local business that gets an obviously
   automated form fill remembers the name. Wing then calls them next week.

**Where it does belong**: as a manual, human step for a small number of high-value B2B targets
where no other address exists. Jack, or a VA, fills five forms. That is fine and always was.
Do not automate it.

---

## 7. PHONE

This is Wing's strongest channel today and the data says so plainly: **86 of 86** leads in
`callable93.json` have a phone number, against 1 with a named-person email.

**Legally clean for B2B.** The Telemarketing Sales Rule exempts "Telephone calls between a
telemarketer and any business to induce the purchase of goods or services or a charitable
contribution by the business" (16 CFR 310.6(b)(7),
[Cornell LII](https://www.law.cornell.edu/cfr/text/16/310.6), checked 2026-08-26). The National
Do Not Call Registry provisions do not apply to genuine business-to-business calls. Calling a
roofing company's published business line to sell them marketing services is a B2B call.

Two caveats that are real:

- The exemption is not total. Even for exempt B2B calls, 310.3(a)(4) still applies, so honour
  an entity-specific "do not call us again" request immediately and permanently. Same source.
- The exemption covers calls to a business about the business. It does not cover calling
  someone's work line to sell them something personally. Wing's use case is squarely on the
  right side of that line.
- The FTC extended the TSR's misrepresentation prohibitions to B2B calls in 2024, meaning
  false or misleading claims on a B2B sales call are now actionable
  ([FTC press release, March 2024](https://www.ftc.gov/news-events/news/press-releases/2024/03/ftc-implements-new-protections-businesses-against-telemarketing-fraud-affirms-protections-against-ai),
  checked 2026-08-26 via search result; ftc.gov blocked direct fetch). Practical effect: do not
  invent stats on calls. Wing's audit-first approach already avoids this.

**Where it sits versus digital**: phone is the channel with the highest coverage, the shortest
path to a booked call, and zero platform risk. Its only cost is Jack's calendar. It does not
scale past one person, which is exactly why it should be paired with something that does.

The existing `call-prep` and `call-day` skills already turn a lead sheet into a briefed dial
list, so this channel is built, not hypothetical.

---

## 8. PUBLIC REPLIES INSTEAD OF DMs

**This is the strategically correct answer and it deserves more weight than everything else in
this document.**

Replying in public to a person who has publicly asked for a recommendation is not outreach in
the legal sense at all. Nobody is being contacted. A person posted a question in a public
forum, and a business answered it in the same public forum. That is participation, not
solicitation. It sits outside CAN-SPAM entirely, because CAN-SPAM governs commercial electronic
mail messages. It sits outside the TSR, because no call is placed. And it sits outside the DM
policies, because no message is sent to an inbox.

The practical differences from a cold DM, all in Wing's favour:

- **Intent is already declared.** "Anyone know a good junk removal company in Plano?" is a lead
  that has raised its own hand. A cold DM interrupts someone with no stated need.
- **Social proof compounds.** The reply is visible to everyone reading the thread, and it stays
  visible. A DM reaches one person once.
- **It builds the client's account instead of endangering it.** Every helpful public reply is
  engagement on the client's profile.
- **Failure is cheap.** An ignored reply costs nothing. A flagged DM costs an account.

Where this works, ranked by how cleanly it can be automated:

1. **Reddit.** Official API, local subreddits, an enormous volume of "recommend me a X in Y"
   posts, and a documented way to read them. The repo already has `ingest/reddit_ingest.py`.
   **Not verified**: I could not fetch Reddit's Data API Terms from this environment
   (redditinc.com is blocked by the fetch tool), so before automating any posting, read those
   terms directly and check the target subreddit's rules on self-promotion. Most local subs
   allow a business to answer a direct request but ban unprompted promotion. This distinction
   is the whole ballgame and must be enforced in code, not vibes.
2. **Nextdoor.** The single highest-intent surface for a DFW junk removal company. The repo
   already has a `nextdoor-post-publisher` skill. **Not verified**: Nextdoor's business terms
   and any API access were not checked in this pass.
3. **Facebook local groups.** Very high intent, but posting requires a logged-in personal
   account in each group and there is no API for it. **Manual only.** Do not automate.
4. **Google Business Profile Q&A.** Public questions on a business listing can be answered.
   Lower volume, but permanent and SEO-visible.

**The correct build**: Sonar already finds these posts. What is missing is the last mile.
Sonar should surface a public ask, draft a reply in the client's voice, and put it in the review
queue for a human to post with one click. Detection and drafting are automated. Posting stays
human. That keeps Wing on the right side of every platform rule while still doing 95% of the
work, and it is a small delta on top of what already exists.

---

## 9. LINKEDIN

Verified, and this project's recorded position is correct. See section 3.3 for the User
Agreement clauses and the hiQ outcome. Short version: LinkedIn's terms forbid scraping and
forbid automated messaging, LinkedIn enforces in court, and it won a 500,000 USD consent
judgment plus a permanent injunction against a company that scraped it.

**What is still allowed**: reading LinkedIn profiles that LinkedIn has let Google index, which
is exactly what `social_discover.py` already does. That is querying a public search index, not
scraping LinkedIn. Do not change it.

**What is allowed but does not scale**: Jack personally connecting and messaging as himself, by
hand, in the LinkedIn UI. For the 3PL lane that is genuinely a good channel. It is just a human
channel, not a system.

---

## 10. Things not on Jack's list that are worth more than DMs

1. **Answer the question of who to talk to, not just how to reach them.** The single most
   valuable field the pipeline is missing is a decision-maker's name, present on only 8 of 86
   rows. A name changes a cold call into a warm one. Texas entity filings and Facebook Page
   About sections would both raise this materially, and neither has a ToS problem.

2. **Let inbound do the DM work.** Cold DM is blocked, but replying to an inbound DM within 24
   hours is explicitly permitted and Wing can automate it. If a client runs any ads or posts
   any content, people DM them. An automated first response that qualifies and books is fully
   inside Meta's rules, uses the API Wing can actually get, and is worth more than any cold
   sequence. This inverts the problem: instead of fighting to get into inboxes, make the inbox
   ring and answer it instantly.

3. **Retargeting instead of DMs.** Meta Custom Audiences from a website visitor list is the
   sanctioned, paid version of "contact people who showed interest". It costs ad spend rather
   than platform risk. For the 3PL lane a small budget against a scraped-then-uploaded prospect
   list is worth testing, subject to Meta's own data-use terms, which were **not verified** in
   this pass.

4. **Direct mail for the local lane.** Unglamorous and it works for trades. Addresses are on
   Google Maps for essentially every one of the 86. No platform can ban it. Costs postage.

5. **SMS: do not.** Text messaging is governed by the TCPA and requires prior express written
   consent for marketing. There is no scraped-list path to compliant cold SMS, and the statutory
   damages are per message. **Not verified in this pass** beyond general knowledge, and it does
   not matter, because there is no version of this that is worth the exposure. Skip it.

6. **Be the answer instead of the outreach.** Wing already runs content engines that rank.
   A page ranking for "3PL for ecommerce brands" produces inbound leads with no channel risk at
   all. Slow, compounding, and it is the only channel nobody can take away.

---

## 11. Recommended contact stack: local consumer-services (junk removal, DFW)

The defining facts: 100% have a phone, roughly 1 in 86 has a named email, the buyer is the
owner and answers their own phone, and the geography is one metro. Volume is low and intent is
local.

| Rank | Channel | Why | Automation |
|---|---|---|---|
| 1 | **Public reply to local asks** | Highest intent that exists. Nextdoor, local subreddits and Facebook groups are full of "who do I call for X in Plano". Costs nothing, builds the client's account | Sonar detects and drafts, **human posts** |
| 2 | **Phone** | 100% coverage. The owner answers. One call beats twenty emails in this segment | `call-prep` briefs, Jack dials |
| 3 | **Google Business Profile** | Q&A answers and review replies are public, permanent and feed local SEO | Semi-automated, human approves |
| 4 | **Inbound DM auto-reply** | Once the client posts anything, people DM. Answering within 24h is explicitly permitted | Fully automatable via IG Messaging API |
| 5 | **Direct mail** | Address is on Maps for all 86. No platform risk | Batch, manual |
| 6 | **Email** | Only 1 named address per 86. Not a channel at this segment, it is a bonus when it appears | `smtp_sender.py`, tiny volume |

**Do not** cold-email `info@` in this lane. Zero for 133 in Wing's own history, and on a
warming domain it is actively harmful.

---

## 12. Recommended contact stack: B2B 3PL selling to ecommerce brands nationally

Completely different problem. The buyer is a named person with a title, at a company with a
real domain and a real mail server. Geography is irrelevant, deal size is large, and volume
matters because the target list is national.

| Rank | Channel | Why | Automation |
|---|---|---|---|
| 1 | **Email to a named decision-maker** | This segment publishes team pages, has corporate domains, and reads email. It is the one segment where `contact_find.py`'s yield should be good rather than 1 in 86 | `contact_find.py` then `smtp_sender.py`, 40/day/mailbox cap, warmup ramp |
| 2 | **Phone follow-up on the email** | Email opens the door, the call closes it. B2B calls are TSR-exempt | `call-prep` |
| 3 | **Jack's own LinkedIn, by hand** | Correct channel for this buyer. Manual, in the UI, as himself. Never automated | **None.** Human only |
| 4 | **`sales@` as a fallback** | Monitored at real companies, unlike at a five-person roofer. Only after the domain is warm, capped and measured separately | `smtp_sender.py`, small share of volume |
| 5 | **Manual contact form** | For high-value targets with no published address. Five per week by hand | **None.** Human only |
| 6 | **Content that ranks** | The 3PL buying cycle starts with a search. Slowest, but zero channel risk | Existing content engines |

The whole B2B stack is gated on the sending domain. Nothing above rank 2 can start until Jack
buys one and it warms.

---

## 13. What Jack must personally buy or decide

**Buy**

1. **A sending domain.** This is the blocker on the entire B2B lane and it is the highest-value
   twenty dollars in the business right now. Requirements:
   - A **separate** domain from Wing's primary one. Cold email must never be able to damage the
     domain that carries client work and Jack's own mail.
   - Something adjacent and honest, for example a `wingdigital`-style variant or a
     `try<something>` domain, never a lookalike of another company.
   - Cost: roughly 10 to 20 USD per year, plus mailbox hosting for 2 to 3 mailboxes at roughly
     6 USD each per month.
   - Configure SPF, DKIM and DMARC before the first send. Without all three, a new domain's
     mail goes to spam regardless of content.
   - Then the 3-week warmup ramp already coded into `smtp_sender.py` runs before any real
     volume. Do not shortcut it. Set `OUTREACH_MAX_PER_MAILBOX` and let the ramp do its job.
   - Lead time from purchase to real sending: about three weeks. Buying it today is worth more
     than any code written this week.

2. Optionally, mailbox hosting for the 2 to 3 rotation mailboxes `smtp_sender.py` expects. Env
   vars only, never values in this file or any other.

**Decide**

3. **Whether Wing will ever post publicly on a client's behalf, and under whose name.** The
   public-reply channel is the best one available, and it requires posting from an account. Get
   this in writing in the client agreement: which account, what tone, and that the client sees
   drafts. This is a contract question, not a code question.

4. **Whether `sales@` is allowed in the B2B lane.** Section 5 recommends yes with limits. Jack
   owns that call because it is his domain reputation.

5. **That cold DM is off the table, permanently.** Write it into the client agreement so no
   future client asks for it and no future agent builds it. The reason to put it in writing is
   that the risk is asymmetric: the upside is a few conversations, the downside is a client's
   Instagram and Facebook Page.

6. **Confirm the CAN-SPAM footer is complete** before the first live send: physical postal
   address present, advertisement disclosure present, working unsubscribe. The unsubscribe and
   the suppression check are already in `smtp_sender.py`. The address and the disclosure need
   eyes on the template.

---

## 14. Verification log

| Claim | Source | Status |
|---|---|---|
| IG requires user to message first | developers.facebook.com IG Messaging API | Fetched and quoted 2026-08-26 |
| Messenger 24h window, tags, sponsored messages | developers.facebook.com Messenger policy overview | Fetched and quoted 2026-08-26 |
| Sponsored Messages need an open conversation | Same | Fetched and quoted 2026-08-26 |
| TikTok has no send-DM API | developers.tiktok.com | **Partially verified.** Site refused all fetches (5x ECONNRESET, curl sandbox-blocked, browser navigation denied). Rests on search-indexed excerpts of TikTok's own docs |
| TikTok bans bulk automation | tiktok.com Community Guidelines | **Partially verified.** Search excerpt of TikTok's own page, page not rendered |
| LinkedIn UA 8.2.1 / 8.2.2 / 8.2.4 / 8.2.13 | linkedin.com/legal/user-agreement | Fetched and quoted 2026-08-26 |
| hiQ consent judgment, injunction, 500k USD | Morgan Lewis and Privacy World | Secondary sources, two independent, checked 2026-08-26 |
| TSR B2B exemption 310.6(b)(7) | law.cornell.edu/cfr/text/16/310.6 | Fetched and quoted 2026-08-26 |
| CAN-SPAM requirements and 53,088 USD penalty | ftc.gov CAN-SPAM compliance guide | **Partially verified.** ftc.gov returned 403 to direct fetch; content from FTC-hosted search results. Confirm the current penalty figure before first send |
| FTC 2024 B2B misrepresentation amendment | ftc.gov press release | **Partially verified**, same 403 |
| Reddit Data API terms | redditinc.com | **Not verified.** Fetch tool blocks the domain. Must be read before automating any Reddit posting |
| Nextdoor business terms | Not attempted | **Not verified** |
| Meta Custom Audiences data-use terms | Not attempted | **Not verified** |
| TCPA rules for cold SMS | Not attempted | **Not verified.** Recommendation is to avoid the channel entirely, so it did not need resolving |
| 86 rows, 100% phone, 17 emails, 15 role, 1 named person, 8 names | leads/callable93.json, read directly | Verified by reading the file 2026-08-26 |
| 133 role sends, 0 replies | ingest/contact_find.py module docstring | Verified as a claim recorded in this repo. The underlying send log was not independently re-counted |
