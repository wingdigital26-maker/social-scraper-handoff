#!/usr/bin/env python3
"""
contact_find.py -- Find a REAL, NAMED contact for identity-verified leads.

The problem this exists to solve: of the identity-verified leads in Supabase,
almost all have a phone but barely a third have any email, and the emails that
do exist are role inboxes (info@, sales@, help@). Wing's own suppression list
calls those ROLE_JUNK_LOCALPARTS -- "never cold-email these" -- and the send
history backs it up: 133 sends to role inboxes, 0 replies, while one researched
email to a NAMED founder closed a deal.

So for every verified candidate that has a website, this mines the site for:

    contact_email       an address that LITERALLY APPEARS on a page we fetched
    email_kind          personal | role | unknown  (classified honestly)
    contact_name        the owner / decision-maker, if the site names one
    contact_title       their title, if the site states it
    email_source        the exact URL the address came from, so a human can check
    contact_checked_at  when we looked

HARD RULES (these are the whole point -- do not relax them):
  * NEVER guess or construct an address. We do not synthesize firstname@domain
    from a name on an About page. A fabricated address bounces and burns the
    sending domain, which is strictly worse than having no address at all.
    Every stored address was matched by regex against bytes we downloaded.
  * The address's domain must match the business's own site domain (or be a
    free-mail address whose local-part clearly IS this business). An address
    lifted from an embedded third-party widget belongs to someone else.
  * robots.txt is respected, requests are paced, and network failures are
    RETRIED -- a DNS blip must never be recorded as "no email found".
  * MX-verified via ghl-cli/verify_emails.verify(). The result is RECORDED
    (email_mx), never used to silently drop a row.

Usage:
    python contact_find.py --dry-run --limit 10     # research + print, write nothing
    python contact_find.py --limit 20               # research + write to Supabase
    python contact_find.py --recheck                # re-do rows already checked
"""
import argparse
import pathlib
import re
import sys
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from db import load_env                    # noqa: E402  (env loader, shared)
from audit_prospect import sb_request      # noqa: E402  (every DB call goes through this)

# Windows consoles default to cp1252 and business names routinely contain
# symbols and emoji it cannot encode. Without this, printing a single
# prospect name raises UnicodeEncodeError and kills the whole run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# MX check, reused rather than reimplemented. Optional: if ghl-cli is not on
# this machine we still find addresses, we just cannot label their MX.
try:
    sys.path.insert(0, r"C:\Users\wjack\ghl-cli")
    from verify_emails import verify as mx_verify
except Exception:                                       # pragma: no cover
    mx_verify = None

try:
    import dns.resolver as _dnsres
except Exception:                                       # pragma: no cover
    _dnsres = None


def mx_status(email: str) -> str:
    """'ok' | 'no-mailserver' | 'bad-syntax' | 'unknown'.

    verify_emails.verify() first (DNS-over-HTTPS). That path is blocked on some
    networks and returns 'unknown' for everything, which would make the column
    worthless, so we fall back to a local MX/A lookup. Either way the answer is
    RECORDED, never used to drop a row -- an address we cannot check is still an
    address a human can look at.
    """
    if mx_verify:
        try:
            st = mx_verify(email)[0]
            if st != "unknown":
                return st
        except Exception:
            pass
    if _dnsres and "@" in email:
        dom = email.split("@", 1)[1]
        for rt in ("MX", "A"):
            try:
                if _dnsres.resolve(dom, rt):
                    return "ok"
            except _dnsres.NoAnswer:
                continue
            except _dnsres.NXDOMAIN:
                return "no-mailserver"
            except Exception:
                return "unknown"
        return "no-mailserver"
    return "unknown"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
FETCH_TIMEOUT = 20
PACE = 1.0          # seconds between page fetches, per site
MAX_PAGES = 7       # homepage + up to 6 contact/about/team pages

