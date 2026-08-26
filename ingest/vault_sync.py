"""Mirror the Sonar lead engine into Jack's Obsidian vault, one folder per client.

Scraped data lives in Supabase, which is invisible day to day. This writes a
per-client dashboard into the vault so every company Wing tracks has a page
showing what its scraper hunts and everything it has found.

Vault rules obeyed: nothing is ever written under raw/, and no keys or
credentials are written into the vault (it syncs to OneDrive). Prospect
contact info is business data and is fine.

    set ENV_FILE=C:/Users/wjack/ghl-cli/.env
    python vault_sync.py --dry-run
    python vault_sync.py --client jackson-roofing
    python vault_sync.py
"""
import argparse
import datetime
import pathlib
import re
import sys

from audit_prospect import sb_request
from db import load_env

# Windows consoles default to cp1252 and business names routinely contain
# symbols and emoji it cannot encode; without this a single print kills the run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VAULT = pathlib.Path(r"C:\Users\wjack\OneDrive\Documentos\Obsidian 2.0\Jacks Ai Brain 2.0")
CLIENTS_DIR = VAULT / "wiki" / "clients"
RAW_DIR = VAULT / "raw"          # HARD CONSTRAINT: never written to, no exceptions
INDEX_MD = VAULT / "wiki" / "index.md"
LOG_MD = VAULT / "wiki" / "log.md"
MAX_PROSPECTS = 300  # a 988-row table would make an unreadable note

# A client slug becomes a directory name. It arrives from the Supabase
# crm_clients table, so it is untrusted input as far as this script is
# concerned: a slug of "../../raw/leaked" resolved straight into the vault's
# raw/ directory, which is the one place that must never be written.
SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$", re.I)


def safe_client_dir(slug):
    """Resolve a client folder, or refuse. Two independent checks: the slug must
    look like a slug, and the resolved path must still sit inside CLIENTS_DIR."""
    if not isinstance(slug, str) or not SAFE_SLUG.match(slug) or slug in (".", ".."):
        sys.exit(f"REFUSING unsafe client slug {slug!r} - it would not stay inside {CLIENTS_DIR}")
    d = (CLIENTS_DIR / slug).resolve()
    root = CLIENTS_DIR.resolve()
    if d != root and root not in d.parents:
        sys.exit(f"REFUSING client slug {slug!r} - resolves outside the clients folder: {d}")
    if d == RAW_DIR.resolve() or RAW_DIR.resolve() in d.parents:
        sys.exit(f"REFUSING client slug {slug!r} - resolves under the vault raw/ folder: {d}")
    return d


def assert_no_secrets(text, env, where):
    """Last line of defence before anything is written to a OneDrive-synced
    folder. A vault page may name an env var, never hold its value."""
    for var in ("SUPABASE_SERVICE_KEY", "SUPABASE_URL", "SUPABASE_ANON_KEY"):
        val = env.get(var)
        if val and len(str(val)) > 8 and str(val) in text:
            sys.exit(f"REFUSING to write {where}: it contains the value of {var}. "
                     f"The vault syncs to OneDrive - pages may name an env var, never hold it.")
    if re.search(r"eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.", text):
        sys.exit(f"REFUSING to write {where}: it contains a JWT-shaped string.")


def fetch(env, table, query):
    r = sb_request("GET", f"{env['SUPABASE_URL']}/rest/v1/{table}?{query}", headers={
        "apikey": env["SUPABASE_SERVICE_KEY"],
        "Authorization": f"Bearer {env['SUPABASE_SERVICE_KEY']}",
    })
    if r is None or r.status_code != 200:
        sys.exit(f"Supabase read failed for {table}: {getattr(r, 'status_code', 'no response')}")
    return r.json()


def cell(v):
    """Make a value safe inside a markdown table cell."""
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v)).replace("|", "\\|").strip()


def clip(v, n):
    s = cell(v)
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def split_list(s):
    return [p.strip() for p in (s or "").split(",") if p.strip()]


def link(url, label="link"):
    return f"[{label}]({url})" if url else ""


