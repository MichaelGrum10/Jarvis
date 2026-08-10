"""Self-improvement and self-debugging.

This is the AutoGPT-shaped part: give it a goal ("the calendar tool crashes on
all-day events, fix it" / "add a weather tool") and it plans, edits its own
source, runs the test suite, reads the failures, and iterates until green.

Safety rails, because this thing edits the code that runs it:
  * All work happens on a throwaway git branch, never on your working branch.
  * It cannot touch .env, .git internals, or anything outside the repo root.
  * Shell access is an allowlist of build/test commands, not arbitrary bash.
  * It never merges, never deploys, never restarts the server. When it finishes
    it hands you a branch and a diff, and you decide.
  * Disabled unless AUTONOMY_ENABLED=true.

A separate tool registry from the chat agent, so a normal conversation can never
wander into rewriting the codebase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings, get_settings
from ..llm.client import LLMError, get_llm

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 120_000
FORBIDDEN = (".env", ".git/", "data/", "__pycache__", ".venv", "node_modules")
ALLOWED_COMMANDS = {
    "pytest", "python", "python3", "ruff", "git", "ls", "cat", "grep", "find", "wc", "head", "tail",
}

SYSTEM = """You are Jarvis's self-improvement engine. You are modifying your own source code.

Repository root: {root}
Goal: {goal}

Work like a careful engineer:
1. Read before you write. Use list_files and read_file to understand the current code.
2. Make the smallest change that achieves the goal. Match the surrounding style.
3. Run the tests after every meaningful edit with run_tests.
4. If tests fail, read the actual error, form a hypothesis, and fix the real cause. Do not
   paper over a failure by weakening a test or wrapping things in try/except.
5. When the goal is met and tests pass, call finish with a summary.

Hard rules:
- Never edit .env, anything under .git, or files outside the repository.
- Never delete a test to make the suite pass.
- If the goal is impossible or unsafe, call finish with success=false and explain why.
- You have a limited number of steps. Spend them on real work, not narration.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in a directory of the repository, recursively.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory relative to repo root. Default '.'."},
                    "pattern": {"type": "string", "description": "Optional glob, e.g. '*.py'."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to repo root."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write a file, creating or fully replacing it. Always read the file first "
                "unless creating it new. Provide the complete final content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to repo root."},
                    "content": {"type": "string", "description": "Complete file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run the project's test suite and return the output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Optional path or -k expression."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run an allowed shell command in the repo. Allowed: "
                + ", ".join(sorted(ALLOWED_COMMANDS))
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The command line."}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "End the run. Call this once the goal is met, or if it cannot be.",
            "parameters": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "description": "Did you achieve the goal?"},
                    "summary": {"type": "string", "description": "What you changed and why."},
                },
                "required": ["success", "summary"],
            },
        },
    },
]


@dataclass
class AutonomyResult:
    success: bool = False
    summary: str = ""
    branch: str = ""
    diff: str = ""
    files_changed: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    steps: int = 0


class SandboxError(RuntimeError):
    pass


