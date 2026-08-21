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


def test_every_imported_binding_actually_exists():
    """The exact failure seen in the browser: "Importing binding name
    'startHudPanels' is not found". Cheap to assert, and it fails here rather
    than on someone's phone — `node --check` is syntax-only and cannot see it.

    Checks every module rather than only app.js: the modules import each other,
    and a missing binding kills the whole graph wherever it is.
    """
    import re
    from pathlib import Path

    web = Path(__file__).resolve().parents[2] / "web"
    modules = sorted(p.name for p in web.glob("*.js"))

    for module in modules:
        source_text = (web / module).read_text()
        for match in re.finditer(
            r"import\s*\{([^}]+)\}\s*from\s*['\"]/static/([\w.]+)\.js[^'\"]*['\"]", source_text
        ):
            target = web / f"{match.group(2)}.js"
            assert target.is_file(), f"{module} imports from {target.name}, which does not exist"
            exported = target.read_text()

            for entry in match.group(1).split(","):
                entry = entry.strip()
                if not entry:
                    continue
                # `setState as reactorState` — the export is the name on the
                # left. Checking the alias asserts against a name that by
                # definition does not exist in the other file.
                name = re.split(r"\s+as\s+", entry)[0].strip()
                pattern = rf"export\s+(async\s+)?(function|const|let|class)\s+{re.escape(name)}\b"
                assert re.search(pattern, exported), (
                    f"{module} imports {name} from {target.name}, which does not export it"
                )


def test_every_element_the_scripts_reach_for_exists():
    """`$('gone').classList` throws, and takes the rest of the module with it.

    Deleting an element from index.html and missing one caller is the exact
    shape of that mistake, and it is invisible until the page is open. Ids the
    scripts build themselves (modal bodies, the skills editor) are found by
    scanning the JS for the markup it writes, so they need no allowlist to
    maintain.
    """
    import re
    from pathlib import Path

    web = Path(__file__).resolve().parents[2] / "web"
    html = (web / "index.html").read_text()
    available = set(re.findall(r'id="([\w-]+)"', html))

    scripts = {p.name: p.read_text() for p in web.glob("*.js")}
    for source in scripts.values():
        # Anything the script itself renders counts as existing.
        available |= set(re.findall(r'id="([\w-]+)"', source))
        available |= set(re.findall(r"\.id\s*=\s*['\"]([\w-]+)['\"]", source))

    missing = []
    for name, source in scripts.items():
        for pattern in (r"\$\('([\w-]+)'\)", r"getElementById\('([\w-]+)'\)"):
            for element in re.findall(pattern, source):
                if element not in available:
                    missing.append(f"{name} reaches for #{element}, which nothing creates")

    assert not missing, "\n".join(sorted(set(missing)))


def test_the_service_worker_caches_the_versions_the_page_asks_for():
    """A stale shell is the worst kind of bug here: the modules import each
    other, so a service worker holding one at v19 while the page requests v20
    produces a dead page that a reload cannot fix — the fix lives inside the
    file that is stuck."""
    import re
    from pathlib import Path

    web = Path(__file__).resolve().parents[2] / "web"
    sw = (web / "sw.js").read_text()
    version = re.search(r"const V = '(\d+)'", sw).group(1)

    asked = set()
    for name in ("index.html", "app.js", "galaxy.js", "hud.js"):
        asked |= set(re.findall(r"/static/[\w./]+\?v=(\d+)", (web / name).read_text()))

    assert asked, "no versioned asset URLs found — has the scheme changed?"
    assert asked == {version}, (
        f"sw.js caches v{version} but the app requests v{sorted(asked)}"
    )

    # Every module the page loads has to be in the shell, or an offline open
    # fails on a static import with no error path to catch it.
    for module in ("app.js", "voice.js", "hud.js", "galaxy.js", "reactor.js"):
        assert f"/static/{module}?v=${{V}}" in sw, f"{module} is missing from the service worker shell"


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