# ---------------------------------------------------------------- rendering ---
def render_readme(client, msgs, prospects, prospect_basis, stamp):
    slug, name = client["slug"], client["name"]
    counts = {s: sum(1 for m in msgs if (m.get("status") or "") == s) for s in ("draft", "approved", "sent")}
    cities = split_list(client.get("scrape_cities"))
    terms = split_list(client.get("scrape_terms"))
    channels = split_list(client.get("channels"))
    last = max((m.get("created_at") or "" for m in msgs), default="")

    L = [
        f"# {name}",
        "",
        f"> Sonar client folder. Auto-written by `ingest/vault_sync.py` \u2014 edits here are overwritten.",
        "",
        "| | |",
        "|---|---|",
        f"| Slug | `{slug}` |",
        f"| Status | {'active' if client.get('active') else 'inactive'} |",
        f"| Channels | {', '.join(channels) or '\u2014'} |",
        f"| Niche hunted | {cell(client.get('scrape_niche')) or '\u2014'} |",
        f"| Cities | {', '.join(cities) or '\u2014'} |",
        f"| Search terms | {', '.join(terms) or '\u2014'} |",
        f"| Last sync | {stamp} |",
        f"| Last message found | {cell(last)[:19] or '\u2014'} |",
        "",
        "## Pipeline",
        "",
        "| Stage | Count |",
        "|---|---|",
        f"| Drafts | {counts['draft']} |",
        f"| Approved | {counts['approved']} |",
        f"| Sent | {counts['sent']} |",
        f"| **Total messages** | **{len(msgs)}** |",
        f"| Prospects tracked | {len(prospects)} |",
        "",
        "## Files",
        "",
        f"- [[{slug}/outbound|Outbound messages]] \u2014 every message drafted or sent for {name}",
        f"- [[{slug}/prospects|Prospects]] \u2014 {prospect_basis}",
        f"- [[_sonar-index|Sonar index]] \u2014 all clients",
        "",
    ]
    if channels:
        L += ["## Channel breakdown", "", "| Channel | Messages |", "|---|---|"]
        for ch in sorted({(m.get("channel") or "unknown") for m in msgs}):
            L.append(f"| {cell(ch)} | {sum(1 for m in msgs if m.get('channel') == ch)} |")
        L.append("")
    return "\n".join(L)


def render_outbound(client, msgs, stamp):
    L = [f"# {client['name']} \u2014 outbound", "",
         f"Auto-written by `ingest/vault_sync.py`. Last sync: {stamp}. Newest first.", ""]
    if not msgs:
        L += ["_No outbound messages recorded for this client yet._", ""]
        return "\n".join(L)

    by_channel = {}
    for m in msgs:
        by_channel.setdefault(m.get("channel") or "unknown", []).append(m)

    for ch in sorted(by_channel):
        rows = sorted(by_channel[ch], key=lambda m: m.get("created_at") or "", reverse=True)
        L += [f"## {ch} ({len(rows)})", "",
              "| Date | Recipient | Subject | Personalization | Status | Tier | Evidence |",
              "|---|---|---|---|---|---|---|"]
        for m in rows:
            recip = cell(m.get("recipient"))
            if m.get("recipient_url"):
                recip = f"[{recip or 'recipient'}]({m['recipient_url']})"
            L.append("| {} | {} | {} | {} | {} | {} | {} |".format(
                cell(m.get("created_at"))[:10],
                recip,
                clip(m.get("subject"), 70),
                clip(m.get("personalization"), 110),
                cell(m.get("status")),
                cell(m.get("tier")),
                link(m.get("evidence_url"), "evidence"),
            ))
        L.append("")
    return "\n".join(L)


