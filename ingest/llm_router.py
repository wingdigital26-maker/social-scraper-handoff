#!/usr/bin/env python3
"""llm_router.py — Wing Digital's FREE-model worker layer.

Claude (in Claude Code) is the brain: it decides WHAT to generate, dispatches
bulk generation to FREE models through this router, then QAs the output.
Nothing generated here is ever sent to a client without passing back through
Claude's review.

Architecture (the cheap lane):
    free worker model  ->  writes / drafts / enriches (volume)
    Claude             ->  checks / verifies / rewrites (the verdict)

Every provider below is FREE and OpenAI-compatible (same request shape).
Kimi + Hermes (paid OpenRouter) have been retired as defaults. OpenRouter is
kept only as an optional manual fallback via `--model or/<id>`.

PROVIDERS (all free, source: awesome-freellm-apis):
    groq      no credit card, 30 RPM   -> fast general + JSON
    cerebras  no credit card, fast     -> fast reasoning
    deepseek  registration             -> long-form SEO drafts
    gemini    no credit card, 1M ctx   -> long context / research gather

WORKER ALIASES (what skills call — provider is chosen for you):
    seo     -> deepseek-chat            long-form SEO + landing pages
    voice   -> groq llama-3.3-70b       persona-voice cold-email / social copy
    json    -> groq llama-3.1-8b        enrichment JSON (cheap + fast)
    fast    -> cerebras llama-3.3-70b   fastest general drafting
    research-> gemini-2.0-flash         gather + summarize sources (Claude verifies)

Auth: per-provider key in C:/Users/wjack/ghl-cli/.env (only set the ones you use):
    GROQ_API_KEY        https://console.groq.com/keys      (free, no card, ~1 min)
    CEREBRAS_API_KEY    https://cloud.cerebras.ai/          (free, no card)
    DEEPSEEK_API_KEY    https://platform.deepseek.com/api_keys
    GEMINI_API_KEY      https://aistudio.google.com/app/apikey  (free, no card)
    OPENROUTER_API_KEY  optional fallback only

Usage:
    python llm_router.py --model seo --system-file <path.md> --prompt "..."
    python llm_router.py --model json --prompt "..." --json
    python llm_router.py --model voice --batch in.jsonl out.jsonl
    python llm_router.py --list            (show aliases + which keys are set)

Persona/voice system prompts live in the vault: wiki/personas/*.md and
brand_briefs/*-voice-profile.md — pass the voice profile as --system-file so
worker copy matches the same voice as blogs, replies, and social.
"""
import argparse, json, os, sys, time
from pathlib import Path

# Model output routinely contains characters cp1252 cannot encode (smart
# quotes, em dashes, emoji). On Windows, print() then raises
# UnicodeEncodeError and a perfectly good response is destroyed — the
# caller sees a traceback instead of text. Cost the intel lane a whole
# video before it was spotted.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests

HERE = Path(__file__).parent

# ---- Free, OpenAI-compatible providers -------------------------------------
# base = chat/completions endpoint; key_env = .env var holding that provider's key
PROVIDERS = {
    "groq": {
        "base": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "get_key": "https://console.groq.com/keys",
    },
    "cerebras": {
        "base": "https://api.cerebras.ai/v1/chat/completions",
        "key_env": "CEREBRAS_API_KEY",
        "get_key": "https://cloud.cerebras.ai/",
    },
    "deepseek": {
        "base": "https://api.deepseek.com/v1/chat/completions",
        "key_env": "DEEPSEEK_API_KEY",
        "get_key": "https://platform.deepseek.com/api_keys",
    },
    "github": {  # GitHub Models — free, OpenAI-compatible, serves DeepSeek et al.
        "base": "https://models.github.ai/inference/chat/completions",
        "key_env": "GITHUB_MODELS_TOKEN",  # a GitHub PAT with the Models permission
        "get_key": "https://github.com/marketplace/models",
    },
    "gemini": {  # Google's OpenAI-compatible endpoint
        "base": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "key_env": "GEMINI_API_KEY",
        "get_key": "https://aistudio.google.com/app/apikey",
    },
    "openrouter": {  # optional manual fallback only
        "base": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "get_key": "https://openrouter.ai/keys",
    },
}

