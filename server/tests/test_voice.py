"""Transcription endpoint: auth, validation, and Whisper error mapping."""

from __future__ import annotations

import io

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def token(client):
    res = client.post("/api/auth/login", json={"password": "test-password", "label": "pytest"})
    return res.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def audio(size: int = 4096, name: str = "speech.webm", mime: str = "audio/webm"):
    return {"audio": (name, io.BytesIO(b"\x1a\x45\xdf\xa3" + b"\x00" * size), mime)}


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def patch_groq(monkeypatch, response):
    """Swap the outbound Whisper call; the point is our handling, not Groq's."""
    captured = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, files=None):
            captured["url"] = url
            captured["files"] = files
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return captured


def test_transcribe_requires_auth(client):
    res = client.post("/api/voice/transcribe", files=audio())
    assert res.status_code == 401


def test_transcribe_returns_text(client, token, monkeypatch):
    captured = patch_groq(monkeypatch, FakeResponse(200, {"text": "  book me a haircut  "}))
    res = client.post("/api/voice/transcribe", files=audio(), headers=auth(token))
    assert res.status_code == 200
    assert res.json()["text"] == "book me a haircut"
    assert captured["url"].endswith("/audio/transcriptions")


def test_language_hint_is_forwarded(client, token, monkeypatch):
    captured = patch_groq(monkeypatch, FakeResponse(200, {"text": "hola"}))
    res = client.post(
        "/api/voice/transcribe", files=audio(), data={"language": "es"}, headers=auth(token)
    )
    assert res.status_code == 200
    assert captured["files"]["language"] == (None, "es")


def test_empty_upload_rejected(client, token):
    res = client.post(
        "/api/voice/transcribe",
        files={"audio": ("empty.webm", io.BytesIO(b""), "audio/webm")},
        headers=auth(token),
    )
    assert res.status_code == 400


def test_oversized_upload_rejected(client, token):
    big = {"audio": ("big.webm", io.BytesIO(b"\x00" * (21 * 1024 * 1024)), "audio/webm")}
    res = client.post("/api/voice/transcribe", files=big, headers=auth(token))
    assert res.status_code == 413


def test_unusual_mime_is_accepted_not_rejected(client, token, monkeypatch):
    """Browsers label the same container inconsistently — refusing on mime alone
    would break recording on whole platforms."""
    patch_groq(monkeypatch, FakeResponse(200, {"text": "ok"}))
    res = client.post(
        "/api/voice/transcribe",
        files=audio(mime="application/octet-stream"),
        headers=auth(token),
    )
    assert res.status_code == 200


def test_rate_limit_surfaces_as_429(client, token, monkeypatch):
    patch_groq(monkeypatch, FakeResponse(429, text="slow down"))
    res = client.post("/api/voice/transcribe", files=audio(), headers=auth(token))
    assert res.status_code == 429


def test_upstream_error_becomes_502(client, token, monkeypatch):
    patch_groq(monkeypatch, FakeResponse(500, text="boom"))
    res = client.post("/api/voice/transcribe", files=audio(), headers=auth(token))
    assert res.status_code == 502


def test_network_failure_becomes_502(client, token, monkeypatch):
    patch_groq(monkeypatch, httpx.ConnectError("no route"))
    res = client.post("/api/voice/transcribe", files=audio(), headers=auth(token))
    assert res.status_code == 502
