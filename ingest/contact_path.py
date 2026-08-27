#!/usr/bin/env python3
"""
contact_path.py -- Given ANY lead row from ANY source in this system, return the
BEST LEGITIMATE way to actually contact that specific person or business.

The insight this module is built on
-----------------------------------
There are two populations in this pipeline and they have been treated as one:

  ASKED       A person who publicly posted "need someone to haul this away,
              $125". They are WAITING to be contacted. Craigslist gives every
              posting an anonymised reply relay THE POSTER DELIBERATELY
              ENABLED. Cold-outreach constraints do not apply to someone who
              asked. These leads already carry their contact path -- nobody
              had surfaced it.

  NEVER-ASKED A roofing company that has no idea Wing exists. This is Wing's
              own agency prospecting. CAN-SPAM, DM policy, deliverability and
              platform messaging windows all apply.

Mislabelling a cold business as someone who asked would licence contact that is
not licensed, so the label is explicit in every result, never implied.

What it returns
---------------
resolve(lead) -> a ContactPlan dict:

    permission        "asked" | "never_asked"
    permission_reason plain-English why, quoting the deciding field
    paths             ranked list; each carries the EXACT thing a human needs:
                        method      phone / platform_reply / email_person / ...
                        action      a URL to click, a tel: to dial, a form URL
                        how         the literal next physical step
                        reason      why this path is believed to exist
                        confidence  0..1 + label, provenance-based
                        evidence    the URL/field the path was read off
    research          NOT contact paths. Search/lookup steps for a human.
                      Never counted as coverage -- a search URL is not a way
                      to reach anyone.
    missing           if there is no contact path, exactly WHAT is missing

Hard rules (enforced in code, not just in comments)
---------------------------------------------------
  * ZERO AI. Parsing, regex, arithmetic.
  * NEVER fabricate. Every path is read off a field we stored or bytes we
    downloaded. No guessed email patterns (firstname@domain), no assumed
    /contact URL that we did not fetch and confirm.
  * NEVER automate what the platform forbids. Craigslist robots.txt DISALLOWS
    /reply -- so the relay is SURFACED for a human to click and is NEVER
    fetched. No CAPTCHA defeat, no login defeat, NO AUTOMATED FORM SUBMISSION;
    there is deliberately no submit function in this file.
  * This module RESOLVES and REPORTS. It never contacts anyone. It never
    writes to Supabase either -- other agents own those columns.
  * Clients are data. No client name is hardcoded anywhere in here.

CLI
    python contact_path.py --self-test
    python contact_path.py --supabase --limit 40            # verified candidates
    python contact_path.py --supabase --forms --limit 20    # + live form discovery
    python contact_path.py --jsonl junk_dfw.jsonl           # any source's jsonl
    python contact_path.py --supabase --jsonl a.jsonl --report
"""
import argparse
import json
import pathlib
import re
import sys
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
FETCH_TIMEOUT = 20
PACE = 1.2          # seconds between site fetches. This lane is PC-bound and
                    # getting the IP blocked would cost the only live lead source.

# ===========================================================================
# Confidence. A number a human can act on, tied to WHERE the fact came from.
# "Phone from a matched Google Maps place record" really is stronger than
# "phone scraped from a page footer", and a person deciding whether to dial
# deserves to be told which one they are looking at.
# ===========================================================================
PROVENANCE = {
    "place_matched":     (0.92, "verified against a matched place record"),
    "identity_verified": (0.88, "on an identity-verified business row"),
    "form_confirmed":    (0.90, "form element confirmed in the page we fetched"),
    "email_on_page":     (0.85, "address literally appeared on a page we fetched"),
    "poster_published":  (0.80, "the poster published it in their own post"),
    "platform_relay":    (0.78, "the platform's own reply mechanism"),
    "page_scraped":      (0.60, "scraped from page markup"),
    "role_inbox":        (0.55, "role inbox, not a named person"),
    "form_thirdparty":   (0.70, "embedded third-party form confirmed on the page"),
    "listing_published": (0.85, "published on the listing page we fetched"),
    "listing_only":      (0.35, "named in a listing, nothing verified yet"),
}


def _conf(prov: str):
    score, why = PROVENANCE[prov]
    label = "high" if score >= 0.8 else "medium" if score >= 0.55 else "low"
    return {"score": score, "label": label, "provenance": prov, "basis": why}


# ===========================================================================
# Extraction from free text (post bodies). Regexes are printed by --self-test
# with repr() so a change to one is visible rather than silent.
# ===========================================================================
# North-American phone. Requires punctuation or a leading 1/+1 so that a bare
# 10-digit run inside "sq ft 2145558888"-style junk does not become a number to
# dial. Deliberately conservative: a wrong number is worse than none.
PHONE_RX = re.compile(
    r"(?:(?<=\D)|^)(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]([2-9]\d{2})[\s.\-](\d{4})(?!\d)")
EMAIL_RX = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
# Craigslist's own anonymised relay addresses. These are NOT the poster's real
# address and must never be treated as one, nor mailed by an automated sender.
CL_RELAY_RX = re.compile(r"@reply\.craigslist\.org$", re.I)


