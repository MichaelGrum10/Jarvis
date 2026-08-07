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
from .llm.pool import build_pool

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)

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
    try:
        response = await client.post(
            f"{endpoint.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {endpoint.api_key}"},
            json={
                "model": endpoint.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto" if tools else None,
                "max_tokens": 300,
            },
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}", "seconds": time.monotonic() - started}

    elapsed = time.monotonic() - started
    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message", "")[:80]
        except ValueError:
            detail = response.text[:80]
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
    }


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

    # 2. Tool calling with a small tool set.
    small = await call(
        client, endpoint,
        [{"role": "user", "content": "What is NVDA trading at right now?"}],
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
        [{"role": "user", "content": "What is NVDA trading at right now?"}],
        tools=[TEST_TOOL, *padding_tools(28)],
    )
    if big["ok"]:
        ok, why = tool_call_correct(big)
        row["tools_large"] = ok
        row["tools_large_why"] = why
        row["large_latency"] = big["seconds"]
        row["prompt_tokens"] = big.get("usage", {}).get("prompt_tokens")
    else:
        row["tools_large"] = False
        row["tools_large_why"] = big["error"][:60]

    return row


def verdict(row: dict) -> str:
    if not row.get("reachable"):
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


async def main() -> int:
    settings = get_settings()
    pool = build_pool(settings)

    print(f"{BOLD}Provider benchmark{RESET}")
    print(f"{DIM}Measured against your own keys — free-tier terms change, so this{RESET}")
    print(f"{DIM}beats any ranking written down in a document.{RESET}\n")

    if not len(pool):
        print(f"{RED}No endpoints configured.{RESET} Set GROQ_API_KEY in .env.")
        return 1

    # Sequential on purpose: running these in parallel would have the providers
    # rate-limiting each other's measurements and produce nonsense.
    async with httpx.AsyncClient() as client:
        rows = []
        for endpoint in pool.endpoints:
            print(f"{DIM}testing {endpoint.label}…{RESET}", flush=True)
            rows.append(await benchmark_endpoint(client, endpoint))

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
