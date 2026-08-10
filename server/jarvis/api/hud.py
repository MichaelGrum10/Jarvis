"""The HUD's data feed.

One endpoint, one round-trip, no model. The HUD refreshes on a timer, and a
dashboard that woke the agent every minute would spend more of the token budget
than the conversation it exists to support.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..hud import snapshot
from ..security import CurrentDevice

router = APIRouter(prefix="/api/hud", tags=["hud"])


@router.get("")
async def hud(device: CurrentDevice):
    return await snapshot()