def phones_in(text: str) -> list:
    out = []
    for m in PHONE_RX.finditer(text or ""):
        num = f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
        if num not in out:
            out.append(num)
    return out


def emails_in(text: str) -> list:
    out = []
    for e in EMAIL_RX.findall(text or ""):
        e = e.strip(".,;:")
        if CL_RELAY_RX.search(e):
            continue
        if e.lower() not in [o.lower() for o in out]:
            out.append(e)
    return out


def pretty_phone(raw: str) -> str:
    """Normalise a published number for display. A number that arrives as a
    bare digit run is still a real number -- it just was not punctuated -- so
    it is formatted, never discarded."""
    got = phones_in(raw or "")
    if got:
        return got[0]
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return (raw or "").strip()


def tel_href(num: str) -> str:
    digits = re.sub(r"\D", "", num)
    if len(digits) == 10:
        digits = "1" + digits
    return "tel:+" + digits


# ===========================================================================
# Normalising every source onto one shape
# ===========================================================================
# source_junk.py craigslist/estatesales rows, source_roofing.py rows and the
# Supabase candidates table all describe leads, and all describe them
# differently. Normalise once here rather than special-casing downstream.
# Hosts that are somebody else's platform, not the prospect's own site.
AGGREGATOR_HOSTS = ("news.google.com", "reddit.com", "redd.it", "google.com",
                    "linkedin.com", "instagram.com", "tiktok.com", "facebook.com",
                    "craigslist.org", "estatesales.net", "youtube.com")

ASKED_TIERS = {"hire"}          # source_junk classify(): an explicit request
POSTED_TIERS = {"event", "signal"}   # public, but they did not ask for service


def normalise(lead: dict) -> dict:
    """Fold any source's row into the fields this resolver reasons about."""
    src = (lead.get("source") or "").lower()
    n = {
        "source": src or "unknown",
        "id": lead.get("id") or lead.get("source_id") or lead.get("url"),
        "label": lead.get("title") or lead.get("company") or lead.get("name") or "(untitled)",
        "url": lead.get("url"),
        "text": " ".join(str(x) for x in [lead.get("desc"), lead.get("body"),
                                          lead.get("title")] if x),
        "website": lead.get("website"),
        "phone": lead.get("phone"),
        "phone_prov": None,
        "email": lead.get("contact_email") or lead.get("email"),
        "email_kind": lead.get("email_kind"),
        "email_source": lead.get("email_source"),
        "contact_name": lead.get("contact_name"),
        "contact_title": lead.get("contact_title"),
        "place": lead.get("place_name") or lead.get("place"),
        "tier": lead.get("category") or lead.get("intent") or lead.get("tier"),
        "lead_kind": lead.get("lead_kind"),
        "is_person": lead.get("is_person"),
        "run_by": lead.get("run_by"),
        "identity": lead.get("identity"),
        "raw": lead,
    }
    # source_b2b rows have no `website` column: for the shipping-policy, careers
    # and store channels the row's `url` IS the brand's own site. For the news
    # and reddit channels it is an aggregator link that belongs to someone else,
    # and treating that as the prospect's site would be a fabricated path.
    if not n["website"] and n["url"] and n["source"] == "b2b":
        host = urlparse(n["url"]).netloc.lower()
        if not any(agg in host for agg in AGGREGATOR_HOSTS):
            n["website"] = n["url"]
        else:
            n["aggregator"] = host

    # Phone provenance: an identity-verified row's phone came through the place
    # match in the audit lane; a phone sitting on an unverified row did not.
    if n["phone"]:
        n["phone_prov"] = ("place_matched" if lead.get("place_name_matched")
                           else "identity_verified" if n["identity"] == "verified"
                           else "page_scraped")
    return n


def is_entity(n: dict) -> bool:
    """False for a row that does not describe a contactable person or business.

    source_roofing emits storm rows with lead_kind='targeting_area': a hail
    swath is a place to go looking, not somebody to call. Reporting one as
    "unreachable" would be true and useless, and would drag the coverage number
    down with rows that were never contactable in the first place.
    """
    return n.get("lead_kind") != "targeting_area"


def classify_permission(n: dict) -> tuple:
    """ASKED or NEVER-ASKED, plus the field that decided it.

    Conservative on purpose. Only an explicit public REQUEST FOR THE SERVICE
    counts as asking. A craigslist "moving out, free couch" post is public, and
    the platform gives it a relay, but that person did not ask to be sold to --
    calling that ASKED would licence contact nobody licensed.
    """
    if n["lead_kind"] == "person_asked" or n["is_person"] is True:
        return "asked", "row is flagged lead_kind=person_asked by its source"
    if n["source"] == "craigslist" and n["tier"] in ASKED_TIERS:
        return "asked", (f"craigslist posting classified tier={n['tier']!r} -- the "
                         "poster publicly asked to hire someone for this work")
    if n["source"] == "craigslist" and n["tier"] in POSTED_TIERS:
        return "never_asked", (f"public craigslist posting but tier={n['tier']!r}: they "
                               "posted, they did not request this service")
    if n["source"] == "estatesales":
        return "never_asked", ("estate-sale listing: the sale is an event, and the "
                               "company running it never asked to be contacted")
    if n["source"] == "b2b":
        return "never_asked", "b2b pain-signal prospect -- Wing's own cold prospecting"
    return "never_asked", (f"source={n['source']!r} carries no request-for-service "
                           "signal; treated as cold by default")


