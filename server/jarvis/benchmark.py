"""Measure the configured providers against each other.

    docker compose exec jarvis python -m jarvis.benchmark

Free-tier terms, model names and rate limits change constantly, and any ranking
written down in a document is stale within months. So rather than asserting which
provider is best, this measures the ones *you* have configured, on requests
shaped like the ones this assistant actually sends.

Three things are tested, because they are the three that decide whether a model
is usable here:

  latency        how long a normal turn takes
  tool calling   whether it can pick a tool and format the call correctly, which
                 is the single most common way a cheap model fails in this app
  large request  whether it survives a full-size turn (~29 tool schemas), which
                 is what the per-minute token caps actually bite on

A model that answers beautifully but fumbles tool calls is useless here. That is
why correctness is reported separately from speed rather than blended into a
single score.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import httpx

from .config import get_settings
from .llm.client import error_payload, normalise_model_id
from .llm.pool import build_pool, model_setting, provider_prefix

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)

# Stands in for the real system prompt: same shape, same instruction to prefer
# tools over guessing, without dragging the whole persona into a benchmark.
SYSTEM_MESSAGE = {
    "role": "system",
    "content": (
        "You are a personal assistant with access to tools. Use them rather than "
        "guessing. Call the appropriate tool when one fits the request."
    ),
}

# A tool the model must choose and fill in correctly. Deliberately unambiguous:
# we are testing formatting ability, not judgement.
TEST_TOOL = {
    "type": "function",
    "function": {
        "name": "get_stock_quote",
        "description": "Get the current market price for a stock ticker symbol.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker, e.g. AAPL"},
            },
            "required": ["symbol"],
        },
    },
}


def padding_tools(count: int) -> list[dict]:
    """Filler schemas to reproduce a real turn's size without inventing content."""
    return [
        {
            "type": "function",
            "function": {
                "name": f"placeholder_tool_{i}",
                "description": (
                    "A stand-in tool used only to reproduce the request size of a "
                    "real turn, which carries roughly this many schemas."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Some input value."},
                        "limit": {"type": "integer", "description": "How many results."},
                    },
                },
            },
        }
        for i in range(count)
    ]


async def call(client: httpx.AsyncClient, endpoint, messages, tools, timeout=60.0) -> dict:
    started = time.monotonic()

    # Both keys are omitted entirely when there are no tools. Sending
    # "tool_choice": null is rejected outright — Groq answers 400 "Only allowed
    # string values for 'tool_choice' are [none, auto, required]" — which made
    # every endpoint look unreachable when the problem was the benchmark.
    payload: dict = {"model": endpoint.model, "messages": messages, "max_tokens": 300}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    try:
        response = await client.post(
            f"{endpoint.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {endpoint.api_key}"},
            json=payload,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}", "seconds": time.monotonic() - started}

    elapsed = time.monotonic() - started
    if response.status_code >= 400:
        # Shared with the client so both cope with every provider's error shape —
        # notably Gemini's, which wraps the error object in a list and crashed a
        # naive .get() the first time a Google key was configured.
        error = error_payload(response)
        detail = str(error.get("message") or error.get("detail") or "")[:200]
        # Groq returns what the model actually emitted in failed_generation.
        # That is the whole diagnosis for "Failed to call a function", so
        # truncating it away leaves an error that says nothing actionable.
        emitted = error.get("failed_generation")
        if emitted:
            detail += f" | model emitted: {str(emitted)[:160]}"
        if not detail:
            detail = response.text[:200]
        return {"ok": False, "error": f"HTTP {response.status_code}: {detail}", "seconds": elapsed}

    try:
        data = response.json()
        message = (data.get("choices") or [{}])[0].get("message") or {}
    except (ValueError, IndexError):
        return {"ok": False, "error": "unparseable response", "seconds": elapsed}

    return {
        "ok": True,
        "seconds": elapsed,
        "content": message.get("content") or "",
        "tool_calls": message.get("tool_calls") or [],
        "usage": data.get("usage", {}),
        "limits": rate_limits(response),
    }


