# SOCIAL-CEILING.md

**What social media can actually produce as a lead source for Jackson Roofing and Hero's Junk Removal, measured.**

Measured 2026-08-26 and 2026-08-27 from Jack's machine (residential IP, Windows).
Every number below came from a query, a fetch, or a database read that was actually run.
Nothing here is estimated unless it says "estimated", and nothing is asserted where the
measurement failed. Where something could not be measured, it says "not measured" and
says what was tried.

Two clients, two trades, two city sets:

* **Jackson Roofing**: Plano, Frisco, McKinney, Allen, Richardson
* **Hero's Junk Removal**: Dallas, Fort Worth, Arlington

---

## 1. The headline number

**Realistic homeowner leads per week from social media: zero for Jackson Roofing, zero for Hero's Junk Removal.**

Not "few". Not "low ROI". Zero, as a measured count, with a mechanism that explains why
it is zero and predicts it will stay zero.

| Client | Queries | Results | Unique URLs | Real user posts (not profiles or hub pages) | Genuine local homeowner demand | Posted in last 30 days |
|---|---|---|---|---|---|---|
| Jackson Roofing | 69 | 459 | 266 | 89 | **0** | **0** |
| Hero's Junk Removal (today) | 31 | 173 | 142 | 55 | **0** | **0** |
| Hero's Junk Removal (full run, 2026-08-26) | 48 | 161 | not recorded | not recorded | **0** (the 9 "kept" rows were all Facebook group names plus one out-of-state business page) | **0** |
| **Combined, today** | **100** | **632** | **394** | **141** | **0** | **0** |

Across 100 queries and 632 results today, plus the 48-query run yesterday, the number of
results that were a real local person in one of those eight cities asking for roofing or
junk removal, recently enough to be worth calling, was **zero**.

### The comparison that decides it

| Source | Run time | Leads produced | Freshness of newest lead |
|---|---|---|---|
| `ingest/source_junk.py` (Craigslist + estatesales.net), market `dfw` | **46 seconds** | **30** (3 hire, 19 event, 8 signal) | **posted yesterday**, 2026-08-26 15:56 |
| All of social media, both trades, all eight cities | roughly 2.5 hours of querying | **0** | n/a |

Verified by running it today, read-only and unmodified: 30 leads, 13 from Craigslist and
17 from estatesales.net. The Craigslist postings are dated 2026-06-17 through 2026-08-26.
All 17 estate sales carry a published start date, most starting today or tomorrow. A
separate `--tier hire` run returned **3 people actively trying to pay someone with a
truck**, in 33 seconds, out of 709 raw Craigslist results.

**The ratio is not 10 to 1 or 100 to 1 in favour of classifieds. It is 30 to 0.**

---

## 2. Platform by platform

Counts below are combined across both trades and all eight cities.