# ---- Worker aliases: what skills call. (provider, model_id) -----------------
MODELS = {
    # DeepSeek direct is primary for long-form (cheap, reliable). If DEEPSEEK_API_KEY
    # is missing/rate-limited the chain falls back to free Gemini then Groq.
    # (GitHub Models was retired Aug 2026 — do not route through it.)
    # Model IDs verified live against each provider's /models endpoint on
    # 2026-08-25. The previous IDs (llama-3.3-70b-versatile, gemini-2.0-flash)
    # had been decommissioned by the providers and returned 404 on every call
    # since 2026-08-18, which silently killed the whole content pipeline: the
    # router returned nothing, so every draft failed blog_lint, so the queue
    # never refilled. Prefer self-updating aliases where a provider offers one,
    # so a future decommission degrades instead of breaking.
    # deepseek/cerebras have NO key configured, so nothing routes to them.
    "seo":      ("groq", "openai/gpt-oss-120b"),
    "voice":    ("groq", "openai/gpt-oss-120b"),
    "json":     ("groq", "openai/gpt-oss-20b"),
    "fast":     ("groq", "openai/gpt-oss-20b"),
    "research": ("gemini", "gemini-flash-latest"),
}

# Ordered fallback so a task still completes if the first provider's key is
# missing or its free tier is rate-limited. Same task class, next-best free home.
FALLBACKS = {
    "seo":      [("gemini", "gemini-flash-latest"), ("groq", "qwen/qwen3.6-27b")],
    "voice":    [("gemini", "gemini-flash-latest"), ("groq", "openai/gpt-oss-20b")],
    "json":     [("groq", "openai/gpt-oss-120b"), ("gemini", "gemini-flash-latest")],
    "fast":     [("groq", "openai/gpt-oss-120b"), ("gemini", "gemini-flash-latest")],
    "research": [("groq", "openai/gpt-oss-120b"), ("gemini", "gemini-2.5-flash")],
}


def load_env():
    env = {}
    p = HERE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()


def _gh_cli_token():
    """Fall back to the authenticated GitHub CLI token so GitHub Models works off
    the existing `gh` connection with no key pasted into .env. Cached per run."""
    global _GH_TOKEN_CACHE
    try:
        return _GH_TOKEN_CACHE
    except NameError:
        pass
    import subprocess
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        _GH_TOKEN_CACHE = out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        _GH_TOKEN_CACHE = ""
    return _GH_TOKEN_CACHE


def provider_key(provider):
    info = PROVIDERS[provider]
    key = ENV.get(info["key_env"]) or os.environ.get(info["key_env"], "")
    if not key and provider == "github":
        key = _gh_cli_token()   # use the existing `gh` login if no PAT is set
    return key


def resolve(model):
    """Return a list of (provider, model_id) to try, in order.

    - alias (seo/voice/json/fast/research) -> primary + its fallbacks
    - 'or/<id>'                            -> OpenRouter fallback, that id
    - '<provider>:<id>'                    -> that exact provider + id
    - bare model id                        -> assume groq
    """
    if model in MODELS:
        return [MODELS[model]] + FALLBACKS.get(model, [])
    if model.startswith("or/"):
        return [("openrouter", model[3:])]
    if ":" in model:
        prov, mid = model.split(":", 1)
        if prov in PROVIDERS:
            return [(prov, mid)]
    return [("groq", model)]


def _call(provider, model_id, messages, temperature, max_tokens, force_json):
    key = provider_key(provider)
    if not key:
        return {"error": f"no key for {provider} (set {PROVIDERS[provider]['key_env']} "
                         f"in .env — free at {PROVIDERS[provider]['get_key']})",
                "skip": True}
    body = {"model": model_id, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens}
    if force_json:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {key}",
               "HTTP-Referer": "https://wingdigital.local",
               "X-Title": "Wing Digital OS"}
    r = requests.post(PROVIDERS[provider]["base"], headers=headers, json=body, timeout=180)
    if r.status_code == 429:
        return {"error": "rate-limited (429)", "retry": True}
    r.raise_for_status()
    data = r.json()
    choice = data["choices"][0]["message"]["content"]
    return {"output": choice, "model": f"{provider}:{data.get('model', model_id)}",
            "usage": data.get("usage", {})}


