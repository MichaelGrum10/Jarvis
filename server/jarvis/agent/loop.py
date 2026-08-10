"""The agent loop: model → tool calls → results → model, until it answers.

Streams events out as it goes so the UI can show tool activity live rather than
staring at a spinner for fifteen seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..llm.client import LLMError, get_llm
from ..skills import match_skill, skill_preamble
from ..telemetry import record_failure, record_if_refusal
from ..tools import recent_events
from ..tools.base import ToolContext, ToolResult, registry
from ..tools.memory_tool import memory_preamble
from .prompts import build_system_prompt
from .toolpicker import describe_selection, select_tools

log = logging.getLogger(__name__)

MAX_STEPS = 8
PARALLEL_TOOL_LIMIT = 5


@dataclass
class AgentEvent:
    type: str  # thinking | tool_start | tool_end | token | final | error
    data: dict = field(default_factory=dict)

    def sse(self) -> str:
        return f"data: {json.dumps({'type': self.type, **self.data}, default=str)}\n\n"


@dataclass
class AgentOutcome:
    reply: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    displays: list[dict] = field(default_factory=list)
    steps: int = 0
    error: str = ""


class Agent:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.llm = get_llm()

    async def run(
        self,
        user_message: str,
        history: list[dict] | None = None,
        ctx: ToolContext | None = None,
        *,
        extra_system: str = "",
    ) -> AgentOutcome:
        outcome = AgentOutcome()
        async for event in self.stream(user_message, history, ctx, extra_system=extra_system):
            if event.type == "final":
                outcome.reply = event.data.get("reply", "")
            elif event.type == "tool_end":
                outcome.tool_calls.append(event.data)
                if event.data.get("display"):
                    outcome.displays.append(event.data["display"])
            elif event.type == "error":
                outcome.error = event.data.get("message", "")
            outcome.steps = max(outcome.steps, event.data.get("step", 0))
        return outcome

    async def stream(
        self,
        user_message: str,
        history: list[dict] | None = None,
        ctx: ToolContext | None = None,
        *,
        extra_system: str = "",
    ) -> AsyncIterator[AgentEvent]:
        ctx = ctx or ToolContext(timezone=self.settings.timezone)
        if not ctx.timezone:
            ctx.timezone = self.settings.timezone

        try:
            memory_block = await memory_preamble()
        except Exception:
            log.exception("Could not load memory")
            memory_block = ""

        # A matching skill supplies a standing procedure for this kind of request,
        # so recurring jobs are done the same way every time instead of being
        # re-improvised.
        try:
            skill = await match_skill(user_message)
        except Exception:
            log.exception("Skill matching failed")
            skill = None
        extra_system = (extra_system + skill_preamble(skill)).strip()

        # Tool results don't survive into the next turn (see history_from_rows),
        # so an event created a moment ago would otherwise have no uid to refer
        # back to when the user says "actually, move it".
        extra_system = (extra_system + recent_events.preamble(ctx.conversation_id)).strip()

        # Which sites it can actually open. Without this it hedges — declining to
        # open a WSJ article as paywalled while holding a working session for it.
        try:
            from ..tools.browser_tool import session_note

            extra_system = (extra_system + session_note()).strip()
        except Exception:
            log.exception("Could not read browser session state")

        messages: list[dict] = [
            {
                "role": "system",
                "content": build_system_prompt(self.settings, ctx, memory_block, extra_system),
            }
        ]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_message})

        # Only offer tools relevant to this turn. Sending all ~30 costs ~4k
        # tokens before the user has spoken, and a model choosing among 30
        # options picks wrong more often than one choosing among seven.
        available = registry.available(self.settings, ctx)
        selected = select_tools(user_message, available)
        log.info("%s", describe_selection(user_message, available, selected))
        tool_schemas = [t.schema() for t in selected]

        # Spent across the whole turn, not per call. Eight steps each allowed
        # their own wait is minutes of silence, and the browser gives up long
        # before that — the user sees "Load failed", which is worse than the
        # error waiting was meant to avoid.
        waits: list[float] = []
        budget = self.settings.llm_turn_wait_budget_seconds

        def note_wait(seconds: float, _label: str) -> None:
            waits.append(seconds)

        for step in range(1, MAX_STEPS + 1):
            remaining = max(0.0, budget - sum(waits))
            # The client waits for a cooling endpoint rather than failing, which
            # without this is a silent pause indistinguishable from a hang — and
            # a silent pause is what makes people reload and spend the scarce
            # capacity twice.
            pool = getattr(self.llm, "pool", None)
            if pool is not None and not pool.ready():
                pause = pool.wait_hint()
                if pause > 2:
                    yield AgentEvent(
                        "thinking",
                        {
                            "text": f"Both providers are at their limit — waiting "
                                    f"{int(pause)}s for capacity.",
                            "step": step,
                        },
                    )

            try:
                response = await self.llm.complete(
                    messages, tool_schemas, on_wait=note_wait, wait_budget=remaining
                )
            except LLMError as exc:
                await record_failure("llm_error", str(exc), request=user_message)
                yield AgentEvent("error", {"message": str(exc), "step": step})
                return

            if not response.wants_tools:
                reply = response.content.strip()
                if not reply:
                    reply = "I didn't get a usable response from the model. Try rephrasing?"
                # A refusal for lack of capability is the strongest signal for a
                # missing feature, and it is invisible in an error log because
                # technically nothing failed.
                await record_if_refusal(user_message, reply)
                yield AgentEvent("final", {"reply": reply, "step": step})
                return

            # The provider's own message, kept intact and tagged with who sent
            # it. Rebuilding it from the parsed fields is what broke Gemini 3:
            # it signs its tool calls and rejects a follow-up whose signature has
            # gone missing. `messages_for` strips such extras again if the next
            # step lands on a different provider.
            if response.raw_message:
                messages.append({**response.raw_message, "_origin": response.origin})
            else:
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content or None,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments),
                                },
                            }
                            for call in response.tool_calls
                        ],
                    }
                )

            if response.content.strip():
                yield AgentEvent("thinking", {"text": response.content.strip(), "step": step})

            calls = response.tool_calls[:PARALLEL_TOOL_LIMIT]
            for call in calls:
                yield AgentEvent(
                    "tool_start", {"tool": call.name, "arguments": call.arguments, "step": step}
                )

            results = await asyncio.gather(
                *(registry.dispatch(c.name, c.arguments, ctx) for c in calls),
                return_exceptions=True,
            )

            for call, result in zip(calls, results, strict=True):
                if isinstance(result, BaseException):
                    log.exception("Tool %s raised", call.name, exc_info=result)
                    result = ToolResult.fail(f"{type(result).__name__}: {result}")

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": result.for_model(),
                    }
                )
                if not result.ok:
                    await record_failure(
                        "tool_failure", result.error, request=user_message, tool=call.name
                    )

                yield AgentEvent(
                    "tool_end",
                    {
                        "tool": call.name,
                        "ok": result.ok,
                        "error": result.error,
                        "display": result.display,
                        "step": step,
                    },
                )

        # Ran out of steps — ask for a wrap-up rather than dropping the turn.
        messages.append(
            {
                "role": "user",
                "content": (
                    "You've used all available tool steps. Answer now with what you have, "
                    "and say plainly what you couldn't finish."
                ),
            }
        )
        try:
            final = await self.llm.complete(messages, tools=None)
            yield AgentEvent("final", {"reply": final.content.strip(), "step": MAX_STEPS})
        except LLMError as exc:
            yield AgentEvent("error", {"message": str(exc), "step": MAX_STEPS})


def history_from_rows(rows: list[Any]) -> list[dict]:
    """Turn stored Message rows into API-shaped history, dropping tool chatter.

    Replaying full tool-call transcripts would blow the context window and confuse
    the model with stale results; the user/assistant text is what carries forward.

    Kept short deliberately, and shortened further after measuring: the system
    prompt and tool schemas already cost ~2,800 tokens per call, against a
    free-tier budget of 8,000 per minute. History is re-sent in full on every
    call of every turn, so each retained message is paid for several times over.
    Durable facts belong in memory_save, which is searched on demand rather than
    carried in every request.
    """
    out: list[dict] = []
    for row in rows:
        if row.role not in ("user", "assistant"):
            continue
        if not (row.content or "").strip():
            continue
        out.append({"role": row.role, "content": row.content})
    return out[-8:]
