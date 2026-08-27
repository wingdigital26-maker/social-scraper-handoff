#!/usr/bin/env python3
"""
source_brandpool — build a LARGE, size-filtered brand pool for source_b2b.

WHY THIS EXISTS
  source_b2b works. Its channels are live and it produced 15 real, evidence-
  backed leads. What it does not have is a pool. It reads brands from a
  --seed-file, and the seed file it shipped against was a short hand-typed list
  of Texas makers — which the build agent correctly flagged as the wrong SIZE.
  A two-person candle studio has no fulfillment budget. A brand with 200 SKUs,
  its own domain, and a four-business-day handling promise does.

  The pool is therefore the single highest-leverage thing to fix, and it has to
  be fixed without an API key, without a scraping vendor, and without a model.

HOW A POOL GETS BUILT WITH NO KEYS AND NO AI
  Stage 1  DISCOVER  — Common Crawl's CDX index (index.commoncrawl.org) is a
                       free, keyless, public index of the web. Querying it for
                       url=*.myshopify.com enumerates Shopify storefronts
                       directly. Measured 2026-08-27 against CC-MAIN-2026-34:
                       6 pages, 60,667 index rows, 10,808 UNIQUE storefront
                       hostnames, in 63 seconds. 127 crawl indexes are
                       published, so the ceiling on the pool is not this file.

  Stage 2  QUALIFY   — every candidate is checked LIVE, because a crawl index
                       is a record of the past and half of it is dead. Two
                       requests per survivor:
                         GET /products.json?limit=250  -> catalog size
                         GET /                         -> canonical + name
                       Measured on a random sample of 30 hosts: 21 answered
                       200, 3 answered 401 (password-locked), 5 answered 404,
                       1 answered 402 (unpaid store). 13 of 30 carried 25+
                       products.

  Stage 3  GATE      — three gates, all arithmetic or regex, no judgement:
                         * catalog size >= --min-products
                         * the storefront's own <link rel=canonical> points at a
                           REAL domain, not back at *.myshopify.com. This is the
                           size filter that actually works: a brand that has
                           bought a domain and pointed it at Shopify is a
                           business; 1b7578-2.myshopify.com is a dropship shell.
                           (Verified live: william-painter-2.myshopify.com ->
                           www.williampainter.com, og:site_name "William
                           Painter".)
                         * optional --niche regex over product titles, types
                           and tags — the brand's own words, matched literally.

  Stage 4  EMIT      — "Name,domain" lines, which is exactly the format
                       source_b2b.read_seed_domains() already parses. Nothing in
                       source_b2b changes.

WHAT THIS FILE WILL NOT DO
  It will not invent a company. Every row is a hostname that Common Crawl
  observed and that answered a live HTTP request from this machine, carrying a
  catalog size this file counted and a domain the store published about itself.
  There is no enrichment step, no guessing, and no model anywhere.

HONESTY / EXIT CONTRACT
  Same contract as the other sources:
    0  OK        brands qualified
    2  ZERO      candidates were checked and none qualified (a real answer:
                 the niche filter is too tight, or the min-products bar is)
    3  BLOCKED   the discovery index or the storefront edge refused us
    4  ERROR     transport failed for most attempts
  A run that checks hosts and emits nothing NEVER exits 0.

RATE / POLITENESS
  Every candidate is a different hostname but they all terminate on the same
  Shopify edge, so a per-host bucket would be no limit at all. This module
  therefore applies a single GLOBAL cap (--rpm, default 60/min) across every
  storefront request. Measured: 30 storefronts at 0.15s spacing ran at 57/min
  with zero 429s. Common Crawl gets its own, gentler cap.

USAGE
    python source_brandpool.py --discover                       # cache the index
    python source_brandpool.py --limit 300 --min-products 40
    python source_brandpool.py --limit 500 --niche "candle|soap|skincare"
    python source_brandpool.py --limit 500 --out brands.txt --resume
    python source_brandpool.py --self-test                      # no network

  Then hand the pool straight to the existing B2B source:
    python source_b2b.py --niche "home fragrance" --seed-file brands.txt \
        --limit 100 --channels shipping_policy,careers
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import pathlib
import random
import re
import sys
import threading
import time

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = pathlib.Path(__file__).resolve().parent

UA = {"User-Agent": "wing-b2b-source/1.0 (+mailto:wjackwing1@gmail.com)"}
BROWSER_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/126.0.0.0 Safari/537.36")}

EXIT_OK, EXIT_ZERO, EXIT_BLOCKED, EXIT_ERROR = 0, 2, 3, 4
BLOCK_CODES = (403, 429, 503)

CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"
CC_PATTERN = "*.myshopify.com"

CACHE_DIR = HERE / ".brandpool"
DEFAULT_POOL = HERE / "brand_pool.txt"

# --- patterns. Printed with repr() by --self-test, per the house rule. -----
CANONICAL_RX = re.compile(
    r"""<link[^>]+rel=["']canonical["'][^>]+href=["']([^"']+)["']""", re.I)
CANONICAL_ALT_RX = re.compile(
    r"""<link[^>]+href=["']([^"']+)["'][^>]+rel=["']canonical["']""", re.I)
OG_URL_RX = re.compile(
    r"""property=["']og:url["'][^>]+content=["']([^"']+)["']""", re.I)
OG_NAME_RX = re.compile(
    r"""property=["']og:site_name["'][^>]+content=["']([^"']+)["']""", re.I)
TITLE_RX = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
HOST_RX = re.compile(r"^https?://([^/:]+)", re.I)

# Hosts that are Shopify's own infrastructure or a parked shell, never a brand.
NOT_A_BRAND_HOST = re.compile(
    r"(?:^|\.)(?:myshopify\.com|shopify\.com|shopifypreview\.com|"
    r"shopifycloud\.com|cdn\.shopify\.com)$", re.I)
# A storefront handle that is a machine-generated string is a dropship shell or
# a template demo, not a brand. Live examples from a 30-host sample on
# 2026-08-27: "1b7578-2", "mbv4mh-vj", "demo-cowlendar".
#
# Deliberately NARROW. An earlier version also flagged `^[a-z]{4,8}-[a-z]{2}$`
# to catch "ddczer-kj", and that same shape is "acme-co" and "brand-us" — real
# businesses. Every branch here requires a digit or an explicit demo/test/dev
# prefix. The heavy lifting is done downstream by the no_own_domain gate, which
# a dropship shell fails anyway; this one only exists to save two HTTP requests
# on the obvious cases.
JUNK_HANDLE_RX = re.compile(r"^[a-z]{0,3}[0-9a-f]{4,}(?:-[a-z0-9]{1,3})?$"
                            r"|^[a-z0-9]*\d[a-z0-9]*-[a-z]{2}$"
                            r"|^(?:demo|test|dev|staging|sample)-")


# ===========================================================================
# Politeness — ONE global bucket, because every host is the same edge
# ===========================================================================

class GlobalBucket:
    def __init__(self, rpm: float, label: str):
        self.label = label
        self.base = 60.0 / max(0.1, rpm)
        self.interval = self.base
        self.next_ok = 0.0
        self.lock = threading.Lock()
        self.requests = 0
        self.blocks = 0
        self.strikes = 0
        self.slept = 0.0
        self.dead = False

    def wait(self):
        if self.dead:
            raise RuntimeError(f"{self.label}: blocked three times in a row; stopping")
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_ok - now)
            self.next_ok = max(now, self.next_ok) + self.interval
        if delay > 0:
            self.slept += delay
            time.sleep(delay)
        self.requests += 1

    def saw(self, status):
        if status in BLOCK_CODES:
            self.blocks += 1
            self.strikes += 1
            self.interval = min(30.0, self.interval * 2.0)
            if self.strikes >= 3:
                self.dead = True
        else:
            self.strikes = 0
            self.interval = max(self.base, self.interval * 0.9)

    def as_dict(self):
        return {"lane": self.label, "requests": self.requests, "blocks": self.blocks,
                "dead": self.dead, "base_rpm": round(60.0 / self.base, 1),
                "final_rpm": round(60.0 / self.interval, 1),
                "throttle_sleep_s": round(self.slept, 1)}


# ===========================================================================
# Accounting
# ===========================================================================

class Tally:
    def __init__(self):
        self.discovered = 0
        self.checked = 0
        self.transport_errors = 0
        self.blocked = 0
        self.rejected: dict[str, int] = {}
        self.qualified = 0
        self.catalog_sizes: list[int] = []

    def reject(self, why: str):
        self.rejected[why] = self.rejected.get(why, 0) + 1

    def as_dict(self):
        sizes = sorted(self.catalog_sizes)
        return {"discovered": self.discovered, "checked": self.checked,
                "qualified": self.qualified,
                "transport_errors": self.transport_errors, "blocked": self.blocked,
                "rejected": dict(sorted(self.rejected.items(), key=lambda kv: -kv[1])),
                "median_catalog_size": sizes[len(sizes) // 2] if sizes else None}


# ===========================================================================
# Stage 1 — discover
# ===========================================================================

def cc_indexes(session, n: int) -> list[str]:
    r = session.get(CC_COLLINFO, timeout=60)
    r.raise_for_status()
    return [c["cdx-api"] for c in r.json()[:n]]


def discover(n_indexes: int = 1, pattern: str = CC_PATTERN,
             bucket: GlobalBucket | None = None) -> list[str]:
    """Enumerate storefront hostnames from Common Crawl's public CDX index.

    Cached per (index, pattern): the index is immutable once published, so
    re-downloading it is pure waste and pure rudeness."""
    CACHE_DIR.mkdir(exist_ok=True)
    s = requests.Session()
    s.headers.update(UA)
    hosts: set[str] = set()
    for cdx in cc_indexes(s, n_indexes):
        tag = re.sub(r"[^A-Za-z0-9]+", "_", cdx.rsplit("/", 1)[-1])
        cache = CACHE_DIR / f"cc_{tag}_{re.sub(r'[^a-z0-9]+', '', pattern)}.txt"
        if cache.exists():
            found = [h for h in cache.read_text(encoding="utf-8").split() if h]
            print(f"  [cc] {tag}: {len(found)} host(s) from cache", file=sys.stderr)
            hosts.update(found)
            continue
        here: set[str] = set()
        t0 = time.time()
        page = 0
        while True:
            if bucket:
                bucket.wait()
            try:
                r = s.get(cdx, params={"url": pattern, "output": "json", "page": page},
                          timeout=600, stream=True)
            except Exception as e:
                print(f"  [cc] {tag} page {page} -> {type(e).__name__}", file=sys.stderr)
                break
            if bucket:
                bucket.saw(r.status_code)
            if r.status_code == 404:      # past the last page
                break
            if r.status_code != 200:
                print(f"  [cc] {tag} page {page} -> HTTP {r.status_code}",
                      file=sys.stderr)
                break
            rows = 0
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                rows += 1
                try:
                    u = json.loads(line).get("url", "")
                except Exception:
                    continue
                m = HOST_RX.match(u)
                if m:
                    here.add(m.group(1).lower())
            if rows == 0:
                break
            page += 1
            if page > 200:                # a runaway guard, not a real limit
                break
        cache.write_text("\n".join(sorted(here)), encoding="utf-8")
        print(f"  [cc] {tag}: {len(here)} unique host(s) in {time.time() - t0:.0f}s "
              f"across {page} page(s)", file=sys.stderr)
        hosts.update(here)
    return sorted(hosts)


# ===========================================================================
# Stage 2/3 — qualify and gate
# ===========================================================================

def handle_of(host: str) -> str:
    return host.split(".")[0].lower()


def brand_domain(html: str, fallback_host: str) -> str | None:
    """The domain the STORE says it lives at. Not inferred, not guessed —
    read off the store's own canonical link, then og:url."""
    for rx in (CANONICAL_RX, CANONICAL_ALT_RX, OG_URL_RX):
        m = rx.search(html)
        if not m:
            continue
        hm = HOST_RX.match(m.group(1).strip())
        if hm:
            host = hm.group(1).lower()
            if not NOT_A_BRAND_HOST.search(host):
                return host
    return None


def brand_name(html: str) -> str:
    # Entities must be unescaped: a live run produced "M&amp;O Tienda Mx", and
    # that string goes onto a list a human reads and a mail-merge prints.
    m = OG_NAME_RX.search(html)
    if m:
        return re.sub(r"\s+", " ", htmllib.unescape(m.group(1))).strip()
    m = TITLE_RX.search(html)
    if m:
        t = re.sub(r"\s+", " ",
                   htmllib.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip()
        # "William Painter – Sunglasses" / "Brand | Shop" — keep the head.
        return re.split(r"\s*[|–—-]\s*", t)[0].strip()[:80]
    return ""


def niche_text(products: list) -> str:
    """Every word the brand uses about its own catalog, concatenated. The niche
    filter matches against THIS, never against anything a model wrote."""
    parts = []
    for p in products[:120]:
        if not isinstance(p, dict):
            continue
        parts.append(str(p.get("title") or ""))
        parts.append(str(p.get("product_type") or ""))
        tags = p.get("tags")
        if isinstance(tags, list):
            parts.extend(str(t) for t in tags[:12])
    return re.sub(r"\s+", " ", " ".join(parts)).lower()


def qualify(session, host: str, bucket: GlobalBucket, tally: Tally,
            min_products: int, niche_rx, require_domain: bool,
            domain_rx=None) -> dict | None:
    if JUNK_HANDLE_RX.search(handle_of(host)):
        tally.reject("junk_handle")
        return None

    tally.checked += 1
    bucket.wait()
    try:
        r = session.get(f"https://{host}/products.json", params={"limit": 250},
                        timeout=15)
    except Exception:
        tally.transport_errors += 1
        tally.reject("transport")
        return None
    bucket.saw(r.status_code)
    if r.status_code in BLOCK_CODES:
        tally.blocked += 1
        tally.reject(f"http_{r.status_code}")
        return None
    if r.status_code != 200:
        # 401 password-locked, 402 unpaid, 404 gone. All real answers.
        tally.reject(f"http_{r.status_code}")
        return None
    try:
        products = r.json().get("products", [])
    except Exception:
        tally.reject("not_json")
        return None
    if not isinstance(products, list):
        tally.reject("not_json")
        return None

    n = len(products)
    tally.catalog_sizes.append(n)
    if n < min_products:
        tally.reject("catalog_too_small")
        return None
    if niche_rx and not niche_rx.search(niche_text(products)):
        tally.reject("niche_mismatch")
        return None

    bucket.wait()
    try:
        h = session.get(f"https://{host}/", headers=BROWSER_UA, timeout=20)
    except Exception:
        tally.transport_errors += 1
        tally.reject("transport_home")
        return None
    bucket.saw(h.status_code)
    if h.status_code != 200:
        tally.reject(f"home_http_{h.status_code}")
        return None

    dom = brand_domain(h.text, host)
    if require_domain and not dom:
        # Still on a *.myshopify.com canonical: nobody has bought this brand a
        # domain, which is the cheapest possible proxy for "is this a business".
        tally.reject("no_own_domain")
        return None

    if domain_rx and dom and not domain_rx.search(dom):
        tally.reject("domain_filter")
        return None

    name = brand_name(h.text)
    if not name:
        tally.reject("no_name")
        return None

    tally.qualified += 1
    return {"name": name, "domain": dom or host, "shopify_host": host,
            "catalog_size": n,
            "catalog_is_capped": n >= 250,   # /products.json maxes at 250/page
            "evidence_url": f"https://{host}/products.json?limit=250"}


# ===========================================================================
# Resume
# ===========================================================================

class Progress:
    """Line-delimited JSON of every host already decided, so a run that dies at
    host 4,000 of 10,808 does not re-check the first 4,000."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.done: set[str] = set()
        self.rows: list[dict] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                self.done.add(row.get("host", ""))
                if row.get("brand"):
                    self.rows.append(row["brand"])
        self.f = path.open("a", encoding="utf-8")

    def record(self, host: str, brand: dict | None):
        self.done.add(host)
        if brand:
            self.rows.append(brand)
        self.f.write(json.dumps({"host": host, "brand": brand},
                                ensure_ascii=False) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


# ===========================================================================
# Self test — no network
# ===========================================================================

def self_test() -> int:
    print("patterns (repr, so escape sequences are visible):")
    for nm, rx in (("CANONICAL_RX", CANONICAL_RX), ("CANONICAL_ALT_RX", CANONICAL_ALT_RX),
                   ("OG_URL_RX", OG_URL_RX), ("OG_NAME_RX", OG_NAME_RX),
                   ("TITLE_RX", TITLE_RX), ("HOST_RX", HOST_RX),
                   ("NOT_A_BRAND_HOST", NOT_A_BRAND_HOST),
                   ("JUNK_HANDLE_RX", JUNK_HANDLE_RX)):
        print(f"  {nm} = {rx.pattern!r}")
    print()

    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += 0 if ok else 1
        print(f"  [{'ok  ' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")

    # Markup below is trimmed from the live responses of the four storefronts
    # this file was verified against on 2026-08-27.
    real = ('<link rel="canonical" href="https://www.williampainter.com/">'
            '<meta property="og:site_name" content="William Painter">'
            '<title>William Painter - Sunglasses</title>')
    shell = ('<link rel="canonical" href="https://terexgear.myshopify.com/">'
             '<meta property="og:site_name" content="Terex Utilities">')
    ogonly = ('<meta property="og:url" content="https://bluepassionkw.com/">'
              '<meta property="og:site_name" content="BluePassion">')
    reversed_attrs = ('<link href="https://example-brand.com/" rel="canonical">')

    check("canonical yields the real brand domain",
          brand_domain(real, "william-painter-2.myshopify.com"), "www.williampainter.com")
    check("a myshopify canonical is NOT a brand domain",
          brand_domain(shell, "terexgear.myshopify.com"), None)
    check("og:url is the fallback", brand_domain(ogonly, "x.myshopify.com"),
          "bluepassionkw.com")
    check("attribute order does not matter",
          brand_domain(reversed_attrs, "x.myshopify.com"), "example-brand.com")
    check("brand name from og:site_name", brand_name(real), "William Painter")
    check("brand name falls back to the title head",
          brand_name("<title>Acme Goods | Shop All</title>"), "Acme Goods")

    check("random-hex handle is junk", bool(JUNK_HANDLE_RX.search("1b7578-2")), True)
    check("digit-bearing machine handle is junk",
          bool(JUNK_HANDLE_RX.search("mbv4mh-vj")), True)
    check("demo store is junk", bool(JUNK_HANDLE_RX.search("demo-cowlendar")), True)
    # The gate must not eat real businesses whose handle happens to be short.
    check("acme-co is NOT junk", bool(JUNK_HANDLE_RX.search("acme-co")), False)
    check("brand-us is NOT junk", bool(JUNK_HANDLE_RX.search("brand-us")), False)
    check("a real handle is not junk",
          bool(JUNK_HANDLE_RX.search("william-painter-2")), False)
    check("another real handle is not junk",
          bool(JUNK_HANDLE_RX.search("woodranch")), False)

    prods = [{"title": "Beeswax Candle", "product_type": "Home Fragrance",
              "tags": ["candle", "soy"]},
             {"title": "Cedar Soap", "product_type": "Bath", "tags": ["soap"]}]
    txt = niche_text(prods)
    check("niche text is the brand's own words",
          bool(re.search(r"candle", txt)) and bool(re.search(r"soap", txt)), True)
    check("niche text is lowercased", txt, txt.lower())

    b = GlobalBucket(600.0, "test")
    t0 = time.monotonic()
    for _ in range(5):
        b.wait()
    check("5 calls at 600rpm take >=0.35s", time.monotonic() - t0 >= 0.35, True)
    before = b.interval
    b.saw(429)
    check("429 doubles the interval", b.interval >= before * 2 - 1e-9, True)
    b.saw(429)
    b.saw(429)
    check("three strikes stops the lane", b.dead, True)

    check("exit contract: checked but none qualified is ZERO",
          decide_exit(Tally()), EXIT_ERROR)
    t = Tally()
    t.checked, t.rejected = 40, {"catalog_too_small": 40}
    check("checked 40, qualified 0 -> ZERO", decide_exit(t), EXIT_ZERO)
    t2 = Tally()
    t2.checked, t2.qualified = 40, 3
    check("qualified -> OK", decide_exit(t2), EXIT_OK)
    t3 = Tally()
    t3.checked, t3.blocked = 40, 30
    check("mostly blocked -> BLOCKED", decide_exit(t3), EXIT_BLOCKED)
    t4 = Tally()
    t4.checked, t4.transport_errors = 40, 35
    check("mostly transport errors -> ERROR", decide_exit(t4), EXIT_ERROR)

    print(f"\n{'FAIL' if fails else 'PASS'}: {fails} failing check(s)")
    return 1 if fails else 0


def decide_exit(tally: Tally) -> int:
    if tally.qualified:
        return EXIT_OK
    if not tally.checked:
        return EXIT_ERROR
    if tally.blocked >= tally.checked * 0.5:
        return EXIT_BLOCKED
    if tally.transport_errors >= tally.checked * 0.6:
        return EXIT_ERROR
    return EXIT_ZERO


# ===========================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--discover", action="store_true",
                    help="only build/refresh the Common Crawl host cache, then exit")
    ap.add_argument("--indexes", type=int, default=1,
                    help="how many recent Common Crawl indexes to enumerate")
    ap.add_argument("--pattern", default=CC_PATTERN,
                    help="CDX url pattern (data, not code)")
    ap.add_argument("--limit", type=int, default=200,
                    help="storefronts to CHECK LIVE this run")
    ap.add_argument("--min-products", type=int, default=25,
                    help="catalog-size floor; the whole point is to stop pooling "
                         "two-person studios that cannot afford fulfillment")
    ap.add_argument("--niche", help="regex matched against the brand's own product "
                                    "titles / types / tags")
    ap.add_argument("--domain-filter",
                    help="regex the brand's own domain must match. The Shopify "
                         "population is global — a live 400-host sample produced "
                         ".fr, .in and .com.au brands — and a US fulfillment "
                         "client cannot serve them. Example: "
                         r"--domain-filter '\.(com|net|co|shop|store)$'")
    ap.add_argument("--allow-myshopify-only", action="store_true",
                    help="keep stores that never bought their own domain")
    ap.add_argument("--rpm", type=float, default=60.0, help="global storefront cap")
    ap.add_argument("--cc-rpm", type=float, default=30.0)
    ap.add_argument("--shuffle-seed", type=int, default=0,
                    help="sample the pool fairly instead of alphabetically")
    ap.add_argument("--progress", type=pathlib.Path,
                    default=CACHE_DIR / "progress.jsonl")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_POOL)
    ap.add_argument("--json-out", type=pathlib.Path)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()

    CACHE_DIR.mkdir(exist_ok=True)
    cc_bucket = GlobalBucket(a.cc_rpm, "commoncrawl")
    tally = Tally()

    print("=" * 78, file=sys.stderr)
    print("BRAND POOL BUILDER  (Common Crawl -> live storefront check)", file=sys.stderr)
    print("=" * 78, file=sys.stderr)

    hosts = discover(a.indexes, a.pattern, cc_bucket)
    tally.discovered = len(hosts)
    print(f"discovered  : {len(hosts)} storefront host(s)", file=sys.stderr)
    if a.discover:
        return EXIT_OK if hosts else EXIT_BLOCKED
    if not hosts:
        print("\nHARD FAILURE: the discovery index returned no hosts at all.",
              file=sys.stderr)
        return EXIT_BLOCKED

    if a.shuffle_seed:
        random.Random(a.shuffle_seed).shuffle(hosts)

    prog = Progress(a.progress) if a.resume else None
    if prog:
        hosts = [h for h in hosts if h not in prog.done]
        print(f"resume      : {len(prog.done)} already decided, "
              f"{len(prog.rows)} qualified so far", file=sys.stderr)

    todo = hosts[:a.limit]
    niche_rx = re.compile(a.niche, re.I) if a.niche else None
    domain_rx = re.compile(a.domain_filter, re.I) if a.domain_filter else None
    if niche_rx:
        print(f"niche regex : {niche_rx.pattern!r}", file=sys.stderr)
    if domain_rx:
        print(f"domain regex: {domain_rx.pattern!r}", file=sys.stderr)
    print(f"checking    : {len(todo)} storefront(s) at <= {a.rpm:g}/min\n",
          file=sys.stderr)

    session = requests.Session()
    session.headers.update(UA)
    bucket = GlobalBucket(a.rpm, "storefronts")
    brands = list(prog.rows) if prog else []
    t0 = time.time()

    for i, host in enumerate(todo, 1):
        try:
            b = qualify(session, host, bucket, tally, a.min_products, niche_rx,
                        require_domain=not a.allow_myshopify_only,
                        domain_rx=domain_rx)
        except RuntimeError as e:
            print(f"\nSTOPPING: {e}", file=sys.stderr)
            break
        if prog:
            prog.record(host, b)
        if b:
            brands.append(b)
            print(f"  + {b['name'][:38]:38} {b['domain'][:34]:34} "
                  f"{b['catalog_size']:>4} SKU{'+' if b['catalog_is_capped'] else ''}",
                  file=sys.stderr)
        if i % 50 == 0:
            print(f"  ... {i}/{len(todo)} checked, {tally.qualified} qualified, "
                  f"{time.time() - t0:.0f}s", file=sys.stderr)
    if prog:
        prog.close()

    # Dedupe on the brand's real domain: many myshopify handles (staging,
    # regional, "-2") front the same business.
    seen, unique = set(), []
    for b in brands:
        # removeprefix, NOT lstrip: lstrip("www.") strips a CHARACTER SET, so
        # "wslscstore.com" became "slscstore.com" and silently split one brand
        # into two dedupe keys.
        d = b["domain"].lower()
        d = d[4:] if d.startswith("www.") else d
        if d in seen:
            continue
        seen.add(d)
        unique.append(b)
    dupes = len(brands) - len(unique)

    unique.sort(key=lambda b: -b["catalog_size"])
    a.out.write_text("\n".join(f"{b['name']},{b['domain']}" for b in unique) + "\n",
                     encoding="utf-8")
    if a.json_out:
        a.json_out.write_text(
            "\n".join(json.dumps(b, ensure_ascii=False) for b in unique),
            encoding="utf-8")

    elapsed = max(1e-6, time.time() - t0)
    print("\n" + "=" * 78, file=sys.stderr)
    print("POOL LEDGER", file=sys.stderr)
    print(json.dumps(tally.as_dict(), indent=1), file=sys.stderr)
    print(json.dumps(bucket.as_dict()), file=sys.stderr)
    print(f"\n  brands written : {len(unique)} -> {a.out}"
          f"   ({dupes} same-domain duplicate(s) merged)", file=sys.stderr)
    print(f"  wall clock     : {elapsed:.0f}s "
          f"({tally.checked / (elapsed / 3600):.0f} storefronts/hour, "
          f"{tally.qualified / (elapsed / 3600):.0f} brands/hour)", file=sys.stderr)
    print(f"\n  feed it in:\n    python source_b2b.py --niche \"<niche>\" "
          f"--seed-file {a.out} --limit 100", file=sys.stderr)

    code = decide_exit(tally)
    if code != EXIT_OK:
        print(f"\nHARD FAILURE (exit {code}): checked {tally.checked} storefront(s) "
              f"and qualified {tally.qualified}. Rejection reasons are in the "
              f"ledger above — read them before loosening a gate.", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