def rate_limits(response) -> dict:
    """The account's real quota, read off the response it just returned.

    Published free-tier numbers go stale and vary by account, so the only figure
    worth reporting is the one the provider just attached to your own request.
    Providers that send no such headers simply report nothing, rather than being
    guessed at.
    """
    out: dict[str, str] = {}
    for field in ("requests", "tokens"):
        for kind in ("limit", "remaining", "reset"):
            value = response.headers.get(f"x-ratelimit-{kind}-{field}")
            if value:
                out[f"{kind}_{field}"] = value
    return out


def _describe_limits(limits: dict) -> str:
    """One line, or nothing if the provider told us nothing."""
    if not limits:
        return ""
    bits = []
    for field in ("tokens", "requests"):
        limit, remaining = limits.get(f"limit_{field}"), limits.get(f"remaining_{field}")
        if limit:
            used = f"{remaining} of {limit}" if remaining else limit
            bits.append(f"{used} {field} left")
    reset = limits.get("reset_tokens") or limits.get("reset_requests")
    if reset:
        bits.append(f"resets in {reset}")
    return ", ".join(bits)


def tool_call_correct(result: dict) -> tuple[bool, str]:
    """Did it call the right tool with the right argument, formatted properly?"""
    calls = result.get("tool_calls") or []
    if not calls:
        return False, "no tool call"

    fn = calls[0].get("function", {})
    name = (fn.get("name") or "").strip()
    if name != "get_stock_quote":
        # The failure seen in real use: arguments crammed into the name field.
        if "get_stock_quote" in name:
            return False, "args inside name"
        return False, f"wrong tool: {name[:24]}"

    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        return False, "arguments not valid JSON"

    symbol = str(args.get("symbol", "")).upper()
    if "NVDA" not in symbol:
        return False, f"wrong symbol: {symbol[:12]}"
    return True, "correct"


async def benchmark_endpoint(client: httpx.AsyncClient, endpoint) -> dict:
    row = {"label": endpoint.label, "model": endpoint.model}

    # 1. Plain latency.
    plain = await call(
        client, endpoint,
        [{"role": "user", "content": "Reply with exactly: ok"}],
        tools=None,
    )
    row["latency"] = plain.get("seconds")
    row["reachable"] = plain["ok"]
    if not plain["ok"]:
        row["error"] = plain["error"]
        return row

    # 2. Tool calling with a small tool set. A system prompt is included because
    #    the real agent always sends one, and its presence measurably changes how
    #    reliably some models format tool calls — testing without it would
    #    measure a request shape this app never actually makes.
    small = await call(
        client, endpoint,
        [SYSTEM_MESSAGE, {"role": "user", "content": "What is NVDA trading at right now?"}],
        tools=[TEST_TOOL],
    )
    if small["ok"]:
        ok, why = tool_call_correct(small)
        row["tools_small"] = ok
        row["tools_small_why"] = why
    else:
        row["tools_small"] = False
        row["tools_small_why"] = small["error"][:40]

    # 3. The same call at realistic size — this is where per-minute caps bite.
    big = await call(
        client, endpoint,
        [SYSTEM_MESSAGE, {"role": "user", "content": "What is NVDA trading at right now?"}],
        tools=[TEST_TOOL, *padding_tools(28)],
    )
    if big["ok"]:
        ok, why = tool_call_correct(big)
        row["tools_large"] = ok
        row["tools_large_why"] = why
        row["large_latency"] = big["seconds"]
        row["prompt_tokens"] = big.get("usage", {}).get("prompt_tokens")
        row["limits"] = big.get("limits") or {}
    else:
        row["tools_large"] = False
        row["tools_large_why"] = big["error"][:60]

    return row


