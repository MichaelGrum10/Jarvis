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
from ..tools.base import ToolContext, ToolResult, registry
from ..tools.memory_tool import memory_preamble
from .prompts import build_system_prompt

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

        messages: list[dict] = [
            {
                "role": "system",
                "content": build_system_prompt(self.settings, ctx, memory_block, extra_system),
            }
        ]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_message})

        tool_schemas = registry.schemas(self.settings, ctx)

        for step in range(1, MAX_STEPS + 1):
            try:
                response = await self.llm.complete(messages, tool_schemas)
            except LLMError as exc:
                yield AgentEvent("error", {"message": str(exc), "step": step})
                return

            if not response.wants_tools:
                reply = response.content.strip()
                if not reply:
                    reply = "I didn't get a usable response from the model. Try rephrasing?"
                yield AgentEvent("final", {"reply": reply, "step": step})
                return

            # Record the assistant's tool-call turn verbatim; the API requires it to
            # precede the matching tool results.
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
    """
    out: list[dict] = []
    for row in rows:
        if row.role not in ("user", "assistant"):
            continue
        if not (row.content or "").strip():
            continue
        out.append({"role": row.role, "content": row.content})
    return out[-20:]
