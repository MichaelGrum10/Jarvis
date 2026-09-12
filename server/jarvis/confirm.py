"""Actions that wait for a person to say yes.

A tool flagged `confirm=True` used to be guarded by a sentence in its own
description asking the model nicely to check first. That is a request, not a
gate: nothing enforced it, and `mail_send` could fire on the model's say-so
alone. Now dispatch intercepts those tools, parks the call here with everything
needed to run it, and hands back a card. The tool runs only when the owner taps
Confirm — from the app, on whatever device is in their hand.

Held in memory on purpose. A pending action that survived a restart would be a
"send this email" button that reappears hours later, and nobody wants that. Five
minutes is long enough to read what it is going to do and decide.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

TTL_SECONDS = 300
# Enough for a burst of "delete these three events"; not enough to be a queue.
MAX_PENDING = 20


@dataclass
class Pending:
    id: str
    tool: str
    arguments: dict
    device_id: str
    conversation_id: int | None
    summary: str
    created: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() - self.created > TTL_SECONDS

    def card(self) -> dict:
        """What the app renders: the button, and exactly what it will do."""
        return {
            "type": "confirm",
            "id": self.id,
            "tool": self.tool,
            "summary": self.summary,
            "arguments": self.arguments,
            "expires_in": max(0, int(TTL_SECONDS - (time.time() - self.created))),
        }


_pending: dict[str, Pending] = {}


def _sweep() -> None:
    for key in [k for k, p in _pending.items() if p.expired]:
        _pending.pop(key, None)
    # Oldest out first if the cap is hit — the newest is the one being looked at.
    while len(_pending) > MAX_PENDING:
        oldest = min(_pending.values(), key=lambda p: p.created)
        _pending.pop(oldest.id, None)


def hold(tool: str, arguments: dict, device_id: str, conversation_id: int | None,
         summary: str) -> Pending:
    _sweep()
    pending = Pending(
        id=secrets.token_urlsafe(9),
        tool=tool,
        arguments=arguments,
        device_id=device_id,
        conversation_id=conversation_id,
        summary=summary,
    )
    _pending[pending.id] = pending
    return pending


def peek(action_id: str) -> Pending | None:
    _sweep()
    return _pending.get(action_id)


def take(action_id: str, device_id: str) -> Pending | None:
    """Claim an action to run it. One claim per action, ever.

    Bound to the device that was asked: a confirmation is a decision, and it
    has to come from the screen the question was put on — not from any device
    that happens to know the id.
    """
    _sweep()
    pending = _pending.get(action_id)
    if pending is None or pending.device_id != device_id:
        return None
    return _pending.pop(action_id)


def cancel(action_id: str, device_id: str) -> bool:
    return take(action_id, device_id) is not None


def describe(tool: str, arguments: dict) -> str:
    """One line a person can read before pressing the button.

    Generic on purpose: a per-tool template would drift from the tool it
    describes, and the arguments *are* what is about to happen.
    """
    shown = {k: v for k, v in arguments.items() if v not in ("", None, False)}
    if not shown:
        return tool.replace("_", " ")
    parts = []
    for key, value in shown.items():
        text = str(value)
        if len(text) > 80:
            text = text[:77] + "…"
        parts.append(f"{key}: {text}")
    return f"{tool.replace('_', ' ')} — " + "; ".join(parts)


def reset() -> None:
    """For tests."""
    _pending.clear()
