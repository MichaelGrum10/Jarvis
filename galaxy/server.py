#!/usr/bin/env python3
"""Serve viewer/ on 127.0.0.1:4700, and answer questions from the notes.

Standard library only. That is a project constraint rather than a preference
about HTTP clients: this runs from a systemd unit calling /usr/bin/python3
directly, and Ubuntu 24.04 refuses system-wide pip installs (PEP 668), so a
vendor SDK would mean a venv and a different ExecStart. Groq speaks the
OpenAI-compatible chat-completions shape — two headers and a JSON body — and
urllib is enough for that.

Bound to loopback deliberately and not configurably. This box has a public IP
and this page has no authentication, so binding 0.0.0.0 would publish the full
text of every note. Reach it over an SSH tunnel:

    ssh -N -L 4700:127.0.0.1:4700 jarvis

There is no --host flag on purpose. A flag is an invitation, and the only
reason anyone reaches for it is the thing that must not happen.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import build

HOST = "127.0.0.1"
PORT = 4700
HERE = Path(__file__).resolve().parent
VIEWER = HERE / "viewer"
CONFIG = HERE / "config.json"

API_URL = "https://api.groq.com/openai/v1/chat/completions"
API_TIMEOUT = 60

TOP_NOTES = 6              # how many notes are put in front of the model
MAX_TOKENS = 1024          # a 2-3 sentence answer needs nowhere near this
RATE_LIMIT = 20            # calls
RATE_WINDOW = 60.0         # seconds
MAX_QUESTION = 2000        # characters
HISTORY_TURNS = 6          # 3 exchanges; enough for "and what about the other one?"
MAX_SESSIONS = 50

CONFIG_TEMPLATE = {"api_key": "PUT-YOUR-KEY-HERE", "model": "openai/gpt-oss-120b"}
# Where the key already lives: the assistant's own .env, one directory up.
SHARED_ENV = HERE.parent / ".env"
SHARED_ENV_KEY = "GROQ_API_KEY"

# Common enough to be noise in a personal vault, and they drag every note into
# every answer if left in.
STOPWORDS = frozenset("""
a an the and or but if then than that this these those is are was were be been being
i me my we our you your he she it they them of in on at to for from with without by
as about into over after before under above do does did done have has had will would
can could should may might must not no yes what when where who whom which why how
""".split())

SYSTEM_PROMPT = """You are answering questions about a specific person's private \
markdown notes. Below are the notes most relevant to their question.

Rules, in order of importance:

1. Answer ONLY from the notes provided. They are the whole of what you know here.
2. If the notes do not cover the question, say so plainly in one sentence. Do not \
fill the gap from general knowledge, and do not soften it — "your notes don't cover \
that" is a useful answer and a guess is not.
3. Keep it to 2-3 sentences.
4. Refer to notes by their title when it helps them find it again.

The notes follow."""


# --------------------------------------------------------------- configuration


def load_config() -> dict:
    """Read config.json, creating a 0600 template on first run.

    The file lives in the project root and never inside viewer/, so it is not
    on any path the static handler can reach even before the traversal guard.
    """
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(CONFIG_TEMPLATE, indent=2) + "\n", encoding="utf-8")
        os.chmod(CONFIG, 0o600)
        print(f"Created {CONFIG} — put your API key in it (chmod 600 already set)")

    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"config.json is not readable JSON: {exc}") from None

    key = (data.get("api_key") or "").strip()
    if not key or key == CONFIG_TEMPLATE["api_key"]:
        # Fall back to the assistant's own key rather than making the user keep
        # the same secret in two files. Two copies means two things to rotate
        # and one of them silently going stale.
        key = key_from_env()
    if not key:
        raise RuntimeError(
            f"No API key. Put one in config.json, or set {SHARED_ENV_KEY} in {SHARED_ENV}."
        )
    return {"api_key": key, "model": data.get("model") or CONFIG_TEMPLATE["model"]}


def key_from_env() -> str:
    """Read GROQ_API_KEY out of the assistant's .env, if it is there."""
    try:
        for line in SHARED_ENV.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == SHARED_ENV_KEY:
                return value.strip().strip("\"'")
    except OSError:
        pass
    return ""


# ------------------------------------------------------------------- the notes


