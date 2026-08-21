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
import email.utils
import json
import logging
import os
import platform
import random
import subprocess
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

# How many AppleScripts may run at once. Mail and Calendar serialise Apple
# events internally anyway, so more than a few buys nothing and a burst of
# commands would otherwise spawn an osascript process for each.
MAX_CONCURRENT = 3

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


# ------------------------------------------------------------------- osascript

SCRIPTS = Path(__file__).resolve().parent / "scripts"

# Under the server's 25s call timeout on purpose, so a slow Apple event produces
# a real explanation from here rather than a bare timeout from the other end.
OSASCRIPT_TIMEOUT = 20.0

FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"


class CapabilityError(RuntimeError):
    """A capability could not run — missing permission, app not running, timeout."""


# macOS reports a refused Apple event as a number, which tells you nothing about
# which switch to flick. Each of these is a specific instruction instead.
TCC_HINTS = (
    ("-1743", "macOS has not been granted permission to control that app. "
              "System Settings > Privacy & Security > Automation, find python3, "
              "and switch on the app you are trying to reach. If python3 is not "
              "listed at all, the prompt was dismissed once and will not reappear."),
    ("not authorized", "macOS has not been granted permission to control that app. "
                       "System Settings > Privacy & Security > Automation."),
    ("-600", "That app is not running. Open it once and try again."),
    ("-1728", "The app answered, but the thing being asked for does not exist "
              "(an empty mailbox or a calendar that has gone away)."),
    ("-10004", "macOS refused the Apple event before the app saw it — usually "
               "Automation permission that was denied rather than granted."),
)


def _explain(stderr: str) -> str:
    """Turn osascript's output into something that names the fix."""
    text = (stderr or "").strip()
    lowered = text.lower()
    for needle, hint in TCC_HINTS:
        if needle in lowered:
            return f"{hint} (osascript said: {text[:200]})"
    return text[:300] or "osascript failed without saying why."


def _osascript(script: str, *args) -> str:
    """Run one of our AppleScripts with arguments.

    Arguments go in as argv elements and reach the script through its `on run
    argv` handler. They are never interpolated into the script's source and
    never touch a shell — so a subject line containing a quote, a semicolon or
    a full AppleScript program is inert data all the way down.
    """
    if platform.system() != "Darwin":
        raise CapabilityError("This capability needs macOS; the agent is not running on a Mac.")

    path = SCRIPTS / script
    if not path.is_file():
        raise CapabilityError(
            f"Missing helper script {path}. Copy the whole mac-agent/scripts "
            f"folder next to jarvis_agent.py."
        )

    argv = ["/usr/bin/osascript", str(path)] + [str(a) for a in args]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=OSASCRIPT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise CapabilityError(
            f"{script} took longer than {OSASCRIPT_TIMEOUT:.0f}s. Mail and Calendar "
            f"go slow while syncing; asking for a smaller range usually helps."
        ) from None
    except OSError as exc:
        raise CapabilityError(f"Could not run osascript: {exc}") from None

    if done.returncode != 0:
        raise CapabilityError(_explain(done.stderr))
    return done.stdout


def _records(raw: str) -> list[list[str]]:
    return [r.split(FIELD_SEP) for r in raw.split(RECORD_SEP) if r.strip()]


def _iso(stamp: str) -> str:
    """Rebuild an ISO timestamp from the numeric components AppleScript sent.

    The components are local wall-clock time, so attaching the Mac's offset here
    is what makes "3pm" mean 3pm to a server in another timezone.
    """
    try:
        year, month, day, hour, minute = (int(part) for part in stamp.split(","))
        return dt.datetime(year, month, day, hour, minute).astimezone().isoformat()
    except (ValueError, TypeError):
        return ""


def _yes(value: str) -> bool:
    return value.strip().lower() == "true"


# ------------------------------------------------------------------- the stubs
# Fake data, real shapes. The shapes are what later prompts wire up, so they are
# worth getting right now: changing them later means changing every caller.


