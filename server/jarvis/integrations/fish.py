"""Fish Audio text-to-speech — the backend behind tts.py.

The request shape here is copied from Fish's own Python SDK (fish-audio-sdk
1.3.0, `resources/tts.py` and `core/client_wrapper.py`) rather than from memory
or from a docs page: `POST /v1/tts` with a **msgpack** body, the key as a
Bearer token, and the model chosen by a `model` *header*, not a body field.
That last one is easy to get wrong and fails as a silent default rather than
an error.

Models, per the SDK's own type: `s1` and `s2-pro` are current; `speech-1.5`
and `speech-1.6` are deprecated and warn. The default follows the SDK's.

Only `fetch()` is public. Caching, hashing and the browser-facing ticket live
one level up, so a provider is a function that turns text into mp3 bytes and
nothing else.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx
import ormsgpack

from ..config import Settings
from .tts import SpeechError

log = logging.getLogger(__name__)

API = "https://api.fish.audio/v1/tts"

CONNECT_TIMEOUT = 8.0
# Time-to-first-byte on a stream we then read incrementally; generous on purpose.
READ_TIMEOUT = 30.0

MODELS = ("s1", "s2-pro")


async def fetch(text: str, settings: Settings) -> AsyncIterator[bytes]:
    """Stream mp3 for `text` in the configured cloned voice."""
    model = (settings.fish_model or "s2-pro").strip()
    if model not in MODELS:
        # Deprecated names still answer today, but the SDK warns on them and
        # they will go. Say so once rather than let the voice vanish one day.
        log.warning("FISH_MODEL=%r is not a current Fish model (%s)", model, ", ".join(MODELS))

    # Field names and defaults are the SDK's TTSRequest, minus what we do not
    # override. `latency: balanced` is the lower-latency mode — the whole reason
    # this streams is to start speaking before the clip is finished.
    payload = {
        "text": text,
        "reference_id": settings.fish_voice_id,
        "format": "mp3",
        "mp3_bitrate": 128,
        "latency": settings.fish_latency or "balanced",
        "normalize": True,
        "chunk_length": 200,
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)
        ) as client:
            async with client.stream(
                "POST",
                API,
                headers={
                    "Authorization": f"Bearer {settings.fish_api_key}",
                    "Content-Type": "application/msgpack",
                    "model": model,
                },
                content=ormsgpack.packb(payload),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:400]
                    raise _explain(response.status_code, body)
                async for chunk in response.aiter_bytes():
                    yield chunk
    except httpx.HTTPError as exc:
        raise SpeechError(f"Could not reach Fish Audio: {exc}") from None


def _explain(status: int, body: str) -> SpeechError:
    """Turn an API error into something the status line can show a person.

    The message reaches a browser. The key travels in a header and nothing
    echoes headers back, but the body is remote text — truncated, never trusted.
    """
    lowered = body.lower()
    if status == 401:
        return SpeechError("Fish Audio rejected the API key.")
    if status == 402 or "credit" in lowered or "balance" in lowered or "quota" in lowered:
        return SpeechError("Fish Audio credits are used up.", quota=True)
    if status == 403:
        return SpeechError("Fish Audio refused that voice — is the reference id yours to use?")
    if status == 404:
        return SpeechError("That Fish Audio voice id does not exist.")
    if status == 422:
        return SpeechError("Fish Audio refused the request (bad text or settings).")
    if status == 429:
        return SpeechError("Fish Audio is rate limiting; try again in a moment.")
    return SpeechError(f"Fish Audio error {status}.")