class Notes:
    """The vault, rescanned when it changes underneath us.

    Syncthing rewrites this folder at any time, so a snapshot taken at startup
    goes stale silently. The staleness check is file count plus newest mtime —
    the same signal build.py fingerprints — which is cheap enough to run per
    question and catches every change that matters.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nodes: list[dict] = []
        self._generation = ""
        self._signature: tuple = ()

    def current(self) -> tuple[str, list[dict]]:
        root = build.notes_dir()
        try:
            paths = [p for p in root.rglob("*.md")
                     if not any(part.startswith(".") for part in p.relative_to(root).parts)]
            signature = (len(paths), max((p.stat().st_mtime for p in paths), default=0.0))
        except OSError:
            signature = ()

        with self._lock:
            if signature != self._signature or not self._nodes:
                self._nodes = build.scan(root)
                self._generation = build.generation(self._nodes)
                self._signature = signature
            return self._generation, self._nodes

    def rebuild(self) -> dict:
        """Re-run the build and rewrite viewer/graph-data.js."""
        root = build.notes_dir()
        nodes = build.scan(root)
        links = build.link(nodes)
        build.write(nodes, links, VIEWER / "graph-data.js")
        with self._lock:
            self._nodes = nodes
            self._generation = build.generation(nodes)
            self._signature = ()      # force a re-stat on the next question
        return {
            "notes": len(nodes),
            "links": len(links),
            "generation": self._generation,
        }


NOTES = Notes()


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']{3,}", text.lower()) if w not in STOPWORDS}


def rank(question: str, nodes: list[dict], limit: int = TOP_NOTES) -> list[dict]:
    """The notes most likely to answer this, title matches weighted.

    A title match is a far stronger signal than one mention buried in a long
    note; without the weighting a single long note wins on surface area alone
    and crowds out the short note that actually answers the question.
    """
    wanted = words(question)
    if not wanted:
        return []

    scored = []
    for node in nodes:
        title_hits = len(wanted & words(node["label"]))
        body_hits = len(wanted & words(node["_text"]))
        if not (title_hits or body_hits):
            continue
        scored.append((title_hits * 5 + body_hits, node))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["label"]))
    return [node for _score, node in scored[:limit]]


# ------------------------------------------------------------------ rate limit


class RateLimit:
    """A sliding window over the last minute, shared by every caller.

    Global rather than per-session on purpose: the point is protecting the API
    bill, and a stray script can mint new session ids for free.
    """

    def __init__(self, calls: int, window: float) -> None:
        self.calls, self.window = calls, window
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def take(self) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            while self._hits and now - self._hits[0] > self.window:
                self._hits.popleft()
            if len(self._hits) >= self.calls:
                return False, self.window - (now - self._hits[0])
            self._hits.append(now)
            return True, 0.0


LIMITER = RateLimit(RATE_LIMIT, RATE_WINDOW)


# --------------------------------------------------------------------- history

SESSIONS: dict[str, deque] = {}
SESSION_LOCK = threading.Lock()


def history_for(session: str) -> deque:
    with SESSION_LOCK:
        if session not in SESSIONS:
            if len(SESSIONS) >= MAX_SESSIONS:
                SESSIONS.pop(next(iter(SESSIONS)))     # oldest out
            SESSIONS[session] = deque(maxlen=HISTORY_TURNS)
        return SESSIONS[session]


# ------------------------------------------------------------------- the model


def ask(question: str, notes: list[dict], history: deque, config: dict) -> str:
    """One Messages API call. Raw HTTP because the project is stdlib-only."""
    if notes:
        context = "\n\n---\n\n".join(
            f"## {n['label']}\n(folder: {n['group']})\n\n{n['excerpt']}" for n in notes
        )
    else:
        context = "(No notes matched this question.)"

    # OpenAI-compatible shape: the system prompt is the first message, not a
    # top-level field the way Anthropic's Messages API has it.
    messages = (
        [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{context}"}]
        + list(history)
        + [{"role": "user", "content": question}]
    )

    body = json.dumps({
        "model": config["model"],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.2,     # this is recall, not writing; keep it close to the notes
        "messages": messages,
    }).encode("utf-8")

    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {config['api_key']}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        # Never let the key reach a log or a browser, whatever the API echoed.
        detail = detail.replace(config["api_key"], "<redacted>")
        if exc.code == 429:
            # Groq's free tier meters tokens per minute, and this shares that
            # bucket with the assistant itself. Say so — a raw 429 body reads
            # like a bug rather than a budget.
            raise RuntimeError(
                "Groq is rate limited right now — its free tier meters tokens per "
                "minute, and Jarvis shares the same bucket. Try again shortly."
            ) from None
        raise RuntimeError(f"Groq returned {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Groq: {exc.reason}") from None

    choices = payload.get("choices") or []
    if not choices:
        return "(no answer came back)"
    text = (choices[0].get("message") or {}).get("content") or ""
    return text.strip() or "(empty answer)"


# ---------------------------------------------------------------- the handler


class Handler(SimpleHTTPRequestHandler):
    """Static files out of viewer/, plus /chat and /rebuild."""

    server_version = "galaxy"

    # --- static, with an explicit refusal to leave viewer/ ---

    def translate_path(self, path: str) -> str:
        """Resolve a request path, and refuse anything that escapes viewer/.

        SimpleHTTPRequestHandler already normalises away "..", so this is belt
        and braces — but config.json holds an API key, and "the base class
        probably handles it" is not the standard that deserves. The check is on
        the *resolved* path, so it holds for encoded traversal (%2e%2e%2f),
        doubled encoding, and symlinks pointing out of the tree.
        """
        resolved = Path(super().translate_path(path)).resolve()
        try:
            resolved.relative_to(VIEWER.resolve())
        except ValueError:
            return str(VIEWER / "__forbidden__")
        return str(resolved)

    def end_headers(self) -> None:
        # graph-data.js is rewritten whenever the vault changes; a cached copy
        # shows yesterday's galaxy and looks like the scanner is broken.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # --- helpers ---

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 64_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    # --- routes ---

    def do_POST(self) -> None:      # noqa: N802  (http.server's naming)
        route = urllib.parse.urlparse(self.path).path
        if route == "/chat":
            self._chat()
        elif route == "/rebuild":
            self._rebuild()
        else:
            self._json(404, {"error": "No such endpoint."})

    def _rebuild(self) -> None:
        try:
            self._json(200, NOTES.rebuild())
        except Exception as exc:                       # noqa: BLE001
            self._json(500, {"error": f"Rebuild failed: {exc}"})

    def _chat(self) -> None:
        data = self._read_json()
        question = (data.get("question") or "").strip()[:MAX_QUESTION]
        session = str(data.get("session") or "default")[:64]
        seen = str(data.get("generation") or "")

        if not question:
            return self._json(400, {"error": "Ask something."})

        allowed, wait = LIMITER.take()
        if not allowed:
            return self._json(429, {
                "error": f"Rate limit: {RATE_LIMIT} questions a minute. "
                         f"Try again in {wait:.0f}s."
            })

        try:
            config = load_config()
        except RuntimeError as exc:
            return self._json(503, {"error": str(exc)})

        generation, nodes = NOTES.current()
        chosen = rank(question, nodes)
        history = history_for(session)

        try:
            answer = ask(question, chosen, history, config)
        except RuntimeError as exc:
            return self._json(502, {"error": str(exc)})

        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})

        self._json(200, {
            "answer": answer,
            "nodes": [n["id"] for n in chosen],
            "generation": generation,
            # Node ids are array positions. If the vault changed since the page
            # loaded, the ids above refer to a graph the browser does not have,
            # and flying the camera to them would land on unrelated notes.
            "stale": bool(seen) and seen != generation,
        })


def main() -> int:
    if not VIEWER.is_dir():
        print(f"No viewer directory at {VIEWER}", file=sys.stderr)
        return 1
    if not (VIEWER / "graph-data.js").is_file():
        print("warning: viewer/graph-data.js missing — run build.py first", file=sys.stderr)
    try:
        load_config()
    except RuntimeError as exc:
        # Not fatal: the galaxy itself works without a key, only /chat needs one.
        print(f"note: {exc}", file=sys.stderr)

    # Threading matters here: an Anthropic call can take several seconds, and a
    # single-threaded server would stall every static request behind it.
    handler = partial(Handler, directory=str(VIEWER))
    server = ThreadingHTTPServer((HOST, PORT), handler)

    print(f"Serving {VIEWER} on http://{HOST}:{PORT} (loopback only)")
    print(f"From your Mac:  ssh -N -L {PORT}:127.0.0.1:{PORT} jarvis")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
