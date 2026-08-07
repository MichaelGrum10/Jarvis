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
    file: UploadFile | None = File(None),
    cookies: str | None = Form(None),
    replace: bool = Form(False),
):
    """Import a cookie export from your own browser.

    Takes either an uploaded file or pasted text. Pasting is what makes this
    workable from a phone: a file upload means getting a JSON blob onto the
    server first, which on iOS means an SSH app and a very long paste into a
    terminal. Copy in Safari, paste here, done.

    Merges with what's already stored unless `replace` is set, so a site whose
    authentication spans two domains can be imported in two goes.
    """
    if file is not None:
        raw = await file.read()
    elif cookies:
        raw = cookies.encode()
    else:
        raise HTTPException(400, "Send either a file or pasted cookie text.")

    if not raw.strip():
        raise HTTPException(400, "Nothing to import — that was empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "That's far larger than any cookie export.")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        # A paste cut short is the common failure on a phone, and "Expecting
        # ',' delimiter: line 1 column 4051" does not suggest "paste it again".
        hint = (
            " The paste looks cut off — copy it again and make sure the whole thing lands."
            if raw.lstrip()[:1] in (b"[", b"{") else
            " That doesn't look like a cookie export at all — it should start with [ or {."
        )
        raise HTTPException(400, f"Not valid JSON: {exc}.{hint}") from exc

    try:
        summary = save_session(_cookies_from(payload), merge=not replace)
    except BrowserError as exc:
        raise HTTPException(400, str(exc)) from exc

    log.info("Browser session imported from device %s", device.get("label", "?"))
    return {"imported": True, **summary}


@router.get("/session")
async def read_session(device: CurrentDevice):
    """Which sites are covered and when the session lapses. No cookie values."""
    return session_summary()


@router.delete("/session")
async def delete_session(device: CurrentDevice):
    clear_session()
    return {"cleared": True}
