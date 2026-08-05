"""Speech-to-text.

Two paths exist for turning your voice into text, and the client picks whichever
works in the browser it's running in:

  1. The Web Speech API, in-browser. Zero round trip, but Chrome ships it,
     Firefox doesn't, and Safari's implementation is inconsistent about
     continuous mode.
  2. This endpoint — the browser records audio and posts it here, and we hand it
     to Groq's Whisper. Works in every browser that can record, costs nothing on
     top of the API key you already have, and transcribes noticeably better than
     the browser engines.

So the client tries (1) for latency and falls back to (2) for coverage.
Text-to-speech stays entirely in the browser via speechSynthesis — free, offline,
and no audio ever leaves the device on the way out.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from ..config import Settings, get_settings
from ..security import CurrentDevice
from ..voiceprint import VoiceprintError, embed_wav
from .identity import load_profile, raise_alert, strip_wake_word, wake_word_present

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/voice", tags=["voice"])

MAX_AUDIO_BYTES = 20 * 1024 * 1024  # Groq's limit on the free tier
ALLOWED_TYPES = {
    "audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg",
    "audio/wav", "audio/x-wav", "audio/flac", "audio/m4a",
}


@router.post("/transcribe")
async def transcribe(
    device: CurrentDevice,
    audio: UploadFile = File(...),
    language: str = Form(""),
    settings: Settings = Depends(get_settings),
):
    if not settings.groq_api_key:
        raise HTTPException(503, "GROQ_API_KEY is not configured.")

    data = await audio.read()
    if not data:
        raise HTTPException(400, "Empty audio upload.")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Recording too long — keep it under about 10 minutes.")

    # Speaker check happens before transcription, so an unrecognised voice never
    # reaches the model at all. The audio is still transcribed afterwards when it
    # fails, purely so the alert can tell you what was said.
    voice_score: float | None = None
    if settings.require_voice_match:
        voice_score = await _score_speaker(data)
        if voice_score is None:
            raise HTTPException(
                409,
                "Voice matching is enabled but no voice is enrolled. Enrol from the "
                "app, or set REQUIRE_VOICE_MATCH=false.",
            )
        if voice_score < settings.voice_match_threshold:
            spoken = await _whisper(data, audio, language, settings, quiet=True)
            await raise_alert(device.get("device_id", ""), voice_score, spoken, settings)
            raise HTTPException(
                403,
                f"That doesn't sound like you (match {voice_score:.2f}, need "
                f"{settings.voice_match_threshold:.2f}). Nothing was actioned and an "
                "alert has been sent.",
            )

    text = await _whisper(data, audio, language, settings)

    if settings.require_wake_word and not wake_word_present(text, settings.wake_word):
        return {
            "text": "",
            "ignored": True,
            "reason": f"No wake word — say '{settings.wake_word}' first.",
        }

    return {
        "text": strip_wake_word(text, settings.wake_word) if text else "",
        "model": settings.whisper_model,
        "voice_score": round(voice_score, 3) if voice_score is not None else None,
    }


async def _score_speaker(data: bytes) -> float | None:
    """Score WAV audio against the enrolled voice. None when nothing is enrolled."""
    profile = await load_profile()
    if profile is None:
        return None
    try:
        return profile.score(await asyncio.to_thread(embed_wav, data))
    except VoiceprintError as exc:
        # Unreadable or too-short audio is not an impostor — treat it as a
        # non-answer rather than silently letting it through as a match.
        log.info("Could not score speaker: %s", exc)
        raise HTTPException(400, str(exc)) from exc


async def _whisper(
    data: bytes,
    audio: UploadFile,
    language: str,
    settings: Settings,
    quiet: bool = False,
) -> str:

    content_type = (audio.content_type or "audio/webm").split(";")[0].strip()
    if content_type not in ALLOWED_TYPES:
        # Browsers label the same container inconsistently; don't reject on that
        # alone, just pass a sane default through to Whisper.
        log.info("Unusual audio content-type %r, sending as webm", content_type)
        content_type = "audio/webm"

    form: dict = {"model": (None, settings.whisper_model), "response_format": (None, "json")}
    if language:
        form["language"] = (None, language[:5])

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            response = await client.post(
                f"{settings.groq_base_url.rstrip('/')}/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                files={"file": (audio.filename or "speech.webm", data, content_type), **form},
            )
    except httpx.HTTPError as exc:
        # `quiet` means we're only transcribing to enrich a rejection alert. The
        # rejection already happened and stands regardless; failing here would
        # lose the alert entirely over a detail.
        if quiet:
            log.info("Alert transcription unavailable: %s", exc)
            return ""
        raise HTTPException(502, f"Could not reach the transcription service: {exc}") from exc

    if response.status_code >= 400:
        if quiet:
            log.info("Alert transcription failed: %s", response.status_code)
            return ""
        if response.status_code == 429:
            raise HTTPException(429, "Transcription is rate-limited right now. Try again shortly.")
        log.warning("Whisper error %s: %s", response.status_code, response.text[:300])
        raise HTTPException(502, f"Transcription failed ({response.status_code}).")

    return (response.json().get("text") or "").strip()