def render_prospects(client, prospects, basis, matched, stamp):
    L = [f"# {client['name']} \u2014 prospects", "",
         f"Auto-written by `ingest/vault_sync.py`. Last sync: {stamp}.", "",
         f"**Source:** {basis}", ""]
    if not prospects:
        L += ["_Nothing to list yet._", ""]
        return "\n".join(L)

    if matched:
        if len(prospects) > MAX_PROSPECTS:
            L += [f"_Showing the top {MAX_PROSPECTS} of {len(prospects)} by need score._", ""]
            prospects = prospects[:MAX_PROSPECTS]
        L += ["| Business | City | Need | Rating | Reviews | SEO rank | Gaps | Phone | Website | Source |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for c in prospects:
            gaps = c.get("audit_gaps") or []
            if isinstance(gaps, str):
                gaps = [gaps]
            L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                clip(c.get("title") or c.get("place_name"), 50),
                cell(c.get("place_name")),
                cell(c.get("need_score")),
                cell(c.get("gmb_rating")),
                cell(c.get("gmb_reviews")),
                cell(c.get("seo_rank")),
                clip("; ".join(str(g) for g in gaps), 80),
                cell(c.get("phone")),
                link(c.get("website"), "site"),
                link(c.get("url"), cell(c.get("source")) or "src"),
            ))
    else:
        L += ["| Recipient | Channel | Status | Link |", "|---|---|---|---|"]
        for m in prospects:
            L.append("| {} | {} | {} | {} |".format(
                clip(m.get("recipient"), 60), cell(m.get("channel")),
                cell(m.get("status")), link(m.get("recipient_url"), "profile")))
    L.append("")
    return "\n".join(L)


def render_index(entries, stamp):
    L = ["# Sonar index", "",
         f"Every client the Sonar lead engine tracks. Auto-written by `ingest/vault_sync.py`. "
         f"Last sync: {stamp}.", "",
         "| Client | Niche | Channels | Drafts | Approved | Sent | Prospects |",
         "|---|---|---|---|---|---|---|"]
    for e in sorted(entries, key=lambda x: x["name"].lower()):
        L.append("| [[{}/README\\|{}]] | {} | {} | {} | {} | {} | {} |".format(
            e["slug"], cell(e["name"]), cell(e["niche"]), cell(e["channels"]),
            e["draft"], e["approved"], e["sent"], e["prospects"]))
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------- main ---
def match_prospects(client, candidates):
    """Real mapping only: candidates.category == the client's scrape_niche,
    narrowed to the client's scrape_cities when it has any. Returns
    (rows, matched?) - matched False means no honest mapping exists."""
    niche = (client.get("scrape_niche") or "").strip().lower()
    if not niche:
        return [], False
    rows = [c for c in candidates if (c.get("category") or "").strip().lower() == niche]
    if not rows:
        return [], False
    cities = {c.lower() for c in split_list(client.get("scrape_cities"))}
    if cities:
        narrowed = [c for c in rows if (c.get("place_name") or "").strip().lower() in cities]
        if narrowed:
            rows = narrowed
    rows.sort(key=lambda c: (c.get("need_score") if c.get("need_score") is not None else -1), reverse=True)
    return rows, True


def write(path, text, dry, env=None):
    # Belt-and-braces: even a caller that skipped safe_client_dir cannot land a
    # write under raw/.
    resolved = pathlib.Path(path).resolve()
    raw = RAW_DIR.resolve()
    if resolved == raw or raw in resolved.parents:
        sys.exit(f"REFUSING to write under the vault raw/ folder: {resolved}")
    if env is not None:
        assert_no_secrets(text, env, resolved)
    if dry:
        print(f"    would write {path}  ({len(text)} chars)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")  # full overwrite = idempotent
    print(f"    wrote {path}")


def append_log(entry, dry):
    """Vault rule: wiki/log.md is updated on every operation. Append only."""
    line = f"## [{datetime.date.today().isoformat()}] build | {entry}"
    if dry:
        print(f"    would append to {LOG_MD}: {line}")
        return
    if not LOG_MD.exists():
        print(f"    WARNING: {LOG_MD} missing - not creating it, log entry not recorded")
        return
    prev = LOG_MD.read_text(encoding="utf-8")
    LOG_MD.write_text(prev.rstrip("\n") + "\n" + line + "\n", encoding="utf-8")
    print(f"    appended 1 entry to {LOG_MD}")


