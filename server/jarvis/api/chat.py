"""Chat endpoints — streaming and non-streaming."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import confirm as confirmations
from ..agent.loop import Agent, history_from_rows
from ..config import Settings, get_settings
from ..db import Conversation, Device, Message, load_history, session_scope, utcnow
from ..security import CurrentDevice
from ..tools.base import ToolContext

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: int | None = None
    lat: float | None = None
    lon: float | None = None
    timezone: str = ""


async def _prepare(body: ChatRequest, device: dict, settings: Settings):
    device_id = device.get("device_id", "")

    async with session_scope() as session:
        # Fresh coordinates from the client win; otherwise fall back to the last
        # location this device reported, so a quick question still knows where you are.
        lat, lon = body.lat, body.lon
        row = (
            await session.execute(select(Device).where(Device.device_id == device_id))
        ).scalar_one_or_none()
        if row is not None:
            if lat is not None and lon is not None:
                row.lat, row.lon, row.location_at = lat, lon, utcnow()
            elif row.lat is not None:
                lat, lon = row.lat, row.lon
            row.last_seen = utcnow()

        if body.conversation_id:
            conversation = await session.get(Conversation, body.conversation_id)
            if conversation is None:
                raise HTTPException(404, "Conversation not found.")
        else:
            conversation = Conversation(title=body.message[:80])
            session.add(conversation)
            await session.flush()

        conversation_id = conversation.id
        history = history_from_rows(await load_history(session, conversation_id))
        session.add(Message(conversation_id=conversation_id, role="user", content=body.message))
        conversation.updated_at = utcnow()

    ctx = ToolContext(
        device_id=device_id,
        lat=lat,
        lon=lon,
        timezone=body.timezone or settings.timezone,
        conversation_id=conversation_id,
    )
    return conversation_id, history, ctx


@router.post("")
async def chat(
    body: ChatRequest, device: CurrentDevice, settings: Settings = Depends(get_settings)
):
    conversation_id, history, ctx = await _prepare(body, device, settings)
    outcome = await Agent(settings).run(body.message, history, ctx)

    async with session_scope() as session:
        session.add(
            Message(
                conversation_id=conversation_id,
                role="assistant",
                content=outcome.reply or outcome.error,
                extra=json.dumps(
                    {"tools": outcome.tool_calls, "displays": outcome.displays}, default=str
                ),
            )
        )

    if outcome.error and not outcome.reply:
        raise HTTPException(502, outcome.error)
    return {
        "conversation_id": conversation_id,
        "reply": outcome.reply,
        "tools": outcome.tool_calls,
        "displays": outcome.displays,
    }


@router.post("/stream")
async def chat_stream(
    body: ChatRequest, device: CurrentDevice, settings: Settings = Depends(get_settings)
):
    conversation_id, history, ctx = await _prepare(body, device, settings)

    async def generate():
        yield f"data: {json.dumps({'type': 'start', 'conversation_id': conversation_id})}\n\n"
        reply, tools, displays = "", [], []
        try:
            async for event in Agent(settings).stream(body.message, history, ctx):
                if event.type == "final":
                    reply = event.data.get("reply", "")
                elif event.type == "tool_end":
                    tools.append({k: v for k, v in event.data.items() if k != "display"})
                    if event.data.get("display"):
                        displays.append(event.data["display"])
                yield event.sse()
        except Exception as exc:
            log.exception("Streaming turn failed")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            if reply:
                async with session_scope() as session:
                    session.add(
                        Message(
                            conversation_id=conversation_id,
                            role="assistant",
                            content=reply,
                            extra=json.dumps({"tools": tools, "displays": displays}, default=str),
                        )
                    )
            yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/conversations")
async def conversations(device: CurrentDevice, limit: int = 30):
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
                )
            ).scalars().all()
        )
    return {
        "conversations": [
            {"id": c.id, "title": c.title, "updated_at": c.updated_at.isoformat()} for c in rows
        ]
    }


@router.get("/conversations/{conversation_id}")
async def conversation_detail(conversation_id: int, device: CurrentDevice):
    async with session_scope() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(404, "Conversation not found.")
        rows = await load_history(session, conversation_id, limit=200)
    return {
        "id": conversation.id,
        "title": conversation.title,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "at": m.created_at.isoformat(),
                "displays": m.meta.get("displays", []),
            }
            for m in rows
        ],
    }


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: int, device: CurrentDevice):
    async with session_scope() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(404, "Conversation not found.")
        for row in await load_history(session, conversation_id, limit=10000):
            await session.delete(row)
        await session.delete(conversation)
    return {"deleted": conversation_id}


# ------------------------------------------------------------ confirmations
#
# The other half of the gate in tools/base.py. A confirm-flagged tool parks its
# call and shows a card; these two endpoints are the only way it ever runs.


@router.get("/confirm/{action_id}")
async def confirm_peek(action_id: str, device: CurrentDevice):
    pending = confirmations.peek(action_id)
    if pending is None or pending.device_id != device.get("device_id", ""):
        raise HTTPException(404, "That action has expired or was already decided.")
    return pending.card()


@router.post("/confirm/{action_id}")
async def confirm_run(action_id: str, device: CurrentDevice, settings: Settings = Depends(get_settings)):
    """Run a parked action. Once, and only from the device that was asked."""
    device_id = device.get("device_id", "")
    pending = confirmations.take(action_id, device_id)
    if pending is None:
        raise HTTPException(404, "That action has expired or was already decided.")

    from ..tools.base import registry

    # The one place ctx.confirmed is ever set, for the one call it covers.
    ctx = ToolContext(
        device_id=device_id,
        timezone=settings.timezone,
        conversation_id=pending.conversation_id,
        confirmed=True,
    )
    result = await registry.dispatch(pending.tool, pending.arguments, ctx)

    # Into the transcript, so the conversation records what was actually done
    # rather than only that it was proposed.
    if pending.conversation_id:
        note = f"✅ Confirmed: {pending.summary}" if result.ok else f"⚠️ {pending.summary} — {result.error}"
        async with session_scope() as session:
            conversation = await session.get(Conversation, pending.conversation_id)
            if conversation is not None:
                session.add(Message(conversation_id=conversation.id, role="assistant", content=note))
                conversation.updated_at = utcnow()

    return {
        "ok": result.ok,
        "error": result.error,
        "display": result.display,
        "summary": pending.summary,
    }


@router.delete("/confirm/{action_id}")
async def confirm_cancel(action_id: str, device: CurrentDevice):
    if not confirmations.cancel(action_id, device.get("device_id", "")):
        raise HTTPException(404, "That action has expired or was already decided.")
    return {"cancelled": True}
