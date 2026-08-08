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
import time

import httpx

from .config import get_settings
from .llm.client import error_payload, normalise_model_id
from .llm.pool import build_pool

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


def verdict(row: dict) -> str:
    if not row.get("reachable"):
        error = row.get("error", "")
        # "Unreachable" for what is really a bad key or a retired model sends
        # people debugging their network instead of their config.
        if "401" in error or "403" in error:
            return f"{RED}key rejected{RESET}"
        if "402" in error:
            return f"{RED}needs a paid plan{RESET}"
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


# Matched on the base URL rather than the label: a label is only reliably a
# provider name when build_pool set it, and the fallback one is derived from the
# hostname's first component — "api" for most of these.
_PROVIDER_BY_HOST = (
    ("api.groq.com", "GROQ_MODEL_LADDER"),
    ("api.cerebras.ai", "CEREBRAS_MODEL"),
    ("openrouter.ai", "OPENROUTER_MODEL"),
    ("api.together.xyz", "TOGETHER_MODEL"),
    ("generativelanguage.googleapis.com", "GEMINI_MODEL"),
    ("models.inference.ai.azure.com", "GITHUB_MODELS_MODEL"),
    ("models.github.ai", "GITHUB_MODELS_MODEL"),
    ("api.mistral.ai", "MISTRAL_MODEL"),
)


def _setting_for(endpoint) -> str:
    """Which .env setting controls this endpoint's model.

    Naming the right provider matters: telling someone to edit GROQ_MODEL_LADDER
    because a *Cerebras* model was retired sends them to a setting that has
    nothing to do with the failure, and leaves the one they need unmentioned.
    """
    for host, setting in _PROVIDER_BY_HOST:
        if host in endpoint.base_url:
            return setting
    return f"{endpoint.label.split(':', 1)[0].upper()}_MODEL"


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


async def main() -> int:
    settings = get_settings()
    pool = build_pool(settings)

    print(f"{BOLD}Provider benchmark{RESET}")
    print(f"{DIM}Measured against your own keys — free-tier terms change, so this{RESET}")
    print(f"{DIM}beats any ranking written down in a document.{RESET}\n")

    if not len(pool):
        print(f"{RED}No endpoints configured.{RESET} Set GROQ_API_KEY in .env.")
        return 1

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
            print(f"{DIM}      fix: bash scripts/setkey.sh {_setting_for(endpoint)} "
                  f"a-model-from-the-list-below{RESET}")
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
    async with httpx.AsyncClient() as client:
        rows = []
        for endpoint in testable:
            print(f"{DIM}testing {endpoint.label}…{RESET}", flush=True)
            try:
                rows.append(await benchmark_endpoint(client, endpoint))
            except Exception as exc:
                # One provider behaving unexpectedly must not discard the results
                # for every other one. Losing a whole run to a single bad
                # response is precisely what a crash here cost before.
                rows.append(
                    {
                        "label": endpoint.label,
                        "model": endpoint.model,
                        "reachable": False,
                        "error": f"{type(exc).__name__}: {exc}"[:120],
                    }
                )

    print(f"\n{BOLD}{'endpoint':<44}{'latency':>9}{'tools':>8}{'@full':>8}  verdict{RESET}")
    print("─" * 88)
    for row in rows:
        latency = f"{row['latency']:.2f}s" if row.get("latency") else "—"
        small = f"{GREEN}✓{RESET}" if row.get("tools_small") else f"{RED}✗{RESET}"
        large = f"{GREEN}✓{RESET}" if row.get("tools_large") else f"{RED}✗{RESET}"
        print(f"{row['label']:<44}{latency:>9}{small:>16}{large:>16}  {verdict(row)}")
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
    import sys

    sys.exit(asyncio.run(main()))