# ===========================================================================
# Path builders
# ===========================================================================
# Try-first order. Deliberately separate from confidence: see resolve().
RANK_ASKED = {"phone": 1, "platform_reply": 2, "email_poster": 3, "email_person": 4,
              "contact_form": 5, "email_role": 6}
RANK_COLD = {"email_person": 1, "phone": 2, "contact_form": 3, "email_role": 4,
             "platform_reply": 5}


def _path(method, label, action, how, reason, prov, evidence=None, **extra):
    p = {"method": method, "label": label, "action": action, "how": how,
         "reason": reason, "confidence": _conf(prov), "evidence": evidence,
         "automatable": False}
    p.update(extra)
    return p


def paths_for_asked(n: dict) -> list:
    """Someone who publicly asked. The path is usually already in the row."""
    out = []
    # 1. A number the poster typed into their own post. They published it so
    #    they would be called. Nothing beats that for speed.
    for num in phones_in(n["text"]):
        out.append(_path(
            "phone", f"Call {num}", tel_href(num),
            f"Dial {num}. Reference their post so they know where you came from.",
            "the poster published this number in the body of their own request",
            "poster_published", evidence=n["url"]))
    # 2. The platform's own reply mechanism.
    if n["source"] == "craigslist" and n["url"]:
        out.append(_path(
            "platform_reply", "Reply through the craigslist posting", n["url"],
            "Open the posting and click its Reply button. craigslist's robots.txt "
            "disallows /reply, so this link is for a HUMAN to click -- it is never "
            "fetched or automated by this system.",
            "craigslist gives every posting an anonymised relay the poster enabled "
            "when they posted",
            "platform_relay", evidence=n["url"],
            platform_rules="robots.txt Disallow: /reply -- surface only, never fetch"))
    # 3. A published address.
    for e in emails_in(n["text"]):
        out.append(_path(
            "email_poster", f"Email {e}", "mailto:" + e,
            f"Email {e} referencing their post.",
            "the poster published this address in their own request",
            "poster_published", evidence=n["url"]))
    return out


def paths_for_business(n: dict, form: dict | None = None) -> list:
    """A business that never asked. Every constraint applies, so the ordering
    is by what actually gets a reply without burning anything:
    a named human, then the phone, then a form (a form is an invited inbound,
    not cold email), then a role inbox last -- the send history is 133 sends to
    role inboxes and 0 replies."""
    out = []
    kind = (n["email_kind"] or "").lower()
    if n["email"] and kind == "personal":
        who = n["contact_name"] or "the named contact"
        out.append(_path(
            "email_person", f"Email {who} at {n['email']}", "mailto:" + n["email"],
            f"Write {who}"
            + (f" ({n['contact_title']})" if n["contact_title"] else "")
            + f" at {n['email']}.",
            "address for a NAMED person, found literally on their own site",
            "email_on_page", evidence=n["email_source"] or n["website"],
            person=n["contact_name"], title=n["contact_title"]))
    if n["phone"]:
        who = n.get("phone_label") or "the business"
        out.append(_path(
            "phone", f"Call {n['phone']}", tel_href(str(n["phone"])),
            f"Dial {n['phone']} -- {who}. Ask for the owner by name if one is known.",
            f"phone for {who}", n["phone_prov"] or "page_scraped",
            evidence=n["website"] or n["url"]))
    if form:
        out.append(_path(
            "contact_form", f"Fill the contact form at {form['url']}", form["url"],
            "Open the form and fill it in by hand -- fields: "
            + ", ".join(form["fields"] or ["(unnamed)"])
            + (". A CAPTCHA is present, so a human must complete it."
               if form["captcha"] else ". No CAPTCHA detected."),
            f"a form element was confirmed on this page ({form['detected']})",
            "form_thirdparty" if form.get("thirdparty") else "form_confirmed",
            evidence=form["url"], fields=form["fields"], captcha=form["captcha"],
            never_automate="forms are filled by a human; this system does not submit"))
    if n["email"] and kind != "personal":
        out.append(_path(
            "email_role", f"Email the role inbox {n['email']}", "mailto:" + n["email"],
            f"Email {n['email']}. Role inbox -- expect low reply rate; prefer the "
            "phone or the form if either is present.",
            f"role/unknown inbox (email_kind={n['email_kind']!r}) found on their site",
            "role_inbox", evidence=n["email_source"] or n["website"]))
    return out