| Platform | What a keyless, robots-respecting program can SEE | What is PERMITTED | Measured demand volume | Freshness |
|---|---|---|---|---|
| **Facebook** | Public business Pages and group *names* through the search index. No group posts. | **Nothing.** `robots.txt` wildcard group is `User-agent: *` / `Disallow: /`, under a header stating automated collection is prohibited without express written permission. Groups API shut down Apr 2024. | 15 queries, 126 results, 102 unique URLs, **1 real user post**, **0 demand**. Everything else was a business Page, a `/biz/` directory page, or Marketplace chrome. | n/a, nothing found |
| **Nextdoor** | Occasional real neighbour post *titles*, via `og:title` on `/p/` pages. Nothing else. | **Nothing usable.** Wildcard group is `Disallow: /` with exactly two exceptions: `/link_preview_image/` and `/for_sale_and_free/`. Fetched `/for_sale_and_free/` and it **redirects to `/login/?next=/for_sale_and_free/`**. The one permitted content path is login-walled. | 15 queries, 115 results, 51 unique, **5 `/p/` post URLs** total: 2 roofing *company* ads, 1 login page, 1 Houston apartment request, 1 ambiguous homeowner aside. **0 usable demand.** | Mixed. One indexed post was dated Aug 4 2026, so the index does carry fresh Nextdoor content when it carries any. |
| **Reddit, local subs** (r/Dallas, r/Plano, r/FortWorth, r/Arlington) | Post titles and slugs through the search index only. | **Nothing.** `reddit.com/robots.txt` is now `User-agent: *` / `Disallow: /`, a blanket disallow. Unauthenticated `.json` endpoints return **HTTP 403** on both `www.reddit.com` and `old.reddit.com`. | 15 queries, 49 results, 35 unique, 35 real posts, **0 demand**. What came back: potholes, a murder trial, vet recommendations, "I'm turning 36, what's the coolest...", "my thoughts moving here from...", an active shooter report, and r/Plano_TX_NSFW personals. | **Dead.** Newest topical indexed post **2024-06-25**. Newest indexed post of any kind estimated 2026-01-22. **0 in the last 30 days.** |
| **Reddit, trade subs** (r/Roofing, r/junkremoval) | Same. | Same blanket disallow. | 14 queries, 48 results, 19 real posts, **0 local demand**. r/junkremoval returned **0 results** on every query. | Dead. Range 2023-05-18 to 2025-03-12. **0 in the last 30 days.** |
| **Instagram** | Public post permalinks through the search index. `instagram.com/robots.txt` does not return a robots file at all: it returns a 616 KB HTML login page. | **Unknown, therefore no.** A host that will not serve its own robots.txt has not granted permission. The messaging API only permits replying to a user who messaged the business first, inside 24 hours. | 14 queries, 104 results, 63 unique, **35 real posts**, **0 demand**. Every identifiable one was a roofing or construction company (Lifetime Roofing, Roofwise TX, 4 Alarm Roofing, Gideon Roofing, Panther Roofing, 3 Kings Roofing) or an unrelated foreign-language account. | Irrelevant, since 0 demand. Most company posts dated 2017 to 2022. |
| **TikTok** | Public video pages through the search index. | **Unknown, therefore no.** `tiktok.com/robots.txt` **could not be fetched at all**: the connection was reset (`ConnectionResetError 10054`) on repeated attempts. There is no send-DM endpoint. | 14 queries, 90 results, 67 unique, **14 real videos**, **0 demand**. The only on-topic ones were three videos from `@junknorthdfw`, a **competing DFW junk removal company**. The rest were "Legacy Rooftop Plano", "Dallas rooftop restaurants kid friendly", and a Miami outfit video. | Irrelevant, since 0 demand. |
| **X** | Public status pages through the search index. | **Partially permitted, but only for named agents.** robots.txt gives explicit rules to Googlebot, Bingbot and facebookexternalhit. There is no wildcard grant. | 13 queries, 100 results, 57 unique, **32 real statuses**, **0 demand**: Arsenal, Kyrie Irving, AP wire copy, a Frisco PD traffic alert, Marco Rubio, baseball trades, a border collie. | **Fresh.** X status IDs in results were current 2026. X is the one platform whose index coverage is genuinely up to date, and it contains no local trade demand whatsoever. |
| **OfferUp** | Item detail pages only. | `Disallow: /search`, `Disallow: /services/search`, plus a wildcard `Disallow: *q=`. Detail pages are allowed but there is **no permitted way to discover them**. Confirmed in robots.txt today. | **Not measured**, because there is no permitted discovery surface to measure. Tried: reading robots.txt and looking for any allowed index or category path. There is none for search. | n/a |
| **Local forums** | Searched for, none found. | n/a | **Not measured further.** No DFW-specific public forum with a crawlable, current, homeowner-request index surfaced in any of the 100 queries. | n/a |

### Benchmark sources, for contrast

| Source | Permitted | Volume measured | Freshness |
|---|---|---|---|
| **Craigslist** | Yes. robots.txt disallows only `/reply`, `/fb/`, `/suggest`, `/flag`, `/mf`, `/mailflag`, `/eaf`. Search and posting pages are open and sitemaps are published. | 709 raw results in one DFW run, 13 kept leads | Newest posted **yesterday** |
| **estatesales.net** | Yes. robots.txt disallows only `/account`, `/homepages`, `/v2`, `/v3`, `/legacy`, `/api/user-view-details`. | 17 DFW-area sales | All carry a **published start date**, most starting today or tomorrow |

---

## 3. Freshness, the finding that kills Reddit

A three-week-old lead is dead. Reddit is far worse than three weeks.

Reddit post IDs are base36 and monotonic, so they can be dated. Calibrated on two posts
datable from their own visible text (`138kajm` in r/plano, "Vote tomorrow Saturday May
6th", so 2023-05-05; and `1dpdl0w` in r/Dallas, the 2026 World Cup broadcast-centre news,
so 2024-06-25), every Reddit result in the roofing sweep dates out as:

* 33 Reddit posts dated
* Oldest **2023-03-07**
* Newest topical post **2024-06-25**
* Newest post of any kind, estimated **2026-01-22** (an r/Plano_TX_NSFW personals ad)
* Posted in the last 30 days: **0**

The search index's Reddit corpus effectively stops around mid-2024. Today is 2026-08-27.
So even where genuine homeowner demand exists on Reddit, and it does exist, **the only
copy we are permitted to see is roughly two years stale.** Those homeowners hired someone
in 2024.

