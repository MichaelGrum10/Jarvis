"""Voice identity: enrol the owner's voice, verify against it, review alerts.

Read the module docstring in jarvis/voiceprint.py before relying on any of this.
Short version: this stops other people in the room from being answered, and tells
you when someone tried. It is not what keeps strangers out of your assistant.
"""

from __future__ import annotations

import logging

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select

from ..config import Settings, get_settings
from ..db import Device, DeviceCommand, SpeakerProfile, VoiceAlert, session_scope, utcnow
from ..security import CurrentDevice
from ..voiceprint import Voiceprint, VoiceprintError, embed_wav

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/identity", tags=["identity"])

MIN_ENROLMENT_SAMPLES = 3
MAX_AUDIO_BYTES = 10 * 1024 * 1024
# Below this, the enrolment samples disagree so much that the profile describes
# no single voice — usually background noise, or two people taking turns.
MIN_COHESION = 0.55


async def load_profile() -> Voiceprint | None:
    async with session_scope() as session:
        row = (await session.execute(select(SpeakerProfile).limit(1))).scalar_one_or_none()
        if row is None or not row.vectors:
            return None
        try:
            return Voiceprint.from_bytes(row.vectors)
        except VoiceprintError:
            log.exception("Stored voiceprint could not be read")
            return None


@router.post("/enroll")
async def enroll(
    device: CurrentDevice,
    audio: UploadFile = File(...),
    reset: bool = Form(False),
):
    """Add one enrolment sample. Call several times with separate recordings."""
    data = await audio.read()
    if not data:
        raise HTTPException(400, "Empty audio upload.")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "Recording too long for enrolment.")

    try:
        vector = embed_wav(data)
    except VoiceprintError as exc:
        raise HTTPException(400, str(exc)) from exc

    async with session_scope() as session:
        row = (await session.execute(select(SpeakerProfile).limit(1))).scalar_one_or_none()

        if row is None or reset:
            profile = Voiceprint([vector])
            if row is None:
                row = SpeakerProfile(label="owner", vectors=b"")
                session.add(row)
        else:
            profile = Voiceprint.from_bytes(row.vectors)
            profile.add(vector)

        row.vectors = profile.to_bytes()
        row.sample_count = len(profile.vectors)
        row.cohesion = profile.cohesion
        row.updated_at = utcnow()

        count, cohesion = row.sample_count, row.cohesion

    ready = count >= MIN_ENROLMENT_SAMPLES and cohesion >= MIN_COHESION
    response = {
        "samples": count,
        "needed": max(0, MIN_ENROLMENT_SAMPLES - count),
        "cohesion": round(cohesion, 3),
        "ready": ready,
    }
    if count >= MIN_ENROLMENT_SAMPLES and cohesion < MIN_COHESION:
        response["warning"] = (
            "These recordings don't sound like the same voice to me. Re-record them "
            "somewhere quieter, at a consistent distance from the microphone."
        )
    return response


@router.post("/verify")
async def verify(
    device: CurrentDevice,
    audio: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
):
    """Score a recording against the enrolled voice, without acting on it.

    Useful for calibrating the threshold: speak normally, see what you score.
    """
    profile = await load_profile()
    if profile is None:
        raise HTTPException(404, "No voice enrolled yet.")

    data = await audio.read()
    try:
        score = profile.score(embed_wav(data))
    except VoiceprintError as exc:
        raise HTTPException(400, str(exc)) from exc

    return {
        "score": round(score, 4),
        "threshold": settings.voice_match_threshold,
        "match": score >= settings.voice_match_threshold,
    }


@router.get("/status")
async def status(device: CurrentDevice, settings: Settings = Depends(get_settings)):
    async with session_scope() as session:
        row = (await session.execute(select(SpeakerProfile).limit(1))).scalar_one_or_none()
        unread = len(
            (
                await session.execute(
                    select(VoiceAlert.id).where(VoiceAlert.acknowledged == 0).limit(50)
                )
            ).all()
        )

    return {
        "enrolled": row is not None and row.sample_count > 0,
        "samples": row.sample_count if row else 0,
        "cohesion": round(row.cohesion, 3) if row else 0.0,
        "ready": bool(row and row.sample_count >= MIN_ENROLMENT_SAMPLES),
        "enforcing": settings.require_voice_match,
        "threshold": settings.voice_match_threshold,
        "wake_word": settings.wake_word,
        "require_wake_word": settings.require_wake_word,
        "unacknowledged_alerts": unread,
        "caveat": (
            "Voice matching is a filter, not a security boundary. A recording of "
            "your voice will pass it. Your password and device token are what "
            "actually protect this server."
        ),
    }


@router.delete("/enroll")
async def clear_enrolment(device: CurrentDevice):
    async with session_scope() as session:
        row = (await session.execute(select(SpeakerProfile).limit(1))).scalar_one_or_none()
        if row is not None:
            await session.delete(row)
    return {"cleared": True}


