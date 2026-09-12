"""The cloned voice: everything the app needs from text-to-speech.

Is a voice configured, what is it called, hash this line, stream it, cache it.
Fish Audio renders the audio (fish.py); this module is the door in front of it
and the two things that must hold whatever is behind that door:

**The key never reaches a browser.** The browser asks this server to speak and
the server holds the credential. A TTS key is billed per character and would be
trivially scraped out of anything served to a phone — see api/voice.py for the
ticket scheme that lets an <audio> element play a line without one.

**Rendered audio is cached, by content.** The boot greeting and the canned
confirmations are identical every time and said many times a day; re-rendering
them is money spent to receive the same bytes back. The cache key includes the
voice and model, so switching either re-renders rather than playing the old
voice out of the cache. Written through a `.part` file and renamed, so a stream
that dies halfway cannot leave a truncated clip to be served forever after.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

# Fish rejects oversized requests, and a reply that long should have been
# chunked before it got here anyway.
MAX_CHARS = 2500

PROVIDER = "fish"
LABEL = "Fish Audio"
# The message the browser keys its "settled, stop asking" fallback on — see
# speech.js, which matches /not configured/. Change both or neither.
NOT_CONFIGURED = "The cloned voice is not configured."


class SpeechError(RuntimeError):
    """Speech could not be produced. The caller falls back to the browser."""

    def __init__(self, message: str, *, quota: bool = False) -> None:
        super().__init__(message)
        # Worth distinguishing: a credit failure will keep failing until the
        # account is topped up, and the client should stop asking rather than
        # add a doomed round trip to every sentence.
        self.quota = quota


# ------------------------------------------------------------- configured?


def configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.fish_api_key.strip() and settings.fish_voice_id.strip())


def provider(settings: Settings | None = None) -> str:
    """The provider that will speak, or "" if none can. Kept as a name rather
    than a bool because it travels in the X-Speech-Source header and the
    /speak reply, where "fish" reads better than "true"."""
    return PROVIDER if configured(settings) else ""


def label(settings: Settings | None = None) -> str:
    """A human name for the status line: "Fish Audio", not "fish"."""
    return LABEL if configured(settings) else ""


def missing(settings: Settings | None = None) -> list[str]:
    """Which env vars would turn the voice on. Drives doctor and /speech-status."""
    settings = settings or get_settings()
    pairs = [("FISH_API_KEY", settings.fish_api_key), ("FISH_VOICE_ID", settings.fish_voice_id)]
    return [name for name, value in pairs if not value.strip()]


# ------------------------------------------------------------------ the cache


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
    """A stable id for one rendering of one line, in one voice, by one model."""
    settings = settings or get_settings()
    material = f"{PROVIDER}|{settings.fish_voice_id}|{settings.fish_model}|{text.strip()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def cached_path(key: str, settings: Settings | None = None) -> Path | None:
    path = cache_dir(settings) / f"{key}.mp3"
    # An empty file is a previous failure, not a clip. Treat it as absent.
    return path if path.is_file() and path.stat().st_size > 512 else None


# ----------------------------------------------------------------- speaking


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
        raise SpeechError(NOT_CONFIGURED)

    key = voice_key(text, settings)
    hit = cached_path(key, settings)
    if hit:
        # 64KB at a time: enough that the browser's decoder never starves,
        # small enough that playback starts on the first read.
        with hit.open("rb") as handle:
            while chunk := handle.read(65536):
                yield chunk
        return

    # Imported here, not at the top: fish.py imports SpeechError from this
    # module, and a module-level import in both directions is a cycle.
    from . import fish

    target = cache_dir(settings) / f"{key}.mp3"
    partial = target.with_suffix(".part")
    written = 0
    handle = partial.open("wb")
    try:
        async for chunk in fish.fetch(text, settings):
            if not chunk:
                continue
            written += len(chunk)
            handle.write(chunk)
            yield chunk
    except BaseException:
        handle.close()
        partial.unlink(missing_ok=True)
        raise
    handle.close()

    if written > 512:
        # Rename only once the stream finished cleanly: a half-written clip
        # renamed into place would be served from cache forever.
        os.replace(partial, target)
    else:
        partial.unlink(missing_ok=True)


async def verify(settings: Settings | None = None) -> dict:
    """Ask Fish whether the key and the voice id are real, without rendering
    anything. Used by doctor and /speech-status?verify=1, so "it is configured"
    and "it works" stop being the same claim."""
    settings = settings or get_settings()
    if not configured(settings):
        return {"ok": False, "error": NOT_CONFIGURED, "missing": missing(settings)}
    from . import fish

    return await fish.verify(settings)
