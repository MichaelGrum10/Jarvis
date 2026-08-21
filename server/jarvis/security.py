"""Single-owner auth: one password, long-lived signed device tokens."""

from __future__ import annotations

import hmac
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import Settings, get_settings

_SALT = "jarvis-device-token"
# A different salt, so a speech ticket can never be replayed as a device token
# and a device token can never be spent as a speech ticket. Same secret, and
# rotating AUTH_SECRET invalidates both at once.
_SPEECH_SALT = "jarvis-speech-ticket"
# Long enough to start playing, short enough that a URL in a log or a history
# entry is worthless by the time anyone reads it.
SPEECH_TICKET_SECONDS = 300


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    if not settings.auth_secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AUTH_SECRET is not configured on the server.",
        )
    return URLSafeTimedSerializer(settings.auth_secret, salt=_SALT)


def _speech_serializer(settings: Settings) -> URLSafeTimedSerializer:
    if not settings.auth_secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AUTH_SECRET is not configured on the server.",
        )
    return URLSafeTimedSerializer(settings.auth_secret, salt=_SPEECH_SALT)


def issue_speech_ticket(key: str, settings: Settings) -> str:
    """Permission to hear one specific line, and nothing else.

    An <audio> element cannot send an Authorization header, so the URL has to
    carry its own proof. Binding the ticket to the clip's hash means a leaked
    URL plays back one line the owner already heard — it is not a token, and it
    cannot be pointed at a different line or at any other endpoint.
    """
    return _speech_serializer(settings).dumps({"k": key})


def read_speech_ticket(ticket: str, key: str, settings: Settings) -> None:
    if not ticket:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing speech ticket.")
    try:
        data = _speech_serializer(settings).loads(ticket, max_age=SPEECH_TICKET_SECONDS)
    except SignatureExpired as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Speech ticket expired.") from exc
    except BadSignature as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid speech ticket.") from exc
    if not hmac.compare_digest(str(data.get("k", "")), key):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That ticket is for a different line.")


def verify_password(candidate: str, settings: Settings) -> bool:
    if not settings.access_password:
        return False
    return hmac.compare_digest(candidate.encode(), settings.access_password.encode())


def issue_token(device_id: str, label: str, settings: Settings) -> str:
    return _serializer(settings).dumps({"device_id": device_id, "label": label})


def read_token(token: str, settings: Settings) -> dict:
    max_age = settings.token_ttl_days * 86400
    try:
        return _serializer(settings).loads(token, max_age=max_age)
    except SignatureExpired as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token expired — sign in again.") from exc
    except BadSignature as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token.") from exc


def new_device_id() -> str:
    return secrets.token_urlsafe(18)


async def current_device(
    authorization: Annotated[str | None, Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token.")
    return read_token(authorization.split(" ", 1)[1].strip(), settings)


async def verify_bridge(
    x_bridge_token: Annotated[str | None, Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> bool:
    """Auth for the Mac bridge — separate secret so a leaked device token can't post messages."""
    if not settings.bridge_token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "BRIDGE_TOKEN not configured.")
    if not x_bridge_token or not hmac.compare_digest(x_bridge_token, settings.bridge_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad bridge token.")
    return True


CurrentDevice = Annotated[dict, Depends(current_device)]
