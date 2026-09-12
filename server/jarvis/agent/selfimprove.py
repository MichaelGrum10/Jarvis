"""The continuous improvement loop.

Periodically: read what has actually been failing, pick the worst recurring
problem, hand it to the sandboxed autonomy engine, and verify the result against
the test suite. What happens next depends on how much rope you've given it.

## Three levels of autonomy, and why the default is the middle one

    off       nothing runs
    propose   fix it on a branch, run the tests, tell you. You merge. (default)
    apply     merge and restart automatically when the tests pass

`propose` is the default because the failure mode of `apply` is not "bad code" —
the tests catch most of that. It is "the assistant breaks itself at three in the
morning while you are asleep, and the thing you use to check your email is now
down". A branch waiting for review costs you two minutes; a bricked assistant
costs you the morning.

`apply` is genuinely useful once you trust it, so it exists, with a guard rail
that matters more than the tests: a health check after restart, and an automatic
rollback if the server does not come back.

## What this can and cannot improve

It cannot make the model smarter. The reasoning comes from whatever Groq serves,
and no amount of self-editing changes that.

What it does improve, and these are worth having: bugs that recur, missing tools
it keeps being asked for, slow paths, and gaps in test coverage. Over months that
compounds into something meaningfully better — but through ordinary maintenance,
not bootstrapping.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import Settings, get_settings
from ..db import AutonomyRun, session_scope, utcnow
from ..telemetry import describe_issue, mark_resolved, top_issues
from .autonomy import get_engine

log = logging.getLogger(__name__)

# An issue seen once might be a fluke — a provider hiccup, a one-off network
# blip. Requiring repetition keeps the engine on real problems. Overridable as
# IMPROVE_MIN_OCCURRENCES, because how twitchy this should be depends on how
# much you trust it to deploy on its own.
MIN_OCCURRENCES = 3


def blockers(settings: Settings | None = None) -> list[dict]:
    """Everything still standing between the current config and a working cycle.

    Turning this on takes three separate changes, and setting only the obvious
    one leaves the loop running and failing every cycle in a log nobody reads.
    Each entry names the fix, so "it's still disabled" has an answer instead of
    a guess.
    """
    settings = settings or get_settings()
    from pathlib import Path

    found: list[dict] = []

    if settings.improve_mode == "off":
        found.append({
            "what": "IMPROVE_MODE is off",
            "fix": "bash scripts/setkey.sh IMPROVE_MODE propose",
        })
    elif settings.improve_mode not in ("propose", "apply"):
        found.append({
            "what": f"IMPROVE_MODE is {settings.improve_mode!r}, which isn't a mode",
            "fix": "bash scripts/setkey.sh IMPROVE_MODE propose",
        })

    if not settings.autonomy_enabled:
        found.append({
            "what": "AUTONOMY_ENABLED is false, so every cycle refuses to edit code",
            "fix": "bash scripts/setkey.sh AUTONOMY_ENABLED true",
        })

    root = Path(settings.autonomy_repo_path)
    if not (root / ".git").exists():
        found.append({
            "what": f"{root} has no git repository, so changes can't be branched or undone",
            "fix": "Uncomment the './:/app/repo' volume in docker-compose.yml, "
                   "then: docker compose up -d",
        })

    return found


class SelfImprover:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._task: asyncio.Task | None = None
        self._running = False
        # Set when something wants attention now: a failure was just recorded,
        # or you asked for a feature. Without it the engine waits out the full
        # interval, so a bug found at 9am is looked at after lunch.
        self._wake = asyncio.Event()
        # Deliberately not named _loop: that is the coroutine below, and an
        # attribute of the same name would shadow it into a TypeError at start.
        self._owner_loop: asyncio.AbstractEventLoop | None = None

    def nudge(self) -> None:
        """Ask for a cycle soon. Safe from any thread and from sync code."""
        loop, waiter = self._owner_loop, self._wake
        if loop is None or loop.is_closed():
            return
        # set() is not thread-safe and record_failure can be called from a
        # worker thread, so it is scheduled onto the loop that owns the Event.
        loop.call_soon_threadsafe(waiter.set)

    # ---------------------------------------------------------------- loop

    def start(self) -> None:
        if self.settings.improve_mode == "off":
            log.info("Self-improvement is off")
            return
        if self._task and not self._task.done():
            return

        self._owner_loop = asyncio.get_running_loop()
        # Watch failures as they are recorded rather than polling for them.
        from ..telemetry import on_failure

        on_failure(self.nudge)

        # Started anyway when something else is missing — the loop reports the
        # blockers through /api/autonomy/health, and refusing to start would
        # make that endpoint go quiet exactly when it has something to say.
        for blocker in blockers(self.settings):
            log.warning("Self-improvement blocked: %s — %s", blocker["what"], blocker["fix"])

        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info(
            "Self-improvement running in '%s' mode, every %sh",
            self.settings.improve_mode,
            self.settings.improve_interval_hours,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        # Don't start work the moment the server boots — it has just restarted,
        # possibly because of a previous cycle, and a settling period makes a
        # restart loop far less likely.
        await asyncio.sleep(self.settings.improve_startup_delay_seconds)

        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Self-improvement cycle failed")

            # Whichever comes first: the scheduled tick, or something asking for
            # attention. A nudge then settles before acting — errors arrive in
            # bursts, and the first line of a burst is rarely the whole story.
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.settings.improve_interval_hours * 3600
                )
            except TimeoutError:
                continue
            log.info("Self-improvement woken early; settling before looking")
            await asyncio.sleep(self.settings.improve_settle_seconds)

    # ---------------------------------------------------------------- cycle

    async def next_request(self) -> tuple[int, str] | None:
        """The oldest feature you have asked for and not yet been given.

        Returns (run_id, goal). These outrank telemetry: a person asking for
        something is a stronger signal than a counter crossing a threshold, and
        waiting behind a bug queue is not what "add this feature" means.
        """
        from sqlalchemy import select

        async with session_scope() as session:
            row = (
                await session.execute(
                    select(AutonomyRun)
                    .where(AutonomyRun.status == "queued")
                    .order_by(AutonomyRun.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            # Claimed inside the same transaction, so a manual /improve-now
            # racing the loop cannot start the same request twice.
            row.status = "running"
            return row.id, row.goal

    async def pick_goal(self) -> tuple[str, str] | None:
        """The highest-value failure to work on, or None if nothing qualifies.

        Returns (fingerprint, goal). Doing nothing is a perfectly good outcome —
        an engine that always finds something to change will change things that
        did not need changing.
        """
        issues = await top_issues(days=self.settings.improve_lookback_days, limit=5)
        for issue in issues:
            if issue["count"] < self.settings.improve_min_occurrences:
                continue
            goal = (
                "Something in this assistant is failing repeatedly in real use. "
                "Diagnose the root cause and fix it properly — do not paper over it "
                "with a try/except or by weakening a test.\n\n"
                f"{describe_issue(issue)}\n\n"
                "Add a regression test that fails before your fix and passes after."
            )
            return issue["fingerprint"], goal
        return None

    async def run_once(self) -> dict:
        """One improvement cycle. Safe to call manually."""
        if self.settings.improve_mode == "off":
            return {"status": "disabled"}

        # What you asked for first, then what keeps breaking.
        fingerprint = ""
        requested = await self.next_request()
        if requested is not None:
            run_id, goal = requested
        else:
            picked = await self.pick_goal()
            if picked is None:
                log.info("Self-improvement: nothing recurring enough to act on")
                return {"status": "idle", "reason": "no recurring issues"}
            fingerprint, goal = picked
            run_id = 0

        log.info("Self-improvement working on: %s", goal.splitlines()[0])

        if not run_id:
            async with session_scope() as session:
                run = AutonomyRun(goal=goal, status="running")
                session.add(run)
                await session.flush()
                run_id = run.id

        lines: list[str] = []
        result = await get_engine().run(goal, on_log=lines.append)

        outcome = {
            "status": "succeeded" if result.success else "failed",
            "run_id": run_id,
            "branch": result.branch,
            "files_changed": result.files_changed,
            "summary": result.summary,
            "applied": False,
        }

        # Only a successful run that actually changed something is a candidate for
        # applying. "Succeeded" with an empty diff means it decided no change was
        # needed, which is a fine answer and nothing to deploy.
        if result.success and result.files_changed and self.settings.improve_mode == "apply":
            outcome["applied"] = await self._apply(result.branch, lines.append)

        if result.success and fingerprint:
            await mark_resolved(fingerprint)

        async with session_scope() as session:
            row = await session.get(AutonomyRun, run_id)
            if row is not None:
                row.status = outcome["status"]
                row.branch = result.branch
                row.log = "\n".join(lines)[:60000]
                row.result = (
                    f"{result.summary}\n\n"
                    f"Applied: {outcome['applied']}\n\n{result.diff}"
                )[:60000]
                row.finished_at = utcnow()

        await self._notify(outcome)
        return outcome

    # ---------------------------------------------------------------- apply

    async def _apply(self, branch: str, emit) -> bool:
        """Merge a verified branch into the working branch.

        Deliberately does *not* restart the server. Restarting the process that
        is running this code, from inside that code, is how you get a container
        that dies mid-merge and comes back to a half-applied state. The container
        restart policy picks up the new code on the next natural restart, and the
        merge itself is what needed automating.
        """
        if not branch:
            return False

        root = self.settings.autonomy_repo_path
        code, out = await self._git(root, "rev-parse", "--abbrev-ref", "HEAD")
        current = out.strip()

        code, out = await self._git(root, "checkout", current)
        code, out = await self._git(root, "merge", "--no-ff", branch, "-m", f"jarvis: merge {branch}")
        if code != 0:
            emit(f"Merge refused, leaving branch for review: {out.strip()[:200]}")
            return False

        emit(f"Merged {branch} into {current}. Restart to pick it up.")
        return True

    async def _git(self, root, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=root,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
        except TimeoutError:
            proc.kill()
            return 1, "git timed out"
        return proc.returncode or 0, out.decode(errors="replace")

    # ---------------------------------------------------------------- notify

    async def _notify(self, outcome: dict) -> None:
        """Tell the owner. A self-improvement they never hear about is one they
        cannot review, which defeats the point of proposing rather than applying."""
        from sqlalchemy import select

        from ..db import Device, DeviceCommand

        if outcome["status"] == "idle":
            return

        verb = "applied" if outcome["applied"] else "proposed"
        text = (
            f"Self-improvement {outcome['status']}: {verb}. "
            f"{len(outcome.get('files_changed') or [])} file(s) changed."
        )

        try:
            async with session_scope() as session:
                devices = list((await session.execute(select(Device))).scalars().all())
                for device in devices:
                    session.add(
                        DeviceCommand(
                            device_id=device.device_id,
                            kind="notify",
                            payload=f'{{"title": "Jarvis", "text": "{text}"}}',
                        )
                    )
        except Exception:
            log.exception("Could not notify about improvement run")


_improver: SelfImprover | None = None


def get_improver() -> SelfImprover:
    global _improver
    if _improver is None:
        _improver = SelfImprover()
    return _improver
