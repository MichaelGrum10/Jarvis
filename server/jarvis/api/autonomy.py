"""Endpoints for the self-improvement engine.

Runs are fire-and-forget background tasks; the UI polls for log lines and the
final diff. Nothing is merged automatically — you get a branch to review.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..agent.autonomy import get_engine
from ..config import Settings, get_settings
from ..db import AutonomyRun, session_scope, utcnow
from ..security import CurrentDevice

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/autonomy", tags=["autonomy"])


class RunRequest(BaseModel):
    goal: str = Field(min_length=8, max_length=4000)


async def _execute(run_id: int, goal: str) -> None:
    lines: list[str] = []

    def collect(line: str) -> None:
        lines.append(line)

    async with session_scope() as session:
        row = await session.get(AutonomyRun, run_id)
        if row:
            row.status = "running"

    try:
        result = await get_engine().run(goal, on_log=collect)
        status = "succeeded" if result.success else "failed"
        summary = result.summary
        branch = result.branch
        diff = result.diff
        files = result.files_changed
    except Exception as exc:
        log.exception("Autonomy run %s crashed", run_id)
        status, summary, branch, diff, files = "failed", f"Crashed: {exc}", "", "", []

    async with session_scope() as session:
        row = await session.get(AutonomyRun, run_id)
        if row:
            row.status = status
            row.branch = branch
            row.log = "\n".join(lines)[:60000]
            row.result = (
                f"{summary}\n\nFiles changed: {', '.join(files) or 'none'}\n\n{diff}"
            )[:60000]
            row.finished_at = utcnow()


@router.post("/run")
async def start_run(
    body: RunRequest, device: CurrentDevice, settings: Settings = Depends(get_settings)
):
    if not settings.autonomy_enabled:
        raise HTTPException(
            403,
            "Autonomous mode is disabled. Set AUTONOMY_ENABLED=true in .env and restart to "
            "let Jarvis modify its own code.",
        )
    async with session_scope() as session:
        row = AutonomyRun(goal=body.goal, status="queued")
        session.add(row)
        await session.flush()
        run_id = row.id

    asyncio.create_task(_execute(run_id, body.goal))
    return {"run_id": run_id, "status": "queued"}


@router.get("/runs")
async def list_runs(device: CurrentDevice, limit: int = 20):
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(AutonomyRun).order_by(AutonomyRun.id.desc()).limit(limit)
                )
            ).scalars().all()
        )
    return {
        "runs": [
            {
                "id": r.id,
                "goal": r.goal,
                "status": r.status,
                "branch": r.branch,
                "created_at": r.created_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            }
            for r in rows
        ]
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: int, device: CurrentDevice):
    async with session_scope() as session:
        row = await session.get(AutonomyRun, run_id)
        if row is None:
            raise HTTPException(404, "Run not found.")
        return {
            "id": row.id,
            "goal": row.goal,
            "status": row.status,
            "branch": row.branch,
            "log": row.log,
            "result": row.result,
            "created_at": row.created_at.isoformat(),
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        }