def research_steps(n: dict) -> list:
    """Explicitly NOT contact paths. A search URL is a lookup, not a way to
    reach anybody, so these never count toward coverage."""
    out = []
    if n["website"]:
        out.append({"step": "open_site", "label": "Open their website",
                    "action": n["website"],
                    "why": "look for a phone, a form, or a named owner by eye"})
    if n.get("profile_url"):
        out.append({"step": "open_company_profile",
                    "label": "Open the sale company's profile on the listing site",
                    "action": n["profile_url"],
                    "why": "their other sales and stated service area, for a repeat "
                           "referral relationship rather than one job"})
    if n["run_by"]:
        out.append({"step": "lookup_company",
                    "label": f"Look up the estate-sale company: {n['run_by']}",
                    "action": "https://www.google.com/search?q="
                              + requests.utils.quote(f'"{n["run_by"]}" '
                                                     + (n["place"] or "")),
                    "why": "the company running the sale is a repeat referral partner "
                           "with a public phone and site -- worth more than the "
                           "one-off sale it was found on"})
    if not n["website"] and n["label"] and n["source"] in ("linkedin", "instagram",
                                                           "tiktok", "b2b"):
        q = n["label"] + " " + (n["place"] or "")
        out.append({"step": "find_site", "label": "Search for an official site/listing",
                    "action": "https://www.google.com/search?q=" + requests.utils.quote(q),
                    "why": "no website on the row; a listing or profile may carry a phone"})
    if n["url"] and n["source"] in ("linkedin", "instagram", "tiktok"):
        out.append({"step": "open_profile",
                    "label": f"Open the {n['source']} profile",
                    "action": n["url"],
                    "why": "public profile may publish a contact button or address. "
                           "A cold DM is NOT licensed here -- platform messaging "
                           "policy applies to a business that never asked."})
    return out


def why_missing(n: dict) -> list:
    """Say exactly what is absent. 'No path' with no explanation is a dead end;
    'no website, no phone, no listing' is a work item."""
    m = []
    if not n["website"]:
        if n.get("aggregator"):
            m.append(f"the only link on this row is {n['aggregator']} (someone "
                     "else's platform) -- the prospect's own domain is unknown")
        else:
            m.append("no website on the row")
    if not n["phone"]:
        m.append("no phone on the row")
    if not n["email"]:
        m.append("no email found on the row")
    if not n["url"]:
        m.append("no source URL to reply through")
    if n["website"] and not n["email"]:
        m.append("site fetched no confirmed form (or --forms was not run)")
    return m or ["fields present but none resolved to an actionable path"]


# ===========================================================================
# Contact FORM discovery. The big win: businesses that publish no email almost
# always publish a form, and nobody had gone looking for it.
#
# Confirms the form EXISTS by fetching the page and finding a form element.
# Records the field names and whether a CAPTCHA is present. It does NOT and
# WILL NOT submit anything -- automated form spam is worse than useless and
# CAPTCHA defeat is prohibited. There is no submit function in this file.
# ===========================================================================
FORM_LINK_RX = re.compile(
    r"""<a[^>]+href=["']([^"'#]+)["'][^>]*>(.{0,120}?)</a>""", re.I | re.S)
FORM_WORDS = re.compile(
    r"contact|get[\s\-_]?a?[\s\-_]?quote|free[\s\-_]?(estimate|quote)|request|"
    r"estimate|schedule|book|appointment|get[\s\-_]?in[\s\-_]?touch|reach[\s\-_]?us",
    re.I)
FORM_TAG_RX = re.compile(r"<form\b[^>]*>(.*?)</form>", re.I | re.S)
INPUT_RX = re.compile(r"<(?:input|textarea|select)\b([^>]*)>", re.I)
NAME_ATTR_RX = re.compile(r"""\bname=["']([^"']+)["']""", re.I)
TYPE_ATTR_RX = re.compile(r"""\btype=["']([^"']+)["']""", re.I)
CAPTCHA_RX = re.compile(r"recaptcha|hcaptcha|turnstile|captcha", re.I)
# Embedded third-party form builders. The <form> lives inside the iframe, so
# the tag test alone would miss these and call a real form page formless.
IFRAME_RX = re.compile(r"""<iframe[^>]+src=["']([^"']+)["']""", re.I)
THIRDPARTY_RX = re.compile(
    r"jotform|gravityforms|hsforms|hubspot|wufoo|typeform|formstack|"
    r"forms\.gle|google\.com/forms|123formbuilder|gravity_forms|wpforms|"
    r"ninjaforms|formidable|zohopublic|calendly", re.I)

# The estate-sale listing page itself publishes the RUNNING COMPANY's phone in
# schema.org JSON-LD, plus a link to that company's profile. Verified live on
# 2026-08-27: /TX/Arlington/76013/5027004 -> "telephone":"(817) 683-6668" and
# /companies/TX/Arlington/76013/20162. That company is a repeat referral partner
# -- arguably worth more than the one-off sale it was found on. robots.txt
# permits both paths (it disallows only /account, /homepages, /v2, /v3, /legacy).
LD_PHONE_RX = re.compile(r'"telephone"\s*:\s*"([^"]{7,25})"')
TEL_HREF_RX = re.compile(r'href="tel:(\+?[\d\-\(\)\. ]{7,20})"')
ES_COMPANY_RX = re.compile(r'/companies/[A-Z]{2}/[^"\'<>\s]+')

_robots = {}


