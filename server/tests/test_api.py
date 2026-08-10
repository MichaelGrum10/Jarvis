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


def test_scripts_are_served_with_revalidation(client):
    """The modules import each other. A browser holding one from cache while
    fetching another fresh gets an import error and a completely dead page —
    every button unbound, nothing to click, no error visible."""
    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "no-cache" in response.headers.get("cache-control", "")


def test_the_hud_export_the_app_imports_actually_exists():
    """The exact failure seen in the browser: "Importing binding name
    'startHudPanels' is not found". Cheap to assert, and it fails at build time
    rather than on someone's phone."""
    import re
    from pathlib import Path

    web = Path(__file__).resolve().parents[2] / "web"
    app_js = (web / "app.js").read_text()

    for match in re.finditer(r"import\s*\{([^}]+)\}\s*from\s*'/static/(\w+)\.js[^']*'", app_js):
        names = [n.strip() for n in match.group(1).split(",") if n.strip()]
        source = (web / f"{match.group(2)}.js").read_text()
        for name in names:
            pattern = rf"export\s+(async\s+)?(function|const|let|class)\s+{re.escape(name)}\b"
            assert re.search(pattern, source), (
                f"app.js imports {name} from {match.group(2)}.js, which does not export it"
            )


def test_the_entry_document_is_never_cached(client):
    """It carries the recovery code, so a stale copy cannot repair itself — the
    fix lives inside the file that is stuck."""
    response = client.get("/")

    assert "no-store" in response.headers.get("cache-control", "")


def test_reset_depends_on_nothing_already_cached(client):
    """The escape hatch has to work when the app itself will not start, which is
    exactly when the caches are the problem."""
    response = client.get("/reset")

    assert response.status_code == 200
    assert "no-store" in response.headers.get("cache-control", "")
    body = response.text
    assert "caches.delete" in body
    assert "unregister" in body


def test_every_module_url_carries_the_same_version():
    """A cached copy lives at a different URL and cannot be served, so a
    mismatch resolves itself. That only holds while the versions agree — one
    stale number reintroduces exactly the bug this prevents."""
    import re
    from pathlib import Path

    web = Path(__file__).resolve().parents[2] / "web"
    versions = set()
    for name in ("app.js", "index.html", "sw.js"):
        text = (web / name).read_text()
        versions.update(re.findall(r"\?v=(\d+)", text))
        versions.update(re.findall(r"const V = '(\d+)'", text))

    assert len(versions) == 1, f"module URLs disagree on version: {sorted(versions)}"
