"""The Mac companion agent: the socket, the allowlist, and the write gate.

Three things here are worth a test rather than a careful reading, because all
three fail silently and only one of them fails loudly later:

- an unauthenticated socket must be closed before it is accepted, not after;
- an unknown command name must die at a dictionary lookup, nowhere near a shell;
- a command that writes must hand back a proposal even when the handler
  succeeded, because "it returned data so it must have done it" is exactly the
  wrong inference to allow.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.agentlink import AgentLink, AgentUnavailable
from jarvis.config import get_settings
from jarvis.main import app

SECRET = "test-agent-secret-0123456789abcdef"


def _load_mac_agent():
    """Import the Mac agent by path — it is not part of the server package."""
    path = Path(__file__).resolve().parents[2] / "mac-agent" / "jarvis_agent.py"
    spec = importlib.util.spec_from_file_location("jarvis_mac_agent", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent_mod = _load_mac_agent()


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


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_secret", SECRET)
    return SECRET


@pytest.fixture(autouse=True)
def _clean_link():
    """Each test gets a link with nothing attached to it."""
    from jarvis import agentlink

    agentlink._link = AgentLink()
    yield
    agentlink._link = AgentLink()


# ------------------------------------------------------------------ the socket


def test_socket_without_credentials_is_closed(client, secret):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/agent/ws"):
            pass


def test_socket_with_the_wrong_secret_is_closed(client, secret):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/agent/ws", headers={"X-Agent-Secret": "nearly-the-right-secret"}
        ):
            pass


def test_basic_auth_does_not_pass_for_the_agent_secret(client, secret):
    """Caddy's basic auth header must not be mistaken for the agent's own."""
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/agent/ws", headers={"Authorization": "Basic dXNlcjpwYXNzd29yZA=="}
        ):
            pass


def test_the_secret_still_works_as_a_bearer_token(client, secret, token):
    """For a direct connection to 127.0.0.1:8000, with no proxy in the way."""
    with client.websocket_connect(
        "/api/agent/ws", headers={"Authorization": f"Bearer {SECRET}"}
    ):
        assert client.get("/api/agent/status", headers=auth(token)).json()["connected"] is True


def test_socket_is_closed_when_no_secret_is_configured(client, monkeypatch):
    """An agent endpoint with no secret set is an open remote-control socket."""
    monkeypatch.setattr(get_settings(), "agent_secret", "")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/agent/ws", headers={"X-Agent-Secret": SECRET}):
            pass


def test_the_agent_sends_its_secret_where_caddy_will_not_eat_it():
    headers = agent_mod._headers(
        {"secret": SECRET, "label": "mac", "basic_auth": {"username": "m", "password": "pw"}}
    )
    assert headers["X-Agent-Secret"] == SECRET
    assert headers["Authorization"] == "Basic bTpwdw=="      # basic auth, not the secret


def test_no_basic_auth_header_when_none_is_configured():
    headers = agent_mod._headers({"secret": SECRET, "label": "mac"})
    assert "Authorization" not in headers


def test_the_right_secret_connects(client, secret, token):
    with client.websocket_connect(
        "/api/agent/ws",
        headers={"X-Agent-Secret": SECRET, "X-Agent-Label": "test-mac"},
    ):
        status = client.get("/api/agent/status", headers=auth(token)).json()
        assert status["connected"] is True
        assert status["label"] == "test-mac"


def test_status_needs_a_device_token(client):
    assert client.get("/api/agent/status").status_code == 401
    assert client.post("/api/agent/call", json={"command": "mail.search"}).status_code == 401


# --------------------------------------------------------------- the allowlist


def test_server_refuses_a_command_outside_the_allowlist(client, token):
    res = client.post(
        "/api/agent/call", headers=auth(token), json={"command": "system.run_shell", "args": {}}
    )
    assert res.status_code == 400
    assert "Unknown command" in res.json()["detail"]


def test_agent_refuses_a_command_outside_its_own_allowlist():
    """The second, independent check. A compromised server still gets nowhere."""
    reply = agent_mod.dispatch({"id": "x", "command": "system.run_shell", "args": {"cmd": "rm -rf /"}})
    assert reply["ok"] is False
    assert "Unknown command" in reply["error"]


def test_the_two_allowlists_agree():
    from jarvis.api.agent import KNOWN_COMMANDS

    assert KNOWN_COMMANDS == set(agent_mod.COMMANDS)


def test_malformed_commands_are_refused():
    assert agent_mod.dispatch({"id": "x", "command": None})["ok"] is False
    assert agent_mod.dispatch({"id": "x", "command": "mail.search", "args": "hi"})["ok"] is False


def test_arguments_must_be_the_type_they_claim():
    reply = agent_mod.dispatch({"id": "x", "command": "mail.search", "args": {"query": {"$ne": 1}}})
    assert reply["ok"] is False
    assert "must be a string" in reply["error"]

    reply = agent_mod.dispatch({"id": "x", "command": "mail.list_recent", "args": {"limit": "5"}})
    assert reply["ok"] is False


def test_integer_arguments_are_clamped_not_refused():
    reply = agent_mod.dispatch({"id": "x", "command": "mail.list_recent", "args": {"limit": 9999}})
    assert reply["ok"] is True
    assert len(reply["data"]["messages"]) == 50


# -------------------------------------------------------------- the write gate


@pytest.mark.parametrize("command,args", [
    ("mail.draft", {"to": "a@example.com", "subject": "hi", "body": "there"}),
    ("system.run_shortcut", {"name": "Goodnight", "input": ""}),
])
def test_writes_return_a_proposal_instead_of_acting(command, args):
    reply = agent_mod.dispatch({"id": "x", "command": command, "args": args})
    assert reply["ok"] is True
    assert reply["data"]["status"] == "pending_confirmation"
    assert reply["data"]["proposed"]


def test_reads_do_not_come_back_pending():
    reply = agent_mod.dispatch({"id": "x", "command": "calendar.list_events", "args": {"days": 2}})
    assert reply["data"].get("status") != "pending_confirmation"
    assert reply["data"]["events"]


def test_every_write_command_is_marked_as_one():
    """The registry is the gate. A handler that sends mail while flagged as a
    read would act on the spot, so the flags themselves are the assertion."""
    writes = {name for name, entry in agent_mod.COMMANDS.items() if entry["writes"]}
    assert writes == {"mail.draft", "system.run_shortcut"}


# ------------------------------------------------------------------- the link


async def test_call_without_an_agent_fails_immediately():
    with pytest.raises(AgentUnavailable):
        await AgentLink().call("mail.search", {})


def test_call_without_an_agent_is_a_503(client, token):
    res = client.post(
        "/api/agent/call", headers=auth(token), json={"command": "mail.search", "args": {}}
    )
    assert res.status_code == 503
    assert "not connected" in res.json()["detail"]


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


async def test_a_reply_resolves_the_waiting_call():
    link = AgentLink()
    await link.attach(FakeSocket(), "test")

    task = asyncio.ensure_future(link.call("mail.search", {"query": "rent"}))
    await asyncio.sleep(0)
    call_id = link._socket.sent[0]["id"]
    link.resolve({"id": call_id, "ok": True, "data": {"messages": []}})

    assert (await task)["ok"] is True


async def test_a_dropped_agent_fails_calls_in_flight_rather_than_hanging():
    link = AgentLink()
    await link.attach(FakeSocket(), "test")

    task = asyncio.ensure_future(link.call("screen.capture", {}))
    await asyncio.sleep(0)
    await link.detach("lid closed")

    with pytest.raises(AgentUnavailable):
        await task


async def test_a_reconnecting_agent_replaces_the_old_socket():
    """A Mac waking from sleep reconnects long before the dead socket times out."""
    link = AgentLink()
    await link.attach(FakeSocket(), "asleep")
    fresh = FakeSocket()
    await link.attach(fresh, "awake")

    assert link._socket is fresh
    assert link.describe()["label"] == "awake"


async def test_a_late_reply_to_a_dead_call_is_ignored():
    link = AgentLink()
    await link.attach(FakeSocket(), "test")
    link.resolve({"id": "no-such-call", "ok": True, "data": {}})   # must not raise


async def test_a_call_that_is_never_answered_times_out():
    link = AgentLink()
    await link.attach(FakeSocket(), "test")

    with pytest.raises(AgentUnavailable) as caught:
        await link.call("screen.capture", {}, timeout=0.05)
    assert "did not answer" in str(caught.value)