# Pages where a business names a human. Ordered: the ones that name owners come
# first, so the crawl budget is spent where decision-makers actually live.
CONTACT_HINTS = re.compile(
    r"/(about|about-us|our-team|team|meet-the-team|our-story|staff|leadership|"
    r"who-we-are|owner|contact|contact-us)\b", re.I)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Strings that look like an email but are not one: asset filenames, JS package
# specifiers ("slick-carousel@1.8.1" is literally sitting in the DB today as a
# lead's "email"), telemetry DSNs, and CMS placeholders.
JUNK_FRAGMENTS = (
    "example.com", "example.org", "yourdomain", "domain.com", "email.com",
    "sentry", "wixpress", "wix.com", "squarespace", "godaddy", "wordpress",
    "cloudflare", "schema.org", "w3.org", "jquery", "bootstrap", "fontawesome",
    "@2x", "@3x", "u003", "sentry.io", "core-js", "babel", "webpack",
)
JUNK_TAIL = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js|ico|woff2?|ttf|mp4)$", re.I)
# Package specifiers: name@1.2.3 -- the "TLD" is all digits.
VERSION_LIKE = re.compile(r"@[\d.]+$")

FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "aol.com", "outlook.com",
             "icloud.com", "live.com", "msn.com", "protonmail.com", "me.com"}

# Straight from wing_suppression.ROLE_JUNK_LOCALPARTS -- the addresses Wing has
# already decided never to cold-email -- plus the obvious extras.
ROLE_LOCALPARTS = {
    "info", "contact", "office", "admin", "sales", "support", "hello", "team",
    "service", "services", "estimates", "estimating", "billing", "accounts",
    "accounting", "hr", "jobs", "careers", "webmaster", "postmaster", "noreply",
    "no-reply", "donotreply", "marketing", "newsletter", "privacy", "legal",
    "abuse", "help", "enquiries", "inquiries", "mail", "orders", "order",
    "booking", "bookings", "schedule", "scheduling", "dispatch", "quotes",
    "quote", "leads", "customerservice", "claims", "warranty", "general",
    "reception", "frontdesk", "email", "web", "website", "everyone",
}


# --------------------------------------------------------------- fetching ---
_robots_cache: dict = {}


def robots_ok(url: str) -> bool:
    """True if robots.txt permits us. Unreachable robots.txt = allowed (the
    conventional reading), but a robots.txt that says no is obeyed."""
    p = urlparse(url)
    root = f"{p.scheme}://{p.netloc}"
    rp = _robots_cache.get(root)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(root + "/robots.txt")
        try:
            r = requests.get(root + "/robots.txt", headers=UA, timeout=10)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])          # cannot read it -> do not block ourselves
        _robots_cache[root] = rp
    try:
        return rp.can_fetch(UA["User-Agent"], url)
    except Exception:
        return True


def fetch(url: str, retries: int = 3):
    """Fetch a page, retrying transient failures.

    This machine has intermittent DNS. A single failed lookup must not be
    written down as "this business has no email" -- that is a silent data lie
    that survives forever. Returns (html, final_url) or (None, reason).
    """
    delay = 2
    last = "unknown"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=FETCH_TIMEOUT, allow_redirects=True)
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
            elif r.status_code >= 400:
                return None, f"HTTP {r.status_code}"      # real 404, not transient
            else:
                return r.text, r.url
        except Exception as e:
            last = type(e).__name__ + ": " + str(e)[:60]
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    return None, last


