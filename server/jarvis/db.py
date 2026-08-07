"""SQLite persistence: conversations, memory, device registrations, autonomy runs."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import get_settings


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # user | assistant | tool | system
    content: Mapped[str] = mapped_column(Text, default="")
    # tool calls / results serialised as JSON so we can replay an exact transcript
    extra: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def meta(self) -> dict:
        try:
            return json.loads(self.extra or "{}")
        except json.JSONDecodeError:
            return {}


class Memory(Base):
    """Long-lived facts Jarvis learns about you."""

    __tablename__ = "memories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(120), index=True)
    value: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40), default="general")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Device(Base):
    """A phone/laptop you have authorised, plus its last known location."""

    __tablename__ = "devices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(80), default="device")
    platform: Mapped[str] = mapped_column(String(40), default="unknown")
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DeviceCommand(Base):
    """Queued action for a device to execute (open app, open URL, run shortcut)."""

    __tablename__ = "device_commands"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # open_url | open_app | shortcut | notify
    payload: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class ChatMessage(Base):
    """An iMessage/SMS mirrored up from the Mac bridge."""

    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guid: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    chat_id: Mapped[str] = mapped_column(String(120), index=True, default="")
    chat_name: Mapped[str] = mapped_column(String(160), default="")
    sender: Mapped[str] = mapped_column(String(160), default="")
    sender_name: Mapped[str] = mapped_column(String(160), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    service: Mapped[str] = mapped_column(String(20), default="iMessage")
    is_from_me: Mapped[int] = mapped_column(Integer, default=0)
    is_group: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True, default=utcnow)
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class OutboundMessage(Base):
    """A message Jarvis wants the Mac bridge to send on the user's behalf."""

    __tablename__ = "outbound_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipient: Mapped[str] = mapped_column(String(160))
    text: Mapped[str] = mapped_column(Text)
    service: Mapped[str] = mapped_column(String(20), default="iMessage")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class BridgeHeartbeat(Base):
    """Last time the Mac bridge checked in, so we can tell the user when it's down."""

    __tablename__ = "bridge_heartbeat"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(120), default="")
    version: Mapped[str] = mapped_column(String(40), default="")
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class SpeakerProfile(Base):
    """Enrolled voiceprint for the owner. One row; re-enrolling replaces it."""

    __tablename__ = "speaker_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(80), default="owner")
    vectors: Mapped[bytes] = mapped_column(LargeBinary)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    cohesion: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class VoiceAlert(Base):
    """An utterance that failed speaker verification, kept so you can review what
    was said and decide whether it was a stranger or just a bad match on you."""

    __tablename__ = "voice_alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    threshold: Mapped[float] = mapped_column(Float, default=0.0)
    transcript: Mapped[str] = mapped_column(Text, default="")
    acknowledged: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Skill(Base):
    """A named routine: trigger phrases plus the instruction they invoke."""

    __tablename__ = "skills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    triggers: Mapped[str] = mapped_column(Text, default="")  # comma separated
    instruction: Mapped[str] = mapped_column(Text)
    enabled: Mapped[int] = mapped_column(Integer, default=1, index=True)
    builtin: Mapped[int] = mapped_column(Integer, default=0)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    last_used: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def trigger_list(self) -> list[str]:
        return [t.strip() for t in (self.triggers or "").split(",") if t.strip()]


class AutonomyRun(Base):
    """One self-improvement / self-debug session."""

    __tablename__ = "autonomy_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    branch: Mapped[str] = mapped_column(String(120), default="")
    log: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine, _sessionmaker
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(f"sqlite+aiosqlite:///{settings.db_path}", future=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def init_db() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session_scope() as session:
        yield session


async def load_history(session: AsyncSession, conversation_id: int, limit: int = 40) -> list[Message]:
    rows = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .limit(limit)
    )
    return list(reversed(rows.scalars().all()))
