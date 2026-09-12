"""The confirmation gate.

`confirm=True` on a tool used to be documentation: a sentence in the description
asking the model to check first, enforced by nothing. These tests pin the gate
being structural — a flagged tool does not run from a chat turn, it runs from
the confirm endpoint, once, from the device that was asked.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jarvis import confirm as confirmations
from jarvis.main import app
from jarvis.tools.base import ToolContext, ToolResult, registry

CALLS: list[dict] = []


async def _dangerous(to: str, body: str = "", ctx: ToolContext = None):
    CALLS.append({"to": to, "body": body, "device": ctx.device_id if ctx else None})
    return ToolResult.success({"sent": True}, display={"type": "device_action", "action": f"sent to {to}"})


@pytest.fixture(autouse=True)
def _tool():
    """A throwaway confirm-flagged tool, so nothing real can be triggered."""
    CALLS.clear()
    confirmations.reset()
    registry.tool(
        name="test_send",
        description="Sends something. Test only.",
        parameters={"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}}},
        confirm=True,
    )(_dangerous)
    yield
    registry._tools.pop("test_send", None)
    confirmations.reset()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def token(client):
    return client.post("/api/auth/login", json={"password": "test-password", "label": "a"}).json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------- the gate


async def test_a_confirm_tool_does_not_run_from_a_chat_turn():
    ctx = ToolContext(device_id="phone", conversation_id=1)
    result = await registry.dispatch("test_send", {"to": "a@b.com", "body": "hi"}, ctx)

    assert CALLS == [], "the tool ran without anyone confirming"
    assert result.ok is True                        # not a failure — a pause
    assert result.data["status"] == "pending_confirmation"
    assert result.display["type"] == "confirm"
    assert "a@b.com" in result.display["summary"]


async def test_the_model_is_told_to_stop_and_ask():
    result = await registry.dispatch("test_send", {"to": "a@b.com"}, ToolContext(device_id="p"))
    assert "pending_confirmation" in result.for_model()
    assert "Do not call this tool again" in result.data["instruction"]


async def test_it_runs_only_when_the_context_says_confirmed():
    ctx = ToolContext(device_id="phone", confirmed=True)
    result = await registry.dispatch("test_send", {"to": "a@b.com"}, ctx)

    assert result.data == {"sent": True}
    assert CALLS == [{"to": "a@b.com", "body": "", "device": "phone"}]


async def test_unflagged_tools_are_untouched():
    """Every other tool goes straight through, exactly as before."""
    async def plain(ctx: ToolContext = None):
        return ToolResult.success("fine")

    registry.tool(name="test_plain", description="x")(plain)
    try:
        result = await registry.dispatch("test_plain", {}, ToolContext(device_id="p"))
        assert result.data == "fine"
    finally:
        registry._tools.pop("test_plain", None)


# ------------------------------------------------------------ the endpoints


def _park(device_id="dev-1"):
    return confirmations.hold("test_send", {"to": "a@b.com", "body": "hi"}, device_id, None, "test send — to: a@b.com")


def test_confirming_runs_the_tool_exactly_once(client, token):
    me = client.get("/api/auth/me", headers=auth(token)).json()
    pending = _park(me["device_id"])

    first = client.post(f"/api/chat/confirm/{pending.id}", headers=auth(token))
    assert first.status_code == 200
    assert first.json()["ok"] is True
    assert first.json()["display"]["action"] == "sent to a@b.com"
    assert len(CALLS) == 1

    again = client.post(f"/api/chat/confirm/{pending.id}", headers=auth(token))
    assert again.status_code == 404
    assert len(CALLS) == 1, "a second tap sent it twice"


def test_only_the_device_that_was_asked_can_confirm(client, token):
    """A confirmation is a decision; it comes from the screen the question was
    put on, not from any device that knows the id."""
    pending = _park("some-other-device")

    res = client.post(f"/api/chat/confirm/{pending.id}", headers=auth(token))
    assert res.status_code == 404
    assert CALLS == []


def test_cancelling_throws_it_away(client, token):
    me = client.get("/api/auth/me", headers=auth(token)).json()
    pending = _park(me["device_id"])

    assert client.delete(f"/api/chat/confirm/{pending.id}", headers=auth(token)).json() == {"cancelled": True}
    assert client.post(f"/api/chat/confirm/{pending.id}", headers=auth(token)).status_code == 404
    assert CALLS == []


def test_an_expired_action_cannot_be_run(client, token, monkeypatch):
    me = client.get("/api/auth/me", headers=auth(token)).json()
    pending = _park(me["device_id"])
    monkeypatch.setattr(confirmations, "TTL_SECONDS", -1)

    assert client.post(f"/api/chat/confirm/{pending.id}", headers=auth(token)).status_code == 404
    assert CALLS == []


def test_confirm_endpoints_need_a_device_token(client):
    assert client.post("/api/chat/confirm/anything").status_code == 401
    assert client.delete("/api/chat/confirm/anything").status_code == 401


def test_the_summary_is_readable_and_never_huge():
    summary = confirmations.describe("mail_send", {"to": "a@b.com", "subject": "Hi", "body": "x" * 500, "cc": ""})
    assert summary.startswith("mail send — ")
    assert "to: a@b.com" in summary
    assert "cc" not in summary                      # empty values are noise
    assert len(summary) < 260


def test_every_real_confirm_tool_is_still_flagged():
    """The four this gate exists for. Losing the flag on one would be silent."""
    from jarvis.tools.base import load_all_tools

    load_all_tools()
    flagged = {t.name for t in registry.all() if t.confirm and not t.name.startswith("test_")}
    assert {"mail_send", "calendar_delete", "memory_forget"} <= flagged
