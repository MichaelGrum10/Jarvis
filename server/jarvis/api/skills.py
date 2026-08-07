"""Manage skills — the named routines Jarvis runs on request."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import Skill, session_scope
from ..security import CurrentDevice

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    triggers: str = Field("", max_length=500)
    instruction: str = Field(min_length=10, max_length=4000)
    enabled: bool = True


def render(row: Skill) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "triggers": row.trigger_list,
        "instruction": row.instruction,
        "enabled": bool(row.enabled),
        "builtin": bool(row.builtin),
        "uses": row.uses,
        "last_used": row.last_used.isoformat() if row.last_used else None,
    }


@router.get("")
async def list_skills(device: CurrentDevice):
    async with session_scope() as session:
        rows = list((await session.execute(select(Skill).order_by(Skill.name))).scalars().all())
    return {"skills": [render(r) for r in rows]}


@router.post("")
async def create_skill(body: SkillIn, device: CurrentDevice):
    async with session_scope() as session:
        clash = (
            await session.execute(select(Skill).where(Skill.name == body.name))
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(409, f"A skill called '{body.name}' already exists.")
        row = Skill(
            name=body.name,
            triggers=body.triggers,
            instruction=body.instruction,
            enabled=int(body.enabled),
        )
        session.add(row)
        await session.flush()
        return render(row)


@router.put("/{skill_id}")
async def update_skill(skill_id: int, body: SkillIn, device: CurrentDevice):
    async with session_scope() as session:
        row = await session.get(Skill, skill_id)
        if row is None:
            raise HTTPException(404, "No such skill.")
        row.name = body.name
        row.triggers = body.triggers
        row.instruction = body.instruction
        row.enabled = int(body.enabled)
        # An edited builtin becomes the user's own, so a later version of the
        # shipped default never quietly overwrites what they wrote.
        row.builtin = 0
        return render(row)


@router.delete("/{skill_id}")
async def delete_skill(skill_id: int, device: CurrentDevice):
    async with session_scope() as session:
        row = await session.get(Skill, skill_id)
        if row is None:
            raise HTTPException(404, "No such skill.")
        await session.delete(row)
    return {"deleted": skill_id}


@router.post("/restore-builtins")
async def restore_builtins(device: CurrentDevice):
    """Reinstate any shipped skills that were deleted."""
    from ..skills import ensure_builtins

    await ensure_builtins()
    async with session_scope() as session:
        rows = list((await session.execute(select(Skill).order_by(Skill.name))).scalars().all())
    return {"skills": [render(r) for r in rows]}
