"""Enrol / verify / reject, through the real HTTP surface."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jarvis.api.identity import strip_wake_word, wake_word_present
from jarvis.main import app
from tests.test_voiceprint import SPEAKER_A, SPEAKER_B, synth, to_wav


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def token(client):
    return client.post(
        "/api/auth/login", json={"password": "test-password", "label": "pytest"}
    ).json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def wav_file(formants, f0, seed=0, **kwargs):
    return {"audio": ("speech.wav", to_wav(synth(f0, formants, seed=seed, **kwargs)), "audio/wav")}


def enrol_owner(client, token, samples=3):
    for i in range(samples):
        response = client.post(
            "/api/identity/enroll",
            files=wav_file(SPEAKER_A, 118 + i, seed=i),
            data={"reset": "true"} if i == 0 else {},
            headers=auth(token),
        )
        assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------- auth


def test_identity_endpoints_require_auth(client):
    assert client.get("/api/identity/status").status_code == 401
    assert client.post("/api/identity/enroll", files=wav_file(SPEAKER_A, 120)).status_code == 401


def test_status_before_enrolment(client, token):
    client.delete("/api/identity/enroll", headers=auth(token))
    body = client.get("/api/identity/status", headers=auth(token)).json()
    assert body["enrolled"] is False
    assert "not a security boundary" in body["caveat"]


# ---------------------------------------------------------------- enrolment


def test_enrolment_accumulates_and_reports_readiness(client, token):
    client.delete("/api/identity/enroll", headers=auth(token))
    final = enrol_owner(client, token)
    assert final["samples"] == 3
    assert final["ready"] is True
    assert final["cohesion"] > 0.5

    status = client.get("/api/identity/status", headers=auth(token)).json()
    assert status["enrolled"] is True and status["ready"] is True


def test_reset_starts_a_fresh_profile(client, token):
    enrol_owner(client, token)
    body = client.post(
        "/api/identity/enroll",
        files=wav_file(SPEAKER_A, 120, seed=42),
        data={"reset": "true"},
        headers=auth(token),
    ).json()
    assert body["samples"] == 1, "reset should discard previous samples"


def test_silent_enrolment_is_rejected_with_a_reason(client, token):
    import io
    import wave

    import numpy as np

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(np.zeros(32_000, dtype="<i2").tobytes())

    response = client.post(
        "/api/identity/enroll",
        files={"audio": ("silence.wav", buffer.getvalue(), "audio/wav")},
        headers=auth(token),
    )
    assert response.status_code == 400
    assert "silent" in response.json()["detail"].lower()


# ---------------------------------------------------------------- verification


def test_owner_verifies_and_impostor_does_not(client, token):
    client.delete("/api/identity/enroll", headers=auth(token))
    enrol_owner(client, token)

    owner = client.post(
        "/api/identity/verify", files=wav_file(SPEAKER_A, 121, seed=90), headers=auth(token)
    ).json()
    impostor = client.post(
        "/api/identity/verify", files=wav_file(SPEAKER_B, 210, seed=91), headers=auth(token)
    ).json()

    assert owner["score"] > impostor["score"] + 0.15
    assert owner["match"] is True


def test_verify_without_enrolment_is_404(client, token):
    client.delete("/api/identity/enroll", headers=auth(token))
    response = client.post(
        "/api/identity/verify", files=wav_file(SPEAKER_A, 120), headers=auth(token)
    )
    assert response.status_code == 404


def test_clearing_enrolment_works(client, token):
    enrol_owner(client, token)
    client.delete("/api/identity/enroll", headers=auth(token))
    assert client.get("/api/identity/status", headers=auth(token)).json()["enrolled"] is False


# ---------------------------------------------------------------- enforcement


def test_unrecognised_voice_is_refused_and_alerted(client, token, monkeypatch):
    """The whole point: with enforcement on, a stranger gets nothing back and an
    alert is recorded for the owner to find."""
    from jarvis.config import get_settings

    client.delete("/api/identity/enroll", headers=auth(token))
    enrol_owner(client, token)

    settings = get_settings()
    monkeypatch.setattr(settings, "require_voice_match", True)
    monkeypatch.setattr(settings, "voice_alert_email", False)  # no SMTP in tests

    response = client.post(
        "/api/voice/transcribe",
        files=wav_file(SPEAKER_B, 210, seed=77),
        headers=auth(token),
    )
    assert response.status_code == 403
    assert "doesn't sound like you" in response.json()["detail"]

    alerts = client.get("/api/identity/alerts", headers=auth(token)).json()["alerts"]
    assert len(alerts) >= 1
    assert alerts[0]["score"] < alerts[0]["threshold"]


def test_enforcement_without_enrolment_fails_loudly(client, token, monkeypatch):
    """Refusing everything because nothing is enrolled would be baffling; say so."""
    from jarvis.config import get_settings

    client.delete("/api/identity/enroll", headers=auth(token))
    monkeypatch.setattr(get_settings(), "require_voice_match", True)

    response = client.post(
        "/api/voice/transcribe", files=wav_file(SPEAKER_A, 120), headers=auth(token)
    )
    assert response.status_code == 409
    assert "no voice is enrolled" in response.json()["detail"].lower()


def test_alerts_can_be_acknowledged(client, token):
    body = client.post("/api/identity/alerts/acknowledge", headers=auth(token)).json()
    assert "acknowledged" in body
    status = client.get("/api/identity/status", headers=auth(token)).json()
    assert status["unacknowledged_alerts"] == 0


# ---------------------------------------------------------------- wake word


@pytest.mark.parametrize(
    "text",
    [
        "Jarvis what is on my calendar",
        "jarvis, read my messages",
        "Travis what's the weather",     # recognisers substitute the initial consonant
        "Harvis open spotify",
        "Jarvus what time is it",
    ],
)
def test_wake_word_tolerates_misrecognition(text):
    """Short names get mangled constantly. A wake word that only fires on a
    perfect transcription feels broken in normal use."""
    assert wake_word_present(text, "jarvis")


@pytest.mark.parametrize(
    "text",
    [
        "what is on my calendar today",
        "read me the news please",
        "tell me about the service level agreement jarvis",  # too late to count
        "marvellous weather we are having",
        "just a moment please",
    ],
)
def test_wake_word_absent(text):
    """The other half of the bargain: a loose matcher would open the microphone
    on ordinary speech, spending quota and recording things it shouldn't."""
    assert not wake_word_present(text, "jarvis")


def test_wake_word_is_stripped_from_the_prompt():
    assert strip_wake_word("Jarvis, what's on my calendar?", "jarvis") == "what's on my calendar?"
    assert strip_wake_word("what's on my calendar?", "jarvis") == "what's on my calendar?"


def test_empty_wake_word_disables_the_check():
    assert wake_word_present("anything at all", "")
