#!/usr/bin/env python3
"""Jarvis Messages bridge — runs on a Mac, mirrors iMessage/SMS to your server.

Why this exists: Apple ships no server API for Messages, and iMessage cannot be
read from Linux at all. But every Mac keeps a local SQLite database of your whole
message history at ~/Library/Messages/chat.db. This script reads it (read-only),
pushes new messages to your Jarvis server, and polls for messages to send back out
via AppleScript.

Requirements:
  * A Mac signed into the same iMessage account as your phone, left powered on.
  * Full Disk Access for whatever runs this (Terminal, or python3 itself):
    System Settings → Privacy & Security → Full Disk Access.
  * Python 3.9+, which macOS already has. No pip install needed.

Usage:
    export JARVIS_URL="https://jarvis.example.com"
    export JARVIS_BRIDGE_TOKEN="the same value as BRIDGE_TOKEN on the server"
    python3 jarvis_bridge.py

Run it forever with launchd — see docs/messages-bridge.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.1.0"
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
STATE_FILE = Path.home() / ".jarvis_bridge_state.json"
APPLE_EPOCH = dt.datetime(2001, 1, 1)

log = logging.getLogger("bridge")

# Apple stores dates as nanoseconds since 2001-01-01 (older builds used seconds).
QUERY = """
SELECT
    message.guid,
    message.text,
    message.attributedBody,
    message.date,
    message.is_from_me,
    message.service,
    handle.id            AS handle_id,
    chat.guid            AS chat_guid,
    chat.display_name    AS chat_name,
    chat.style           AS chat_style
