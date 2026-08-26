#!/usr/bin/env python3
"""intel_propose.py — turn an AI-creator video transcript into reviewable proposals.

Pipeline position:
    intel_watch.py      -> intel_items   (what the creators published)
    intel_transcript.py -> intel_items.transcript
    intel_propose.py    -> intel_proposals   <-- THIS FILE
    a human            -> approves / rejects (nothing here touches Wing's systems)

What it does: reads each transcribed intel_items row, asks a FREE worker model
(via ghl-cli/llm_router.py — never a paid API, never Claude) whether the video
contains anything genuinely applicable to what Wing Digital actually runs, and
writes 0-3 rows into intel_proposals with status 'proposed'.

ZERO PROPOSALS IS THE NORMAL OUTCOME. Most videos are hype, tool demos for
stacks Wing does not use, or generic advice. Inventing a proposal so the run
"produces something" is the worst possible failure here, so the prompt says so
and the code below actively throws proposals away.

THE HARD RULE — every proposal must be grounded in the transcript:
    evidence_quote must be a VERBATIM span of the transcript.
Models paraphrase constantly. verify_quote() re-locates the model's quote in the
real transcript (whitespace/punctuation-insensitive) and stores the ACTUAL
transcript substring. If it cannot be located, the proposal is DROPPED — a
fabricated quote is never written.

Safety: this script only ever INSERTs rows with status 'proposed'. It does not
edit, deploy, configure, or run anything in Wing's systems.

Usage:
    python intel_propose.py --dry-run            # analyse, print, write nothing
    python intel_propose.py --limit 5            # analyse + write up to 5 items
    python intel_propose.py --item 3 --dry-run   # one specific intel_items.id
    python intel_propose.py --self-test          # quote-verification unit tests
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from db import load_env                      # noqa: E402  ENV_FILE-aware .env loader
from audit_prospect import sb_request        # noqa: E402  retrying Supabase caller

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROUTER = pathlib.Path(os.environ.get("LLM_ROUTER_PATH",
                                     "C:/Users/wjack/ghl-cli/llm_router.py"))

# Direct-REST fallback for the cloud. The GitHub Actions runner only checks out
# social-scraper-handoff, so ghl-cli/llm_router.py does NOT exist there and the
# subprocess call would fail. When the router is missing we speak the same free
# providers ourselves, with the SAME model ids the repaired router uses.
#
# NEVER put llama-3.3-70b-versatile or gemini-2.0-flash here. Both were
# decommissioned by their providers and 404 on every call — that is precisely
# what silently killed Wing's content pipeline for 8 days. Verified live
# 2026-08-25. No paid API is reachable from this file by design.
DIRECT_CHAINS = {
    # groq first: gemini's OpenAI-compatible endpoint was returning intermittent
    # 404s on 2026-08-25, and groq's 120b is both the stronger reasoner here and
    # the one with headroom for a full transcript.
    "research": [("groq", "openai/gpt-oss-120b"), ("gemini", "gemini-flash-latest")],
    "seo":      [("groq", "openai/gpt-oss-120b"), ("gemini", "gemini-flash-latest")],
    "voice":    [("groq", "openai/gpt-oss-120b"), ("gemini", "gemini-flash-latest")],
    "json":     [("groq", "openai/gpt-oss-20b"), ("groq", "openai/gpt-oss-120b")],
    "fast":     [("groq", "openai/gpt-oss-20b"), ("gemini", "gemini-flash-latest")],
}
DIRECT_PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/"
               "chat/completions", "GEMINI_API_KEY"),
}

# --------------------------------------------------------------------------
# EDIT ME. This is the only description the model gets of what Wing runs, and
# it is the difference between "you should use AI agents!" and a proposal that
# names a real system. Keep it concrete, keep it current, keep it short enough
# that a small free model can hold it.
# --------------------------------------------------------------------------
WING_CONTEXT = """
Wing Digital is a one-person digital agency (Jack Wing). These are the ONLY
systems that exist. A proposal that does not touch one of these is worthless.

1. OS DASHBOARD  (target_system: "os-dashboard")
   Next.js app deployed on Vercel. Internal CRM, invoicing, agent run
   monitoring, and a vault knowledge base rendered from markdown.
   Reads a private git repo for vault content. Supabase behind it.

