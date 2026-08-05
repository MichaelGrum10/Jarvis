"""API surface: auth gates, bridge isolation, device queues."""

from __future__ import annotations

import datetime as dt

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
    assert res.status_code == 200
    return res.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_health_is_public(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    assert res.json()["tools"] > 20


def test_wrong_password_rejected(client):
    res = client.post("/api/auth/login", json={"password": "nope"})
    assert res.status_code == 401


def test_protected_routes_need_a_token(client):
    assert client.get("/api/chat/conversations").status_code == 401
    assert client.get("/api/device/commands").status_code == 401
    assert client.post("/api/autonomy/run", json={"goal": "x" * 20}).status_code == 401


def test_garbage_token_rejected(client):
    res = client.get("/api/chat/conversations", headers=auth("not-a-real-token"))
    assert res.status_code == 401


def test_login_then_access(client, token):
    res = client.get("/api/auth/me", headers=auth(token))
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_device_token_cannot_reach_bridge(client, token):
    """A phone that can chat must not be able to inject fake messages."""
    res = client.post(
        "/api/bridge/ingest",
        json={"hostname": "fake", "messages": []},
        headers=auth(token),
    )
    assert res.status_code == 401


def test_bridge_token_works_and_is_idempotent(client):
    headers = {"X-Bridge-Token": "test-bridge-token"}
    payload = {
        "hostname": "mac.local",
        "version": "0.1.0",
        "messages": [
            {
                "guid": "unique-guid-1",
                "sender": "+15551234567",
                "text": "Are we still on for tomorrow?",
                "sent_at": dt.datetime.now(dt.UTC).isoformat(),
            }
        ],
    }
    first = client.post("/api/bridge/ingest", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["stored"] == 1

    # Replaying the same guid must not duplicate the message.
    second = client.post("/api/bridge/ingest", json=payload, headers=headers)
    assert second.json()["stored"] == 0

    health = client.get("/api/bridge/health", headers=headers)
    assert health.json()["connected"] is True
    assert health.json()["hostname"] == "mac.local"


def test_location_update_and_command_queue(client, token):
    res = client.post(
        "/api/device/location", json={"lat": 42.36, "lon": -71.06}, headers=auth(token)
    )
    assert res.status_code == 200

    # Nothing queued yet.
    assert client.get("/api/device/commands", headers=auth(token)).json()["commands"] == []


def test_autonomy_blocked_when_disabled(client, token):
    res = client.post(
        "/api/autonomy/run", json={"goal": "rewrite everything please"}, headers=auth(token)
    )
    assert res.status_code == 403
    assert "disabled" in res.json()["detail"].lower()


def test_index_serves_the_app(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Jarvis" in res.text


def test_shortcut_style_minimal_payload_accepted(client):
    """An iOS Shortcut can't supply a chat.db GUID or a formatted timestamp, so
    the endpoint must accept just a sender and some text."""
    headers = {"X-Bridge-Token": "test-bridge-token"}
    payload = {
        "hostname": "iPhone",
        "messages": [{"sender": "+15559998888", "text": "running late, start without me"}],
    }
    res = client.post("/api/bridge/ingest", json=payload, headers=headers)
    assert res.status_code == 200
    assert res.json()["stored"] == 1

    # Firing twice for one message must not duplicate it.
    again = client.post("/api/bridge/ingest", json=payload, headers=headers)
    assert again.json()["stored"] == 0


def test_derived_guids_distinguish_different_senders(client):
    headers = {"X-Bridge-Token": "test-bridge-token"}
    body = lambda sender: {  # noqa: E731
        "messages": [{"sender": sender, "text": "same words"}]
    }
    first = client.post("/api/bridge/ingest", json=body("+15550000001"), headers=headers)
    second = client.post("/api/bridge/ingest", json=body("+15550000002"), headers=headers)
    assert first.json()["stored"] == 1
    assert second.json()["stored"] == 1


def test_explicit_guid_still_wins(client):
    """The Mac bridge's real GUIDs must keep taking precedence over derived ones."""
    headers = {"X-Bridge-Token": "test-bridge-token"}
    payload = {
        "messages": [
            {"guid": "real-chatdb-guid-42", "sender": "+15551112222", "text": "hello"}
        ]
    }
    assert client.post("/api/bridge/ingest", json=payload, headers=headers).json()["stored"] == 1
    assert client.post("/api/bridge/ingest", json=payload, headers=headers).json()["stored"] == 0
