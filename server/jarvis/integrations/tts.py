"""The cloned voice: one door, two providers behind it.

Everything the rest of the app needs from text-to-speech lives here — is a
voice configured, what is it called, hash this line, stream it, cache it. Which
service actually renders the audio is a detail behind `stream()`, chosen from
the settings: Fish Audio or ElevenLabs, whichever is configured, with
`TTS_PROVIDER` to force one when both are.

The two things that must not change with the provider:

**The key never reaches a browser.** The browser asks this server to speak and
the server holds the credential. A TTS key is billed per character and would be
trivially scraped out of anything served to a phone — see api/voice.py for the
ticket scheme that lets an <audio> element play a line without one.

**Rendered audio is cached, by content.** The boot greeting and the canned
confirmations are identical every time and said many times a day; re-rendering
them is money spent to receive the same bytes back. The cache key includes the
provider, voice and model, so switching any of them re-renders rather than
playing the old voice out of the cache. Written through a `.part` file and
renamed, so a stream that dies halfway cannot leave a truncated clip to be
served forever after.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

# Both providers reject oversized requests, and a reply that long should have
# been chunked before it got here anyway.
MAX_CHARS = 2500

PROVIDERS = ("fish", "elevenlabs")
LABELS = {"fish": "Fish Audio", "elevenlabs": "ElevenLabs"}


class SpeechError(RuntimeError):
    """Speech could not be produced. The caller falls back to the browser."""

    def __init__(self, message: str, *, quota: bool = False) -> None:
        super().__init__(message)
        # Worth distinguishing: a quota failure will keep failing until the
        # account is topped up, and the client should stop asking rather than
        # add a doomed round trip to every sentence.
        self.quota = quota


# ------------------------------------------------------------ which provider


def _fish_ready(settings: Settings) -> bool:
    return bool(settings.fish_api_key and settings.fish_voice_id)


def _eleven_ready(settings: Settings) -> bool:
    return bool(settings.elevenlabs_api_key and settings.elevenlabs_voice_id)


def provider(settings: Settings | None = None) -> str:
    """The provider that will speak, or "" if none can.

    An explicit TTS_PROVIDER wins, but only if that provider is actually
    configured — naming one with no key would silently fall through to the
    other, and "I set Fish and it is still ElevenLabs" is the wrong surprise.
    Without a setting, Fish is preferred when both are present, because it is
    the one being added and the one that costs less per character.
    """
    settings = settings or get_settings()
    wanted = (settings.tts_provider or "").strip().lower()
    ready = {"fish": _fish_ready(settings), "elevenlabs": _eleven_ready(settings)}
    if wanted in ready:
        return wanted if ready[wanted] else ""
    for name in PROVIDERS:
        if ready[name]:
            return name
    return ""


def configured(settings: Settings | None = None) -> bool:
    return bool(provider(settings))


def label(settings: Settings | None = None) -> str:
    """A human name for the status line: "Fish Audio", not "fish"."""
    return LABELS.get(provider(settings), "")


def missing(settings: Settings | None = None) -> list[str]:
    """Which env vars would turn the voice on. Drives doctor and /speech-status.

    Names the provider that is closest to working: if a Fish key is set but
    the voice id is not, the answer is FISH_VOICE_ID, not a list of every
    variable either provider could take.
    """
    settings = settings or get_settings()
    if configured(settings):
        return []
    wanted = (settings.tts_provider or "").strip().lower()
    if wanted == "elevenlabs" or (not wanted and settings.elevenlabs_api_key and not settings.fish_api_key):
        pairs = [("ELEVENLABS_API_KEY", settings.elevenlabs_api_key),
                 ("ELEVENLABS_VOICE_ID", settings.elevenlabs_voice_id)]
    else:
        pairs = [("FISH_API_KEY", settings.fish_api_key), ("FISH_VOICE_ID", settings.fish_voice_id)]
    return [name for name, value in pairs if not value]


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
    """A stable id for one rendering of one line, in one voice, by one provider."""
    settings = settings or get_settings()
    name = provider(settings)
    if name == "fish":
        voice, model = settings.fish_voice_id, settings.fish_model
    else:
        voice, model = settings.elevenlabs_voice_id, settings.elevenlabs_model
    material = f"{name}|{voice}|{model}|{text.strip()}"
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

    name = provider(settings)
    if not name:
        raise SpeechError("The cloned voice is not configured.")

    key = voice_key(text, settings)
    hit = cached_path(key, settings)
    if hit:
        # 64KB at a time: enough that the browser's decoder never starves,
        # small enough that playback starts on the first read.
        with hit.open("rb") as handle:
            while chunk := handle.read(65536):
                yield chunk
        return

    # Imported here, not at the top: the backends import SpeechError from this
    # module, and a module-level import in both directions is a cycle.
    if name == "fish":
        from . import fish as backend
    else:
        from . import elevenlabs as backend

    target = cache_dir(settings) / f"{key}.mp3"
    partial = target.with_suffix(".part")
    written = 0
    handle = partial.open("wb")
    try:
        async for chunk in backend.fetch(text, settings):
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