def rank_providers(rows: list[dict]) -> list[str]:
    """The measured order, best first, from a benchmark's rows.

    A provider is judged by its best endpoint. Passing a full-size tool-calling
    turn is what matters — that is the request the per-minute caps bite on and
    the one a fallback exists to catch — so those providers come first, then
    the ones that manage only the small tool test, and within each group the
    fastest first. A provider whose every endpoint failed is left out: naming
    it would put a known-dead provider ahead of one that was never measured.
    """
    best: dict[str, tuple[int, int, float]] = {}
    for row in rows:
        name = str(row.get("label", "")).split(":", 1)[0]
        if not name or not row.get("reachable"):
            continue
        score = (
            1 if row.get("tools_large") else 0,
            1 if row.get("tools_small") else 0,
            -float(row.get("latency") or 999.0),
        )
        if name not in best or score > best[name]:
            best[name] = score
    ranked = [name for name, score in best.items() if score[0] or score[1]]
    return sorted(ranked, key=lambda name: best[name], reverse=True)


def recommend_models(rows: list[dict], limit: int = 3) -> dict[str, list[str]]:
    """Per model setting, the models that passed a full-size turn, fastest first.

    Keyed by the .env setting that chooses the model (GEMINI_MODEL,
    GROQ_MODEL_LADDER, CUSTOM_MODEL, …), because that is what gets written.
    Every provider takes a comma list, and the pool tries it in order, so the
    top few passing models become that provider's own ladder. A row that did
    not pass the full-size turn is not a candidate at all: the small test is
    not the job.
    """
    passing: dict[str, list[tuple[float, str]]] = {}
    for row in rows:
        setting, model = row.get("setting"), row.get("model")
        if not (setting and model and row.get("tools_large")):
            continue
        speed = row.get("large_latency")
        if speed is None:
            speed = row.get("latency")
        passing.setdefault(setting, []).append((float(99.0 if speed is None else speed), model))
    return {s: [m for _, m in sorted(pairs)][:limit] for s, pairs in passing.items()}


def verdict(row: dict) -> str:
    if not row.get("reachable"):
        error = row.get("error", "")
        # "Unreachable" for what is really a bad key or a retired model sends
        # people debugging their network instead of their config.
        if "401" in error or "403" in error:
            return f"{RED}key rejected{RESET}"
        if "402" in error:
            return f"{RED}needs a paid plan{RESET}"
        if "410" in error:
            # Gone, in the HTTP sense that means it. GitHub Models answers this
            # during its retirement brownouts.
            return f"{RED}service withdrawn{RESET}"
        if "429" in error:
            # A 429 on a benchmark's first call is not a busy minute — nothing
            # has been spent yet. On these providers it means this model carries
            # no free allowance, which is a model choice to change, not a wait.
            lowered = error.lower()
            if "quota" in lowered or "billing" in lowered or "plan" in lowered:
                return f"{RED}no free quota for this model{RESET}"
            return f"{YELLOW}rate limited right now{RESET}"
        if "404" in error:
            return f"{RED}model not available{RESET}"
        if "400" in error:
            return f"{RED}request rejected{RESET}"
        return f"{RED}unreachable{RESET}"
    if not row.get("tools_large"):
        # Fine for chat, unusable here — this assistant is tool calls almost end
        # to end, so failing the realistic case disqualifies it.
        return f"{RED}fails at full size{RESET}"
    if not row.get("tools_small"):
        return f"{YELLOW}unreliable tools{RESET}"
    latency = row.get("large_latency") or row.get("latency") or 99
    if latency < 2.0:
        return f"{GREEN}excellent{RESET}"
    if latency < 5.0:
        return f"{GREEN}good{RESET}"
    return f"{YELLOW}usable but slow{RESET}"


def _setting_for(endpoint) -> str:
    return model_setting(endpoint)


def _key_setting_for(endpoint) -> str:
    return f"{provider_prefix(endpoint)}_API_KEY"


def _base_url_setting_for(endpoint) -> str:
    return f"{provider_prefix(endpoint)}_BASE_URL"


# Preferred first. A provider's catalogue is mostly noise for this purpose —
# transcription, embeddings, image models, and small models that cannot hold a
# 30-schema turn — so candidates are ordered by what has actually worked here
# rather than tried alphabetically.
# "free" leads because OpenRouter marks its zero-cost models with a `:free`
# suffix and everything else on that catalogue answers 402. No other provider
# puts the word in a model name, so this costs them nothing.
_PROMISING = ("free", "gpt-oss-120b", "flash", "70b", "qwen", "gpt-4o", "mistral-large", "glm")