def robots_ok(url: str) -> bool:
    """robots.txt is obeyed. Unreachable robots.txt = allowed (conventional)."""
    p = urlparse(url)
    root = f"{p.scheme}://{p.netloc}"
    rp = _robots.get(root)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(root + "/robots.txt")
        try:
            r = requests.get(root + "/robots.txt", headers=UA, timeout=10)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])
        _robots[root] = rp
    try:
        return rp.can_fetch(UA["User-Agent"], url)
    except Exception:
        return True


def fetch(url: str, retries: int = 2):
    """(html, final_url) or (None, reason). Transient failures are retried --
    a DNS blip must never be recorded as 'this business has no form'."""
    delay, last = 2, "unknown"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=FETCH_TIMEOUT, allow_redirects=True)
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
            elif r.status_code >= 400:
                return None, f"HTTP {r.status_code}"
            else:
                return r.text, r.url
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:60]}"
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    return None, last


def form_in_html(html: str, url: str) -> dict | None:
    """Confirm a usable contact form in bytes we actually downloaded.

    A <form> whose only control is a search box or a newsletter signup is not a
    contact form, and calling it one would send a human to fill in a mailing
    list. Require either a message-ish field or both a name and an email field.
    """
    captcha = bool(CAPTCHA_RX.search(html))
    best = None
    for body in FORM_TAG_RX.findall(html):
        fields, kinds = [], []
        for attrs in INPUT_RX.findall(body):
            nm = NAME_ATTR_RX.search(attrs)
            ty = (TYPE_ATTR_RX.search(attrs).group(1).lower()
                  if TYPE_ATTR_RX.search(attrs) else "text")
            if ty in ("hidden", "submit", "button", "image"):
                continue
            name = nm.group(1) if nm else f"({ty})"
            if name not in fields:
                fields.append(name)
                kinds.append(ty)
        blob = " ".join(fields).lower() + " " + body[:400].lower()
        has_msg = bool(re.search(r"message|comment|detail|describe|project|"
                                 r"how can we|textarea", blob)) or "<textarea" in body.lower()
        has_name = bool(re.search(r"\bname\b|fname|first|full[_\- ]?name", blob))
        has_mail = "email" in blob or "e-mail" in blob
        if re.search(r"\b(search|newsletter|subscribe|login|signin|password)\b", blob) \
                and not has_msg:
            continue
        if not (has_msg or (has_name and has_mail)):
            continue
        if len(fields) < 2:
            continue
        cand = {"url": url, "fields": fields, "captcha": captcha,
                "detected": "<form> element with contact-shaped fields",
                "thirdparty": False}
        if best is None or len(fields) > len(best["fields"]):
            best = cand
    if best:
        return best
    # No inline form -- is a known form builder embedded?
    for src in IFRAME_RX.findall(html):
        if THIRDPARTY_RX.search(src):
            return {"url": url, "fields": ["(fields live inside the embedded form)"],
                    "captcha": captcha, "thirdparty": True,
                    "detected": f"embedded third-party form: {src[:120]}"}
    if THIRDPARTY_RX.search(html):
        return {"url": url, "fields": ["(fields rendered by an embedded form script)"],
                "captcha": captcha, "thirdparty": True,
                "detected": "third-party form script referenced on the page"}
    return None


def find_contact_form(website: str, budget: int = 4, pace: float = PACE) -> dict:
    """Fetch the site and look for a REAL contact form.

    Returns {"form": {...}} on confirmation, or {"form": None, "reason": ...}.
    A form URL that was not fetched and confirmed is a fabrication, so an
    unconfirmed guess is never returned.
    """
    if not website:
        return {"form": None, "reason": "no website"}
    if not website.startswith("http"):
        website = "https://" + website
    if not robots_ok(website):
        return {"form": None, "reason": "robots.txt disallows fetching this site"}
    html, final = fetch(website)
    if html is None:
        return {"form": None, "reason": f"site unreachable ({final})"}

    # The homepage itself often carries the form.
    f = form_in_html(html, final)
    if f:
        return {"form": f, "reason": "form on the homepage"}

    # Otherwise follow contact-shaped links -- and only ones the page really has.
    seen, tried = set(), []
    links = []
    for href, text in FORM_LINK_RX.findall(html):
        if not FORM_WORDS.search(text) and not FORM_WORDS.search(href):
            continue
        if href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(final, href)
        if urlparse(full).netloc != urlparse(final).netloc:
            continue
        if full in seen:
            continue
        seen.add(full)
        # A link whose text says contact beats one that only matched the href.
        links.append((0 if FORM_WORDS.search(text) else 1, full))
    links.sort()
    for _, full in links[:budget]:
        if not robots_ok(full):
            continue
        time.sleep(pace)
        h2, f2 = fetch(full)
        tried.append(full)
        if h2 is None:
            continue
        got = form_in_html(h2, f2)
        if got:
            return {"form": got, "reason": "form on a contact-shaped page linked "
                                           "from the homepage"}
    if not links:
        # Distinguish "this site has no contact page" from "we were handed no
        # usable HTML". appleroofing.net returns 200 with an empty <title> and
        # zero anchors (JS-rendered); firefighterroofing.com answers 202 with an
        # empty body (bot wall). Reporting either as "no contact page" would be
        # a quiet lie about a site that very likely has one.
        anchors = len(re.findall(r"<a\b[^>]+href=", html, re.I))
        if anchors < 3 or len(re.sub(r"<[^>]+>", " ", html).split()) < 40:
            return {"form": None,
                    "reason": "homepage served no crawlable HTML (JavaScript-rendered "
                              "or bot-walled) -- a human should open it in a browser"}
        return {"form": None, "reason": "homepage links to no contact-shaped page"}
    return {"form": None, "reason": "contact pages fetched but no form element found "
                                    "(likely JavaScript-rendered) -- tried: "
                                    + ", ".join(tried[:3])}