A second freshness finding, and this one affects the whole project: **`timelimit="m"` does
not work on `site:` queries.** Running `site:nextdoor.com roofer Plano TX` with
`timelimit="m"` and again with no time limit returned **5 of 5 identical URLs**. The
last-month filter this pipeline relies on is inert on these queries. Anything that trusted
it has been ingesting years-old content and labelling it fresh.

---

## 4. Two established beliefs that measured out differently

**a) The search index is not the bottleneck.** The working belief was 3 to 10 results per
query. Measured over the first 51 queries today: **6.9 results per query on average**,
with Facebook at 9.0 and Nextdoor at 8.8. The index answered generously. The results were
simply not demand. This matters because it kills the tempting conclusion "we just need a
better search API". A better index returns more roofing companies, faster.

The index **does** soft-block on sustained use, though. After roughly 80 queries in one
session, latency went from about 4 seconds to about 3 minutes, then to repeated
`ConnectTimeout`. That is why the Hero's sweep is partial and stopped at 31 of 63 planned
queries. The Jackson Roofing sweep completed.

**b) Reddit is no longer a credential problem.** The established note was that Reddit's API
needs a free credential Jack has not created. Still true, but no longer the binding
constraint: **`reddit.com/robots.txt` is now a blanket `User-agent: *` / `Disallow: /`.**
Creating the credential would not make scraping the site permitted. It would make the
*API* permitted, which is a different surface with its own terms.

---

## 5. The historical record agrees

Read from Supabase today, read-only, `candidates` table:

* **1,224 rows total**
* linkedin **798**, instagram **353**, tiktok **73**
* reddit **0**, facebook **0**, nextdoor **0**, craigslist **0**, estatesales **0**

Every social row ever banked came from LinkedIn, Instagram or TikTok. Sampling shows what
they actually are:

> Cedar Roofing, Prestigious Construction, Addison Roof, Fisher Brothers Exteriors,
> Dallas TX Commercial Roofing, Murphy's Roofing Supply, Blackline Roofing,
> Litz Roofing/Construction LLC, Prosper Roofing & Construction

**1,224 rows and not one of them is a customer.** They are all competitors. The entire
historical social yield is a competitor directory that was mistaken for a lead list.

That is the structural point, and it is the reason this will not change. Social platforms
index **businesses**, because businesses buy ads, fill out profiles, and want to be found.
Homeowners with a leaking roof post inside a closed group, a private feed, or a
login-walled neighbourhood. **The public, crawlable layer of social media is the supply
side of the market, permanently.**

---

## 6. What would have to change

Honest answers, including the ones that are not simply "no".

**A paid search API (Serper, Brave, Bing): would not help.**
The failure is not retrieval. 632 results were retrieved today and 0 were demand. More
results means more roofing companies. **Estimated unlock: 0 leads per week.**

**A free Reddit API credential: would help slightly, and is worth the ten minutes.**
This is the only real unlock on the list. The official API is the permitted route now that
the site itself is `Disallow: /`, and it returns *current* posts instead of the index's
2024 snapshot. But size it honestly. r/Dallas, r/Plano, r/FortWorth and r/Arlington are
the right rooms, and an unrestricted all-time search of r/Dallas for junk removal
surfaced 7 posts spanning 2016 to 2023, which is roughly one relevant post every few
months per sub. Even reading every post in all four subs in real time, **estimated unlock:
well under 1 lead per week for both clients combined.** Worth wiring as a cheap monitoring
feed. Not worth building a lead engine on.

**A Nextdoor Business presence: would help, but it is advertising, not scraping.**
Nextdoor genuinely contains the demand. The proof is a real post title lifted from
`og:title` today:

> "Did anyone here in Teaswood have any hail damage from the storm that came about 2 weeks ago?"

That is precisely a Jackson Roofing lead. But the body, the author, and any route to
contact them are all behind the login. Teaswood is near Conroe, not DFW. And replying
requires a verified Nextdoor account that lives in that specific neighbourhood. The unlock
is the client claiming a **Nextdoor Business page** and buying **Nextdoor Local Deals or
Ads**. **Estimated unlock through scraping: 0. Through paid Nextdoor advertising: real,
but that is an ad-spend decision for the client, not an engineering project for Wing.**

**Facebook or Instagram Graph API access: would not help.**
Both messaging APIs only permit replying to a user who messaged the business first, within
24 hours. There is no endpoint that finds a stranger asking for a roofer. The Groups API
that would have mattered shut down in April 2024. **Estimated unlock: 0.**

**Anything requiring a login defeat, a scraped session, or a CAPTCHA defeat: not
recommended at any price.** Not only on principle. The accounts at risk are **the
clients'**. A banned Jackson Roofing Facebook page or a banned Hero's Nextdoor business
profile costs them their actual marketing presence and costs Jack the client. The downside
is asymmetric and it lands on somebody else.

---

