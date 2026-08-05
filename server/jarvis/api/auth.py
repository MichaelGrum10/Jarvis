"""Device pairing: one password, then a long-lived token per device."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import Settings, get_settings
from ..db import Device, session_scope
from ..security import CurrentDevice, issue_token, new_device_id, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str
    label: str = Field("device", max_length=80)
    platform: str = Field("unknown", max_length=40)
    device_id: str = ""


class LoginResponse(BaseModel):
    token: str
    device_id: str
    label: str


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, settings: Settings = Depends(get_settings)):
    if settings.missing_for("auth"):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Server auth is not configured: set AUTH_SECRET and ACCESS_PASSWORD.",
        )
    if not verify_password(body.password, settings):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong password.")

    device_id = body.device_id or new_device_id()
    async with session_scope() as session:
        existing = (
            await session.execute(select(Device).where(Device.device_id == device_id))
        ).scalar_one_or_none()
        if existing:
            existing.label = body.label
            existing.platform = body.platform
        else:
            session.add(Device(device_id=device_id, label=body.label, platform=body.platform))

    return LoginResponse(
        token=issue_token(device_id, body.label, settings), device_id=device_id, label=body.label
    )


@router.get("/me")
async def me(device: CurrentDevice, settings: Settings = Depends(get_settings)):
    return {"device_id": device.get("device_id"), "label": device.get("label"), "ok": True}
