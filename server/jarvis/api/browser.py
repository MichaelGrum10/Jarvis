"""Importing and inspecting the browser session.

The session is imported rather than created: you sign in normally in your own
browser, export the cookies, and upload them here. Jarvis never handles your
password, and the request never leaves your own server.

Cookie values are never returned by any endpoint on this router. A session
cookie for a site you're signed into is as good as the password for it, and an
endpoint that echoes one back turns a screenshot or a shared log into an account
compromise.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..integrations.browser import (
    BrowserError,
    clear_session,
    save_session,
    session_summary,
)
from ..security import CurrentDevice

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/browser", tags=["browser"])

MAX_UPLOAD_BYTES = 2 * 1024 * 1024


def _cookies_from(payload) -> list[dict]:
    """Find the cookie list in whatever shape the export arrived in.

    Extensions disagree: some export a bare array, some wrap it in
    `{"cookies": [...]}`, and Playwright's own storage state uses the latter.
    Accepting all three costs a few lines and saves explaining which button to
    press in which extension.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("cookies", "Cookies", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise BrowserError(
        "That file doesn't look like a cookie export — expected a JSON array of "
        "cookies, or an object with a 'cookies' array."
    )


@router.post("/session")
async def import_session(
    device: CurrentDevice,
    file: UploadFile = File(...),
    replace: bool = Form(False),
):
    """Upload a cookie export from your own browser.

    Merges with what's already stored unless `replace` is set, so a site whose
    authentication spans two domains can be imported in two goes.
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "That file is far larger than any cookie export.")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"Not valid JSON: {exc}") from exc

    try:
        summary = save_session(_cookies_from(payload), merge=not replace)
    except BrowserError as exc:
        raise HTTPException(400, str(exc)) from exc

    log.info("Browser session imported from device %s", device.id)
    return {"imported": True, **summary}


@router.get("/session")
async def read_session(device: CurrentDevice):
    """Which sites are covered and when the session lapses. No cookie values."""
    return session_summary()


@router.delete("/session")
async def delete_session(device: CurrentDevice):
    clear_session()
    return {"cleared": True}
