"""The Mac agent's socket, and the way to call it.

The agent connects here; nothing here ever connects to the agent. See
agentlink.py for why that direction is load-bearing.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..agentlink import AgentUnavailable, get_link
from ..config import get_settings
from ..security import CurrentDevice

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])

# The same list the agent enforces. Duplicated deliberately rather than shared:
# the agent must refuse an unknown command even if this server is compromised
# and asks for one, so neither side may trust the other's allowlist.
KNOWN_COMMANDS = {
    "mail.list_recent",
    "mail.search",
    "mail.draft",
    "calendar.list_events",
    "screen.capture",
    "system.run_shortcut",
}


class Call(BaseModel):
    command: str
    args: dict = {}


@router.websocket("/ws")
async def agent_socket(websocket: WebSocket):
    """Accept the Mac agent, once it proves it knows the shared secret."""
    settings = get_settings()
    secret = settings.agent_secret

    if not secret:
        # Refuse before the handshake completes. An agent endpoint with no
        # secret configured is an open remote-control socket.
        await websocket.close(code=1008, reason="Agent link not configured")
        return

    offered = websocket.headers.get("authorization", "")
    offered = offered.removeprefix("Bearer ").strip()
    # compare_digest, not ==: string comparison returns early on the first
    # wrong byte, and over enough attempts that timing distinguishes a nearly
    # correct secret from a wholly wrong one.
    if not offered or not hmac.compare_digest(offered, secret):
        log.warning("Rejected an agent socket with a bad secret")
        await websocket.close(code=1008, reason="Bad agent secret")
        return

    label = websocket.headers.get("x-agent-label", "mac")[:64]
    await websocket.accept()

    link = get_link()
    await link.attach(websocket, label)
    try:
        while True:
            message = await websocket.receive_json()
            link.resolve(message)
    except WebSocketDisconnect:
        await link.detach("disconnected")
    except Exception as exc:              # noqa: BLE001 — a dead socket is normal
        await link.detach(f"socket error: {exc}")


@router.get("/status")
async def status(device: CurrentDevice):
    return {**get_link().describe(), "commands": sorted(KNOWN_COMMANDS)}


@router.post("/call")
async def call(device: CurrentDevice, body: Call):
    """Invoke one named capability on the Mac.

    The command name is checked against the allowlist here as well as on the
    agent. Two independent checks, because a name that reached a shell would be
    the whole ballgame and one bug should not be enough.
    """
    if body.command not in KNOWN_COMMANDS:
        raise HTTPException(400, f"Unknown command: {body.command}")

    try:
        reply = await get_link().call(body.command, body.args)
    except AgentUnavailable as exc:
        raise HTTPException(503, str(exc)) from None

    if not reply.get("ok"):
        raise HTTPException(502, reply.get("error") or "The agent refused that.")
    return reply.get("data", {})