2. SONAR LEAD ENGINE  (target_system: "sonar")
   Python. Scrapes search indexes (Nextdoor, Reddit) for people asking for
   local services, scores each hit for relevance, and drafts outreach using
   per-client voice templates. Runs as scheduled jobs.

3. CONTENT PIPELINE  (target_system: "content-pipeline")
   Python. Drafts client blog posts with free models, runs brand-safety and
   quality gates, publishes to WordPress. Weekly cadence per client.

4. FREE MODEL ROUTER  (target_system: "llm-router")
   ghl-cli/llm_router.py. Single entry point that routes drafting work to free
   providers (Groq, Gemini) with alias + fallback chains. No paid API calls.

5. SUPABASE  (target_system: "supabase")   Postgres storage for all of the above.

6. GITHUB ACTIONS  (target_system: "scheduling")  Cron for every unattended job.

Wing does NOT have: paying API budget, a team, Kubernetes, a mobile app,
enterprise customers, a data warehouse, or anything running on AWS/GCP.
"""

SYSTEM_PROMPT = """You are a skeptical engineering reviewer for a one-person \
software agency. You watch AI-tooling videos and almost always conclude they \
contain nothing worth changing. Your reputation depends on NOT recommending \
things. You never paraphrase: when you quote, you copy characters exactly. \
You reply with a single JSON object and nothing else."""

USER_TEMPLATE = """{wing}

Below is the transcript of a video by {handle}, titled "{title}".

--- TRANSCRIPT START ---
{transcript}
--- TRANSCRIPT END ---

TASK: decide whether this transcript contains a SPECIFIC, CONCRETE technique,
tool, or practice that would measurably improve one of the six numbered Wing
systems above.

Return ZERO proposals if the video is any of:
 - hype, reaction, or "this changes everything" with no mechanism described
 - a demo of a product Wing does not and would not use
 - generic advice ("write better prompts", "use AI", "automate your business")
 - about a stack Wing does not run
 - something Wing plainly already does
ZERO IS THE EXPECTED ANSWER for most videos. Returning an empty list is a
CORRECT, GOOD answer and is preferred over a weak proposal. Never invent a
proposal to fill space. At most 3, and only if each is genuinely strong.

For each proposal:
 - "title": short imperative, e.g. "Add a self-audit pass to blog drafting"
 - "rationale": 2-3 sentences. Name the exact Wing system and what changes.
 - "evidence_quote": COPY-PASTE a span of 15-40 consecutive words from the
   transcript above, character for character, that states the technique. Do NOT
   summarise, reword, fix grammar, or stitch together separate sentences. If you
   cannot find a literal span that supports the proposal, DELETE the proposal.
 - "evidence_ts": timestamp near that span if the transcript shows one, else ""
 - "target_system": exactly one of: os-dashboard, sonar, content-pipeline,
   llm-router, supabase, scheduling
 - "target_paths": likely files/dirs to touch, comma-separated, or ""
 - "effort": "small" (<1h), "medium" (a few hours), or "large" (a day+)
 - "risk": one honest sentence on what breaks if this is applied badly.

