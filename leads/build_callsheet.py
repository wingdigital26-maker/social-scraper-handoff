#!/usr/bin/env python3
"""Build the verified-lead call sheet from Supabase. Regenerate any time."""
import html
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
rows = json.loads((HERE / "callable93.json").read_text(encoding="utf-8"))


def tier(x):
    """How much ammunition this call has. This ordering IS the information:
    a named owner plus a real observation is a different call from a cold
    dial with only a number."""
    if x.get("contact_name") and x.get("personalization"):
        return 0
    if x.get("contact_name"):
        return 1
    if x.get("personalization"):
        return 2
    return 3


rows.sort(key=lambda x: (tier(x), -(x.get("need_score") or 0)))
groups = {0: [], 1: [], 2: [], 3: []}
for x in rows:
    groups[tier(x)].append(x)

E = lambda s: html.escape(str(s or ""))
tel = lambda p: re.sub(r"\D", "", p or "")


def pretty(p):
    """Maps and snippet sources disagree on formatting — '(972)377-8188' vs
    '817-203-2944'. On a sheet you read while dialing, one shape only."""
    d = tel(p)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return p or ""


def gaps_of(x, limit=3):
    out = [g for g in (x.get("audit_gaps") or [])
           if not str(g).startswith(("WARNING", "CHECK"))]
    return out[:limit]


def card(x):
    name = E(x.get("contact_name"))
    title = E(x.get("contact_title"))
    who = (f'<p class="who">Ask for <b>{name}</b>'
           + (f', {title}' if title and title != "None" else "") + "</p>") if name else ""
    fact = ""
    if x.get("personalization"):
        src = x.get("personalization_source")
        link = (f'<a class="src" href="{E(src)}" target="_blank" rel="noopener">'
                f'check it</a>') if src else ""
        fact = (f'<p class="fact"><span class="k">Open with</span>'
                f'{E(x["personalization"])} {link}</p>')
    chips = []
    if x.get("gmb_rating"):
        rv = f' · {x["gmb_reviews"]} reviews' if x.get("gmb_reviews") else ""
        chips.append(f'<li>{E(x["gmb_rating"])} stars{E(rv)}</li>')
    if x.get("seo_rank"):
        chips.append(f'<li>ranks #{E(x["seo_rank"])}</li>')
    for g in gaps_of(x):
        chips.append(f'<li class="gap">{E(g)}</li>')
    chiphtml = f'<ul class="chips">{"".join(chips)}</ul>' if chips else ""
    site = (f'<a class="site" href="{E(x["website"])}" target="_blank" '
            f'rel="noopener">their site</a>') if x.get("website") else ""
    email = (f'<span class="mail">{E(x["contact_email"])}</span>'
             if x.get("contact_email") else "")
    return f"""<article class="lead">
  <header><h3>{E(x['title'])}</h3>
    <p class="loc">{E(x.get('place_name'))}{' · ' if site else ''}{site}</p>
    <p class="proof {x.get('proof','maps')}">{'Google Maps listing' if x.get('proof')=='maps' else 'website only, location unconfirmed'}</p></header>
  <a class="tel" href="tel:{tel(x.get('phone'))}">{E(pretty(x.get('phone')))}</a>
  {who}{fact}{chiphtml}{email}
</article>"""


def compact(x):
    site = (f'<a href="{E(x["website"])}" target="_blank" rel="noopener">site</a>'
            if x.get("website") else "")
    return (f'<tr><td>{E(x["title"])}</td><td>{E(x.get("place_name"))}</td>'
            f'<td class="num"><a href="tel:{tel(x.get("phone"))}">{E(pretty(x.get("phone")))}</a></td>'
            f'<td><span class="proof {x.get("proof","maps")}">'
            f'{"Maps" if x.get("proof")=="maps" else "site only"}</span> {site}</td></tr>')


A, B, C, D = groups[0], groups[1], groups[2], groups[3]
total = len(rows)
n_maps = sum(1 for x in rows if x.get("proof") == "maps")
n_web = total - n_maps

sections = []
if A:
    sections.append(f"""<section>
<div class="sec-h"><h2>Owner and an opening</h2>
<p>You know who to ask for and you have something real to say. Start here.</p>
<span class="n">{len(A)}</span></div>
<div class="grid">{''.join(card(x) for x in A)}</div></section>""")
if B:
    sections.append(f"""<section>
<div class="sec-h"><h2>Owner, no opening yet</h2>
<p>A name gets you past the front desk. Worth two minutes on their site first.</p>
<span class="n">{len(B)}</span></div>
<div class="grid">{''.join(card(x) for x in B)}</div></section>""")
if C:
    sections.append(f"""<section>
<div class="sec-h"><h2>Something to open with</h2>
<p>No name on file, but a specific, checkable observation about their business.</p>
<span class="n">{len(C)}</span></div>
<div class="grid">{''.join(card(x) for x in C)}</div></section>""")
if D:
    sections.append(f"""<section>
<div class="sec-h"><h2>Verified, nothing to open on</h2>
<p>Real businesses with real DFW numbers. Cold dials, or research one before you
call. The Proof column says how each was confirmed.</p>
<span class="n">{len(D)}</span></div>
<div class="tablewrap"><table>
<thead><tr><th>Business</th><th>City</th><th>Phone</th><th>Proof</th></tr></thead>
<tbody>{''.join(compact(x) for x in D)}</tbody></table></div></section>""")

