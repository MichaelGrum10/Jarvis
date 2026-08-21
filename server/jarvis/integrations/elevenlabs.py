"""ElevenLabs text-to-speech, streamed and cached.

The browser never talks to ElevenLabs. It asks this server, and this server
holds the key — the same rule as every other credential here, and it matters
more than usual because a TTS key is billed per character and would be trivially
scrapeable out of a page served to a phone.

## Streaming, not batch

The batch endpoint returns nothing until the whole clip is rendered, which for a
sentence or two is a couple of seconds of silence before he starts. The
streaming endpoint sends audio as it is produced, so the first bytes arrive in a
few hundred milliseconds and the browser can start playing immediately.
`optimize_streaming_latency` trades a little prosody for a lot of that delay.

## Caching

Keyed by a hash of the text, the voice, and the model. The boot greeting and the
canned confirmations are said many times a day and are identical every time;
re-rendering them is money spent to receive the same bytes back. A cached clip
also plays instantly, with no network at all.

The cache is written through a temporary file and renamed, so a stream that dies
halfway cannot leave a truncated clip behind to be served forever after.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

API = "https://api.elevenlabs.io/v1/text-to-speech"

# 0 is best quality, 4 is fastest. 2 keeps the delay short without the flattened
# delivery that the highest setting produces.
LATENCY_MODE = 2

# ElevenLabs rejects oversized requests, and a reply that long should have been
# chunked before it got here anyway.
MAX_CHARS = 2500

CONNECT_TIMEOUT = 8.0
# Generous: this is time-to-*first-byte* on a stream we then read incrementally.
READ_TIMEOUT = 30.0


class SpeechError(RuntimeError):
    """Speech could not be produced. The caller falls back to the browser."""

    def __init__(self, message: str, *, quota: bool = False) -> None:
        super().__init__(message)
        # Worth distinguishing: a quota failure will keep failing until the month
        # turns over, and the client should stop asking rather than retry.
        self.quota = quota


def cache_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = settings.data_dir / "tts-cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def speakable_length(text: str) -> str:
    """Normalise a line before it is hashed or sent.

    Whitespace-only differences would otherwise mint a new cache entry — and a
    new charge — for a line already rendered.
    """
    return " ".join(str(text or "").split())[:MAX_CHARS]


def voice_key(text: str, settings: Settings | None = None) -> str:
    """A stable id for one rendering of one line in one voice."""
    settings = settings or get_settings()
    material = f"{settings.elevenlabs_voice_id}|{settings.elevenlabs_model}|{text.strip()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def cached_path(key: str, settings: Settings | None = None) -> Path | None:
    path = cache_dir(settings) / f"{key}.mp3"
    # An empty file is a previous failure, not a clip. Treat it as absent.
    return path if path.is_file() and path.stat().st_size > 512 else None


def configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.elevenlabs_api_key and settings.elevenlabs_voice_id)


async def stream(text: str, settings: Settings | None = None) -> AsyncIterator[bytes]:
    """Yield mp3 bytes for `text`, from cache when possible.

    Whatever is yielded is also written to the cache, so the first person to say
    a line pays for it and nobody pays again.
    """
    settings = settings or get_settings()
    text = (text or "").strip()
    if not text:
        raise SpeechError("Nothing to say.")
    if len(text) > MAX_CHARS:
        raise SpeechError(f"Too long to speak in one request ({len(text)} characters).")
    if not configured(settings):
        raise SpeechError("ElevenLabs is not configured.")

    key = voice_key(text, settings)
    hit = cached_path(key, settings)
    if hit:
        # 64KB at a time: enough that the browser's decoder never starves,
        # small enough that playback starts on the first read.
        with hit.open("rb") as handle:
            while chunk := handle.read(65536):
                yield chunk
        return

    target = cache_dir(settings) / f"{key}.mp3"
    partial = target.with_suffix(".part")
    written = 0

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

                handle = partial.open("wb")
                try:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        written += len(chunk)
                        handle.write(chunk)
                        yield chunk
                finally:
                    handle.close()

        if written > 512:
            # Rename only once the stream finished cleanly: a half-written clip
            # renamed into place would be served from cache forever.
            os.replace(partial, target)
        else:
            partial.unlink(missing_ok=True)
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise SpeechError(f"Could not reach ElevenLabs: {exc}") from None
    except SpeechError:
        partial.unlink(missing_ok=True)
        raise
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _explain(status: int, body: str) -> SpeechError:
    """Turn an API error into something the status line can show a person.

    The message reaches a browser, so it must never carry the key — it does not,
    because the key travels in a header and nothing echoes headers back, but the
    body is remote text and is truncated rather than trusted.
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
