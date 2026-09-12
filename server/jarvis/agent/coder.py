"""Claude, as the engine that writes Jarvis's own code.

Two different jobs want two different models, and this file is where they part.

**Answering you** — what's on my calendar, read me that email — runs on the free
pool (Groq, Gemini, OmniRoute). It happens dozens of times a day, it is mostly
tool-dispatch, and a free tier does it well.

**Editing this source** is not that job. It happens when something is already
broken or when you have asked for a feature, it is the one place where a wrong
answer costs you a working assistant, and it is worth the best model available.
So when an Anthropic API key is configured, the autonomy engine runs on Claude
and nothing else changes.

## The credential, plainly

This needs an **API key** from console.anthropic.com — `ANTHROPIC_API_KEY`. A
claude.ai subscription is a different product with a different billing model,
and its session token is not an API credential; using one here would be both
against Anthropic's terms and trivially breakable, since those tokens are
short-lived. There is no way around that and this file does not pretend
otherwise. Without a key the engine falls back to the free pool, which works
and is worse.

## Why this speaks the pool's language

`complete()` returns the same `LLMResponse` the OpenAI-compatible pool returns,
so `AutonomyEngine`'s loop is untouched and either brain drops into it. The
translation is not symmetric, though, and one asymmetry matters:

**Thinking blocks must go back verbatim.** Claude reasons before it acts, and
on a tool-use turn those thinking blocks have to be replayed unchanged or the
next request is rejected. The engine's history is OpenAI-shaped and has nowhere
to put them, so rebuilding an Anthropic request from it would silently drop
them. Instead this keeps its own native transcript and only reads the *new*
messages the engine appends, which is also how `raw_message` solves the same
problem for Gemini's thought signatures one layer up.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import Settings, get_settings
from ..llm.client import LLMError, LLMResponse, ToolCall

log = logging.getLogger(__name__)

# Non-streaming, so this stays under the SDK's HTTP timeout. A code edit that
# needs more than this wants to be several edits anyway.
MAX_TOKENS = 16000


def available(settings: Settings | None = None) -> bool:
    """Is Claude configured to do the coding?"""
    settings = settings or get_settings()
    if not settings.anthropic_api_key.strip():
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        log.warning("ANTHROPIC_API_KEY is set but the anthropic package is not installed")
        return False
    return True


def _tools_for_claude(tools: list[dict] | None) -> list[dict]:
    """OpenAI's `{type, function: {name, parameters}}` to Claude's flat shape."""
    out = []
    for tool in tools or []:
        fn = tool.get("function", tool)
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


class ClaudeCoder:
    """One coding session. Not reusable across runs — it holds a transcript."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(
            api_key=self.settings.anthropic_api_key,
            # Editing code and running a test suite is slow work; the default
            # two retries on 429/5xx is right, the default timeout is generous.
            max_retries=3,
        )
        self._system: str = ""
        self._native: list[dict] = []
        self._consumed = 0

    # ------------------------------------------------------------- translation

    def _absorb(self, messages: list[dict]) -> None:
        """Fold newly-appended engine messages into the native transcript.

        Assistant messages are skipped on purpose: the engine reconstructs them
        from parsed fields, and `_native` already holds the real ones, thinking
        blocks and all.
        """
        pending_results: list[dict] = []

        def flush() -> None:
            # Every tool result for one assistant turn goes back in a single
            # user message. Splitting them across messages is what teaches a
            # model to stop calling tools in parallel.
            if pending_results:
                self._native.append({"role": "user", "content": list(pending_results)})
                pending_results.clear()

        for message in messages[self._consumed:]:
            role = message.get("role")
            if role == "system":
                self._system = str(message.get("content") or "")
            elif role == "tool":
                pending_results.append({
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id", ""),
                    "content": str(message.get("content") or "")[:20000],
                })
            elif role == "user":
                flush()
                self._native.append({"role": "user", "content": str(message.get("content") or "")})
            # assistant: already recorded natively by complete()

        flush()
        self._consumed = len(messages)

    # ---------------------------------------------------------------- request

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        temperature: float | None = None,   # noqa: ARG002 — see below
        **_: Any,
    ) -> LLMResponse:
        """One turn. Signature matches the pool's so the engine cannot tell them apart.

        `temperature` is accepted and ignored: the current Claude models reject
        sampling parameters outright (HTTP 400), and depth is controlled by
        effort instead. Silently dropping it beats making the caller special-case
        which brain it is talking to.
        """
        self._absorb(messages)
        if not self._native:
            raise LLMError("Nothing to send to Claude.")

        model = self.settings.anthropic_model
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=self._system or self._anthropic.NOT_GIVEN,
                # A copy: the transcript keeps growing after this call returns,
                # and handing out the live list makes what was sent depend on
                # when you look at it.
                messages=list(self._native),
                tools=_tools_for_claude(tools) or self._anthropic.NOT_GIVEN,
                # Thinking is on by default on Opus 5; effort is the dial that
                # decides how hard it works before answering. Reading unfamiliar
                # code and forming a hypothesis about a failure is exactly the
                # work that repays a high setting.
                output_config={"effort": self.settings.anthropic_effort},
                # The transcript is re-sent in full every turn and grows with
                # each file read, so caching its prefix is most of the bill.
                cache_control={"type": "ephemeral"},
            )
        except self._anthropic.AuthenticationError:
            raise LLMError(
                "Anthropic rejected the API key. Check ANTHROPIC_API_KEY at console.anthropic.com."
            ) from None
        except self._anthropic.PermissionDeniedError:
            raise LLMError("That Anthropic key is not allowed to use the Messages API.") from None
        except self._anthropic.NotFoundError:
            raise LLMError(
                f"Anthropic has no model called {model!r}. Set ANTHROPIC_MODEL to a current one."
            ) from None
        except self._anthropic.RateLimitError as exc:
            retry = exc.response.headers.get("retry-after", "60") if exc.response else "60"
            raise LLMError(f"Anthropic is rate limiting; retry in {retry}s.") from None
        except self._anthropic.APIStatusError as exc:
            # 400 on a coding loop is usually a request this code built wrong,
            # so say what it said rather than a generic failure.
            raise LLMError(f"Anthropic error {exc.status_code}: {str(exc.message)[:300]}") from None
        except self._anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach Anthropic: {exc}") from None

        # Checked before content is read: a refusal is a 200 with nothing useful
        # in it, and treating it as an empty answer would loop pointlessly.
        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            why = getattr(detail, "explanation", "") or getattr(detail, "category", "") or ""
            raise LLMError(f"Claude declined to work on this{f': {why}' if why else '.'}")

        # The authentic assistant turn, thinking blocks included, for replay.
        self._native.append({"role": "assistant", "content": response.content})

        text_parts, calls = [], []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                # `block.input` is already a dict from the SDK — never string-match
                # the serialised form, whose escaping varies by model.
                arguments = block.input if isinstance(block.input, dict) else {}
                calls.append(ToolCall(id=block.id, name=block.name, arguments=arguments))

        usage = response.usage
        log.info(
            "claude coder: %s in / %s out / %s cached, stop=%s",
            usage.input_tokens, usage.output_tokens,
            getattr(usage, "cache_read_input_tokens", 0), response.stop_reason,
        )

        return LLMResponse(
            content="\n".join(text_parts).strip(),
            tool_calls=calls,
            finish_reason=response.stop_reason or "stop",
            model=response.model,
            usage={
                "prompt_tokens": usage.input_tokens,
                "completion_tokens": usage.output_tokens,
                "cached_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            },
        )


# Providers whose free tier meters tokens per *minute* tightly enough that one
# file read can spend a whole minute's budget. Not a quality judgement — Groq is
# the fastest thing here and the best choice for a chat turn. It is the wrong
# shape for a turn that carries a 3000-line module.
_TIGHT_BUDGET = frozenset({"groq"})


def free_pool_order(settings: Settings | None = None) -> list[str]:
    """Provider names, best-for-coding first, from IMPROVE_PROVIDER_ORDER."""
    settings = settings or get_settings()
    return [p.strip().lower() for p in settings.improve_provider_order.split(",") if p.strip()]


def reorder_for_coding(pool, settings: Settings | None = None) -> None:
    """Put the roomy endpoints first, in place.

    The pool's usual order is tuned for chat: fastest first, which is right when
    the request is a sentence. A coding turn re-sends the whole transcript plus
    every file read so far, so what matters is context and a per-minute budget
    big enough to carry it. Same endpoints, different question.
    """
    settings = settings or get_settings()
    wanted = free_pool_order(settings)
    if not wanted or not pool.endpoints:
        return

    def rank(endpoint) -> tuple[int, int]:
        name = endpoint.label.split(":", 1)[0]
        preferred = wanted.index(name) if name in wanted else len(wanted)
        # Tight-budget providers go last rather than being dropped: when they
        # are all you have, a slow cycle beats no cycle.
        return preferred, 1 if name in _TIGHT_BUDGET else 0

    # sorted() is stable, so endpoints of equal rank keep the order the pool
    # built them in — a provider's own best-first ladder survives.
    pool.endpoints = sorted(pool.endpoints, key=rank)


def make_brain(settings: Settings | None = None):
    """The model that will do the editing: Claude when configured, else the pool.

    A fresh object every call — a coder carries one run's transcript, and
    handing the next run a used one would replay the last one's history.
    """
    settings = settings or get_settings()
    if available(settings):
        log.info("Autonomy running on %s", settings.anthropic_model)
        return ClaudeCoder(settings)

    # A separate client, not the shared one: reordering the pool in place would
    # change which model answers your next question too.
    from ..llm.client import GroqClient

    client = GroqClient(settings)
    reorder_for_coding(client.pool, settings)
    log.info(
        "Autonomy running on the free pool, %s first",
        client.pool.endpoints[0].label if len(client.pool) else "nothing configured",
    )
    return client


def describe(settings: Settings | None = None) -> str:
    """One line for doctor and the dashboard: what will actually do the editing."""
    settings = settings or get_settings()
    if available(settings):
        return f"Claude ({settings.anthropic_model}, effort {settings.anthropic_effort})"
    if settings.anthropic_api_key.strip():
        return "ANTHROPIC_API_KEY is set but the anthropic package is missing — using the free pool"

    # Name the endpoint rather than "the free pool", so the answer to "what is
    # writing my code" is a model, not a category.
    try:
        from ..llm.pool import build_pool

        pool = build_pool(settings)
        reorder_for_coding(pool, settings)
        if pool.endpoints:
            first = pool.endpoints[0]
            tight = first.label.split(":", 1)[0] in _TIGHT_BUDGET
            return f"{first.label} (free{', tight per-minute budget' if tight else ''})"
    except Exception:   # noqa: BLE001 — describing must never be the thing that fails
        log.debug("Could not describe the free coding pool", exc_info=True)
    return "the free pool"


__all__ = [
    "ClaudeCoder",
    "available",
    "describe",
    "free_pool_order",
    "make_brain",
    "reorder_for_coding",
]
