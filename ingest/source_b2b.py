#!/usr/bin/env python3
"""
source_b2b.py — a lead SOURCE for clients whose customers are other businesses.

WHY THIS FILE EXISTS
  watch_social.py sources local CONSUMER demand: a neighbour posting "my roof is
  leaking". That model has no analogue for a business that sells to other
  businesses. Nobody posts on Nextdoor asking for a fulfillment partner, so a
  B2B client pointed at the consumer watcher produces exactly zero leads and the
  run reports "0 drafts", which reads as "no demand this week" instead of "this
  client was aimed at the wrong universe". client_config_lint.py RULE 7 already
  flags that mismatch. This module is the other half of the fix: somewhere for
  those clients to actually be pointed.

THE MODEL
  A B2B prospect is not found by someone asking for the service. It is found by
  the prospect PUBLICLY EXHIBITING THE PAIN the service removes. For a 3PL /
  fulfillment client that pain is visible in a brand's own published words and
  in the public record of its growth:

    - the brand's own shipping policy admitting a long order handling time,
      or admitting that order volume is outrunning the person packing them
    - the brand hiring warehouse / fulfillment / pick-pack staff, i.e. paying
      salaries to do in-house the thing the client does as a service
    - the brand raising money, launching, or landing retail distribution,
      i.e. a volume spike arriving on top of self-fulfillment

  "Ships physical products" is FIT, not a lead. Only the pain scores.

NEVER FABRICATES
  Every scored signal carries the exact substring that produced it plus the URL
  the substring was read from. A candidate with no evidence cannot exist: the
  emitter refuses to build one (see Candidate.finish). If a channel finds
  nothing, the run says so and names the queries that came back empty. A
  well-evidenced "this channel is dead" is a real deliverable.

  The regexes are deliberately anchored. An earlier draft of the handling-time
  rule matched "Refunds ... standard processing time is 2-3 business days" on a
  brand with excellent fulfillment — a refund SLA read as a shipping delay. That
  is the same class of error as reading a service area out of the word
  "Customer SERVice". Every pattern here is anchored to an order/shipment
  subject, and every pattern is printed with repr() by --explain-regex so it can
  be eyeballed rather than trusted.

BLOCKED IS NOT EMPTY
  Each channel keeps its own counters (attempted / ok / blocked / errors /
  yielded). A soft block — an HTTP 403, or an endpoint that starts answering
  200-with-zero-rows for every input including inputs known to have rows — is
  indistinguishable from a genuinely dry source at the level of one request. It
  is only visible across a whole run, so the run is where it is judged. See
  CHANNEL_NOTES for the channels already known to be blocked or degraded and the
  evidence for each.

HARD FAILURE
  Attempts > 0 with zero total yield exits non-zero. This project was burned by
  a scraper that exited 0 with 0 rows four times before anyone noticed.

    exit 0  all good, leads found
    exit 2  attempted work and yielded nothing (the hard failure)
    exit 3  the run was mostly blocked — not the same bug, do not fix it as one
    exit 1  bad invocation / config

NO CLIENT NAMES ARE HARDCODED. Clients are rows in crm_clients. The pain
vocabulary is keyed by the client's scrape_niche, exactly as trade_vocab.py
keys consumer phrasing by trade.

GEOGRAPHY — a deliberate decision, see decide_geography()
  The home-services clients filter leads to a metro because a roofer cannot
  drive to another state. A fulfillment provider ships nationally, so the same
  filter would be actively wrong: it would discard the majority of the real
  market. Geography here is a RANKING NUDGE, never a gate. A brand near the
  client's own warehouse gets a small bonus (cheaper inbound freight, the
  prospect can tour the building) and a brand anywhere else is still a lead.

    ENV_FILE="$HOME/ghl-cli/.env" python source_b2b.py --client <slug>
    python source_b2b.py --client <slug> --channels news --dry-source news
    python source_b2b.py --explain-regex
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as htmllib
import json
import pathlib
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from db import load_env  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = pathlib.Path(__file__).resolve().parent
UA = {"User-Agent": "wing-b2b-source/1.0 (+mailto:wjackwing1@gmail.com)"}
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0.0.0 Safari/537.36"}
BLOCK_CODES = {401, 403, 405, 406, 418, 429, 503}


# ===========================================================================
# What each channel is worth, and the evidence for that judgement.
# Recorded here so the next person does not re-spend a day re-discovering it.
# ===========================================================================

CHANNEL_NOTES = {
    "shipping_policy": (
        "LIVE. The brand's own published shipping/FAQ page. 27 of 112 path "
        "probes returned 200 on 2026-08-26 and 6 brands carried a quotable "
        "handling-time or solo-operator admission. Highest-trust channel here: "
        "the prospect wrote the words itself, on its own domain."),
    "careers": (
        "LIVE BUT DRY on small DTC brands. 0 of 60 careers-path probes across "
        "15 real brands returned 200 on 2026-08-26 — makers that small do not "
        "publish a careers page. Keep it: it is the strongest signal that "
        "exists (a brand paying its 3rd packer is buying the client's service "
        "badly), it just needs a bigger-brand pool to fire on."),
    "news": (
        "LIVE. Google News RSS, keyless, no robots restriction on /rss/. "
        "Returned 30/17/59 real dated items for three funding queries on "
        "2026-08-26 (e.g. a facial-wipes brand's $4M seed + Target rollout). "
        "Needs a strict title gate — the raw feed is full of SaaS/AI rounds "
        "that ship nothing physical."),
    "store": (
        "LIVE, but it is a FIT check, not a pain signal. Shopify's public "
        "/products.json answered 200 for 16 of 20 domains probed. Catalog size "
        "sizes the prospect; it never scores on its own."),
    "reddit": (
        "BLOCKED from this host. r/ecommerce, r/shopify and r/FulfillmentByAmazon "
        "are where founders openly complain about their 3PL, and the keyless "
        "JSON endpoint is the obvious way in, but every combination of "
        "www/old.reddit.com x search.json/new.json x two user-agents returned "
        "HTTP 403 on 2026-08-26. This is an IP-level block, not an empty "
        "result. Needs the free Reddit OAuth app credential to reopen; until "
        "then the channel is attempted, recorded as blocked, and excluded from "
        "the dry-source verdict so it cannot mask a real failure elsewhere."),
    "trustpilot": (
        "OFF LIMITS BY RULE, not attempted. https://www.trustpilot.com/robots.txt "
        "reads 'User-agent: *' / 'Disallow: /' — the entire site. Fetching a "
        "review page would break the no-robots-violation rule, so this channel "
        "is never requested. Do not re-add it."),
    "indeed": (
        "BLOCKED. indeed.com/robots.txt permits /jobs, but the live request "
        "returned HTTP 403 with an anti-bot interstitial on 2026-08-26. Also "
        "un-blockable without CAPTCHA defeat, which is prohibited. Job-posting "
        "signal has to come from the employer's own careers page instead."),
    "reviews_judgeme": (
        "DEGRADED — DO NOT TRUST ITS ZEROS. judge.me's public "
        "reviews_for_widget endpoint answered HTTP 200 for every one of 16 "
        "Shopify domains probed on 2026-08-26 while reporting "
        "number_of_reviews=0 for all of them, including large brands that "
        "visibly have hundreds of reviews. A uniform zero across a pool that "
        "cannot be uniformly zero is the signature of a soft block or a "
        "silently-changed contract, not of an empty source. Left unwired on "
        "purpose: a channel that reports 'nothing' when it means 'I can no "
        "longer see' is worse than no channel."),
}

# Channels excluded from "did the run yield anything" because they are known to
# be unavailable. A blocked channel yielding zero is expected, not a bug, and
# must not be allowed to trip — or to suppress — the hard failure.
KNOWN_UNAVAILABLE = {"reddit", "trustpilot", "indeed", "reviews_judgeme"}


# ===========================================================================
# Pain vocabulary, keyed by niche. Clients are data; so is this.
# ===========================================================================

# Words that mean "this company physically ships things it makes". Used to
# reject the SaaS/AI/fintech rounds that dominate a raw funding feed.
PHYSICAL_PRODUCT_WORDS = (
    "skincare", "skin care", "beauty", "cosmetics", "haircare", "hair care",
    "supplement", "supplements", "vitamin", "nutrition", "wellness",
    "candle", "fragrance", "perfume", "soap", "grooming", "apparel",
    "footwear", "jewelry", "coffee", "tea", "snack", "beverage", "drink",
    "pet food", "petcare", "pet care", "toy", "toys", "cookware", "home goods",
    "furniture", "mattress", "bottle", "packaged", "cpg", "consumer brand",
    "dtc", "d2c", "direct-to-consumer",
)

# A funding/expansion event only matters if it means MORE BOXES SOON.
VOLUME_EVENT = re.compile(
    r"\b(?:raises?|raised|secures?|closes?|lands?)\b[^.]{0,40}?"
    r"\b(?:seed|series\s+[a-d]|pre-seed|round|funding|investment|\$\d)"
    r"|\bexpands?\s+(?:into|to)\b[^.]{0,40}\b(?:retail|target|walmart|costco|"
    r"ulta|sephora|cvs|walgreens|whole foods|kroger|nationwide|stores)\b"
    r"|\blaunch(?:es|ed|ing)?\b[^.]{0,30}\b(?:at|in|into)\b[^.]{0,20}"
    r"\b(?:target|walmart|costco|ulta|sephora|cvs|walgreens|whole foods|kroger)\b"
    r"|\bnew (?:warehouse|distribution cent(?:er|re)|fulfillment cent(?:er|re))\b",
    re.I)

# Things that look like funding news but ship nothing. Hard reject.
NOT_A_SHIPPER = re.compile(
    r"\b(?:saas|software|platform|app|ai|artificial intelligence|fintech|"
    r"crypto|blockchain|bank|insurtech|marketplace|agency|clinic|telehealth|"
    r"studio|media|podcast|game|gaming|cloud|api|cybersecurity|security)\b",
    re.I)

# A brand's own careers page listing the job the client's service replaces.
# Anchored to whole role names; a bare "warehouse" would match an address.
HIRING = re.compile(
    r"\b(?:warehouse\s+(?:associate|assistant|manager|lead|worker|team member)"
    r"|fulfill?ment\s+(?:associate|specialist|manager|coordinator|lead|assistant)"
    r"|shipping\s+(?:clerk|associate|manager|coordinator|assistant|specialist)"
    r"|pick(?:er)?[ /-]?(?:and[ /-]?)?pack(?:er|ing)?\s*(?:associate|specialist|role|position)?"
    r"|order\s+fulfill?ment"
    r"|packing\s+associate"
    r"|inventory\s+(?:associate|coordinator|specialist)"
    r"|logistics\s+coordinator)\b", re.I)

# DELIVERY time is the carrier's problem, not the brand's, and switching 3PL
# does not fix it. Tropical Oasis publishes "Processing Time: 1 business day"
# (healthy) next to "Delivery Time: 6-10 business days" (the post office), and
# a first cut of HANDLING_TIME scored them as a slow self-fulfiller off the
# second number. Any window ending at the number that mentions delivery or
# transit is discarded. Same error class as reading a service area out of
# "Customer SERVice": the words were there, they just meant something else.
DELIVERY_NOT_HANDLING = re.compile(
    r"\b(?:deliver(?:y|ed|ies)|transit|arrive[sd]?|arrival|in the mail|"
    r"on the road|door|shipping time|estimated delivery)\b", re.I)

# Handling time the brand admits to, anchored to an ORDER or SHIPMENT subject.
# The subject anchor is what stops it reading a refund SLA as a shipping delay.
HANDLING_TIME = re.compile(
    r"(?:order|orders|package|packages|shipment|shipments|we|item|items)"
    r"[^.!?]{0,70}?"
    r"\b(?:ship|ships|shipped|shipping|process|processed|processing|"
    r"fulfill|fulfilled|fulfillment|fulfilment|dispatch|dispatched|pack|packed)\b"
    r"[^.!?]{0,50}?"
    r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*(?:business\s+|working\s+)?(day|week)s?",
    re.I)

# The brand saying out loud that order volume is beating the person packing it.
# These are admissions, not inferences.
OVERWHELM = re.compile(
    r"(?:due to (?:the )?(?:high )?(?:number|volume) of orders)"
    r"|(?:orders?|shipments?)\s+(?:may|might|could|will)\s+(?:be delayed|take longer|experience delays)"
    r"|(?:current(?:ly)?|please note)[^.!?]{0,50}?(?:shipping|fulfill?ment|order)[^.!?]{0,40}?delay"
    r"|(?:we\s+(?:are|'re)\s+(?:a\s+)?(?:small|one[- ]woman|one[- ]man|two[- ]person|family)[- ]"
    r"(?:team|business|shop|run|operated|operation))"
    r"|(?:ship(?:ped|s|ping)?\s+(?:out\s+)?(?:from|out of)\s+(?:my|our)\s+"
    r"(?:home|house|garage|kitchen|spare room|living room))"
    r"|(?:i\s+(?:pack|ship|fulfill)\s+(?:every|each|all)\s+order)"
    r"|(?:backlog of orders)", re.I)

# First-person singular inside a shipping policy = one human is doing the
# packing. Corroborating only; never scores by itself.
SOLO_VOICE = re.compile(
    r"\bI\s+(?:receive|ship|pack|fulfill|make|process)\b"
    r"|\b(?:my|our one[- ]person)\s+(?:studio|workshop|kitchen|garage)\b")

# Handling time at or above this many business days reads as self-fulfillment
# under strain. Set to 4, not 3, from live evidence: Texas Tallow Products
# publishes "Orders are processed within 1-3 business days" and Brooklyn Candle
# Studio "packed and shipped within 1-3 business days" — both are ordinary,
# well-run fulfillment, and a threshold of 3 scored both as leads. 1-3 days is
# the industry-normal promise; 4+ is where a human packing boxes starts falling
# behind. Raising this dropped two weak positives and cost nothing real.
SLOW_HANDLING_DAYS = 4

SHIPPING_PATHS = ["/policies/shipping-policy", "/pages/shipping",
                  "/pages/shipping-policy", "/pages/shipping-returns",
                  "/pages/faq", "/shipping"]
CAREERS_PATHS = ["/pages/careers", "/careers", "/pages/jobs", "/jobs",
                 "/pages/work-with-us", "/pages/join-us"]

# Query templates for the news channel. {niche} is the client's own niche, so
# nothing about any particular client is baked in here.
NEWS_QUERIES = [
    '"{niche}" brand raises seed funding',
    '"{niche}" brand raises Series A',
    '{niche} DTC brand funding round',
    '{niche} brand expands into retail',
    '{niche} brand new warehouse OR "distribution center"',
]


# ===========================================================================
# Run accounting — the thing that makes a soft block visible
# ===========================================================================

class ChannelStat:
    def __init__(self, name: str):
        self.name = name
        self.attempted = 0
        self.ok = 0
        self.blocked = 0
        self.errors = 0
        self.yielded = 0
        self.empty_queries: list[str] = []   # proof of a genuinely dry source
        self.block_detail: list[str] = []

    @property
    def block_rate(self) -> float:
        return self.blocked / self.attempted if self.attempted else 0.0

    def verdict(self) -> str:
        if self.name in KNOWN_UNAVAILABLE and self.yielded == 0:
            return "UNAVAILABLE"
        if not self.attempted:
            return "SKIPPED"
        if self.yielded:
            return "LIVE"
        if self.block_rate >= 0.5:
            return "BLOCKED"
        if self.errors >= self.attempted:
            return "ERROR"
        return "DRY"

    def as_dict(self) -> dict:
        return {"channel": self.name, "verdict": self.verdict(),
                "attempted": self.attempted, "ok": self.ok,
                "blocked": self.blocked, "errors": self.errors,
                "yielded": self.yielded,
                "block_detail": self.block_detail[:8],
                "empty_queries": self.empty_queries[:12]}


class Run:
    def __init__(self):
        self.stats: dict[str, ChannelStat] = {}
        self.candidates: list[dict] = []

    def stat(self, name: str) -> ChannelStat:
        return self.stats.setdefault(name, ChannelStat(name))

    def emit(self, cand: dict):
        self.candidates.append(cand)
        self.stat(cand["channel"]).yielded += 1


def fetch(run: Run, channel: str, url: str, *, params=None, browser=False,
          timeout=8, want_json=False):
    """One accounted HTTP GET. Returns (response|None, kind) where kind is one
    of ok / blocked / miss / error. Blocked is tracked separately from empty on
    purpose — see the module docstring."""
    st = run.stat(channel)
    st.attempted += 1
    try:
        r = requests.get(url, headers=BROWSER_UA if browser else UA,
                         params=params, timeout=timeout)
    except Exception as e:
        st.errors += 1
        return None, f"error:{e.__class__.__name__}"
    if r.status_code in BLOCK_CODES:
        st.blocked += 1
        st.block_detail.append(f"HTTP {r.status_code} {url[:90]}")
        return None, "blocked"
    if r.status_code != 200:
        return None, "miss"
    if want_json:
        try:
            r.json()
        except Exception:
            return None, "miss"
    st.ok += 1
    return r, "ok"


# ===========================================================================
# Candidate construction — cannot produce an evidence-free lead
# ===========================================================================

class Candidate:
    def __init__(self, channel: str, company: str, url: str):
        self.channel = channel
        self.company = (company or "").strip()
        self.url = url
        self.evidence: list[dict] = []
        self.fit: list[str] = []
        self.score = 0

    def signal(self, points: int, kind: str, quote: str, url: str):
        """Record a PAIN signal. Quote and url are mandatory and are stored
        verbatim so a human can open the page and read the same words."""
        quote = htmllib.unescape(re.sub(r"\s+", " ", quote or "")).strip()
        if not quote or not url:
            raise ValueError(f"{kind}: refusing to score a signal with no "
                             f"quote/url — that is how a fabricated lead gets in")
        self.score += points
        self.evidence.append({"kind": kind, "points": points,
                              "quote": quote[:400], "source_url": url})

    def note_fit(self, text: str):
        """Fit facts (ships physical goods, catalog size, home region). These
        describe the prospect; they are not why it is a lead."""
        self.fit.append(text)

    def finish(self) -> dict | None:
        # The hard rule, enforced in code rather than in a comment: no pain
        # evidence, no candidate. "Ships physical products" is not a lead.
        if not self.evidence:
            return None
        return {
            "source": "b2b",
            "channel": self.channel,
            "company": self.company,
            "url": self.url,
            "score": min(100, self.score),
            "tier": "A" if self.score >= 55 else "B" if self.score >= 35
                    else "C" if self.score >= 20 else "D",
            "pain_signals": self.evidence,
            "fit_notes": self.fit,
            "found_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }


# ===========================================================================
# Channel: the brand's own shipping policy
# ===========================================================================

def visible_text(html: str) -> str:
    t = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html,
               flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", htmllib.unescape(t)).strip()


def worst_handling_days(text: str):
    """Largest self-declared order handling time, in business days, plus the
    sentence it came from. Weeks are converted at 5 business days each."""
    worst = None
    for m in HANDLING_TIME.finditer(text):
        lo, hi, unit = int(m.group(1)), int(m.group(2)), m.group(3).lower()
        if hi < lo or hi > 60:
            continue                      # "2-1 days", or a phone number
        if DELIVERY_NOT_HANDLING.search(m.group(0)):
            continue                      # carrier transit time, not the brand's
        days = hi * 5 if unit.startswith("week") else hi
        if worst is None or days > worst[0]:
            worst = (days, m.group(0), m.start(), m.end())
    return worst


def channel_shipping_policy(run: Run, company: str, domain: str, home_hit: bool):
    ch = "shipping_policy"
    base = f"https://{domain}"
    for path in SHIPPING_PATHS:
        r, kind = fetch(run, ch, base + path, browser=True)
        if kind == "blocked":
            return None
        if r is None:
            continue
        text = visible_text(r.text)
        if len(text) < 200:
            continue
        cand = Candidate(ch, company, r.url)

        worst = worst_handling_days(text)
        if worst and worst[0] >= SLOW_HANDLING_DAYS:
            s, e = worst[2], worst[3]
            cand.signal(30, "slow_self_fulfillment",
                        text[max(0, s - 130):e + 130],
                        r.url)
            cand.note_fit(f"self-declared handling time up to {worst[0]} business day(s)")
        elif worst:
            # Explicitly NOT a lead signal. Recorded as fit so the run shows we
            # looked and judged, rather than silently dropping the brand.
            cand.note_fit(f"handling time {worst[0]} business day(s) — "
                          f"fulfillment looks healthy, not a pain signal")

        m = OVERWHELM.search(text)
        if m:
            cand.signal(35, "admits_fulfillment_strain",
                        text[max(0, m.start() - 150):m.end() + 150], r.url)

        if cand.evidence:
            sv = SOLO_VOICE.search(text)
            if sv:
                cand.signal(10, "solo_operator_voice",
                            text[max(0, sv.start() - 110):sv.end() + 110], r.url)
            if home_hit:
                cand.signal(5, "near_client_warehouse",
                            f"brand address/policy text references the client's "
                            f"home region: {home_hit}", r.url)
            return cand.finish()
        run.stat(ch).empty_queries.append(f"{r.url} (read, no pain language)")
        return None
    run.stat(ch).empty_queries.append(f"{domain}: no shipping policy page found "
                                      f"at {len(SHIPPING_PATHS)} standard paths")
    return None


# ===========================================================================
# Channel: the brand's own careers page
# ===========================================================================

def channel_careers(run: Run, company: str, domain: str):
    ch = "careers"
    base = f"https://{domain}"
    for path in CAREERS_PATHS:
        r, kind = fetch(run, ch, base + path, browser=True)
        if kind == "blocked":
            return None
        if r is None:
            continue
        text = visible_text(r.text)
        m = HIRING.search(text)
        if not m:
            run.stat(ch).empty_queries.append(f"{r.url} (careers page, no "
                                              f"fulfillment role listed)")
            continue
        cand = Candidate(ch, company, r.url)
        cand.signal(45, "hiring_fulfillment_staff",
                    text[max(0, m.start() - 150):m.end() + 200], r.url)
        cand.note_fit("paying salaries to fulfill in-house — the exact spend "
                      "this client's service replaces")
        return cand.finish()
    run.stat(ch).empty_queries.append(f"{domain}: no careers page at "
                                      f"{len(CAREERS_PATHS)} standard paths")
    return None


# ===========================================================================
# Channel: Shopify public store facts (FIT ONLY — never scores)
# ===========================================================================

def channel_store(run: Run, domain: str):
    r, kind = fetch(run, "store", f"https://{domain}/products.json",
                    params={"limit": 250}, browser=True, want_json=True)
    if r is None:
        return None
    try:
        prods = r.json().get("products", [])
    except Exception:
        return None
    if not prods:
        return None
    run.stat("store").yielded += 1   # a fit lookup that worked
    return {"platform": "shopify", "catalog_size": len(prods),
            "source_url": r.url}


# ===========================================================================
# Channel: Google News RSS (volume-spike events)
# ===========================================================================

def channel_news(run: Run, niche: str, days: int, per_query: int):
    ch = "news"
    out = []
    seen = set()
    for tmpl in NEWS_QUERIES:
        q = tmpl.format(niche=niche)
        r, kind = fetch(run, ch, "https://news.google.com/rss/search",
                        params={"q": f"{q} when:{days}d", "hl": "en-US",
                                "gl": "US", "ceid": "US:en"}, timeout=25)
        if r is None:
            if kind != "blocked":
                run.stat(ch).empty_queries.append(f"{q} ({kind})")
            continue
        try:
            items = ET.fromstring(r.content).findall(".//item")
        except Exception:
            run.stat(ch).errors += 1
            continue
        kept = 0
        for it in items[:per_query]:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            if not title or not link or link in seen:
                continue
            seen.add(link)
            # Three gates, all on the headline the publisher wrote. Nothing is
            # inferred about the company beyond what the headline says.
            if NOT_A_SHIPPER.search(title):
                continue
            if not any(w in title.lower() for w in PHYSICAL_PRODUCT_WORDS) \
               and not any(w in niche.lower() for w in PHYSICAL_PRODUCT_WORDS):
                continue
            ev = VOLUME_EVENT.search(title)
            if not ev:
                continue
            # Company name = the headline's leading proper-noun run, up to the
            # event verb. Conservative: if it does not look like a name, drop
            # the item rather than guess.
            company = title[:ev.start()].strip(" -–—,:|")
            if not company or len(company) > 70 or len(company.split()) > 8:
                continue
            cand = Candidate(ch, company, link)
            cand.signal(30, "volume_spike_event",
                        f"{title}  [{(it.findtext('pubDate') or '')[:16]}]", link)
            cand.note_fit(f"matched query: {q}")
            fin = cand.finish()
            if fin:
                out.append(fin)
                kept += 1
        if not kept:
            run.stat(ch).empty_queries.append(f"{q} ({len(items)} items, none "
                                              f"passed the physical-product + "
                                              f"volume-event gate)")
        time.sleep(0.6)
    return out


# ===========================================================================
# Channel: Reddit (attempted so the block is recorded, never assumed)
# ===========================================================================

def channel_reddit(run: Run, subs: list[str], terms: list[str]):
    """Where founders genuinely complain about their 3PL. Attempted every run
    so that 'blocked' stays a measured fact with today's date on it rather than
    a belief inherited from a comment. Yield is expected to be zero until the
    free Reddit credential exists."""
    ch = "reddit"
    out = []
    for sub in subs[:3]:
        q = " OR ".join(f'"{t}"' for t in terms[:4]) or "3PL"
        r, kind = fetch(run, ch, f"https://www.reddit.com/r/{sub}/search.json",
                        params={"q": q, "restrict_sr": 1, "sort": "new",
                                "limit": 25, "t": "year"},
                        want_json=True, timeout=15)
        if r is None:
            if kind != "blocked":
                run.stat(ch).empty_queries.append(f"r/{sub} {q} ({kind})")
            continue
        for c in r.json().get("data", {}).get("children", []):
            d = c.get("data", {})
            title = d.get("title") or ""
            body = d.get("selftext") or ""
            m = re.search(r"\b(3pl|fulfill?ment (?:partner|center|centre|company)|"
                          r"warehouse partner)\b", title + " " + body, re.I)
            if not m:
                continue
            link = "https://www.reddit.com" + (d.get("permalink") or "")
            cand = Candidate(ch, d.get("author") or "reddit poster", link)
            cand.signal(25, "founder_complains_about_fulfillment",
                        (title + ". " + body)[:400], link)
            fin = cand.finish()
            if fin:
                out.append(fin)
        time.sleep(1.2)
    return out


# ===========================================================================
# Geography — the decision, written down
# ===========================================================================

def decide_geography(home_region: str) -> str:
    return (
        "GEOGRAPHY DECISION for a business-to-business client:\n"
        "  The DFW metro filter used by the home-services clients is NOT applied "
        "here, and applying it would be a defect. A roofer's market is bounded by "
        "a truck's drive time. A fulfillment provider ships parcels nationally, so "
        "a brand in Ohio is as reachable a customer as one across town; filtering "
        "to a metro would discard most of the real market and manufacture the same "
        "'zero leads' the wrong-channel config already produces.\n"
        f"  Home region ({home_region or 'not set'}) is used ONLY as a small "
        "ranking bonus, never as a gate: a nearby brand means cheaper inbound "
        "freight and a prospect who can walk the warehouse. Distance never "
        "disqualifies.\n"
        "  Note this is the opposite of client_config_lint RULE 3, which calls a "
        "state-where-a-city-belongs an ERROR. That rule is right for a local "
        "trade and wrong for this client — which is itself the tell that the "
        "national-B2B case needs its own source rather than a reused config."
    )


# ===========================================================================
# Client config (clients are data)
# ===========================================================================

def fetch_client(env: dict, slug: str) -> dict:
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY (set ENV_FILE)")
    r = requests.get(f"{url}/rest/v1/crm_clients", timeout=30,
                     headers={"apikey": key, "Authorization": f"Bearer {key}"},
                     params={"select": "*"})
    if not r.ok:
        sys.exit(f"crm_clients read failed: HTTP {r.status_code} {r.text[:200]}")
    for c in r.json():
        if (c.get("slug") or "").lower() == slug.lower() \
           or (c.get("name") or "").lower() == slug.lower():
            return c
    sys.exit(f"No client matching '{slug}' in crm_clients")


def read_seed_domains(path: pathlib.Path) -> list[tuple[str, str]]:
    """Seed pool: 'Company Name<TAB or comma>domain' or a bare domain per line.
    The pool is an INPUT. This module never invents a company."""
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[\t,]", line, maxsplit=1)
        if len(parts) == 2 and not parts[0].strip().count("."):
            name, dom = parts[0].strip(), parts[1].strip()
        else:
            dom = parts[-1].strip()
            name = ""
        dom = re.sub(r"^https?://", "", dom).strip("/").split("/")[0]
        if dom:
            out.append((name or dom, dom))
    return out


# ===========================================================================
# Main
# ===========================================================================

ALL_CHANNELS = ["shipping_policy", "careers", "news", "reddit"]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client", help="crm_clients slug or name")
    ap.add_argument("--niche", help="override the client's scrape_niche")
    ap.add_argument("--home-region", default="",
                    help="the client's own warehouse region; ranking bonus only")
    ap.add_argument("--seed-file", type=pathlib.Path,
                    help="brand pool: one 'Name,domain' or domain per line")
    ap.add_argument("--channels", default=",".join(ALL_CHANNELS))
    ap.add_argument("--dry-source", default="",
                    help="comma list of channels to force-empty, to prove the "
                         "hard failure fires (testing only)")
    ap.add_argument("--limit", type=int, default=25, help="brands from the seed pool")
    ap.add_argument("--news-days", type=int, default=180)
    ap.add_argument("--news-per-query", type=int, default=25)
    ap.add_argument("--out", type=pathlib.Path,
                    default=HERE / "b2b_candidates.jsonl")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--explain-regex", action="store_true",
                    help="print repr() of every pattern and exit")
    args = ap.parse_args()

    if args.explain_regex:
        for n, rx in [("VOLUME_EVENT", VOLUME_EVENT), ("NOT_A_SHIPPER", NOT_A_SHIPPER),
                      ("HIRING", HIRING), ("HANDLING_TIME", HANDLING_TIME),
                      ("OVERWHELM", OVERWHELM), ("SOLO_VOICE", SOLO_VOICE)]:
            print(f"{n} =\n  {rx.pattern!r}\n")
        print(f"SLOW_HANDLING_DAYS = {SLOW_HANDLING_DAYS!r}")
        return 0

    niche = args.niche or ""
    home = args.home_region
    if args.client:
        c = fetch_client(load_env(), args.client)
        niche = niche or (c.get("scrape_niche") or "")
        home = home or (c.get("scrape_cities") or "")
    if not niche:
        print("ERROR: no niche. Pass --niche or --client with scrape_niche set.\n"
              "       This module will not guess what a client sells.",
              file=sys.stderr)
        return 1

    wanted = {x.strip() for x in args.channels.split(",") if x.strip()}
    dry = {x.strip() for x in args.dry_source.split(",") if x.strip()}
    run = Run()

    print("=" * 78)
    print("B2B PAIN-SIGNAL SOURCE")
    print(f"niche       : {niche}")
    print(f"home region : {home or '(none)'}   (ranking bonus only, never a filter)")
    print(f"channels    : {', '.join(sorted(wanted))}")
    if dry:
        print(f"FORCED DRY  : {', '.join(sorted(dry))}   (testing the hard failure)")
    print("=" * 78)

    pool = read_seed_domains(args.seed_file) if args.seed_file else []
    if pool:
        pool = pool[:args.limit]
        print(f"seed pool   : {len(pool)} brand(s) from {args.seed_file}")

    # --- per-brand channels ------------------------------------------------
    home_words = [w.strip().lower() for w in (home or "").split(",") if w.strip()]
    for name, dom in pool:
        fitinfo = channel_store(run, dom) if "store" not in dry else None
        home_hit = next((w for w in home_words if w in dom.lower()), "")
        for ch, fn in (("shipping_policy",
                        lambda: channel_shipping_policy(run, name, dom, home_hit)),
                       ("careers", lambda: channel_careers(run, name, dom))):
            if ch not in wanted or ch in dry:
                if ch in dry:
                    run.stat(ch).attempted += 1
                    run.stat(ch).empty_queries.append(f"{dom}: forced dry by --dry-source")
                continue
            cand = fn()
            if cand:
                if fitinfo:
                    cand["fit_notes"].append(
                        f"{fitinfo['platform']} store, {fitinfo['catalog_size']} "
                        f"products listed ({fitinfo['source_url']})")
                run.emit(cand)
                print(f"  + [{cand['tier']}] {cand['channel']:16s} {cand['company'][:40]}  "
                      f"score={cand['score']}")

    # --- pool-free channels ------------------------------------------------
    if "news" in wanted:
        if "news" in dry:
            run.stat("news").attempted += 1
            run.stat("news").empty_queries.append("forced dry by --dry-source")
        else:
            for cand in channel_news(run, niche, args.news_days, args.news_per_query):
                run.emit(cand)
                print(f"  + [{cand['tier']}] {cand['channel']:16s} "
                      f"{cand['company'][:40]}  score={cand['score']}")

    if "reddit" in wanted and "reddit" not in dry:
        for cand in channel_reddit(run, ["ecommerce", "shopify", "FulfillmentByAmazon"],
                                   ["3PL", "fulfillment center", "our fulfillment",
                                    "warehouse partner"]):
            run.emit(cand)

    # --- dedupe -------------------------------------------------------------
    # One funding round gets written up by four trade outlets, so the same
    # company arrives four times under "REMEDY", "Remedy" and "Remedy Science".
    # Merge on the normalised name and KEEP EVERY source url — more independent
    # write-ups is corroboration, and Jack should see all of them rather than
    # have three quietly deleted.
    merged: dict[str, dict] = {}
    for c in run.candidates:
        key = (c["channel"], re.sub(r"[^a-z0-9]", "", c["company"].lower())[:24])
        if key in merged:
            for e in c["pain_signals"]:
                if e["source_url"] not in [x["source_url"]
                                           for x in merged[key]["pain_signals"]]:
                    merged[key]["pain_signals"].append(e)
            merged[key]["duplicates_merged"] = merged[key].get("duplicates_merged", 1) + 1
        else:
            merged[key] = c
    run.candidates = list(merged.values())

    # --- report ------------------------------------------------------------
    run.candidates.sort(key=lambda c: c["score"], reverse=True)
    args.out.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in run.candidates),
        encoding="utf-8")

    print("\n" + "=" * 78)
    print("CHANNEL LEDGER  (blocked is NOT empty — read the verdict column)")
    print("=" * 78)
    print(f"{'CHANNEL':18} {'VERDICT':12} {'TRIED':>6} {'OK':>5} {'BLOCKED':>8} "
          f"{'ERR':>5} {'LEADS':>6}")
    for st in run.stats.values():
        print(f"{st.name:18} {st.verdict():12} {st.attempted:>6} {st.ok:>5} "
              f"{st.blocked:>8} {st.errors:>5} {st.yielded:>6}")
    for st in run.stats.values():
        if st.verdict() in ("DRY", "BLOCKED", "UNAVAILABLE") and (
                st.empty_queries or st.block_detail):
            print(f"\n  {st.name} — {st.verdict()}. Evidence:")
            for q in (st.block_detail or st.empty_queries)[:6]:
                print(f"    - {q}")
            if st.name in CHANNEL_NOTES:
                print(f"    note: {CHANNEL_NOTES[st.name]}")

    print("\n" + decide_geography(home))

    print("\n" + "=" * 78)
    print(f"{len(run.candidates)} candidate(s) written to {args.out}")
    for c in run.candidates[:10]:
        print(f"\n[{c['tier']}] {c['company']}  (score {c['score']}, via {c['channel']})")
        print(f"    {c['url']}")
        for e in c["pain_signals"]:
            print(f"    PAIN {e['kind']} (+{e['points']}): \"{e['quote'][:180]}\"")
            print(f"         verify: {e['source_url']}")
        for f in c["fit_notes"]:
            print(f"    fit  {f}")

    if args.json:
        print(json.dumps({"candidates": run.candidates,
                          "channels": [s.as_dict() for s in run.stats.values()]},
                         indent=2, ensure_ascii=False))

    # --- exit contract -----------------------------------------------------
    judged = [s for s in run.stats.values()
              if s.attempted and s.name not in KNOWN_UNAVAILABLE]
    tried = sum(s.attempted for s in judged)
    got = sum(s.yielded for s in judged)
    if tried and not got:
        blocked_ch = [s.name for s in judged if s.verdict() == "BLOCKED"]
        if blocked_ch:
            print(f"\nRUN BLOCKED: {tried} attempt(s), 0 leads, and "
                  f"{', '.join(blocked_ch)} were blocked rather than empty. "
                  f"This is an access problem, not a demand problem — do not "
                  f"'fix' it by loosening the scoring.", file=sys.stderr)
            return 3
        print(f"\nHARD FAILURE: {tried} attempt(s) across "
              f"{', '.join(s.name for s in judged)} produced 0 leads. "
              f"A source that tries and yields nothing must never exit 0 — that "
              f"is how four empty runs went unnoticed. Dry-source evidence is "
              f"printed above; if the channels really are dead, say so and "
              f"retire them deliberately.", file=sys.stderr)
        return 2
    if not tried:
        print("\nNothing was attempted (no seed pool and no pool-free channel "
              "selected). Not a failure, but not a run either.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