# --------------------------------------------------------- email plumbing ---
def registrable(host: str) -> str:
    host = (host or "").lower().strip("/").replace("www.", "")
    host = host.split(":")[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return host
    # Handle co.uk-style two-part suffixes cheaply.
    if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org", "gov", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def norm_biz(s: str) -> str:
    """Business name / domain root -> comparable alnum token (noise dropped)."""
    s = re.sub(r"[^a-z0-9]+", " ", (s or "").lower())
    noise = {"llc", "inc", "co", "corp", "ltd", "the", "tx", "texas", "dfw",
             "roofing", "roofers", "roof", "roofer", "construction", "contractors",
             "contractor", "company", "and", "of", "services", "service", "group",
             "exteriors", "exterior", "restoration", "solutions", "home", "homes"}
    return "".join(t for t in s.split() if t not in noise)


def plausible_email(e: str) -> bool:
    low = e.lower()
    if any(j in low for j in JUNK_FRAGMENTS):
        return False
    if JUNK_TAIL.search(low) or VERSION_LIKE.search(low):
        return False
    dom = low.split("@", 1)[1]
    if dom.split(".")[-1].isdigit() or len(dom) < 4:
        return False
    if len(low) > 90:
        return False
    return True


def emails_on_page(html: str) -> list:
    """Every address that literally appears in this page's bytes.

    Covers plain text, mailto: hrefs, and the two obfuscations that actually
    show up in the wild (&#64; entity and the "name [at] domain" spelling).
    Nothing here invents an address -- each one is read off the page.
    """
    text = html
    text = text.replace("&#64;", "@").replace("&commat;", "@").replace("%40", "@")
    text = re.sub(r"\s*\[\s*at\s*\]\s*|\s*\(\s*at\s*\)\s*", "@", text, flags=re.I)
    text = re.sub(r"\s*\[\s*dot\s*\]\s*|\s*\(\s*dot\s*\)\s*", ".", text, flags=re.I)
    found = []
    # mailto: first -- an address a human deliberately published
    for m in re.findall(r'mailto:\s*([^"\'?>\s&]+)', text, re.I):
        found.append(m)
    found += EMAIL_RE.findall(text)
    out, seen = [], set()
    for e in found:
        e = e.strip(".,;:<>()\"'").lower()
        if EMAIL_RE.fullmatch(e) and plausible_email(e) and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def belongs_to_business(email: str, site_host: str, biz_name: str) -> bool:
    """Is this address the BUSINESS's, or someone else's?

    Sites embed third-party widgets, agency credits, and chat scripts, all of
    which leak addresses belonging to other companies. Only two things pass:
    an address on the business's own registrable domain, or a free-mail address
    whose local-part clearly IS this business (mesquiteroofing@gmail.com for
    mesquiteroofing.com -- very common for small trades).
    """
    site_reg = registrable(site_host)
    if not site_reg:
        return False
    edom = registrable(email.split("@", 1)[1])
    if edom == site_reg:
        return True
    if edom in FREE_MAIL:
        local = norm_biz(email.split("@", 1)[0])
        for target in (norm_biz(site_reg.split(".")[0]), norm_biz(biz_name)):
            if target and len(target) >= 4 and local and len(local) >= 4:
                if local == target or local in target or target in local:
                    return True
    return False


# Local-part words that prove an address is a BUSINESS mailbox even though it
# is not a classic role inbox. Without this, "mckinney.roofer@gmail.com" scores
# as personal on shape alone -- two alpha parts, neither of them info/sales --
# and a "personal email" count built on that is a lie.
NOT_A_PERSON_PART = {
    "roofer", "roofers", "roofing", "roof", "roofs", "construction", "contractor",
    "contractors", "exteriors", "exterior", "restoration", "remodeling", "builders",
    "hvac", "plumbing", "solar", "gutters", "siding", "windows", "company", "biz",
    "business", "llc", "inc", "co", "group", "tx", "texas", "dfw", "usa", "the",
    "my", "get", "call", "book", "free", "quote", "estimate", "repair", "repairs",
}


def classify_email(email: str, names: list) -> str:
    """personal | role | unknown -- classified HONESTLY.

    Labelling info@ as "personal" would make the report look better and the
    campaign perform worse, so the bar for "personal" is evidence: the
    local-part must look like a human, ideally one this site actually names.
    Anything ambiguous is "unknown", never upgraded.
    """
    local = email.split("@", 1)[0].lower()
    parts = [p for p in re.split(r"[._\-+0-9]+", local) if p]
    if local in ROLE_LOCALPARTS or any(p in ROLE_LOCALPARTS for p in parts):
        return "role"

    name_tokens = set()
    for n in names:
        for t in re.split(r"[^A-Za-z]+", n.lower()):
            if len(t) > 2:
                name_tokens.add(t)

    # Strongest evidence: the local-part is built out of a person this site names.
    if name_tokens:
        if any(p in name_tokens for p in parts if len(p) > 2):
            return "personal"
        squashed = re.sub(r"[^a-z]", "", local)
        for n in names:
            toks = [t for t in re.split(r"[^A-Za-z]+", n.lower()) if t]
            if len(toks) >= 2:
                first, last = toks[0], toks[-1]
                if squashed in (first + last, last + first,
                                first[0] + last, first + last[0]):
                    return "personal"

    # Shape evidence: "jane.smith@" / "j.smith@" is a person's mailbox layout
    # that no business uses for a role inbox -- but only if neither half is a
    # trade or company word.
    if any(p in NOT_A_PERSON_PART for p in parts):
        return "unknown"
    if len(parts) == 2 and all(p.isalpha() for p in parts):
        if len(parts[0]) >= 1 and len(parts[1]) >= 2 and not any(
                p in ROLE_LOCALPARTS for p in parts):
            return "personal"

    return "unknown"


# ------------------------------------------------------------ human names ---
# Reused from ghl-cli/wing_enrich_roofers.py owner_from() -- same detectors,
# extended with title capture and schema.org markup.
OWNER_RE = re.compile(
    r"(?:owner|founder|co-founder|president|ceo|owned (?:and|&) operated by|"
    r"proudly owned by|meet the owner|owner/operator)[^A-Za-z]{0,30}"
    r"([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)")
# The other direction: "Jane Smith, Owner" / "Jane Smith - Founder". Team pages
# very often render the name in caps with the title straight after and no
# punctuation at all ("RALPH HARRIS President"), so the separator is optional.
TITLES = (r"Owner|Co-?Owner|Founder|Co-?Founder|President|CEO|COO|"
          r"Vice President(?: of [A-Z][a-z]+)?|General Manager|Managing Partner|"
          r"Principal|Operations Manager|Office Director|Director of Operations|"
          r"Director of [A-Z][a-z]+|Project Manager|Sales Manager")
NAME_THEN_TITLE = re.compile(
    r"\b((?:[A-Z][a-z]+|[A-Z]{2,})(?:\s+[A-Z]\.?)?\s+(?:[A-Z][a-z]+|[A-Z]{2,}))"
    r"\s*(?:[,\-\u2013|]\s*)?(" + TITLES + r")\b")
SCHEMA_PERSON = re.compile(
    r'"(?:founder|owner|employee|author)"\s*:\s*(?:\[\s*)?\{[^}]*?"name"\s*:\s*"([^"]{3,60})"', re.I)

BAD_NAME_WORDS = {
    "the", "our", "we", "roofing", "company", "about", "home", "contact",
    "hello", "welcome", "call", "get", "free", "your", "this", "these",
    "please", "learn", "read", "see", "click", "schedule", "request",
    "quality", "trusted", "local", "family", "veteran", "licensed", "privacy",
    "terms", "service", "services", "united", "states", "texas", "north",
    "south", "east", "west", "google", "facebook", "all", "rights", "reserved",
    "customer", "reviews", "review", "roof", "storm", "insurance", "new",
}


def tidy_name(name: str) -> str:
    """'RALPH HARRIS' -> 'Ralph Harris'. Mixed-case names are left alone so we
    do not mangle a McDonald or a DeLuca that the site spelled correctly."""
    toks = []
    for t in name.split():
        toks.append(t.capitalize() if t.isupper() and len(t) > 1 else t)
    return " ".join(toks)


def looks_like_person(name: str) -> bool:
    toks = name.split()
    if not 2 <= len(toks) <= 3:
        return False
    for t in toks:
        bare = t.strip(".").lower()
        if bare in BAD_NAME_WORDS:
            return False
        if not re.fullmatch(r"[A-Z][a-z]+|[A-Z]{2,}|[A-Z]\.?", t):
            return False
    return True


def strip_tags(html: str) -> str:
    txt = re.sub(r"<(script|style|noscript).*?</\1>", " ", html, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#039;", "'")
    return re.sub(r"\s+", " ", txt)


def people_on_page(html: str) -> list:
    """[(name, title_or_None)] for humans this page names. Evidence only."""
    out = []
    for n in SCHEMA_PERSON.findall(html):
        n = tidy_name(n.strip())
        if looks_like_person(n):
            out.append((n, None))
    text = strip_tags(html)
    for n, t in NAME_THEN_TITLE.findall(text):
        if looks_like_person(n):
            out.append((tidy_name(n.strip()), t.strip()))
    for m in OWNER_RE.finditer(text):
        n = tidy_name(m.group(1).strip())
        if looks_like_person(n):
            title = m.group(0).split(n)[0]
            title = re.sub(r"[^A-Za-z/ &-]", " ", title).strip() or None
            out.append((n, title.title() if title else None))
    # dedupe, keeping the first (and the first titled) sighting
    seen, dedup = set(), []
    for n, t in out:
        if n.lower() in seen:
            if t:
                for i, (dn, dt) in enumerate(dedup):
                    if dn.lower() == n.lower() and not dt:
                        dedup[i] = (dn, t)
            continue
        seen.add(n.lower())
        dedup.append((n, t))
    return dedup


# ------------------------------------------------------------- the miner ---
def mine(cand: dict) -> dict:
    """Crawl one lead's site. Returns the columns to write (or a no-find)."""
    site = (cand.get("website") or "").strip()
    biz = cand.get("title") or ""
    if not site.startswith("http"):
        site = "https://" + site
    host = urlparse(site).netloc
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out = {"contact_email": None, "email_kind": None, "contact_name": None,
           "contact_title": None, "email_source": None, "contact_checked_at": now}

    if not robots_ok(site):
        print("      robots.txt disallows the homepage -- skipping this site")
        return out

    html, final = fetch(site)
    if html is None:
        # A fetch failure is a fetch failure. It is NOT "no email found", so we
        # leave contact_checked_at unset and let the next run try again.
        print(f"      site unreachable ({final}) -- leaving unchecked for a retry")
        return None
    pages = [(final, html)]

    links = set(re.findall(r'href=["\']([^"\']+)["\']', html))
    internal = []
    for l in links:
        if l.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        full = urljoin(final, l)
        if urlparse(full).netloc != urlparse(final).netloc:
            continue
        if CONTACT_HINTS.search(urlparse(full).path or ""):
            full = full.split("#")[0]
            if full not in [p for p, _ in pages] and full not in internal:
                internal.append(full)
    for link in internal[:MAX_PAGES - 1]:
        if not robots_ok(link):
            continue
        time.sleep(PACE)
        h, f = fetch(link, retries=2)
        if h:
            pages.append((f, h))

    # --- who does this site name?
    people = []
    for _, h in pages:
        people.extend(people_on_page(h))
    names = [n for n, _ in people]

    # --- what addresses literally appear, and on which page?
    found = []              # (email, source_url)
    for purl, h in pages:
        for e in emails_on_page(h):
            if belongs_to_business(e, host, biz) and e not in [x for x, _ in found]:
                found.append((e, purl))
            elif not belongs_to_business(e, host, biz):
                print(f"      rejected off-domain {e} (site is {registrable(host)})")

    if not found:
        print(f"      no on-domain address on {len(pages)} page(s)"
              f"{' | names seen: ' + ', '.join(names[:3]) if names else ''}")
        # Still record any human we found -- a name is worth having even without
        # an address, and it is what a human researcher would follow up on.
        if people:
            out["contact_name"], out["contact_title"] = people[0]
        return out

    # Prefer a personal address over a role one. Never relabel to get there.
    scored = [(e, u, classify_email(e, names)) for e, u in found]
    rank = {"personal": 0, "unknown": 1, "role": 2}
    scored.sort(key=lambda x: rank[x[2]])
    email, source, kind = scored[0]

    out["contact_email"] = email
    out["email_kind"] = kind
    out["email_source"] = source

    # Attach the human, preferring the one the address itself points at.
    person = None
    if kind == "personal":
        local_parts = set(p for p in re.split(r"[._\-+0-9]+", email.split("@")[0]) if len(p) > 2)
        person = next((p for p in people
                       if local_parts & set(t.lower() for t in p[0].split())), None)
    # Otherwise take the most senior person named, not merely the first one on
    # the page -- a team page lists ten project managers before the owner.
    def seniority(p):
        t = (p[1] or "").lower()
        for i, w in enumerate(("owner", "founder", "president", "ceo", "principal",
                               "partner", "vice president", "general manager",
                               "director", "manager")):
            if w in t:
                return i
        return 99
    person = person or (sorted(people, key=seniority)[0] if people else None)
    if person:
        out["contact_name"], out["contact_title"] = person

    mx = mx_status(email)
    out["email_mx"] = mx or None
    print(f"      {kind.upper():8} {email}  <- {source}")
    if out["contact_name"]:
        print(f"      name: {out['contact_name']}"
              f"{' (' + out['contact_title'] + ')' if out['contact_title'] else ''}")
    if mx:
        print(f"      mx: {mx}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="research + print, write nothing")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--recheck", action="store_true", help="re-do rows already checked")
    args = ap.parse_args()

    env = load_env()
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json", "Prefer": "return=minimal"}

    params = {"identity": "eq.verified", "website": "not.is.null",
              "select": "id,title,website,email,contact_email,contact_checked_at",
              "order": "id.asc"}
    if not args.recheck:
        params["contact_checked_at"] = "is.null"
    if args.limit:
        params["limit"] = str(args.limit)
    r = sb_request("GET", f"{url}/rest/v1/candidates", headers=h, params=params)
    if r is None or not r.ok:
        sys.exit("Could not read verified leads from Supabase.")
    rows = r.json()
    if not rows:
        print("Nothing to check."); return

    print(f"Mining {len(rows)} verified leads with a website for a NAMED contact\n")
    tally = {"personal": 0, "role": 0, "unknown": 0, "none": 0, "unreachable": 0}
    has_mx_col = True

    for c in rows:
        print(f"[{c['id']}] {c.get('title')}  {c.get('website')}")
        try:
            res = mine(c)
        except Exception as e:
            print(f"      ERROR {type(e).__name__}: {str(e)[:90]}")
            tally["unreachable"] += 1
            continue
        if res is None:
            tally["unreachable"] += 1
            continue
        tally[res["email_kind"] or "none"] += 1
        if not args.dry_run:
            payload = dict(res)
            mx = payload.pop("email_mx", None)
            if mx and has_mx_col:
                payload["email_mx"] = mx
            pr = sb_request("PATCH", f"{url}/rest/v1/candidates", headers=h,
                            params={"id": f"eq.{c['id']}"}, json=payload)
            if pr is not None and pr.status_code == 400 and "email_mx" in (pr.text or ""):
                # Column not in this schema -- record everything else rather
                # than losing the whole row over one optional field.
                has_mx_col = False
                payload.pop("email_mx", None)
                pr = sb_request("PATCH", f"{url}/rest/v1/candidates", headers=h,
                                params={"id": f"eq.{c['id']}"}, json=payload)
            if pr is None or not pr.ok:
                print(f"      WRITE FAILED: {getattr(pr, 'text', 'no response')[:120]}")
        time.sleep(PACE)

    print("\n" + "-" * 60)
    print(f"{'DRY RUN -- nothing written' if args.dry_run else 'written to Supabase'}")
    print(f"  personal email  : {tally['personal']}")
    print(f"  role email only : {tally['role']}   <- do not cold-email these")
    print(f"  unknown kind    : {tally['unknown']}")
    print(f"  no address found: {tally['none']}")
    print(f"  site unreachable: {tally['unreachable']} (left unchecked, will retry)")


if __name__ == "__main__":
    main()