Reply with exactly this JSON shape:
{{"proposals": []}}
or
{{"proposals": [{{"title": "...", "rationale": "...", "evidence_quote": "...",
"evidence_ts": "", "target_system": "...", "target_paths": "...",
"effort": "...", "risk": "..."}}]}}
"""

VALID_SYSTEMS = {"os-dashboard", "sonar", "content-pipeline", "llm-router",
                 "supabase", "scheduling"}
VALID_EFFORT = {"small", "medium", "large"}
MIN_QUOTE_CHARS = 40      # below this a "quote" can match by accident
# Free-tier context is the binding constraint. A 17k-char transcript analysed
# fine; a 24k one 404'd/failed across every free provider and the whole item was
# skipped. Truncating is strictly better than skipping: the technique in these
# videos is nearly always described in the first stretch, and the quote check
# runs against the FULL transcript either way, so truncation can only cost us a
# proposal, never produce an ungrounded one.
MAX_TRANSCRIPT_CHARS = 15000


# ----------------------------------------------------------- quote grounding --
def _normalize(text):
    """Lowercase, drop everything but letters/digits/spaces, collapse runs.

    Returns (normalized_string, index_map) where index_map[i] is the offset in
    the ORIGINAL text of normalized character i. That map is what lets us hand
    back the real transcript substring instead of the model's version of it.
    """
    out, idx = [], []
    prev_space = True
    for i, ch in enumerate(text):
        if ch.isalnum():
            out.append(ch.lower())
            idx.append(i)
            prev_space = False
        elif not prev_space:
            out.append(" ")
            idx.append(i)
            prev_space = True
    while out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def verify_quote(quote, transcript):
    """Return the VERBATIM transcript span matching `quote`, or None.

    None means the model paraphrased (or hallucinated) and the caller MUST drop
    the proposal. Matching ignores whitespace, casing, and punctuation only —
    it does not tolerate changed, added, or removed words.
    """
    if not quote or not transcript:
        return None
    n_quote, _ = _normalize(quote)
    if len(n_quote.replace(" ", "")) < MIN_QUOTE_CHARS:
        return None
    n_tr, idx_map = _normalize(transcript)
    pos = n_tr.find(n_quote)
    if pos < 0:
        return None
    start = idx_map[pos]
    end = idx_map[pos + len(n_quote) - 1] + 1
    return transcript[start:end].strip()


TS_RE = re.compile(r"[\[(]?(\d{1,2}:\d{2}(?::\d{2})?)[\])]?")


def timestamp_before(transcript, span):
    """Nearest timestamp marker at or before the quote, if the transcript has any."""
    at = transcript.find(span)
    if at < 0:
        return ""
    last = ""
    for m in TS_RE.finditer(transcript, 0, at + len(span)):
        last = m.group(1)
    return last


# ------------------------------------------------------------------- router --
def _direct_key(env, name):
    return (os.environ.get(name) or env.get(name) or "").strip()


def llm_available(env):
    """(ok, how). False means: analyse nothing and write nothing."""
    if ROUTER.exists():
        return True, f"router subprocess ({ROUTER})"
    have = [p for p, (_, kn) in DIRECT_PROVIDERS.items() if _direct_key(env, kn)]
    if have:
        return True, f"direct free-provider REST ({'+'.join(have)}); router not present"
    return False, ("no router at %s and no GROQ_API_KEY / GEMINI_API_KEY" % ROUTER)


def call_direct(env, model, prompt, system, max_tokens):
    """Free-provider REST, used when llm_router.py is not on this machine."""
    import requests
    last = "no provider tried"
    for provider, model_id in DIRECT_CHAINS.get(model, DIRECT_CHAINS["research"]):
        url, key_name = DIRECT_PROVIDERS[provider]
        key = _direct_key(env, key_name)
        if not key:
            last = f"no {key_name}"
            continue
        try:
            r = requests.post(url, timeout=180,
                              headers={"Authorization": f"Bearer {key}"},
                              json={"model": model_id,
                                    "messages": [{"role": "system", "content": system},
                                                 {"role": "user", "content": prompt}],
                                    "temperature": 0.2, "max_tokens": max_tokens})
            if r.status_code >= 300:
                last = f"{provider}:{model_id} HTTP {r.status_code} {r.text[:120]}"
                continue
            return r.json()["choices"][0]["message"]["content"], None
        except Exception as e:
            last = f"{provider}:{model_id} {str(e)[:120]}"
    return None, f"all free providers failed: {last}"


def call_router(model, prompt, system, max_tokens=6000, env=None):
    """Jack's FREE model router as a subprocess, or free-provider REST in the
    cloud where the router file does not exist. Never a paid API, never Claude."""
    if not ROUTER.exists():
        return call_direct(env or {}, model, prompt, system, max_tokens)
    cmd = [sys.executable, str(ROUTER), "--model", model,
           "--prompt", prompt, "--system", system,
           "--temperature", "0.2", "--max-tokens", str(max_tokens)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300, cwd=str(ROUTER.parent))
    except subprocess.TimeoutExpired:
        return None, "router timed out"
    if r.returncode != 0:
        return None, (r.stdout or r.stderr or "router failed").strip()[:300]
    return r.stdout, None


def parse_proposals(raw):
    """Pull the proposals list out of router stdout.

    Returns (list, error). error is not None when NO parseable JSON object was
    found — which must NOT be reported as "zero proposals". gpt-oss-120b emits a
    long <think> block before its answer; with too small a max_tokens the reply
    got cut off mid-reasoning and the old code read that as a confident zero.
    Silent zeros are indistinguishable from a broken pipeline, which is exactly
    how Wing's content lane died quietly for 8 days. Never again: no JSON is an
    ERROR, an explicit empty list is a real answer.
    """
    if not raw or not raw.strip():
        return [], "empty model response"
    text = raw
    # drop reasoning blocks (and an unterminated one from a truncated reply)
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.S | re.I)
    if re.search(r"<think>", text, re.I):
        text = re.sub(r"<think>.*$", " ", text, flags=re.S | re.I)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    # last balanced {...} in the text — the answer, not a brace inside prose
    obj = None
    for start in [m.start() for m in re.finditer(r"\{", text)][::-1]:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        cand = json.loads(text[start:i + 1])
                    except Exception:
                        cand = None
                    if isinstance(cand, dict) and "proposals" in cand:
                        obj = cand
                    break
        if obj is not None:
            break
    if obj is None:
        return [], f"no parseable JSON in model reply ({len(raw)} chars)"
    props = obj.get("proposals")
    if not isinstance(props, list):
        return [], "'proposals' was not a list"
    return props, None


# ------------------------------------------------------------------ analysis --
REQUOTE_NUDGE = """
Your previous answer was REJECTED. Every evidence_quote you gave was a
paraphrase — it does not appear in the transcript character for character, so
the proposal could not be verified and was thrown away. Quotes that were
rejected:
{bad}