def listing_contact(url: str) -> dict:
    """Read the contact the LISTING PAGE itself publishes.

    An estatesales.net sale page names the company running the sale and
    publishes that company's phone. The row we stored kept only the name, so
    every one of these leads reads as unreachable when the phone was sitting on
    the page the whole time. Everything returned here was matched against bytes
    downloaded from that page -- nothing is inferred.
    """
    if not url:
        return {"phone": None, "reason": "no listing url"}
    if not robots_ok(url):
        return {"phone": None, "reason": "robots.txt disallows this listing page"}
    html, final = fetch(url)
    if html is None:
        return {"phone": None, "reason": f"listing unreachable ({final})"}
    phone = None
    m = LD_PHONE_RX.search(html) or TEL_HREF_RX.search(html)
    if m:
        phone = pretty_phone(m.group(1)) or None
    prof = ES_COMPANY_RX.search(html)
    return {"phone": phone,
            "profile_url": (urljoin(final, prof.group(0)) if prof else None),
            "reason": "read off the listing page" if phone
                      else "listing page publishes no phone"}


# ===========================================================================
# The resolver
# ===========================================================================
def resolve(lead: dict, form_lookup=None, listing_lookup=None) -> dict:
    """Best legitimate contact path for one lead, ranked.

    form_lookup:    optional callable(website) -> {"form": ...|None, "reason": str}
    listing_lookup: optional callable(url)     -> {"phone": ...|None, ...}
    Left None, no network happens at all and those paths simply do not appear.
    """
    n = normalise(lead)
    permission, why = classify_permission(n)

    if not is_entity(n):
        return {"lead_id": n["id"], "lead": n["label"], "source": n["source"],
                "permission": permission, "permission_reason": why,
                "contactable_entity": False, "paths": [], "best": None,
                "actionable": False, "research": [],
                "missing": ["this row is a targeting AREA, not a person or a "
                            "business -- there is nobody to contact. Use it to "
                            "choose WHO to reach, not as a lead."]}

    # Listing-page enrichment. Runs before path building so the phone the page
    # publishes is available to rank alongside everything else.
    listing_note = None
    if listing_lookup and n["source"] == "estatesales" and n["url"] and not n["phone"]:
        got = listing_lookup(n["url"]) or {}
        listing_note = got.get("reason")
        if got.get("phone"):
            n["phone"] = got["phone"]
            n["phone_prov"] = "listing_published"
            n["phone_label"] = (f"{n['run_by']} (company running the sale)"
                                if n["run_by"] else "the company running the sale")
        if got.get("profile_url"):
            n["profile_url"] = got["profile_url"]

    if permission == "asked":
        paths = paths_for_asked(n)
        # Someone who asked may ALSO be a business row with stored contacts.
        paths += paths_for_business(n)
    else:
        form = None
        form_reason = "form discovery not run"
        if form_lookup and n["website"]:
            got = form_lookup(n["website"]) or {}
            form = got.get("form")
            form_reason = got.get("reason", "")
        paths = paths_for_business(n, form=form)
        if n["source"] == "craigslist" and n["url"] and n["tier"] in POSTED_TIERS:
            # The relay exists and the platform intends it to be used, but this
            # poster did not ask to be sold to. Surfaced, ranked last, flagged.
            paths.append(_path(
                "platform_reply", "Reply through the craigslist posting (UNSOLICITED)",
                n["url"],
                "Open the posting and click Reply. This person did NOT ask for this "
                "service -- only reply if the message is genuinely about their post. "
                "Never fetched or automated (robots.txt disallows /reply).",
                "public posting with a platform relay, but no request for service",
                "platform_relay", evidence=n["url"], unsolicited=True,
                platform_rules="robots.txt Disallow: /reply -- surface only"))

    # Priority is NOT the same thing as confidence. A place-matched phone is the
    # fact we are most SURE of; a named person's email is the one most likely to
    # get a reply (the send history: 133 role-inbox sends 0 replies, one
    # researched named-founder email closed a deal). So order by what to try
    # first, and break ties with how sure we are.
    order = RANK_ASKED if permission == "asked" else RANK_COLD
    paths.sort(key=lambda p: (order.get(p["method"], 99), -p["confidence"]["score"]))
    plan = {
        "lead_id": n["id"],
        "lead": n["label"],
        "source": n["source"],
        "contactable_entity": True,
        "permission": permission,
        "permission_reason": why,
        "paths": paths,
        "best": paths[0] if paths else None,
        "actionable": bool(paths),
        "research": research_steps(n),
        "missing": ([] if paths else
                    why_missing(n) + ([listing_note] if listing_note else [])),
    }
    if listing_note:
        plan["listing_note"] = listing_note
    if permission == "never_asked" and n["website"] and not any(
            p["method"] == "contact_form" for p in paths):
        plan["form_note"] = form_reason if form_lookup else "form discovery not run"
    return plan


