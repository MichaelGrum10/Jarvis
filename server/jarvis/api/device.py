"""Device-side endpoints: report location, collect queued actions."""

from __future__ import annotations

import json

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from ..db import Device, DeviceCommand, session_scope, utcnow
from ..security import CurrentDevice

router = APIRouter(prefix="/api/device", tags=["device"])


class LocationUpdate(BaseModel):
    lat: float
    lon: float
    accuracy_m: float | None = None


@router.post("/location")
async def update_location(body: LocationUpdate, device: CurrentDevice):
    device_id = device.get("device_id", "")
    async with session_scope() as session:
        row = (
            await session.execute(select(Device).where(Device.device_id == device_id))
        ).scalar_one_or_none()
        if row is None:
            row = Device(device_id=device_id, label=device.get("label", "device"))
            session.add(row)
        row.lat, row.lon, row.accuracy_m = body.lat, body.lon, body.accuracy_m
        row.location_at = utcnow()
    return {"ok": True}


@router.get("/commands")
async def pending_commands(device: CurrentDevice, limit: int = 5):
    """The app polls this; anything queued by device_* tools comes back here to run."""
    device_id = device.get("device_id", "")
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(DeviceCommand)
                    .where(DeviceCommand.device_id == device_id, DeviceCommand.status == "pending")
                    .order_by(DeviceCommand.id)
                    .limit(limit)
                )
            ).scalars().all()
        )
        commands = []
        for row in rows:
            try:
                payload = json.loads(row.payload)
            except json.JSONDecodeError:
                payload = {}
            commands.append({"id": row.id, "kind": row.kind, **payload})
            row.status = "delivered"
            row.delivered_at = utcnow()
    return {"commands": commands}