## 7. Finding leads ON social is not the same business as the client POSTING on social

Jack sells both. Only one of them works, and the distinction is worth stating plainly.

**Finding leads on social: measured at zero. Stop.**
100 queries, 632 results, 141 real user posts, 0 leads, plus 1,224 historical rows that
were all competitors. There is no version of this that pays for the engineering.

**The client posting on social: this is the real product, and the same data proves it.**
That 1,224-row competitor directory is a failure as a lead list and a success as market
research. Every serious roofer in DFW is on Instagram and TikTok. Cedar Roofing, Panther
Roofing, 3 Kings Roofing, Gideon Roofing and dozens more are posting. So is
`@junknorthdfw` in Hero's own market. They are all there because social is where a
homeowner **checks you out after they have already found you somewhere else**, and where
"who do you recommend?" gets answered by a neighbour who remembers your name.

So, the honest split:

* Social is a **reputation and recall surface**. Post the work, collect the reviews, be
  findable, be the name a neighbour types into a Nextdoor thread. Both clients should be
  doing this and Wing should be selling it.
* Social is **not a prospecting surface**. The demand never appears in public.
* Demand for these two trades lives on **transactional classifieds**, where someone with a
  problem, a deadline and a budget posts because posting it is the norm. Craigslist and
  estatesales.net, measured today, produce 30 dated, addressable, one-day-old leads in
  46 seconds.

---

## 8. Recommendation

**Stop all engineering on social lead-finding for Jackson Roofing and Hero's Junk Removal.
Do not build a better scraper, do not buy a search API, do not add platforms.** The
ceiling has been measured at zero, and the mechanism guarantees it stays there: the public
layer of social media indexes suppliers, and the platforms have closed the demand layer
both technically (login walls, 403s, connection resets) and legally (`Disallow: /`).

Concretely:

1. **Retire the social watcher for these two clients.** It produced 0 leads across 100
   queries today and 1,224 historical rows that were all competitors.
2. **Put the lead engine on `source_junk.py` and its pattern.** 30 leads per 46-second
   run, freshest posted yesterday. Build the roofing equivalent against demand-side
   classified surfaces, not social ones.
3. **Fix the freshness bug wherever `timelimit="m"` is trusted.** It is inert on `site:`
   queries and any pipeline relying on it is mislabelling old content as new.
4. **Spend ten minutes creating the free Reddit API credential** and run the four local
   subs as a cheap, low-expectation monitoring feed. Budget it at under 1 lead per week
   and build nothing expensive on top of it.
5. **Re-sell social to both clients as posting and reputation, not prospecting.** That is
   an honest, deliverable product, the competitor data proves the audience is there, and
   the content engines to do it already exist in this stack.
6. **Never touch a client's login.** The account at risk is theirs, not Wing's.

One line: **social media is where these clients get remembered, not where they get found.
The finding happens on classifieds, and that part is already built.**

---

## Appendix: what was actually run

* `robots.txt` fetched live for facebook.com, nextdoor.com, offerup.com, reddit.com,
  instagram.com, tiktok.com, x.com, twitter.com, craigslist.org, estatesales.net.
  tiktok.com reset the connection. instagram.com served an HTML login page instead of a
  robots file.
* Unauthenticated reachability tested: reddit.com `.json` (403), old.reddit.com `.json`
  (403), nextdoor.com `/for_sale_and_free/` (redirect to login), nextdoor.com `/p/<id>`
  (200, `og:title` only, body login-walled), facebook.com `/search/posts` (400).
* 100 search-index queries via `DDGS().text(q, max_results=10, timelimit="m")` across
  7 platform surfaces, 2 trades, 8 cities, 6 intent phrasings.
* Freshness cross-check: identical queries run with and without `timelimit="m"`.
* Reddit post IDs dated by base36, calibrated on two independently datable posts. Note
  the calibration is linear and is only trusted for 7-character IDs, meaning 2023 onward.
  Older 6-character IDs are reported only as "pre-2023".
* `ingest/source_junk.py` run twice against market `dfw`, read-only and unmodified.
* Supabase `candidates` and `outbound` tables read read-only through PostgREST. Nothing
  was written anywhere.

**Coverage gap, stated plainly:** the Hero's Junk Removal sweep was cut off by the search
index at 31 of 63 planned queries when sustained querying triggered a soft block. Dallas
was covered fully, Fort Worth partially, Arlington not at all. The Jackson Roofing sweep
completed at 69 queries. The Hero's conclusion is therefore carried jointly by today's 31
queries and the full 48-query run of 2026-08-26, which returned 161 results and 9 "kept"
rows that were all Facebook group names plus one out-of-state business page. Both runs
agree on zero. If Jack wants the remaining 32 queries run, they need a fresh session and a
slower rate.
