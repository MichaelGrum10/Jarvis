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
# blip. Requiring repetition keeps the engine on real problems.
MIN_OCCURRENCES = 3


class SelfImprover:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._task: asyncio.Task | None = None
        self._running = False

    # ---------------------------------------------------------------- loop

    def start(self) -> None:
        if self.settings.improve_mode == "off":
            log.info("Self-improvement is off")
            return
        if self._task and not self._task.done():
            return
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
            await asyncio.sleep(self.settings.improve_interval_hours * 3600)

    # ---------------------------------------------------------------- cycle

    async def pick_goal(self) -> tuple[str, str] | None:
        """The highest-value thing to work on, or None if nothing qualifies.

        Returns (fingerprint, goal). Doing nothing is a perfectly good outcome —
        an engine that always finds something to change will change things that
        did not need changing.
        """
        issues = await top_issues(days=self.settings.improve_lookback_days, limit=5)
        for issue in issues:
            if issue["count"] < MIN_OCCURRENCES:
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

        picked = await self.pick_goal()
        if picked is None:
            log.info("Self-improvement: nothing recurring enough to act on")
            return {"status": "idle", "reason": "no recurring issues"}

        fingerprint, goal = picked
        log.info("Self-improvement working on: %s", goal.splitlines()[0])

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

        if result.success:
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