def coverage(plans: list) -> dict:
    """What fraction of leads now have a REAL way to be contacted, per source."""
    by = {}
    for p in plans:
        s = by.setdefault(p["source"], {"total": 0, "actionable": 0, "asked": 0,
                                        "never_asked": 0, "methods": {}, "stuck": [],
                                        "not_an_entity": 0})
        if not p.get("contactable_entity", True):
            # Counted, and kept out of the denominator: a hail swath was never
            # a thing you could phone.
            s["not_an_entity"] += 1
            continue
        s["total"] += 1
        s[p["permission"]] += 1
        if p["actionable"]:
            s["actionable"] += 1
            for path in p["paths"]:
                s["methods"][path["method"]] = s["methods"].get(path["method"], 0) + 1
        else:
            s["stuck"].append({"lead": p["lead"], "id": p["lead_id"],
                               "missing": p["missing"]})
    for s in by.values():
        s.setdefault("not_an_entity", 0)
    tot = sum(s["total"] for s in by.values())
    act = sum(s["actionable"] for s in by.values())
    return {"total": tot, "actionable": act,
            "pct": round(100.0 * act / tot, 1) if tot else 0.0,
            "by_source": by}


# ===========================================================================
# CLI
# ===========================================================================
def load_supabase(limit: int, identity: str = "verified") -> list:
    from db import load_env
    env = load_env()
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    params = {"select": "id,source,title,url,website,phone,email,contact_email,"
                        "email_kind,email_source,contact_name,contact_title,"
                        "identity,place_name,place_name_matched,category,intent",
              "order": "id.asc"}
    if identity != "any":
        params["identity"] = f"eq.{identity}"
    if limit:
        params["limit"] = str(limit)
    r = requests.get(f"{url}/rest/v1/candidates", headers=h, params=params, timeout=60)
    if not r.ok:
        sys.exit(f"Supabase read failed: {r.status_code} {r.text[:200]}")
    return r.json()


def print_plan(p: dict, verbose=True):
    mark = "OK " if p["actionable"] else "-- "
    print(f"{mark}[{p['source']}:{p['lead_id']}] {str(p['lead'])[:64]}")
    print(f"     permission: {p['permission'].upper()}  ({p['permission_reason']})")
    for i, path in enumerate(p["paths"], 1):
        c = path["confidence"]
        flag = "  !! UNSOLICITED" if path.get("unsolicited") else ""
        print(f"     {i}. {path['method']:<14} {c['label']:<6} {c['score']:.2f}  "
              f"{path['label']}{flag}")
        if verbose:
            print(f"        action: {path['action']}")
            print(f"        why   : {path['reason']} [{c['basis']}]")
            if path.get("fields"):
                print(f"        fields: {', '.join(path['fields'])}  "
                      f"captcha={path.get('captcha')}")
    if not p["actionable"]:
        print(f"     NO CONTACT PATH -- missing: {'; '.join(p['missing'])}")
        for rstep in p["research"]:
            print(f"        research: {rstep['label']} -> {rstep['action'][:90]}")
    elif p.get("form_note"):
        print(f"     form: {p['form_note']}")
    print()