class Sandbox:
    """Filesystem + shell access, fenced to the repository."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def resolve(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise SandboxError(f"Path escapes the repository: {relative}") from exc
        rel = str(candidate.relative_to(self.root))
        for blocked in FORBIDDEN:
            if rel == blocked.rstrip("/") or rel.startswith(blocked):
                raise SandboxError(f"Access to '{rel}' is not permitted.")
        return candidate

    def list_files(self, path: str = ".", pattern: str = "") -> str:
        base = self.resolve(path or ".")
        if not base.is_dir():
            raise SandboxError(f"Not a directory: {path}")
        matches = base.rglob(pattern) if pattern else base.rglob("*")
        rows = []
        for item in sorted(matches):
            if not item.is_file():
                continue
            rel = str(item.relative_to(self.root))
            if any(b.rstrip("/") in rel for b in FORBIDDEN):
                continue
            rows.append(f"{rel} ({item.stat().st_size}b)")
            if len(rows) >= 300:
                rows.append("... truncated")
                break
        return "\n".join(rows) or "(empty)"

    def read_file(self, path: str) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise SandboxError(f"No such file: {path}")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise SandboxError(f"File too large to read: {path}")
        return target.read_text(encoding="utf-8", errors="replace")

    def write_file(self, path: str, content: str) -> str:
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.is_file()
        target.write_text(content, encoding="utf-8")
        return f"{'Updated' if existed else 'Created'} {path} ({len(content)} chars)"

    async def run(self, command: str, timeout: int = 180) -> str:
        parts = shlex.split(command)
        if not parts:
            raise SandboxError("Empty command.")
        binary = Path(parts[0]).name
        if binary not in ALLOWED_COMMANDS:
            raise SandboxError(
                f"'{binary}' is not on the allowlist. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}"
            )
        if any(token in command for token in ("&&", "||", ";", "|", ">", "<", "`", "$(")):
            raise SandboxError("Shell metacharacters are not allowed; run one command at a time.")

        proc = await asyncio.create_subprocess_exec(
            *parts,
            cwd=self.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            raise SandboxError(f"Command timed out after {timeout}s: {command}") from None
        output = stdout.decode(errors="replace")
        return f"exit={proc.returncode}\n{output[-8000:]}"


class AutonomyEngine:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.llm = get_llm()

    async def run(self, goal: str, on_log=None) -> AutonomyResult:
        result = AutonomyResult()

        if not self.settings.autonomy_enabled:
            result.summary = (
                "Autonomous mode is off. Set AUTONOMY_ENABLED=true in .env to let Jarvis "
                "modify its own code."
            )
            return result

        root = Path(self.settings.autonomy_repo_path).resolve()
        sandbox = Sandbox(root)

        def emit(line: str) -> None:
            log.info("[autonomy] %s", line)
            result.log.append(line)
            if on_log:
                on_log(line)

        # Branch isolation is the safety rail that makes everything else
        # recoverable. Without a git repo there is no branch, no diff and no way
        # to throw the work away — so refuse rather than edit files in place.
        if not (root / ".git").exists():
            result.summary = (
                f"{root} is not a git repository, so there's no way to isolate or undo "
                "changes. Refusing to edit code in place.\n\n"
                "In Docker: uncomment the './:/app/repo' volume in docker-compose.yml "
                "and restart. Outside Docker: point AUTONOMY_REPO_PATH at your checkout."
            )
            emit(result.summary)
            return result

        # Remembered so the checkout can be put back. Leaving the repository on
        # the work branch hijacks the user's own copy: their next `git pull`
        # asks the remote for a branch that only exists locally, and fails with
        # "couldn't find remote ref".
        _, original = await self._git(root, "rev-parse", "--abbrev-ref", "HEAD")
        original = original.strip()

        branch = await self._create_branch(root, goal, emit)
        if not branch:
            result.summary = (
                "Could not create a working branch, so changes could not be isolated. "
                "Nothing was modified. Check that git works in the repository and that "
                "there are no uncommitted changes blocking a checkout."
            )
            emit(result.summary)
            return result
        result.branch = branch

        messages = [
            {"role": "system", "content": SYSTEM.format(root=root, goal=goal)},
            {"role": "user", "content": goal},
        ]

        for step in range(1, self.settings.autonomy_max_iterations + 1):
            result.steps = step
            try:
                response = await self.llm.complete(messages, TOOLS, temperature=0.1)
            except LLMError as exc:
                result.summary = f"Model error: {exc}"
                emit(result.summary)
                await self._restore_branch(root, original, emit)
                break

            if not response.wants_tools:
                # No tool call and no finish — nudge it once rather than stalling.
                emit(f"step {step}: model replied without acting")
                messages.append({"role": "assistant", "content": response.content})
                messages.append(
                    {
                        "role": "user",
                        "content": "Use a tool or call finish. Do not reply with prose alone.",
                    }
                )
                continue

            messages.append(
                {
                    "role": "assistant",
                    "content": response.content or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in response.tool_calls
                    ],
                }
            )

            done = False
            for call in response.tool_calls:
                if call.name == "finish":
                    result.success = bool(call.arguments.get("success"))
                    result.summary = str(call.arguments.get("summary", ""))
                    emit(f"step {step}: finish — {result.summary[:200]}")
                    done = True
                    break

                emit(f"step {step}: {call.name}({self._brief(call.arguments)})")
                output = await self._execute(sandbox, call.name, call.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": output[:10000],
                    }
                )
                if call.name == "write_file":
                    path = str(call.arguments.get("path", ""))
                    if path and path not in result.files_changed:
                        result.files_changed.append(path)

            if done:
                break
        else:
            result.summary = (
                f"Hit the {self.settings.autonomy_max_iterations}-step limit without finishing. "
                "The branch has whatever partial work was done."
            )
            emit(result.summary)

        result.diff = await self._diff(root)
        if result.files_changed:
            await self._commit(root, goal, emit)
        else:
            emit("No files changed; nothing to commit.")
        await self._restore_branch(root, original, emit)
        return result

    async def _restore_branch(self, root: Path, original: str, emit) -> None:
        """Put the checkout back where it was, leaving the work branch behind.

        The branch is kept — reviewing it is the whole point of `propose` — but
        the working tree belongs to the user. Left on the work branch, their next
        update asks the remote for a branch that exists only locally and fails
        with "couldn't find remote ref", which looks like GitHub being down.
        """
        if not original or original == "HEAD":
            return
        code, out = await self._git(root, "checkout", original)
        if code != 0:
            emit(
                f"Could not return the checkout to {original}: {out.strip()[:160]}. "
                f"Run: git checkout {original}"
            )
        else:
            emit(f"Checkout returned to {original}.")

    async def _execute(self, sandbox: Sandbox, name: str, args: dict) -> str:
        try:
            if name == "list_files":
                return sandbox.list_files(args.get("path", "."), args.get("pattern", ""))
            if name == "read_file":
                return sandbox.read_file(args["path"])
            if name == "write_file":
                return sandbox.write_file(args["path"], args["content"])
            if name == "run_tests":
                command = self.settings.autonomy_test_command
                if args.get("target"):
                    command = f"{command} {args['target']}"
                return await sandbox.run(command, timeout=300)
            if name == "run_command":
                return await sandbox.run(args["command"])
            return f"Unknown tool '{name}'."
        except SandboxError as exc:
            return f"BLOCKED: {exc}"
        except KeyError as exc:
            return f"Missing argument: {exc}"
        except Exception as exc:
            log.exception("Autonomy tool %s failed", name)
            return f"ERROR {type(exc).__name__}: {exc}"

    @staticmethod
    def _brief(args: dict) -> str:
        parts = []
        for key, value in args.items():
            text = str(value)
            parts.append(f"{key}={text[:60] + '…' if len(text) > 60 else text}")
        return ", ".join(parts)[:200]

    async def _git(self, root: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=root,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            return 1, "git timed out"
        return proc.returncode or 0, out.decode(errors="replace")

    async def _prepare_git(self, root: Path, emit) -> None:
        """Make git willing to work in a repository it did not create.

        The checkout is bind-mounted from the host, so it belongs to the host
        user while this process runs as root. Git refuses that outright —
        "detected dubious ownership" — and every operation fails before doing
        anything. The image marks /app safe, but the mount lands at /app/repo,
        and the path is configurable anyway, so it is added here where the real
        value is known.

        A commit identity is set for the same reason: without one, git aborts
        the commit after the work is already done.
        """
        await self._git(root, "config", "--global", "--add", "safe.directory", str(root))
        code, _ = await self._git(root, "config", "user.email")
        if code != 0:
            await self._git(root, "config", "user.email", "jarvis@localhost")
            await self._git(root, "config", "user.name", "Jarvis")
            emit("Set a local commit identity for this repository.")

    async def _create_branch(self, root: Path, goal: str, emit) -> str:
        await self._prepare_git(root, emit)
        slug = re.sub(r"[^a-z0-9]+", "-", goal.lower())[:40].strip("-") or "task"
        import time

        branch = f"{self.settings.autonomy_branch_prefix}/{slug}-{int(time.time())}"
        code, out = await self._git(root, "checkout", "-b", branch)
        if code != 0:
            emit(f"Could not create branch: {out.strip()[:300]}")
            return ""
        emit(f"Working on branch {branch}")
        return branch

    async def _diff(self, root: Path) -> str:
        _, out = await self._git(root, "diff", "--stat")
        _, full = await self._git(root, "diff")
        return (out + "\n\n" + full)[:20000]

    async def _commit(self, root: Path, goal: str, emit) -> None:
        await self._git(root, "add", "-A")
        code, out = await self._git(
            root, "commit", "-m", f"jarvis: {goal[:70]}", "-m", "Autonomous change. Review before merging."
        )
        emit("Committed." if code == 0 else f"Commit skipped: {out.strip()[:200]}")


_engine: AutonomyEngine | None = None


def get_engine() -> AutonomyEngine:
    global _engine
    if _engine is None:
        _engine = AutonomyEngine()
    return _engine