Answer again. For each proposal you still believe in, scroll the transcript,
find the sentence that actually says it, and COPY THAT SENTENCE EXACTLY —
same words, same order, same wording, including any transcription errors or
awkward phrasing. Do not clean it up. If no literal sentence in the transcript
supports the proposal, drop that proposal and return fewer (or zero).
"""


def analyse(item, model, verbose=True, env=None):
    """Return (kept_proposals, dropped_reasons) for one intel_items row."""
    transcript = (item.get("transcript") or "").strip()
    prompt = USER_TEMPLATE.format(
        wing=WING_CONTEXT.strip(),
        handle=item.get("source_handle") or "unknown",
        title=item.get("title") or "(untitled)",
        transcript=transcript[:MAX_TRANSCRIPT_CHARS],
    )
    if verbose and len(transcript) > MAX_TRANSCRIPT_CHARS:
        print(f"    (transcript {len(transcript)} chars — analysing first "
              f"{MAX_TRANSCRIPT_CHARS}; quote check still runs on the full text)")
    raw, err = call_router(model, prompt, SYSTEM_PROMPT, env=env)
    if err:
        return None, [f"router error: {err}"]

    props, perr = parse_proposals(raw)
    if perr:
        # Do NOT let an unparseable reply masquerade as "nothing applicable".
        return None, [f"unusable model reply: {perr}"]

    kept, dropped = _score(props, item, transcript)

    # If the ONLY reason we kept nothing is that every quote was a paraphrase,
    # give the model exactly one chance to go back and copy the real sentence.
    # The idea may well be sound while the quoting was lazy. The re-ask changes
    # nothing about verification — the second answer is checked just as hard,
    # and if it paraphrases again the proposals stay dropped.
    if not kept and dropped and all("not found verbatim" in d for d in dropped):
        bad = "\n".join(f"  - {d.split('model said: ', 1)[-1]}" for d in dropped)
        raw2, err2 = call_router(model, prompt + REQUOTE_NUDGE.format(bad=bad),
                                 SYSTEM_PROMPT, env=env)
        if not err2:
            props2, perr2 = parse_proposals(raw2)
            if not perr2:
                kept2, dropped2 = _score(props2, item, transcript)
                if verbose:
                    print(f"    [re-asked for verbatim quotes] "
                          f"{len(kept2)} verified, {len(dropped2)} still ungrounded")
                kept = kept2
                dropped = dropped + dropped2

    if verbose and dropped:
        for d in dropped:
            print(f"    [dropped] {d}")
    return kept, dropped


def _score(props, item, transcript):
    """Validate + ground each raw model proposal. Returns (kept, dropped)."""
    kept, dropped = [], []
    for p in (props or [])[:3]:
        if not isinstance(p, dict):
            continue
        title = (p.get("title") or "").strip()
        if not title:
            dropped.append("missing title")
            continue

        span = verify_quote(p.get("evidence_quote", ""), transcript)
        if span is None:
            # THE important branch: the model did not quote the transcript.
            dropped.append(
                f"{title!r}: evidence_quote not found verbatim in transcript "
                f"-> DROPPED (model said: {str(p.get('evidence_quote'))[:110]!r})")
            continue

        system = (p.get("target_system") or "").strip().lower()
        if system not in VALID_SYSTEMS:
            dropped.append(f"{title!r}: target_system {system!r} is not a Wing system")
            continue
        effort = (p.get("effort") or "").strip().lower()
        if effort not in VALID_EFFORT:
            effort = "medium"
        risk = (p.get("risk") or "").strip()
        if not risk:
            dropped.append(f"{title!r}: no risk stated")
            continue

        paths = p.get("target_paths") or ""
        if isinstance(paths, list):
            paths = ", ".join(str(x) for x in paths)

        kept.append({
            "intel_item_id": item["id"],
            "source_handle": item.get("source_handle"),
            "video_title": item.get("title"),
            "video_url": item.get("url"),
            "title": title[:200],
            "rationale": (p.get("rationale") or "").strip(),
            "evidence_quote": span,                       # verbatim, from the transcript
            "evidence_ts": (p.get("evidence_ts") or "").strip()
                           or timestamp_before(transcript, span),
            "target_system": system,
            "target_paths": str(paths)[:500],
            "effort": effort,
            "risk": risk,
            "status": "proposed",
        })
    return kept, dropped


# ------------------------------------------------------------------ supabase --
def fetch_items(env, auth, limit, item_id):
    base = env["SUPABASE_URL"].rstrip("/")
    q = ("select=id,source_handle,title,url,transcript,transcript_status"
         "&transcript=not.is.null&order=id.asc")
    if item_id:
        q = "select=id,source_handle,title,url,transcript,transcript_status&id=eq.%d" % item_id
    else:
        q += f"&limit={max(limit * 4, limit)}"
    r = sb_request("GET", f"{base}/rest/v1/intel_items?{q}", headers=auth)
    if r is None or r.status_code >= 300:
        print(f"  fetch failed: {r.status_code if r else 'no response'} "
              f"{r.text[:200] if r is not None else ''}")
        return []
    return [x for x in r.json() if (x.get("transcript") or "").strip()]


def already_proposed(env, auth, ids):
    """intel_item_ids that already have proposals — never re-analyse those."""
    if not ids:
        return set()
    base = env["SUPABASE_URL"].rstrip("/")
    lst = ",".join(str(i) for i in ids)
    r = sb_request("GET", f"{base}/rest/v1/intel_proposals"
                          f"?select=intel_item_id&intel_item_id=in.({lst})", headers=auth)
    if r is None or r.status_code >= 300:
        return set()
    return {x["intel_item_id"] for x in r.json()}


def insert_proposals(env, auth, rows):
    base = env["SUPABASE_URL"].rstrip("/")
    h = {**auth, "Content-Type": "application/json",
         "Prefer": "return=representation,resolution=ignore-duplicates"}
    r = sb_request("POST",
                   f"{base}/rest/v1/intel_proposals?on_conflict=intel_item_id,title",
                   headers=h, json=rows)
    if r is None or r.status_code >= 300:
        print(f"  INSERT FAILED: {r.status_code if r else 'no response'} "
              f"{r.text[:300] if r is not None else ''}")
        return []
    return r.json()


# ----------------------------------------------------------------- self-test --
def self_test():
    tr = ("So the trick here is you run the draft through a second pass where the "
          "model grades its own output against the checklist before you ever ship it.")
    cases = [
        ("verbatim span",
         "you run the draft through a second pass where the model grades its own "
         "output against the checklist", True),
        ("case/punctuation/whitespace differences",
         "You run the DRAFT through a second pass, where the model grades\n its own "
         "output against the checklist.", True),
        ("PARAPHRASE (one word swapped)",
         "you run the draft through a second stage where the model grades its own "
         "output against the checklist", False),
        ("PARAPHRASE (reworded, same meaning)",
         "have the model review its own draft against a checklist before shipping", False),
        ("FABRICATION (not in transcript at all)",
         "you should always cache your prompts to save money on tokens every run", False),
        ("too short to be evidence", "the trick here", False),
    ]
    ok = True
    for name, quote, should_pass in cases:
        got = verify_quote(quote, tr)
        passed = (got is not None)
        mark = "PASS" if passed == should_pass else "FAIL"
        if passed != should_pass:
            ok = False
        print(f"  [{mark}] {name}: {'accepted' if passed else 'rejected'}")
        if got:
            print(f"         stored verbatim span -> {got!r}")
    print("\nself-test:", "all good" if ok else "FAILURES")
    return 0 if ok else 1


# ---------------------------------------------------------------------- main --
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=5, help="max items to analyse")
    ap.add_argument("--item", type=int, help="one intel_items.id")
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing")
    # 'seo' maps to groq openai/gpt-oss-120b — the strongest free model with a
    # context big enough for a transcript, and the one that stayed up while
    # gemini (the 'research' primary) was 404ing. Judgement task, not SEO; the
    # alias name is just the router's routing key.
    ap.add_argument("--model", default="seo",
                    help="llm_router alias: seo|research|json|fast|voice")
    ap.add_argument("--self-test", action="store_true",
                    help="run quote-verification unit tests and exit")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(self_test())

    os.environ.setdefault("ENV_FILE", "C:/Users/wjack/ghl-cli/.env")
    env = load_env()
    if not env.get("SUPABASE_URL") or not env.get("SUPABASE_SERVICE_KEY"):
        sys.exit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY")
    key = env["SUPABASE_SERVICE_KEY"]
    auth = {"apikey": key, "Authorization": f"Bearer {key}"}

    ok, how = llm_available(env)
    if not ok:
        print(f"No free model available: {how}.\n"
              "Exiting without writing anything — a proposal is never written "
              "without real model analysis.")
        return
    print(f"LLM lane: {how}")

    items = fetch_items(env, auth, a.limit, a.item)
    if not items:
        print("No transcribed intel_items to analyse "
              "(intel_transcript.py fills intel_items.transcript).")
        return
    done = already_proposed(env, auth, [i["id"] for i in items])
    todo = [i for i in items if i["id"] not in done][:a.limit]
    print(f"{len(items)} transcribed, {len(done)} already proposed on, "
          f"{len(todo)} to analyse  (model={a.model}, "
          f"{'DRY RUN' if a.dry_run else 'WRITING'})\n")

    total_kept = total_dropped = 0
    for item in todo:
        print(f"[{item['id']}] {item.get('title')}")
        kept, dropped = analyse(item, a.model, env=env)
        if kept is None:
            print(f"    SKIPPED: {dropped[0]}\n")
            continue
        total_dropped += len(dropped)
        if not kept:
            print("    0 proposals — nothing in this video applies to Wing.\n")
            continue
        total_kept += len(kept)
        for p in kept:
            print(f"    + {p['title']}   [{p['target_system']} / {p['effort']}]")
            print(f"      rationale: {p['rationale']}")
            print(f"      evidence : \"{p['evidence_quote']}\""
                  + (f"  @{p['evidence_ts']}" if p["evidence_ts"] else ""))
            print(f"      paths    : {p['target_paths'] or '(none)'}")
            print(f"      risk     : {p['risk']}")
        if not a.dry_run:
            wrote = insert_proposals(env, auth, kept)
            print(f"    -> wrote {len(wrote)} row(s) to intel_proposals "
                  f"(status 'proposed', nothing applied)")
        print()

    print(f"DONE: {total_kept} proposal(s) kept, {total_dropped} dropped "
          f"(ungrounded/invalid) across {len(todo)} video(s).")
    if not a.dry_run and total_kept:
        print("All rows are status 'proposed'. A human approves before anything changes.")


if __name__ == "__main__":
    main()
