#!/usr/bin/env python3
"""Jarvis companion agent — runs on your Mac, dials out to the server.

Some capabilities cannot be reached from a Linux VM at all: Apple Mail and
Calendar live behind AppleScript, screen capture needs a logged-in session, and
Shortcuts is a macOS binary. This agent runs where those things are and offers
them over one outbound socket.

## Direction

The Mac connects to the server. The server never connects to the Mac. Your Mac
is behind NAT with no static address, and the alternative is forwarding a port
on your home router — which is a standing invitation to the entire internet in
exchange for saving one outbound connection.

## Safety

Three rules, in order of how badly they would end:

1. **Commands are named, and the names are an allowlist.** An unrecognised name
   is refused and logged. It is never passed anywhere that could run it.
2. **Nothing from the socket ever reaches a shell.** Commands take typed,
   structured arguments. There is no code path from a received string to
   subprocess, os.system, eval, or AppleScript source. When these stubs are
   wired to real implementations, arguments must be passed as argv elements —
   never interpolated into a command string.
3. **Anything that writes, sends, or changes state returns `pending_confirmation`
   instead of doing it.** Reading your mail and sending mail are not the same
   risk, and the difference has to be structural rather than remembered.

Every received command is appended to ~/jarvis-agent.log with a timestamp, so
what the server asked for is auditable independently of what the server says it
asked for.

## Running it

    pip3 install --user websockets
    python3 jarvis_agent.py

Configuration lives in ~/.jarvis-agent.json (created on first run, mode 600).
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import os
import platform
import random
import sys
from pathlib import Path

try:
    import websockets
except ImportError:
    sys.exit(
        "The websockets library is missing. Install it without admin rights:\n"
        "    pip3 install --user websockets"
    )

CONFIG = Path.home() / ".jarvis-agent.json"
LOG_FILE = Path.home() / "jarvis-agent.log"

CONFIG_TEMPLATE = {
    "server": "wss://michael-jarvis.duckdns.org/api/agent/ws",
    "secret": "PUT-YOUR-AGENT-SECRET-HERE",
    "label": platform.node() or "mac",
    # Only if Caddy's basic auth guards the site — it does. Leave the username
    # empty to send no basic auth at all.
    "basic_auth": {"username": "", "password": ""},
}

# Backoff for reconnects. A Mac wakes from sleep to a dead socket several times
# a day, so reconnecting has to be routine rather than exceptional — but a tight
# retry loop against a server that is genuinely down is a denial of service you
# inflict on yourself.
BACKOFF_START = 1.0
BACKOFF_MAX = 60.0

log = logging.getLogger("jarvis-agent")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
    )


def load_config() -> dict:
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(CONFIG_TEMPLATE, indent=2) + "\n")
        os.chmod(CONFIG, 0o600)
        sys.exit(
            f"Created {CONFIG} (mode 600).\n"
            "Put your server URL and agent secret in it, then run this again."
        )
    config = json.loads(CONFIG.read_text())
    if config.get("secret", "") in ("", CONFIG_TEMPLATE["secret"]):
        sys.exit(f"No agent secret set in {CONFIG}.")
    return config


# --------------------------------------------------------------- the registry

# Named capabilities. Adding one means adding an entry here and a handler — the
# dispatcher needs no changes, which is the point: a new capability should never
# be a reason to touch the code that decides what is allowed to run.
#
# `writes` marks anything that sends, creates or changes state. Those return
# pending_confirmation rather than acting.
COMMANDS: dict[str, dict] = {}


def command(name: str, *, writes: bool = False):
    def register(fn):
        COMMANDS[name] = {"handler": fn, "writes": writes}
        return fn
    return register


def _string(args: dict, key: str, default: str = "") -> str:
    """Read a string argument, refusing anything that is not one.

    Type checks are the boundary. Everything past this point assumes its
    arguments are the shape they claim to be, and the moment a dict or a list
    slips through into something that builds an argv, that assumption is how it
    goes wrong.
    """
    value = args.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value[:500]


def _int(args: dict, key: str, default: int, low: int, high: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be a whole number")
    return max(low, min(high, value))


# ------------------------------------------------------------------- the stubs
# Fake data, real shapes. The shapes are what later prompts wire up, so they are
# worth getting right now: changing them later means changing every caller.


@command("mail.list_recent")
def mail_list_recent(args: dict) -> dict:
    limit = _int(args, "limit", 5, 1, 50)
    return {
        "stub": True,
        "messages": [
            {"id": f"stub-{i}", "subject": f"Placeholder message {i}",
             "sender": "someone@example.com", "unread": i == 1,
             "received": "2026-08-21T09:00:00Z"}
            for i in range(1, limit + 1)
        ],
    }


@command("mail.search")
def mail_search(args: dict) -> dict:
    query = _string(args, "query")
    limit = _int(args, "limit", 5, 1, 50)
    return {
        "stub": True,
        "query": query,
        "messages": [
            {"id": f"stub-hit-{i}", "subject": f"Result {i} for {query!r}",
             "sender": "someone@example.com", "received": "2026-08-20T14:30:00Z"}
            for i in range(1, min(limit, 3) + 1)
        ],
    }


@command("mail.draft", writes=True)
def mail_draft(args: dict) -> dict:
    return {
        "to": _string(args, "to"),
        "subject": _string(args, "subject"),
        "body": _string(args, "body"),
    }


@command("calendar.list_events")
def calendar_list_events(args: dict) -> dict:
    days = _int(args, "days", 1, 1, 30)
    return {
        "stub": True,
        "days": days,
        "events": [
            {"id": "stub-event-1", "summary": "Placeholder appointment",
             "start": "2026-08-21T15:00:00Z", "end": "2026-08-21T16:00:00Z",
             "calendar": "Home"},
        ],
    }


@command("screen.capture")
def screen_capture(args: dict) -> dict:
    # A read, so no confirmation gate — but it is the most privacy-sensitive
    # capability here, and the real implementation should say so on screen when
    # it fires rather than capturing silently.
    display = _int(args, "display", 1, 1, 8)
    return {
        "stub": True,
        "display": display,
        "format": "png",
        "width": 0,
        "height": 0,
        "image_base64": "",
        "note": "Stub. Real capture needs Screen Recording permission.",
    }


@command("system.run_shortcut", writes=True)
def system_run_shortcut(args: dict) -> dict:
    return {
        "shortcut": _string(args, "name"),
        "input": _string(args, "input"),
    }


# -------------------------------------------------------------- the dispatcher


def audit(command_name: str, args: dict, outcome: str) -> None:
    """Every command, timestamped, in a file you own.

    Logged before it runs and again for the outcome, so a command that hangs or
    crashes still leaves a record that it arrived.
    """
    log.info("command=%s args=%s outcome=%s", command_name, json.dumps(args, default=str)[:400], outcome)


def dispatch(message: dict) -> dict:
    call_id = message.get("id")
    name = message.get("command")
    args = message.get("args") or {}

    if not isinstance(name, str) or not isinstance(args, dict):
        audit(str(name), {}, "malformed")
        return {"id": call_id, "ok": False, "error": "Malformed command."}

    entry = COMMANDS.get(name)
    if entry is None:
        # The refusal that matters. An unknown name goes nowhere near anything
        # that could execute it — it is a dictionary miss and a log line.
        audit(name, args, "REFUSED unknown command")
        return {"id": call_id, "ok": False, "error": f"Unknown command: {name}"}

    audit(name, args, "received")
    try:
        payload = entry["handler"](args)
    except ValueError as exc:
        audit(name, args, f"bad arguments: {exc}")
        return {"id": call_id, "ok": False, "error": str(exc)}
    except Exception as exc:                       # noqa: BLE001
        audit(name, args, f"failed: {exc}")
        return {"id": call_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if entry["writes"]:
        # Structural, not remembered: a write handler cannot act even if it
        # wanted to, because what it returns is a proposal and the acting
        # happens somewhere else entirely.
        audit(name, args, "pending confirmation")
        return {"id": call_id, "ok": True, "data": {
            "status": "pending_confirmation",
            "command": name,
            "proposed": payload,
            "note": "Nothing has happened yet. This needs confirming before it acts.",
        }}

    audit(name, args, "ok")
    return {"id": call_id, "ok": True, "data": payload}


# ------------------------------------------------------------------ the socket


def _connect(url: str, headers: dict):
    """websockets.connect, whichever spelling this version wants.

    The header keyword was renamed in websockets 14. Your Mac's `pip3 install
    --user websockets` will fetch a current one, but system Pythons carry
    whatever they carry, and failing to connect because of a keyword name is a
    bad way to find that out.
    """
    options = dict(
        # A closed lid means no pongs. Ping often enough to notice a dead link
        # quickly, and let the reconnect loop deal with it.
        ping_interval=20,
        ping_timeout=20,
        max_size=8 * 1024 * 1024,        # room for a screenshot later
    )
    try:
        return websockets.connect(url, additional_headers=headers, **options)
    except TypeError:
        return websockets.connect(url, extra_headers=headers, **options)


def _headers(config: dict) -> dict:
    """What the handshake carries.

    The agent secret rides X-Agent-Secret rather than Authorization, because
    Caddy's basic auth sits in front of the endpoint and claims Authorization
    for itself — and one request cannot carry two of them. If basic auth is
    configured here, it goes in the header Caddy expects; the agent's own secret
    is untouched by it.
    """
    headers = {
        "X-Agent-Secret": config["secret"],
        "X-Agent-Label": config.get("label", "mac"),
    }
    basic = config.get("basic_auth") or {}
    if basic.get("username"):
        pair = f"{basic['username']}:{basic.get('password', '')}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
    return headers


async def session(config: dict) -> None:
    async with _connect(config["server"], _headers(config)) as socket:
        log.info("Connected to %s", config["server"])
        async for raw in socket:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            reply = dispatch(message)
            await socket.send(json.dumps(reply))


async def run() -> None:
    config = load_config()
    delay = BACKOFF_START

    while True:
        try:
            await session(config)
            delay = BACKOFF_START            # a clean session resets the backoff
            log.info("Connection closed cleanly; reconnecting")
        except Exception as exc:             # noqa: BLE001
            log.warning("Disconnected (%s); retrying in %.0fs", exc, delay)

        # Jitter, so a server restart does not bring every agent back at once.
        await asyncio.sleep(delay + random.uniform(0, delay * 0.3))
        delay = min(delay * 2, BACKOFF_MAX)


def main() -> int:
    setup_logging()
    log.info("Jarvis agent starting (%s, %s)", platform.node(), dt.date.today())
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
