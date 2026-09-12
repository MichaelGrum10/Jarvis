"""ElevenLabs text-to-speech — the backend behind tts.py.

Only `fetch()` is public. Caching, hashing and the browser-facing ticket live
one level up in tts.py, so a provider is a function that turns text into mp3
bytes and nothing else.

Streaming, not batch: the batch endpoint returns nothing until the whole clip
is rendered, which for a sentence or two is a couple of seconds of silence
before he starts. `/stream` sends audio as it is produced, and
`optimize_streaming_latency` trades a little prosody for a lot of that delay.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from ..config import Settings
from .tts import SpeechError

log = logging.getLogger(__name__)

API = "https://api.elevenlabs.io/v1/text-to-speech"

# 0 is best quality, 4 is fastest. 2 keeps the delay short without the flattened
# delivery that the highest setting produces.
LATENCY_MODE = 2

CONNECT_TIMEOUT = 8.0
# Time-to-first-byte on a stream we then read incrementally; generous on purpose.
READ_TIMEOUT = 30.0


async def fetch(text: str, settings: Settings) -> AsyncIterator[bytes]:
    """Stream mp3 for `text` in the configured cloned voice."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)
        ) as client:
            async with client.stream(
                "POST",
                f"{API}/{settings.elevenlabs_voice_id}/stream",
                params={"optimize_streaming_latency": LATENCY_MODE, "output_format": "mp3_44100_128"},
                headers={
                    "xi-api-key": settings.elevenlabs_api_key,
                    "accept": "audio/mpeg",
                },
                json={
                    "text": text,
                    "model_id": settings.elevenlabs_model,
                    "voice_settings": {
                        "stability": settings.elevenlabs_stability,
                        "similarity_boost": settings.elevenlabs_similarity,
                    },
                },
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:400]
                    raise _explain(response.status_code, body)
                async for chunk in response.aiter_bytes():
                    yield chunk
    except httpx.HTTPError as exc:
        raise SpeechError(f"Could not reach ElevenLabs: {exc}") from None


def _explain(status: int, body: str) -> SpeechError:
    """Turn an API error into something the status line can show a person.

    The message reaches a browser. The key travels in a header and nothing
    echoes headers back, but the body is remote text — truncated, never trusted.
    """
    lowered = body.lower()
    if status == 401:
        return SpeechError("ElevenLabs rejected the API key.")
    if status == 429 or "quota" in lowered or "credit" in lowered:
        return SpeechError("ElevenLabs quota is used up.", quota=True)
    if status == 404:
        return SpeechError("That ElevenLabs voice id does not exist.")
    if status == 422:
        return SpeechError("ElevenLabs refused the request (bad voice settings or text).")
    return SpeechError(f"ElevenLabs error {status}.")