def _messages(raw: str) -> list[dict]:
    """Parse the shared message record shape used by both mail scripts."""
    out = []
    for row in _records(raw):
        if len(row) < 5:
            continue
        uid, subject, sender, read_flag, stamp = row[:5]
        # Mail hands back "Some Name <someone@example.com>" as one string. The
        # display name is what a person recognises and the address is what a
        # reply needs, so keep both rather than picking one.
        name, address = email.utils.parseaddr(sender)
        out.append({
            "uid": uid,
            "subject": subject,
            "sender": name or address or sender,
            "sender_email": address,
            "date": _iso(stamp),
            "unread": not _yes(read_flag),
            "mailbox": "INBOX",
        })
    return out


@command("mail.list_recent")
def mail_list_recent(args: dict) -> dict:
    limit = _int(args, "limit", 5, 1, 50)
    messages = _messages(_osascript("mail_recent.applescript", limit))
    return {"source": "Mail.app", "messages": messages, "count": len(messages)}


@command("mail.search")
def mail_search(args: dict) -> dict:
    query = _string(args, "query")
    limit = _int(args, "limit", 5, 1, 50)
    if not query.strip():
        raise ValueError("query cannot be empty")
    messages = _messages(_osascript("mail_search.applescript", query, limit))
    return {
        "source": "Mail.app",
        "query": query,
        "messages": messages,
        "count": len(messages),
        # Said plainly so an empty result is not read as an empty inbox. Body
        # search through AppleScript pulls every message across one at a time
        # and takes minutes on a real mailbox.
        "searched": "subject and sender, not message bodies",
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
    # A window in minutes either side of now. `days` still works for a simple
    # "next N days" caller; the offsets are what a real date range needs, since
    # "today" asked at 2pm starts in the past.
    if "start_offset" in args or "end_offset" in args:
        start_offset = _int(args, "start_offset", 0, -525_600, 525_600)
        end_offset = _int(args, "end_offset", 1440, -525_600, 525_600)
    else:
        start_offset = 0
        end_offset = _int(args, "days", 1, 1, 30) * 1440
    if end_offset <= start_offset:
        raise ValueError("end_offset must be after start_offset")

    events = []
    for row in _records(_osascript("calendar_list.applescript", start_offset, end_offset)):
        if len(row) < 7:
            continue
        uid, summary, calendar, location, all_day, start, end = row[:7]
        events.append({
            "uid": uid,
            "summary": summary,
            "calendar": calendar,
            "location": location,
            "all_day": _yes(all_day),
            "start": _iso(start),
            "end": _iso(end),
        })
    # Calendar returns each calendar's events in its own order, so the merged
    # list is grouped by calendar rather than by time — which is not the order
    # anyone means by "what's on today".
    events.sort(key=lambda e: e["start"])
    return {
        "source": "Calendar.app",
        "window": {"start_offset": start_offset, "end_offset": end_offset},
        "events": events,
        "count": len(events),
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
    except CapabilityError as exc:
        # Expected and explainable: a permission not granted, an app not open, a
        # sync taking too long. The message already names the fix, so it goes
        # back verbatim rather than being flattened into "something went wrong".
        audit(name, args, f"unavailable: {exc}")
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

        # AppleScript blocks for seconds at a time. Running it on the event loop
        # would stop this socket answering its own keepalive pings, and the
        # connection would drop halfway through the very command that was
        # working — so every handler goes to a thread.
        send_lock = asyncio.Lock()
        slots = asyncio.Semaphore(MAX_CONCURRENT)
        running: set = set()

        async def handle(message: dict) -> None:
            try:
                async with slots:
                    reply = await asyncio.to_thread(dispatch, message)
                # One writer at a time: concurrent sends on one WebSocket
                # interleave frames, and a torn frame kills the connection.
                async with send_lock:
                    await socket.send(json.dumps(reply))
            except asyncio.CancelledError:
                raise
            except Exception as exc:                  # noqa: BLE001
                log.warning("Could not answer %s: %s", message.get("command"), exc)

        try:
            async for raw in socket:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                task = asyncio.ensure_future(handle(message))
                running.add(task)
                task.add_done_callback(running.discard)
        finally:
            # The socket is going away; anything still waiting on it cannot be
            # answered, and leaving the tasks running would pile up osascript
            # processes across every reconnect.
            for task in list(running):
                task.cancel()


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