def generate(model, prompt, system=None, temperature=0.7, max_tokens=2048,
             force_json=False, retries=2):
    """One completion across the alias's provider chain.

    Tries the primary provider, then fallbacks. Within a provider, retries on
    429/transient errors. Returns {"output", "model", "usage"} or {"error"}.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_err = None
    for provider, model_id in resolve(model):
        for attempt in range(retries):
            try:
                res = _call(provider, model_id, messages, temperature, max_tokens, force_json)
                if res.get("skip"):
                    last_err = res["error"]
                    break  # no key — move to next provider, don't retry
                if res.get("retry"):
                    last_err = res["error"]
                    time.sleep(5 * (attempt + 1))
                    continue
                if "error" in res:
                    last_err = res["error"]
                    time.sleep(2 * (attempt + 1))
                    continue
                return res
            except Exception as e:
                last_err = str(e)[:300]
                time.sleep(2 * (attempt + 1))
    return {"error": f"all providers failed: {last_err}", "model": model}


def run_batch(model, in_path, out_path, system=None, temperature=0.7,
              max_tokens=2048, force_json=False):
    """Durable batch: one JSONL row in -> one row out, appended immediately."""
    in_p, out_p = Path(in_path), Path(out_path)
    done_ids = set()
    if out_p.exists():
        for line in out_p.read_text(encoding="utf-8").splitlines():
            try:
                done_ids.add(json.loads(line).get("id"))
            except Exception:
                pass
    rows = [json.loads(l) for l in in_p.read_text(encoding="utf-8").splitlines() if l.strip()]
    todo = [r for r in rows if r.get("id") not in done_ids]
    print(f"[router] {len(rows)} rows, {len(done_ids)} already done, {len(todo)} to run "
          f"on alias '{model}'", flush=True)
    ok = fail = 0
    with out_p.open("a", encoding="utf-8") as out:
        for i, row in enumerate(todo, 1):
            res = generate(model, row["prompt"], system=row.get("system", system),
                           temperature=temperature, max_tokens=max_tokens,
                           force_json=force_json)
            out.write(json.dumps({**row, **res}, ensure_ascii=False) + "\n")
            out.flush()
            if "error" in res:
                fail += 1
                print(f"[router] {i}/{len(todo)} id={row.get('id')} FAILED: {res['error'][:80]}", flush=True)
            else:
                ok += 1
                if i % 10 == 0 or i == len(todo):
                    print(f"[router] {i}/{len(todo)} done ({fail} failed)", flush=True)
            time.sleep(0.5)
    print(f"[router] BATCH COMPLETE: {ok} ok, {fail} failed -> {out_p}", flush=True)


def list_aliases():
    print("Worker aliases (free lane):")
    for alias, (prov, mid) in MODELS.items():
        has = "OK" if provider_key(prov) else "no key"
        print(f"  {alias:9s} -> {prov}:{mid:28s} [{has}]")
    print("\nProviders / keys:")
    for prov, info in PROVIDERS.items():
        has = "OK" if provider_key(prov) else f"MISSING {info['key_env']}"
        print(f"  {prov:11s} {has:24s} {info['get_key']}")
    print("\nActivate the free lane: set at least GROQ_API_KEY in ghl-cli/.env "
          "(free, no card, ~1 min at https://console.groq.com/keys).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="seo",
                    help="alias: seo|voice|json|fast|research | provider:id | or/<id>")
    ap.add_argument("--prompt", help="single prompt text")
    ap.add_argument("--prompt-file", help="read prompt from file")
    ap.add_argument("--system", help="system prompt text")
    ap.add_argument("--system-file", help="read system prompt from file (e.g. a voice profile)")
    ap.add_argument("--batch", nargs=2, metavar=("IN_JSONL", "OUT_JSONL"))
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--json", action="store_true", help="force JSON object output")
    ap.add_argument("--list", action="store_true", help="show aliases + which keys are set")
    a = ap.parse_args()

    if a.list:
        list_aliases()
        return

    system = a.system
    if a.system_file:
        system = Path(a.system_file).read_text(encoding="utf-8")
    if a.batch:
        run_batch(a.model, a.batch[0], a.batch[1], system=system,
                  temperature=a.temperature, max_tokens=a.max_tokens, force_json=a.json)
        return
    prompt = a.prompt
    if a.prompt_file:
        prompt = Path(a.prompt_file).read_text(encoding="utf-8")
    if not prompt:
        sys.exit("Need --prompt, --prompt-file, or --batch (or --list)")
    res = generate(a.model, prompt, system=system, temperature=a.temperature,
                   max_tokens=a.max_tokens, force_json=a.json)
    if "error" in res:
        sys.exit(f"ERROR: {res['error']}")
    print(res["output"])
    u = res.get("usage", {})
    print(f"\n[router] model={res['model']} tokens in/out: "
          f"{u.get('prompt_tokens','?')}/{u.get('completion_tokens','?')}", file=sys.stderr)


if __name__ == "__main__":
    main()