FROM message
LEFT JOIN handle ON message.handle_id = handle.ROWID
LEFT JOIN chat_message_join ON message.ROWID = chat_message_join.message_id
LEFT JOIN chat ON chat_message_join.chat_id = chat.ROWID
WHERE message.date > ?
ORDER BY message.date ASC
LIMIT ?
"""


def apple_to_datetime(value: int) -> dt.datetime:
    if value > 1_000_000_000_000:  # nanoseconds
        seconds = value / 1_000_000_000
    else:
        seconds = value
    return APPLE_EPOCH + dt.timedelta(seconds=seconds)


def decode_attributed_body(blob: bytes | None) -> str:
    """Newer macOS stores message text in an NSAttributedString archive rather than
    message.text. Pulling the plain string out of the archive avoids empty bodies."""
    if not blob:
        return ""
    try:
        raw = blob.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    marker = "NSString"
    idx = raw.find(marker)
    if idx == -1:
        return ""
    chunk = raw[idx + len(marker) :]
    # Skip the archive's type/length prelude, then take printable text.
    start = 0
    for i, ch in enumerate(chunk[:16]):
        if ch.isprintable() and ch not in "+ \x01\x02\x86\x84\x94\x95":
            start = i
            break
    text = chunk[start:]
    out = []
    for ch in text:
        if ch == "\x86" or ch == "\x00":
            break
        if ch.isprintable() or ch in "\n\t":
            out.append(ch)
    return "".join(out).strip()


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_date = 0
        self.load()

    def load(self) -> None:
        if self.path.is_file():
            try:
                self.last_date = json.loads(self.path.read_text()).get("last_date", 0)
            except (json.JSONDecodeError, OSError):
                log.warning("State file unreadable; starting from now.")
        if not self.last_date:
            # First run: start from 24h ago rather than importing years of history.
            delta = dt.datetime.now() - dt.timedelta(days=1) - APPLE_EPOCH
            self.last_date = int(delta.total_seconds() * 1_000_000_000)

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps({"last_date": self.last_date}))
        except OSError as exc:
            log.warning("Could not save state: %s", exc)


class Server:
    def __init__(self, base_url: str, token: str) -> None:
        self.base = base_url.rstrip("/")
        self.token = token

    def post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, payload)

    def get(self, path: str) -> dict:
        return self._request("GET", path, None)

    def _request(self, method: str, path: str, payload: dict | None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-Bridge-Token": self.token},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode() or "{}")


def read_new_messages(state: State, limit: int = 200) -> list[dict]:
    if not CHAT_DB.is_file():
        raise FileNotFoundError(
            f"{CHAT_DB} not found. Is this a Mac signed into Messages?"
        )

    # Copy the db first: Messages holds a WAL lock, and reading it live can fail
    # or return a torn view. A copy is cheap and always consistent.
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "chat.db"
        try:
            shutil.copy2(CHAT_DB, snapshot)
            for suffix in ("-wal", "-shm"):
                sidecar = CHAT_DB.with_name(CHAT_DB.name + suffix)
                if sidecar.is_file():
                    shutil.copy2(sidecar, snapshot.with_name(snapshot.name + suffix))
        except PermissionError as exc:
            raise PermissionError(
                "Permission denied reading chat.db. Grant Full Disk Access to your "
                "terminal (System Settings → Privacy & Security → Full Disk Access)."
            ) from exc

        connection = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(QUERY, (state.last_date, limit)).fetchall()
        finally:
            connection.close()

    messages = []
    highest = state.last_date
    for row in rows:
        highest = max(highest, row["date"])
        text = (row["text"] or "").strip() or decode_attributed_body(row["attributedBody"])
        if not text:
            continue  # attachment-only or reaction; nothing useful to summarise
        handle = row["handle_id"] or ""
        is_group = (row["chat_style"] or 0) == 43
        messages.append(
            {
                "guid": row["guid"],
                "chat_id": row["chat_guid"] or handle,
                "chat_name": row["chat_name"] or "",
                "sender": handle,
                "sender_name": "",
                "text": text[:4000],
                "service": row["service"] or "iMessage",
                "is_from_me": bool(row["is_from_me"]),
                "is_group": is_group,
                "sent_at": apple_to_datetime(row["date"]).isoformat(),
            }
        )

    state.last_date = highest
    return messages


def applescript_string(value: str) -> str:
    """Escape a Python string for embedding in an AppleScript literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def send_imessage(recipient: str, text: str, service: str = "iMessage") -> None:
    """Deliver via AppleScript. Messages.app must be running and signed in."""
    # `service type` takes a bare enum (iMessage / SMS), so it is validated against
    # a fixed set rather than interpolated from the server's payload.
    service_enum = "SMS" if service.strip().upper() == "SMS" else "iMessage"
    safe_text = applescript_string(text)
    safe_recipient = applescript_string(recipient)

    script = (
        'tell application "Messages"\n'
        f"    set targetService to 1st service whose service type = {service_enum}\n"
        f'    set targetBuddy to buddy "{safe_recipient}" of targetService\n'
        f'    send "{safe_text}" to targetBuddy\n'
        "end tell"
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript failed")


def process_outbox(server: Server) -> None:
    try:
        jobs = server.get("/api/bridge/outbox").get("jobs", [])
    except urllib.error.URLError as exc:
        log.warning("Could not fetch outbox: %s", exc)
        return

    for job in jobs:
        try:
            send_imessage(job["recipient"], job["text"], job.get("service", "iMessage"))
            log.info("Sent message %s to %s", job["id"], job["recipient"])
            server.post("/api/bridge/outbox/ack", {"id": job["id"], "ok": True})
        except Exception as exc:
            log.error("Failed to send %s: %s", job["id"], exc)
            try:
                server.post(
                    "/api/bridge/outbox/ack", {"id": job["id"], "ok": False, "error": str(exc)}
                )
            except urllib.error.URLError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis Messages bridge")
    parser.add_argument("--url", default=os.getenv("JARVIS_URL", ""), help="Jarvis server base URL")
    parser.add_argument("--token", default=os.getenv("JARVIS_BRIDGE_TOKEN", ""), help="BRIDGE_TOKEN")
    parser.add_argument("--interval", type=int, default=5, help="Poll interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--no-send", action="store_true", help="Mirror only; never send")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )

    if not args.url or not args.token:
        log.error("Set JARVIS_URL and JARVIS_BRIDGE_TOKEN (or pass --url/--token).")
        return 2
    if sys.platform != "darwin":
        log.error("This bridge only works on macOS — chat.db exists nowhere else.")
        return 2

    server = Server(args.url, args.token)
    state = State(STATE_FILE)
    hostname = os.uname().nodename
    log.info("Bridge %s starting against %s", VERSION, args.url)

    backoff = args.interval
    while True:
        try:
            messages = read_new_messages(state)
            if messages or backoff > args.interval:
                response = server.post(
                    "/api/bridge/ingest",
                    {"hostname": hostname, "version": VERSION, "messages": messages},
                )
                if messages:
                    log.info("Pushed %s message(s), stored %s",
                             len(messages), response.get("stored"))
                state.save()
            else:
                # Heartbeat even when idle so the server knows the Mac is alive.
                server.post(
                    "/api/bridge/ingest",
                    {"hostname": hostname, "version": VERSION, "messages": []},
                )

            if not args.no_send:
                process_outbox(server)

            backoff = args.interval
        except (PermissionError, FileNotFoundError) as exc:
            log.error("%s", exc)
            return 1
        except urllib.error.HTTPError as exc:
            log.error("Server rejected the request (%s). Check BRIDGE_TOKEN.", exc.code)
            backoff = min(backoff * 2, 120)
        except urllib.error.URLError as exc:
            log.warning("Server unreachable (%s); retrying in %ss", exc.reason, backoff)
            backoff = min(backoff * 2, 120)
        except Exception:
            log.exception("Unexpected error")
            backoff = min(backoff * 2, 120)

        if args.once:
            return 0
        time.sleep(backoff)


if __name__ == "__main__":
    sys.exit(main())