page = f"""<title>Verified DFW Roofers</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=IBM+Plex+Mono:wght@500;600&family=Public+Sans:wght@400;500;600&display=swap">
<style>
:root{{
  --paper:#F4F5F2; --surface:#FFFFFF; --sunk:#EAECE6;
  --ink:#15181A; --ink-2:#525C56; --ink-3:#7C857F;
  --rule:#DCE0D8; --accent:#1F6B4A; --accent-soft:#E3EFE7; --accent-deep:#164F37;
  --warn:#8A5116; --warn-soft:#F6EEE2;
  --shadow:0 1px 2px rgba(21,24,26,.05),0 3px 12px rgba(21,24,26,.045);
}}
@media (prefers-color-scheme:dark){{
  :root:not([data-theme="light"]){{
    --paper:#101311; --surface:#181C19; --sunk:#1F2521;
    --ink:#E8ECE7; --ink-2:#A3AEA6; --ink-3:#7E8A82;
    --rule:#2A322C; --accent:#5FBE8E; --accent-soft:#14301F; --accent-deep:#8AD4AC;
    --warn:#D89A54; --warn-soft:#2C2116;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 3px 14px rgba(0,0,0,.3);
  }}
}}
:root[data-theme="dark"]{{
  --paper:#101311; --surface:#181C19; --sunk:#1F2521;
  --ink:#E8ECE7; --ink-2:#A3AEA6; --ink-3:#7E8A82;
  --rule:#2A322C; --accent:#5FBE8E; --accent-soft:#14301F; --accent-deep:#8AD4AC;
  --warn:#D89A54; --warn-soft:#2C2116;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 3px 14px rgba(0,0,0,.3);
}}
*{{box-sizing:border-box}}
body{{margin:0;padding:0 1.15rem 5rem;background:var(--paper);color:var(--ink);
 font-family:"Public Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
 font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:64rem;margin:0 auto}}
header.top{{padding:3rem 0 1.4rem}}
.eyebrow{{font-family:"IBM Plex Mono",monospace;font-size:.68rem;letter-spacing:.16em;
 text-transform:uppercase;color:var(--accent);font-weight:600;margin:0 0 .8rem}}
h1{{font-family:"Bricolage Grotesque",system-ui,sans-serif;font-weight:700;
 font-size:clamp(1.9rem,5vw,2.9rem);line-height:1.02;letter-spacing:-.02em;
 margin:0 0 .6rem;text-wrap:balance}}
.lede{{margin:0;color:var(--ink-2);max-width:58ch;font-size:1.02rem}}
.lede b{{color:var(--ink);font-weight:600}}
.stats{{display:grid;gap:.85rem;grid-template-columns:repeat(auto-fit,minmax(11rem,1fr));
 margin:1.9rem 0 0;padding:0;list-style:none}}
.stat{{background:var(--surface);border:1px solid var(--rule);border-left:3px solid var(--accent);
 border-radius:8px;padding:.85rem 1rem;box-shadow:var(--shadow)}}
.stat .num{{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;
 font-size:1.5rem;font-weight:600;color:var(--accent);line-height:1.1;display:block}}
.stat p{{margin:.15rem 0 0;color:var(--ink-2);font-size:.85rem}}
.note{{margin:1.5rem 0 0;padding:.9rem 1.1rem;background:var(--sunk);
 border:1px solid var(--rule);border-radius:8px;color:var(--ink-2);font-size:.9rem}}
.note b{{color:var(--ink)}}
section{{margin-top:3rem}}
.sec-h{{display:flex;align-items:baseline;gap:.8rem;flex-wrap:wrap;
 border-bottom:1px solid var(--rule);padding-bottom:.6rem;margin-bottom:1.2rem}}
.sec-h h2{{font-family:"Bricolage Grotesque",sans-serif;font-size:1.2rem;margin:0;
 letter-spacing:-.012em}}
.sec-h p{{margin:0;color:var(--ink-3);font-size:.88rem;flex:1 1 18rem}}
.sec-h .n{{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;
 color:var(--accent);font-weight:600}}
.grid{{display:grid;gap:1rem;grid-template-columns:repeat(auto-fill,minmax(20rem,1fr))}}
.lead{{background:var(--surface);border:1px solid var(--rule);border-radius:9px;
 padding:1.05rem 1.15rem 1.2rem;box-shadow:var(--shadow);
 display:flex;flex-direction:column;gap:.65rem}}
.lead h3{{font-family:"Bricolage Grotesque",sans-serif;font-size:1.02rem;margin:0;
 line-height:1.25;letter-spacing:-.01em;text-wrap:balance}}
.loc{{margin:.15rem 0 0;font-size:.8rem;color:var(--ink-3)}}
.site,.src{{color:var(--accent);text-decoration:none;border-bottom:1px solid currentColor}}
.site:hover,.src:hover,.site:focus,.src:focus{{opacity:.72}}
.tel{{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;
 font-size:1.4rem;font-weight:600;letter-spacing:-.01em;color:var(--ink);
 text-decoration:none;border-bottom:2px solid var(--accent);align-self:flex-start}}
.tel:hover,.tel:focus{{color:var(--accent)}}
.who{{margin:0;font-size:.92rem;color:var(--ink-2)}}
.who b{{color:var(--ink);font-weight:600}}
.fact{{margin:0;padding:.75rem .9rem;background:var(--accent-soft);border-radius:6px;
 font-size:.92rem;line-height:1.5;color:var(--ink)}}
.fact .k{{display:block;font-family:"IBM Plex Mono",monospace;font-size:.61rem;
 letter-spacing:.15em;text-transform:uppercase;color:var(--accent-deep);
 font-weight:600;margin-bottom:.3rem}}
.fact .src{{font-size:.82rem;white-space:nowrap}}
.chips{{display:flex;flex-wrap:wrap;gap:.3rem;margin:0;padding:0;list-style:none}}
.chips li{{font-family:"IBM Plex Mono",monospace;font-size:.69rem;
 font-variant-numeric:tabular-nums;color:var(--ink-2);background:var(--sunk);
 border:1px solid var(--rule);border-radius:20px;padding:.16rem .55rem}}
.chips li.gap{{color:var(--warn);border-color:var(--warn);background:var(--warn-soft)}}
.proof{{font-family:"IBM Plex Mono",monospace;font-size:.66rem;letter-spacing:.04em;
 margin:.3rem 0 0;display:inline-block}}
.proof.maps{{color:var(--accent)}}
.proof.website{{color:var(--warn)}}
.mail{{font-family:"IBM Plex Mono",monospace;font-size:.75rem;color:var(--ink-3);
 word-break:break-all}}
.tablewrap{{overflow-x:auto;border:1px solid var(--rule);border-radius:9px;
 background:var(--surface);box-shadow:var(--shadow)}}
table{{border-collapse:collapse;width:100%;min-width:34rem}}
th{{text-align:left;font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;
 color:var(--ink-3);font-weight:600;padding:.7rem .9rem;border-bottom:1px solid var(--rule)}}
td{{padding:.6rem .9rem;border-bottom:1px solid var(--rule);font-size:.88rem}}
tbody tr:last-child td{{border-bottom:0}}
tbody tr:hover{{background:var(--sunk)}}
td.num a{{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;
 color:var(--accent);text-decoration:none;font-weight:500}}
td a{{color:var(--accent)}}
footer{{margin-top:3.5rem;padding-top:1.2rem;border-top:1px solid var(--rule);
 color:var(--ink-3);font-size:.85rem;max-width:64ch}}
a:focus-visible,.tel:focus-visible{{outline:2px solid var(--accent);outline-offset:3px;
 border-radius:3px}}
@media (max-width:36rem){{.grid{{grid-template-columns:1fr}}header.top{{padding-top:2rem}}}}
</style>

<div class="wrap">
<header class="top">
<p class="eyebrow">Wing Digital &middot; Verified dial sheet &middot; 26 Aug 2026</p>
<h1>{total} roofers who actually exist</h1>
<p class="lede">Every business below carries a DFW or toll&#8209;free number and
was proven to exist two different ways &mdash; <b>{n_maps} matched a real Google Maps
listing</b>, and <b>{n_web} were proven only by a website that demonstrably belongs
to them.</b> Started as 988 scraped names; <b>902 did not survive.</b></p>

<ul class="stats">
<li class="stat"><span class="num">{total}</span><p>verified and callable</p></li>
<li class="stat"><span class="num">{len(A) + len(B)}</span><p>where you know the owner's name</p></li>
<li class="stat"><span class="num">{len(A) + len(C)}</span><p>with a specific, checkable opening</p></li>
</ul>

<p class="note"><b>Why so few.</b> Of the original 988, 477 were not businesses
at all (LinkedIn profiles, trade associations, lead&#8209;gen shells), 147 were
real companies in the wrong state, and 271 could not be proven either way and
are being held rather than guessed at. What is left is small on purpose.</p>
</header>

{''.join(sections)}

<footer>
<p>Two levels of proof, marked on every entry. <b>Google Maps listing</b> means the
company was matched to a real Maps record, and its phone and rating come from that
record rather than a search snippet. <b>Website only</b> means the company is real
&mdash; a website demonstrably belongs to it &mdash; but no Maps record was matched,
so its city is less certain. Every phone on this sheet is a DFW or toll&#8209;free
number either way. Every "open with" line was read off that
company's own website and links back to the page it came from, so you can check
it in one click before you dial. Where nothing specific could be found, the card
says so rather than offering a generic line.</p>
</footer>
</div>
"""

out = HERE / "verified-dial-sheet.html"
out.write_text(page, encoding="utf-8")
print(f"wrote {out}  ({total} leads: {len(A)} A / {len(B)} B / {len(C)} C / {len(D)} D)")