def update_index(n_clients, dry):
    """Vault rule: wiki/index.md is updated on every operation.

    Narrowly refreshes the client count on the existing per-client-vault-sync
    row. If the anchor is not found this says so rather than guessing where the
    row belongs — index.md is hand-curated and must not be rewritten blindly.
    """
    if not INDEX_MD.exists():
        print(f"    WARNING: {INDEX_MD} missing - index not updated")
        return
    text = INDEX_MD.read_text(encoding="utf-8")
    rx = re.compile(r"(per-client-vault-sync.*?)(\d+) clients? synced")
    print(f"    index regex: {rx.pattern!r}")
    m = rx.search(text)
    if not m:
        print(f"    WARNING: no 'N clients synced' anchor on the per-client-vault-sync row "
              f"in {INDEX_MD}. Not guessing - index row left as-is.")
        return
    if int(m.group(2)) == n_clients:
        print(f"    {INDEX_MD.name} already reads {n_clients} clients synced - no change")
        return
    new = rx.sub(lambda mm: f"{mm.group(1)}{n_clients} clients synced", text, count=1)
    if dry:
        print(f"    would update {INDEX_MD}: {m.group(2)} -> {n_clients} clients synced")
        return
    INDEX_MD.write_text(new, encoding="utf-8")
    print(f"    updated {INDEX_MD}: {m.group(2)} -> {n_clients} clients synced")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print what would be written, write nothing")
    ap.add_argument("--client", help="one client slug (default: every active client)")
    args = ap.parse_args()

    env = load_env()
    if not env.get("SUPABASE_URL") or not env.get("SUPABASE_SERVICE_KEY"):
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY (set ENV_FILE=C:/Users/wjack/ghl-cli/.env)")

    if not VAULT.exists():
        sys.exit(f"Vault not found: {VAULT}")

    clients = fetch(env, "crm_clients", "select=*&active=eq.true")
    if args.client:
        clients = [c for c in clients if c["slug"] == args.client]
        if not clients:
            sys.exit(f"No active client with slug '{args.client}'")

    outbound = fetch(env, "outbound", "select=*&order=created_at.desc")
    candidates = fetch(env, "candidates", "select=*")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"Sonar vault sync{' (DRY RUN)' if args.dry_run else ''} \u2014 {len(clients)} active client(s), "
          f"{len(outbound)} outbound rows, {len(candidates)} candidates")

    entries = []
    for client in clients:
        slug, name = client["slug"], client["name"]
        # outbound.client holds the display name; tolerate a slug there too
        keys = {name.strip().lower(), slug.strip().lower()}
        msgs = [m for m in outbound if (m.get("client") or "").strip().lower() in keys]
        prospects, matched = match_prospects(client, candidates)
        if matched:
            basis = (f"{len(prospects)} scraped candidates matching niche "
                     f"`{client.get('scrape_niche')}`" +
                     (f" in {client.get('scrape_cities')}" if client.get("scrape_cities") else ""))
        else:
            prospects = msgs
            basis = ("No scraped candidates map to this client (the candidates table holds no "
                     f"`{client.get('scrape_niche')}` rows), so this lists the outbound recipients instead.")

        counts = {s: sum(1 for m in msgs if (m.get("status") or "") == s) for s in ("draft", "approved", "sent")}
        print(f"  {slug}: {len(msgs)} messages "
              f"(draft {counts['draft']} / approved {counts['approved']} / sent {counts['sent']}), "
              f"{len(prospects)} prospects [{'candidates' if matched else 'outbound fallback'}]")

        d = safe_client_dir(slug)
        write(d / "README.md", render_readme(client, msgs, prospects, basis, stamp), args.dry_run, env)
        write(d / "outbound.md", render_outbound(client, msgs, stamp), args.dry_run, env)
        write(d / "prospects.md", render_prospects(client, prospects, basis, matched, stamp), args.dry_run, env)

        entries.append({"slug": slug, "name": name, "niche": client.get("scrape_niche"),
                        "channels": client.get("channels"), "prospects": len(prospects), **counts})

    if args.client:
        print("  (single-client run: leaving _sonar-index.md alone)")
    else:
        write(CLIENTS_DIR / "_sonar-index.md", render_index(entries, stamp), args.dry_run, env)

    # Vault rule: index.md and log.md are updated on every operation.
    total_msgs = sum(e["draft"] + e["approved"] + e["sent"] for e in entries)
    total_pros = sum(e["prospects"] for e in entries)
    if not args.client:
        update_index(len(entries), args.dry_run)
    else:
        print("  (single-client run: index.md client count left alone)")
    append_log(f"vault_sync: mirrored {len(entries)} Sonar client(s) into wiki/clients "
               f"({total_msgs} outbound messages, {total_pros} prospects listed). "
               f"Slug path guard + secret scan active; nothing written under raw/.",
               args.dry_run)
    print("done" + (" (nothing written)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