@router.get("/alerts")
async def alerts(device: CurrentDevice, limit: int = 20):
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(VoiceAlert).order_by(VoiceAlert.id.desc()).limit(limit)
                )
            ).scalars().all()
        )
    return {
        "alerts": [
            {
                "id": r.id,
                "score": round(r.score, 3),
                "threshold": round(r.threshold, 3),
                "transcript": r.transcript,
                "acknowledged": bool(r.acknowledged),
                "at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


@router.post("/alerts/acknowledge")
async def acknowledge(device: CurrentDevice):
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(select(VoiceAlert).where(VoiceAlert.acknowledged == 0))
            ).scalars().all()
        )
        for row in rows:
            row.acknowledged = 1
    return {"acknowledged": len(rows)}


async def raise_alert(device_id: str, score: float, transcript: str, settings: Settings) -> None:
    """Record an unrecognised voice and notify the owner.

    Notification is best-effort and deliberately non-fatal: a failure to send the
    email must not turn into a failure to *record* that this happened, which is
    the part that matters when reviewing later.
    """
    async with session_scope() as session:
        session.add(
            VoiceAlert(
                device_id=device_id,
                score=score,
                threshold=settings.voice_match_threshold,
                transcript=transcript[:2000],
            )
        )
        # Push a banner to every registered device, not just the one that heard it —
        # if this is a stranger, the phone in your pocket is where you want it.
        devices = list((await session.execute(select(Device))).scalars().all())
        for target in devices:
            session.add(
                DeviceCommand(
                    device_id=target.device_id,
                    kind="notify",
                    payload=(
                        '{"title": "Unrecognised voice", "text": '
                        f'"Someone spoke to Jarvis and did not match your voice (score '
                        f'{score:.2f}). Nothing was actioned."}}'
                    ),
                )
            )

    if settings.voice_alert_email and not settings.missing_for("mail"):
        try:
            from ..integrations.apple_mail import get_mail

            await get_mail().send(
                to=settings.icloud_email,
                subject="Jarvis: unrecognised voice",
                body=(
                    "Someone spoke to Jarvis and did not match your enrolled voice.\n\n"
                    f"Similarity score: {score:.3f} (threshold {settings.voice_match_threshold})\n"
                    f"What was said: {transcript[:500] or '(not transcribed)'}\n\n"
                    "The request was refused and nothing was actioned.\n\n"
                    "If this was you, your voice may not be enrolling well — re-enrol "
                    "from the app, or lower VOICE_MATCH_THRESHOLD in .env."
                ),
            )
        except Exception:
            log.exception("Could not send voice alert email")


def wake_word_present(text: str, wake_word: str) -> bool:
    """Is the wake word near the start of the utterance?

    Checked loosely on purpose. Speech recognition mangles a short name
    constantly — "Jarvis" comes back as "Travis", "Charice", "service" — and a
    strict match would make the wake word feel broken. Only the opening few words
    are considered, so the word occurring mid-sentence doesn't count.
    """
    if not wake_word:
        return True
    opening = " ".join(text.lower().split()[:4])
    target = wake_word.lower()
    if target in opening:
        return True

    # Edit distance rather than a first-letter rule: the initial consonant is the
    # single most commonly substituted sound ("Jarvis" -> "Travis", "Harvest"),
    # so anchoring on it rejects the very cases this exists to catch.
    #
    # The tolerance stays tight on purpose. A loose wake word is not a harmless
    # convenience — every false trigger opens the microphone and spends
    # transcription quota, so mishearings get caught and unrelated speech doesn't.
    allowed = 2 if len(target) > 4 else 1
    for candidate in opening.split()[:2]:
        candidate = "".join(ch for ch in candidate if ch.isalpha())
        if candidate and _edit_distance(candidate, target) <= allowed:
            return True
    return False


def _edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Damerau-Levenshtein distance, abandoned early once it exceeds `cap`.

    Transpositions count as one edit rather than two, which matters more than it
    sounds: "Jarvis" heard as "Travis" is a substitution plus a swapped pair, and
    under plain Levenshtein that scores 3 — far enough to look like an unrelated
    word. Adjacent-swap is a genuine speech-recognition failure mode, so counting
    it once is the accurate measure, not merely a more permissive one.
    """
    if abs(len(a) - len(b)) > cap:
        return cap + 1

    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j

    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = int(a[i - 1] != b[j - 1])
            rows[i][j] = min(
                rows[i - 1][j] + 1,          # deletion
                rows[i][j - 1] + 1,          # insertion
                rows[i - 1][j - 1] + cost,   # substitution
            )
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)  # transposition
        if min(rows[i]) > cap:
            return cap + 1

    return rows[len(a)][len(b)]


def strip_wake_word(text: str, wake_word: str) -> str:
    """Remove a leading wake word so the model doesn't see 'Jarvis, ' every turn."""
    words = text.split()
    if not words:
        return text
    first = "".join(ch for ch in words[0].lower() if ch.isalpha())
    if first and (first == wake_word.lower() or wake_word.lower() in first):
        return " ".join(words[1:]).lstrip(",. ") or text
    return text


def score_against(profile: Voiceprint, vector: np.ndarray) -> float:
    return profile.score(vector)
