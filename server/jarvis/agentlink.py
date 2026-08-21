"""The live link to the Mac companion agent.

Direction is the whole design. The Mac dials out and holds the socket open; this
server never dials in. That is not a preference — the Mac sits behind NAT on a
domestic connection with no static address, and the alternative is asking
someone to port-forward their home router at the exact moment they are least
equipped to judge what that exposes.

One agent at a time, deliberately. A second connection replaces the first rather
than being refused: a Mac waking from sleep reconnects long before the old
socket's TCP timeout notices it is dead, so the newest connection is always the
real one and refusing it would lock the agent out until a timer expired.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

log = logging.getLogger(__name__)

CALL_TIMEOUT = 25.0        # seconds; a screen capture is the slow one


class AgentUnavailable(RuntimeError):
    """No agent is connected, or it dropped mid-call."""


class AgentLink:
    def __init__(self) -> None:
        self._socket: Any = None
        self._pending: dict[str, asyncio.Future] = {}
        self._label = ""
        self._since: float = 0.0
        self._events = None

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def describe(self) -> dict:
        return {
            "connected": self.connected,
            "label": self._label,
            "since": self._since,
            "in_flight": len(self._pending),
        }

    async def attach(self, socket: Any, label: str) -> None:
        if self._socket is not None:
            # The old one is almost certainly a dead socket from before a sleep.
            log.info("Replacing an existing agent connection with %s", label)
            await self._drop("replaced by a new connection")
        self._socket = socket
        self._label = label
        self._since = asyncio.get_event_loop().time()
        log.info("Mac agent connected: %s", label)

    async def detach(self, reason: str = "disconnected") -> None:
        if self._socket is None:
            return
        log.info("Mac agent %s: %s", self._label, reason)
        await self._drop(reason)

    async def _drop(self, reason: str) -> None:
        self._socket = None
        self._label = ""
        # Anything waiting on this socket will never be answered. Failing the
        # futures now turns an indefinite hang into an immediate, honest error.
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(AgentUnavailable(f"Agent {reason}."))
        self._pending.clear()

    async def call(self, command: str, args: dict | None = None,
                   timeout: float = CALL_TIMEOUT) -> dict:
        """Send one command and wait for its answer."""
        if self._socket is None:
            raise AgentUnavailable(
                "The Mac agent is not connected. Start it on your Mac, or check "
                "it is awake."
            )

        call_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[call_id] = future

        try:
            await self._socket.send_json(
                {"id": call_id, "command": command, "args": args or {}}
            )
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise AgentUnavailable(
                f"The Mac agent did not answer {command} within {timeout:.0f}s."
            ) from None
        except (RuntimeError, OSError) as exc:
            # The socket died between the check above and the send.
            await self.detach(f"send failed: {exc}")
            raise AgentUnavailable("The Mac agent dropped mid-call.") from None
        finally:
            self._pending.pop(call_id, None)

    def on_event(self, handler) -> None:
        """Register what happens when the agent speaks first.

        Almost everything here is request/response, but the wake word is not:
        the Mac hears something and pushes it up unasked. That needs a route
        that is not "match this to a pending call".
        """
        self._events = handler

    def resolve(self, message: dict) -> None:
        """Hand a reply from the agent back to whoever is waiting for it."""
        event = message.get("event")
        if event:
            handler = getattr(self, "_events", None)
            if handler is None:
                log.info("Agent sent a %s event with nobody listening", event)
                return
            # Fire and forget: the agent is not waiting, and a slow handler must
            # not stall the socket's read loop behind it.
            asyncio.ensure_future(handler(message))
            return

        call_id = message.get("id")
        future = self._pending.get(call_id)
        if future is None or future.done():
            # A reply to a call that already timed out. Nothing to do, and not
            # worth an error — the caller has long since been told.
            return
        future.set_result(message)


_link: AgentLink | None = None


def get_link() -> AgentLink:
    global _link
    if _link is None:
        _link = AgentLink()
    return _link
