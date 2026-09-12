"""Asking Jarvis to fix itself, or to grow a new capability, in conversation.

This is the tool behind "that's broken, sort it out" and "add a tool that
tracks parcels". It does not edit anything itself — it queues the request for
the improvement engine, which branches, writes, tests, and (in apply mode)
ships. The reply you get back is an acknowledgement, not a result: building a
feature takes minutes, and a chat turn should not block on it.

Deliberately `confirm=True`. A model that can silently commission changes to its
own source on a loose reading of "can you make this better" is one bad
paraphrase away from rewriting something you liked. The confirmation card shows
the exact wording that will be handed to the engine.
"""

from __future__ import annotations

import logging

from ..config import get_settings
from .base import ToolContext, ToolResult, registry

log = logging.getLogger(__name__)

MIN_GOAL = 8


@registry.tool(
    name="request_improvement",
    description=(
        "Ask Jarvis to change its own code: fix a bug you have hit, or build a new "
        "feature or tool you want. Use this when the user asks for something this "
        "assistant genuinely cannot do yet, or reports something broken and wants it "
        "fixed rather than worked around. The work happens in the background over "
        "minutes — never claim it is already done. Do not use this for questions you "
        "can answer, or for one-off tasks another tool covers."
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "What to fix or build, in the user's own words where possible. Be "
                    "specific about the observed behaviour and the wanted behaviour."
                ),
            }
        },
        "required": ["goal"],
    },
    tags=["system"],
    confirm=True,
)
async def request_improvement(goal: str, ctx: ToolContext = None):
    settings = get_settings()
    goal = (goal or "").strip()

    if len(goal) < MIN_GOAL:
        return ToolResult.fail(
            "Say what to fix or build — a few words is not enough to work from."
        )

    if not settings.autonomy_enabled:
        return ToolResult.fail(
            "I'm not allowed to edit my own code yet. On the server: "
            "bash scripts/setkey.sh AUTONOMY_ENABLED true"
        )
    if settings.improve_mode == "off":
        return ToolResult.fail(
            "Self-improvement is off, so nothing would pick this up. On the server: "
            "bash scripts/self-improve.sh propose"
        )

    from ..agent.selfimprove import get_improver
    from ..db import AutonomyRun, session_scope

    framed = (
        "The owner of this assistant asked for this, in their own words:\n\n"
        f"{goal}\n\n"
        "Build or fix it properly and in keeping with the surrounding code: read the "
        "existing patterns first, put it where a similar feature already lives, and add "
        "tests that cover it. If the request is ambiguous, implement the smallest "
        "reasonable reading of it and say in your summary what you assumed. If it cannot "
        "be done safely, finish with success=false and explain why."
    )

    async with session_scope() as session:
        row = AutonomyRun(goal=framed, status="queued")
        session.add(row)
        await session.flush()
        run_id = row.id

    get_improver().nudge()
    log.info("Improvement requested from chat (run %s): %s", run_id, goal[:120])

    shipping = settings.improve_mode == "apply"
    return ToolResult.success(
        {
            "run_id": run_id,
            "status": "queued",
            "mode": settings.improve_mode,
            "will_deploy_itself": shipping,
            # Said plainly in the data, because the model reads this and must
            # not tell you the feature exists yet.
            "note": (
                "Queued, not built. This takes minutes. Tell the user it is being "
                "worked on in the background."
            ),
        },
        display={
            "kind": "card",
            "title": "Queued for building",
            "lines": [
                goal[:200],
                "It will be written on a branch and tested first."
                + (
                    " If the tests pass it deploys itself."
                    if shipping
                    else " You'll be told when there's a branch to review."
                ),
            ],
        },
    )