def self_test() -> int:
    print("regexes:")
    for nm, rx in [("PHONE_RX", PHONE_RX), ("EMAIL_RX", EMAIL_RX),
                   ("CL_RELAY_RX", CL_RELAY_RX), ("FORM_WORDS", FORM_WORDS),
                   ("CAPTCHA_RX", CAPTCHA_RX), ("THIRDPARTY_RX", THIRDPARTY_RX)]:
        print(f"  {nm} = {rx.pattern!r}")
    fails = []

    def ck(cond, msg):
        if not cond:
            fails.append(msg)

    ck(phones_in("call me at 214-555-0134 today") == ["(214) 555-0134"], "phone parse")
    ck(phones_in("sqft 2145550134") == [], "bare digit run must not become a number")
    ck(emails_in("mail a@b.com and x@reply.craigslist.org") == ["a@b.com"],
       "craigslist relay address must never be treated as the poster's own")
    ck(tel_href("(214) 555-0134") == "tel:+12145550134", "tel href")

    hire = {"source": "craigslist", "source_id": "1", "url": "https://dallas.craigslist.org/x/1.html",
            "title": "Need junk hauled", "desc": "need someone to haul this away, $125",
            "category": "hire", "intent": "hire"}
    p = resolve(hire)
    ck(p["permission"] == "asked", "hire posting must be ASKED")
    ck(p["paths"][0]["method"] == "platform_reply", "relay is the path for an asker")
    ck(all(not x["automatable"] for x in p["paths"]), "nothing is automatable")

    event = dict(hire, category="event", intent="event", desc="estate sale saturday")
    pe = resolve(event)
    ck(pe["permission"] == "never_asked", "an event posting is NOT someone who asked")
    ck(pe["paths"][0].get("unsolicited") is True, "relay for a non-asker must be flagged")

    biz = {"source": "linkedin", "id": 9, "title": "Some Roofing Co",
           "website": "https://example.com", "phone": "(972) 555-0100",
           "identity": "verified", "place_name_matched": True}
    pb = resolve(biz)
    ck(pb["permission"] == "never_asked", "a business row is never ASKED")
    ck(pb["paths"][0]["method"] == "phone", "phone leads when there is no named email")
    ck(pb["paths"][0]["confidence"]["provenance"] == "place_matched",
       "matched place record must outrank a scraped footer")

    naked = {"source": "linkedin", "id": 10, "title": "No Data Co"}
    pn = resolve(naked)
    ck(pn["actionable"] is False, "must refuse to invent a path")
    ck("no website on the row" in pn["missing"], "must say WHAT is missing")
    ck(pn["paths"] == [], "no fabricated paths")

    html = ('<form action="/send"><input name="your-name"><input name="your-email" '
            'type="email"><textarea name="message"></textarea>'
            '<div class="g-recaptcha"></div></form>')
    f = form_in_html(html, "https://example.com/contact")
    ck(f and f["fields"] == ["your-name", "your-email", "message"], "form fields")
    ck(f["captcha"] is True, "captcha detection")
    ck(form_in_html('<form><input name="s" type="search"></form>', "u") is None,
       "a search box is not a contact form")
    ck(form_in_html('<form><input name="EMAIL"><input type="submit"></form>', "u") is None,
       "a newsletter signup is not a contact form")
    ck(not hasattr(sys.modules[__name__], "submit_form"),
       "this module must never contain a form submitter")

    for f_ in fails:
        print("  FAIL:", f_)
    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURES'}")
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--supabase", action="store_true", help="resolve candidates table")
    ap.add_argument("--identity", default="verified", help="verified | any")
    ap.add_argument("--jsonl", action="append", default=[],
                    help="a source's jsonl output (repeatable)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--forms", action="store_true",
                    help="live contact-form discovery (fetches sites, never submits)")
    ap.add_argument("--listings", action="store_true",
                    help="live listing-page enrichment (reads the phone an "
                         "estate-sale listing publishes for the company running it)")
    ap.add_argument("--live", action="store_true", help="--forms and --listings")
    ap.add_argument("--form-budget", type=int, default=4)
    ap.add_argument("--pace", type=float, default=PACE)
    ap.add_argument("--quiet", action="store_true", help="one line per lead")
    ap.add_argument("--report", action="store_true", help="coverage summary only")
    ap.add_argument("--out", help="write resolved plans as jsonl")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()
    if a.live:
        a.forms = a.listings = True

    leads = []
    for path in a.jsonl:
        p = pathlib.Path(path)
        if not p.exists():
            print(f"skip (missing): {p}", file=sys.stderr)
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                leads.append(json.loads(line))
    if a.supabase:
        leads += load_supabase(a.limit, a.identity)
    if not leads:
        ap.error("nothing to resolve: pass --supabase and/or --jsonl")
    if a.limit and len(leads) > a.limit and not a.supabase:
        leads = leads[:a.limit]

    cache = {}

    def lookup(site):
        if site not in cache:
            cache[site] = find_contact_form(site, budget=a.form_budget, pace=a.pace)
            time.sleep(a.pace)
        return cache[site]

    lcache = {}

    def llookup(u):
        if u not in lcache:
            lcache[u] = listing_contact(u)
            time.sleep(a.pace)
        return lcache[u]

    plans = [resolve(l, form_lookup=lookup if a.forms else None,
                     listing_lookup=llookup if a.listings else None) for l in leads]

    if not a.report:
        for p in plans:
            print_plan(p, verbose=not a.quiet)

    cov = coverage(plans)
    print("=" * 68)
    print(f"COVERAGE: {cov['actionable']}/{cov['total']} leads have at least one "
          f"real contact path ({cov['pct']}%)")
    for src, s in sorted(cov["by_source"].items()):
        pct = round(100.0 * s["actionable"] / s["total"], 1) if s["total"] else 0.0
        print(f"  {src:<14} {s['actionable']}/{s['total']} ({pct}%)  "
              f"asked={s['asked']} never_asked={s['never_asked']}  "
              f"methods={s['methods']}"
              + (f"  [{s['not_an_entity']} targeting-area rows, nobody to contact]"
                 if s["not_an_entity"] else ""))
        for stuck in s["stuck"]:
            print(f"      UNREACHABLE [{stuck['id']}] {str(stuck['lead'])[:44]} "
                  f"-- {'; '.join(stuck['missing'])}")
    if a.out:
        pathlib.Path(a.out).write_text(
            "\n".join(json.dumps(p, ensure_ascii=False) for p in plans) + "\n",
            encoding="utf-8")
        print(f"\nwrote {len(plans)} plans -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