# Matched as whole tokens, never as substrings. "gemini" contains "mini", so a
# substring test silently discards every Gemini model — including while trying
# to repair a Gemini endpoint, where the whole list would come back empty and
# the provider would be declared dead.
_HOPELESS_TOKENS = frozenset({
    "whisper", "tts", "guard", "embed", "embedding", "embeddings", "vision",
    "image", "images", "audio", "speech", "rerank", "reranker", "moderation",
    "nano", "mini", "lite", "small", "tiny",
    # Too small to hold a full-size turn, whatever else they can do.
    "1b", "2b", "3b", "4b", "7b", "8b", "9b",
})


def _tokens(model: str) -> set[str]:
    return set(re.split(r"[^a-z0-9]+", model.lower())) - {""}


def _version_of(model: str) -> float:
    """The version number in a model name, for preferring the newest.

    Only a dotted number counts. A bare integer is usually a parameter count or
    a date — reading 120 out of "gpt-oss-120b" as a version would rank it above
    everything.
    """
    match = re.search(r"\b(\d+\.\d+)\b", model)
    return float(match.group(1)) if match else 0.0


def _candidate_models(catalogue: set[str], exclude: str) -> list[str]:
    """Models worth trying as a replacement, best first.

    Newest first within a family, because every attempt costs a real request
    against a quota that is usually the reason we are here. Searching a Gemini
    catalogue in name order tried 2.0 and 2.5 — one out of quota, one withdrawn
    — before reaching the 3.6 that worked.
    """
    def rank(name: str) -> tuple[int, float, int]:
        lowered = name.lower()
        promise = next((i for i, p in enumerate(_PROMISING) if p in lowered), len(_PROMISING))
        return promise, -_version_of(lowered), len(name)

    usable = [
        m for m in catalogue
        if m != exclude and not (_tokens(m) & _HOPELESS_TOKENS)
    ]
    return sorted(usable, key=rank)


def _is_quota_error(error: str) -> bool:
    lowered = error.lower()
    return "429" in error and ("quota" in lowered or "billing" in lowered or "plan" in lowered)


async def find_working_model(
    client: httpx.AsyncClient, endpoint, catalogue: set[str], limit: int = 6
) -> tuple[str | None, str]:
    """Try alternatives from the same provider until one answers a tool call.

    This exists because hardcoded model defaults go stale faster than anyone
    updates them — three of them expired during a single afternoon's setup, each
    presenting as a different kind of failure. The provider's own catalogue plus
    a real request is the only thing that stays true.

    Returns (model, note) on success, or (None, diagnosis). Only the small tool
    test is used: it is one request per candidate, and a model that cannot manage
    that will not manage a full-size turn either.
    """
    from .llm.pool import Endpoint

    quota_hits = 0
    for model in _candidate_models(catalogue, endpoint.model)[:limit]:
        trial = Endpoint(
            model=model, api_key=endpoint.api_key, base_url=endpoint.base_url,
            label=f"{endpoint.label.split(':', 1)[0]}:{model}",
        )
        print(f"{DIM}  trying {model}…{RESET}", flush=True)
        result = await call(
            client, trial,
            [SYSTEM_MESSAGE, {"role": "user", "content": "What is NVDA trading at right now?"}],
            tools=[TEST_TOOL],
        )
        if not result["ok"]:
            print(f"{DIM}    {result['error'][:80]}{RESET}")
            if _is_quota_error(result["error"]):
                quota_hits += 1
                # Two different models refusing on quota is the account's
                # allowance, not the models. Continuing spends more of an
                # allowance that is already gone, to learn the same thing again.
                if quota_hits >= 2:
                    return None, "quota"
            continue
        ok, why = tool_call_correct(result)
        if ok:
            return model, f"{result['seconds']:.2f}s, tool call correct"
        print(f"{DIM}    answered but {why}{RESET}")
    return None, "quota" if quota_hits else "none"


