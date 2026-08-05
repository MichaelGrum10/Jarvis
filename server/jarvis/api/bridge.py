"""Endpoints the Mac Messages bridge talks to.

Authenticated with BRIDGE_TOKEN, separate from device tokens: a phone that can
chat with Jarvis still cannot inject fake messages into your history.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import BridgeHeartbeat, ChatMessage, OutboundMessage, session_scope, utcnow
from ..security import verify_bridge

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/bridge", tags=["bridge"], dependencies=[Depends(verify_bridge)])


class IncomingMessage(BaseModel):
    guid: str
    chat_id: str = ""
    chat_name: str = ""
    sender: str = ""
    sender_name: str = ""
    text: str = ""
    service: str = "iMessage"
    is_from_me: bool = False
    is_group: bool = False
    sent_at: dt.datetime


class IngestRequest(BaseModel):
    hostname: str = ""
    version: str = ""
    messages: list[IncomingMessage] = Field(default_factory=list)


@router.post("/ingest")
async def ingest(body: IngestRequest):
    """Bridge pushes new messages here. Idempotent on guid, so replays are harmless."""
    stored = 0
    async with session_scope() as session:
        for item in body.messages:
            exists = (
                await session.execute(select(ChatMessage.id).where(ChatMessage.guid == item.guid))
            ).first()
            if exists:
                continue
            session.add(
                ChatMessage(
                    guid=item.guid,
                    chat_id=item.chat_id,
                    chat_name=item.chat_name,
                    sender=item.sender,
                    sender_name=item.sender_name,
                    text=item.text,
                    service=item.service,
                    is_from_me=int(item.is_from_me),
                    is_group=int(item.is_group),
                    sent_at=item.sent_at.replace(tzinfo=None)
                    if item.sent_at.tzinfo
                    else item.sent_at,
                )
            )
            stored += 1

        beat = (await session.execute(select(BridgeHeartbeat).limit(1))).scalar_one_or_none()
        if beat is None:
            session.add(BridgeHeartbeat(hostname=body.hostname, version=body.version))
        else:
            beat.hostname, beat.version, beat.last_seen = body.hostname, body.version, utcnow()

    return {"received": len(body.messages), "stored": stored}


@router.get("/outbox")
async def outbox(limit: int = 10):
    """Bridge polls this for messages to send."""
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(OutboundMessage)
                    .where(OutboundMessage.status == "pending")
                    .order_by(OutboundMessage.id)
                    .limit(limit)
                )
            ).scalars().all()
        )
        jobs = [
            {"id": r.id, "recipient": r.recipient, "text": r.text, "service": r.service}
            for r in rows
        ]
        for row in rows:
            row.status = "claimed"
    return {"jobs": jobs}


class AckRequest(BaseModel):
    id: int
    ok: bool
    error: str = ""


@router.post("/outbox/ack")
async def ack(body: AckRequest):
    async with session_scope() as session:
        row = await session.get(OutboundMessage, body.id)
        if row is None:
            return {"ok": False, "reason": "unknown job"}
        row.status = "sent" if body.ok else "failed"
        row.error = body.error[:1000]
        row.sent_at = utcnow()
    return {"ok": True}


@router.get("/health")
async def bridge_health():
    async with session_scope() as session:
        beat = (await session.execute(select(BridgeHeartbeat).limit(1))).scalar_one_or_none()
    if beat is None:
        return {"connected": False}
    return {
        "connected": True,
        "hostname": beat.hostname,
        "version": beat.version,
        "last_seen": beat.last_seen.isoformat(),
        "seconds_ago": int((utcnow() - beat.last_seen).total_seconds()),
    }
