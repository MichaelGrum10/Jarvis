"""The Mac agent's socket, and the way to call it.

The agent connects here; nothing here ever connects to the agent. See
agentlink.py for why that direction is load-bearing.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import time

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
    "voice.status",
    "voice.mute",
    "voice.unmute",
    "audio.play",
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

    # The secret rides its own header, not Authorization. Caddy's basic auth sits
    # in front of this endpoint and claims Authorization for itself — and a
    # request cannot carry two of them. Bearer is still accepted for a direct
    # connection to 127.0.0.1:8000, which has no proxy in the way.
    offered = websocket.headers.get("x-agent-secret", "").strip()
    if not offered:
        offered = websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
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
    link.on_event(handle_wake)
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
    """What the Mac can do right now, including whether its mic is open.

    The browser shows this rather than a wake-word indicator of its own: a
    wake word cannot work in any browser here, and an indicator that implies
    otherwise is worse than none.
    """
    link = get_link()
    body = {**link.describe(), "commands": sorted(KNOWN_COMMANDS)}
    if link.connected:
        try:
            reply = await link.call("voice.status", {}, timeout=6.0)
            body["wake_word"] = reply.get("data", {}) if reply.get("ok") else {}
        except AgentUnavailable:
            body["wake_word"] = {}
    return body


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


# ------------------------------------------------------------- the wake word
#
# The Mac hears "Jarvis", records what follows, and pushes it here. Everything
# after that is the ordinary pipeline — transcribe, answer, speak — with the
# result played back through the Mac's own speakers rather than a browser.

MAX_WAKE_AUDIO = 4 * 1024 * 1024        # ~2 minutes of 16kHz mono; a sentence is far less


async def handle_wake(message: dict) -> None:
    """Answer a wake-word utterance out loud on the Mac."""
    from ..config import get_settings
    from ..integrations import elevenlabs
    from ..security import issue_speech_ticket

    settings = get_settings()
    link = get_link()

    try:
        audio = base64.b64decode(message.get("audio") or "", validate=True)
    except (ValueError, binascii.Error):
        log.warning("Wake event carried audio that was not base64")
        return
    if not audio or len(audio) > MAX_WAKE_AUDIO:
        log.warning("Wake event audio was empty or oversized (%d bytes)", len(audio))
        return

    try:
        from .voice import transcribe_bytes

        said = await transcribe_bytes(audio, settings)
    except Exception as exc:                      # noqa: BLE001
        log.warning("Could not transcribe the wake utterance: %s", exc)
        return

    said = (said or "").strip()
    if not said:
        log.info("Wake word with nothing intelligible after it")
        return
    log.info("Wake word on %s: %r", link._label or "the Mac", said[:120])

    try:
        from ..agent.loop import Agent
        from ..tools.base import ToolContext

        # A device id of its own, so the Mac's spoken turns are distinguishable
        # from the phone's in memory and in the logs.
        outcome = await Agent(settings).run(
            said,
            ctx=ToolContext(
                device_id=f"mac:{link.describe()['label'] or 'agent'}",
                timezone=settings.timezone,
            ),
        )
        reply = outcome.reply or outcome.error or "I have nothing for that, sir."
    except Exception as exc:                      # noqa: BLE001
        log.warning("Wake-word turn failed: %s", exc, exc_info=True)
        reply = "I'm afraid something went wrong answering that, sir."

    if not elevenlabs.configured(settings):
        # Without the cloned voice there is nothing to play — the Mac has no
        # speech engine of its own here, and `say` is not the voice you cloned.
        log.info("Answered the wake word but ElevenLabs is not configured, so nothing was spoken")
        return

    text = elevenlabs.speakable_length(reply)
    key = elevenlabs.voice_key(text, settings)
    from .voice import _pending, _sweep

    _sweep()
    _pending[key] = (text, time.time())
    url = (
        f"{settings.public_url.rstrip('/')}/api/voice/audio/{key}"
        f"?t={issue_speech_ticket(key, settings)}"
    )

    try:
        await link.call("audio.play", {"url": url}, timeout=180.0)
    except AgentUnavailable as exc:
        log.warning("Could not play the answer on the Mac: %s", exc)