async def available_models(client: httpx.AsyncClient, pool) -> dict[str, set[str]]:
    """Ask each provider what its key can actually reach, keyed by base URL.

    Worth doing before testing anything: a model name that has been renamed or
    retired returns 404, which is indistinguishable from a broken provider in the
    results table. Better to say "that model doesn't exist on your account" than
    to report the endpoint as unreachable.
    """
    found: dict[str, set[str]] = {}
    for base_url, api_key in {(e.base_url, e.api_key) for e in pool.endpoints}:
        try:
            response = await client.get(
                f"{base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=20.0,
            )
            if response.status_code < 400:
                found[base_url] = {
                    m.get("id") for m in response.json().get("data", []) if m.get("id")
                }
        except (httpx.HTTPError, ValueError):
            continue
    return found


async def main(explore: int = 0) -> int:
    settings = get_settings()
    pool = build_pool(settings)

    print(f"{BOLD}Provider benchmark{RESET}")
    print(f"{DIM}Measured against your own keys — free-tier terms change, so this{RESET}")
    print(f"{DIM}beats any ranking written down in a document.{RESET}\n")

    if not len(pool):
        print(f"{RED}No endpoints configured.{RESET} Set GROQ_API_KEY in .env.")
        return 1

    # Named up front so a provider you thought you configured is obvious by its
    # absence. A key that was written to .env without restarting the container
    # otherwise shows up as nothing at all, which reads as the provider failing
    # rather than never having been loaded.
    providers = sorted({e.label.split(":", 1)[0] for e in pool.endpoints})
    print(f"{BOLD}Pool:{RESET} {len(pool)} endpoints across "
          f"{len(providers)} providers — {', '.join(providers)}")
    print(f"{DIM}A provider missing from that list has no key in the running "
          f"container. Setting one needs: docker compose up -d{RESET}\n")

    async with httpx.AsyncClient() as client:
        catalogue = await available_models(client, pool)

    missing = [
        e for e in pool.endpoints
        if e.base_url in catalogue
        and normalise_model_id(e.model)
        not in {normalise_model_id(m) for m in catalogue[e.base_url]}
    ]
    if missing:
        print(f"{YELLOW}Configured models your key cannot reach:{RESET}")
        for endpoint in missing:
            print(f"  {RED}✗{RESET} {endpoint.label}")
            print(f"{DIM}      {_setting_for(endpoint)} — a replacement is searched "
                  f"for below{RESET}")
        print()

    for base_url, models in catalogue.items():
        host = base_url.split("//")[-1].split("/")[0]
        chat_models = sorted(
            m for m in models
            if not any(skip in m.lower() for skip in ("whisper", "tts", "guard", "embed"))
        )
        print(f"{BOLD}{host}{RESET} {DIM}— {len(chat_models)} chat models available{RESET}")
        for model in chat_models[:12]:
            marker = f" {GREEN}(in your ladder){RESET}" if any(
                e.model == model for e in pool.endpoints
            ) else ""
            print(f"  {DIM}{model}{RESET}{marker}")
        if len(chat_models) > 12:
            print(f"  {DIM}… and {len(chat_models) - 12} more{RESET}")
        print()

    # Sequential on purpose: running these in parallel would have the providers
    # rate-limiting each other's measurements and produce nonsense.
    testable = [e for e in pool.endpoints if e not in missing]

    async def measure(client, endpoint, candidate=False) -> dict:
        print(f"{DIM}testing {endpoint.label}…{RESET}", flush=True)
        try:
            row = await benchmark_endpoint(client, endpoint)
        except Exception as exc:
            # One provider behaving unexpectedly must not discard the results
            # for every other one. Losing a whole run to a single bad
            # response is precisely what a crash here cost before.
            row = {
                "label": endpoint.label,
                "model": endpoint.model,
                "reachable": False,
                "error": f"{type(exc).__name__}: {exc}"[:120],
            }
        row["setting"] = _setting_for(endpoint)
        row["candidate"] = candidate
        return row

    async with httpx.AsyncClient() as client:
        rows = [await measure(client, endpoint) for endpoint in testable]

        # "Test more": the configured model is one guess per provider. With
        # --explore, the provider's own catalogue supplies a few more, ordered
        # by what has worked here before, and each is measured the same way.
        # What passes becomes that provider's ladder — see recommend_models.
        if explore > 0:
            from .llm.pool import Endpoint

            print(f"\n{BOLD}Trying up to {explore} more models per provider…{RESET}")
            seen = {(e.base_url, normalise_model_id(e.model)) for e in pool.endpoints}
            for base_url, models in catalogue.items():
                anchor = next((e for e in testable if e.base_url == base_url), None)
                if anchor is None or not models:
                    continue
                candidates = [
                    m for m in _candidate_models(models, anchor.model)
                    if (base_url, normalise_model_id(m)) not in seen
                ][:explore]
                for model in candidates:
                    trial = Endpoint(
                        model=model, api_key=anchor.api_key, base_url=base_url,
                        label=f"{anchor.label.split(':', 1)[0]}:{model}",
                    )
                    rows.append(await measure(client, trial, candidate=True))

    print(f"\n{BOLD}{'endpoint':<44}{'latency':>9}{'tools':>8}{'@full':>8}  verdict{RESET}")
    print("─" * 88)
    for row in rows:
        latency = f"{row['latency']:.2f}s" if row.get("latency") else "—"
        small = f"{GREEN}✓{RESET}" if row.get("tools_small") else f"{RED}✗{RESET}"
        large = f"{GREEN}✓{RESET}" if row.get("tools_large") else f"{RED}✗{RESET}"
        label = row["label"] + (" +" if row.get("candidate") else "")
        print(f"{label:<44}{latency:>9}{small:>16}{large:>16}  {verdict(row)}")
        for key in ("error", "tools_small_why", "tools_large_why"):
            value = row.get(key)
            if value and value != "correct":
                print(f"    {DIM}{key.replace('_', ' ')}: {value}{RESET}")
        quota = _describe_limits(row.get("limits") or {})
        if quota:
            print(f"    {DIM}quota: {quota}{RESET}")
        used = row.get("prompt_tokens")
        if used:
            print(f"    {DIM}a full-size turn costs ~{used} input tokens here{RESET}")

    # The order these measurements argue for. Printed as a setting rather than
    # applied, because .env lives on the host and this runs in the container —
    # scripts/bestmodels.sh reads the last line and applies it.
    order = rank_providers(rows)
    current = [n.strip() for n in settings.provider_order.split(",") if n.strip()]
    if any(r.get("candidate") for r in rows):
        print(f"{DIM}  + a model from the provider's catalogue, not yet in .env{RESET}")
    if order:
        print(f"\n{BOLD}Measured order, best first:{RESET} {', '.join(order)}")
        if current == order:
            print(f"{DIM}Already what PROVIDER_ORDER says.{RESET}")
        else:
            print(f"{DIM}Apply it, and keep it current every week:{RESET}")
            print(f"    {BOLD}bash scripts/bestmodels.sh{RESET}")
            print(f"{DIM}or by hand:  bash scripts/setkey.sh PROVIDER_ORDER {','.join(order)} "
                  f"&& docker compose up -d{RESET}")
        print(f"RECOMMENDED_ORDER={','.join(order)}")
    # Per provider, the models that passed, fastest first — the provider's
    # own ladder. Same machine-readable form; scripts/bestmodels.sh applies
    # every RECOMMENDED_ line it finds.
    for setting, models in recommend_models(rows).items():
        have = [m.strip() for m in str(getattr(settings, setting.lower(), "") or "").split(",") if m.strip()]
        if models != have:
            print(f"{BOLD}{setting}{RESET} {DIM}measured:{RESET} {', '.join(models)}"
                  + (f"  {DIM}(now: {', '.join(have) or 'unset'}){RESET}" if have != models else ""))
        print(f"RECOMMENDED_{setting}={','.join(models)}")

    # An endpoint that failed for a reason a different model would fix. A rate
    # limit is excluded — that one is about timing, and swapping models to dodge
    # it would quietly move you off the model you chose.
    #
    # Endpoints whose model is absent from the provider's catalogue come first,
    # and they were the omission that mattered: they are skipped before testing,
    # so they never produce a result row, so the search never reached the one
    # case where the catalogue has already proved a replacement is needed.
    repairable: list[tuple] = [(e, {"error": "model not in this provider's catalogue"})
                               for e in missing]
    repairable += [
        (e, r) for e, r in zip(testable, rows, strict=False)
        if not r.get("tools_large")
        and any(code in str(r.get("error", "")) for code in ("402", "404", "410", "429"))
        and "rate limit reached" not in str(r.get("error", "")).lower()
    ]
    if repairable:
        print(f"\n{BOLD}Looking for models that do work…{RESET}")
        for endpoint, row in repairable:
            models = catalogue.get(endpoint.base_url)
            if not models:
                # No catalogue and a 404 is not a model problem: the address is
                # wrong, and hunting for a better model name cannot help. Saying
                # so beats the silence this produced before.
                print(f"{DIM}{endpoint.label}:{RESET}")
                print(f"  {RED}✗ couldn't list this provider's models either.{RESET}")
                if "404" in str(row.get("error", "")):
                    host = endpoint.base_url.split("//")[-1].split("/")[0]
                    print(f"    {DIM}A 404 with no catalogue usually means the base URL "
                          f"is wrong rather than the model.{RESET}")
                    print(f"    {DIM}Currently {host}. Check the provider's current "
                          f"endpoint, then:{RESET}")
                    print(f"    {DIM}bash scripts/setkey.sh "
                          f"{_base_url_setting_for(endpoint)} https://...{RESET}")
                else:
                    print(f"    {DIM}Usually a rejected key.{RESET}")
                continue
            print(f"{DIM}{endpoint.label}:{RESET}")
            async with httpx.AsyncClient() as repair_client:
                found = await find_working_model(repair_client, endpoint, models)
            model, note = found
            if model:
                print(f"  {GREEN}✓ {model}{RESET} {DIM}({note}){RESET}")
                print(f"    {BOLD}bash scripts/setkey.sh {_setting_for(endpoint)} {model}{RESET}")
                row["replacement"] = model
            elif note == "quota":
                # Every model refusing on quota is today's allowance, not a dead
                # provider. Telling someone to clear the key would throw away a
                # working provider that recovers on its own.
                print(f"  {YELLOW}! every model refused on quota — this account's free "
                      f"allowance is spent.{RESET}")
                print(f"    {DIM}It resets on the provider's own cycle, usually daily. "
                      f"Leave the key in place.{RESET}")
            else:
                key_setting = _key_setting_for(endpoint)
                print(f"  {RED}✗ nothing on this provider answered a tool call.{RESET}")
                print(f"    {DIM}Its free tier may be closed to new accounts.{RESET}")
                print(f"    {DIM}Clear it: bash scripts/setkey.sh {key_setting} ''{RESET}")

    # Remembered so the pool can put the working ones first without anyone
    # having to reorder settings by hand.
    from .llm.health import record

    record(rows)

    usable = [r for r in rows if r.get("tools_large")]
    print()
    if usable:
        best = min(usable, key=lambda r: r.get("large_latency") or r.get("latency") or 99)
        print(f"{GREEN}Fastest that handles a full-size turn: {best['label']}{RESET}")
        print(f"{DIM}Put it first in GROQ_MODEL_LADDER, or make its provider primary.{RESET}")
    else:
        print(f"{RED}Nothing here handles a full-size turn.{RESET}")
        print(f"{DIM}Usually a per-minute token cap. Add another provider —{RESET}")
        print(f"{DIM}see docs/model-capacity.md.{RESET}")

    print(f"\n{DIM}Tool calling matters more than speed here: this assistant is tool{RESET}")
    print(f"{DIM}calls end to end, so a fast model that fumbles them is unusable.{RESET}")
    return 0


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Measure the configured providers against each other.")
    parser.add_argument(
        "--explore", type=int, default=0, metavar="N",
        help="also try up to N more models per provider, from its own catalogue (default 0)",
    )
    sys.exit(asyncio.run(main(explore=max(0, parser.parse_args().explore))))
