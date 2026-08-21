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


def test_integer_arguments_are_clamped_not_refused(monkeypatch):
    """An absurd limit is capped, not rejected — and the cap is what reaches
    AppleScript, so nobody can ask Mail for a million messages."""
    seen = {}

    def fake(script, *args):
        seen["script"], seen["args"] = script, args
        return ""

    monkeypatch.setattr(agent_mod, "_osascript", fake)
    reply = agent_mod.dispatch({"id": "x", "command": "mail.list_recent", "args": {"limit": 9999}})

    assert reply["ok"] is True
    assert seen["args"] == (50,)
    assert seen["script"] == "mail_recent.applescript"


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


def test_reads_do_not_come_back_pending(monkeypatch):
    monkeypatch.setattr(
        agent_mod, "_osascript",
        lambda *a: "u\x1fStandup\x1fWork\x1f\x1ffalse\x1f2026,8,21,9,0\x1f2026,8,21,9,15\x1e",
    )
    reply = agent_mod.dispatch({"id": "x", "command": "calendar.list_events", "args": {"days": 2}})

    assert reply["data"].get("status") != "pending_confirmation"
    assert reply["data"]["events"]


def test_every_write_command_is_marked_as_one():
    """The registry is the gate. A handler that sends mail while flagged as a
    read would act on the spot, so the flags themselves are the assertion.

    voice.unmute is here and voice.mute is not, deliberately: turning a
    microphone off is the safe direction and must not need approval, while
    turning one on from a remote instruction is exactly what confirmation is
    for.
    """
    writes = {name for name, entry in agent_mod.COMMANDS.items() if entry["writes"]}
    assert writes == {"mail.draft", "system.run_shortcut", "voice.unmute"}


# ------------------------------------------------ parsing what AppleScript says

# Exactly the bytes the scripts emit: ASCII 31 between fields, ASCII 30 between
# records, dates as local wall-clock components.
FS = "\x1f"
RS = "\x1e"


def _canned(monkeypatch, output):
    monkeypatch.setattr(agent_mod, "_osascript", lambda *a, **k: output)


def test_calendar_events_are_parsed_and_sorted(monkeypatch):
    _canned(monkeypatch, (
        f"uid-2{FS}Dentist{FS}Home{FS}Baker St{FS}false{FS}2026,8,21,15,0{FS}2026,8,21,16,0{RS}"
        f"uid-1{FS}Standup{FS}Work{FS}{FS}false{FS}2026,8,21,9,30{FS}2026,8,21,9,45{RS}"
    ))
    out = agent_mod.dispatch({"id": "x", "command": "calendar.list_events", "args": {"days": 1}})

    assert out["ok"] is True
    events = out["data"]["events"]
    assert [e["summary"] for e in events] == ["Standup", "Dentist"]     # by time, not calendar
    assert events[0]["start"].startswith("2026-08-21T09:30")
    assert events[1]["location"] == "Baker St"
    assert out["data"]["count"] == 2


def test_a_summary_containing_a_comma_stays_one_event(monkeypatch):
    """The reason the delimiters are control characters and not commas."""
    _canned(monkeypatch, f"u{FS}Lunch, then dentist{FS}Home{FS}{FS}false{FS}2026,8,21,12,0{FS}2026,8,21,13,0{RS}")
    out = agent_mod.dispatch({"id": "x", "command": "calendar.list_events", "args": {}})

    assert out["data"]["count"] == 1
    assert out["data"]["events"][0]["summary"] == "Lunch, then dentist"


def test_all_day_events_are_flagged(monkeypatch):
    _canned(monkeypatch, f"u{FS}Bank holiday{FS}Home{FS}{FS}true{FS}2026,8,25,0,0{FS}2026,8,26,0,0{RS}")
    out = agent_mod.dispatch({"id": "x", "command": "calendar.list_events", "args": {}})

    assert out["data"]["events"][0]["all_day"] is True


def test_mail_senders_are_split_into_name_and_address(monkeypatch):
    _canned(monkeypatch, (
        f"id-1{FS}Lease renewal{FS}Ada Lovelace <ada@example.com>{FS}false{FS}2026,8,21,9,0{RS}"
    ))
    out = agent_mod.dispatch({"id": "x", "command": "mail.list_recent", "args": {"limit": 5}})

    message = out["data"]["messages"][0]
    assert message["sender"] == "Ada Lovelace"
    assert message["sender_email"] == "ada@example.com"
    assert message["unread"] is True            # read status false means unread
    assert message["date"].startswith("2026-08-21T09:00")


def test_a_bare_address_still_parses(monkeypatch):
    _canned(monkeypatch, f"id-1{FS}Hello{FS}someone@example.com{FS}true{FS}2026,8,21,9,0{RS}")
    out = agent_mod.dispatch({"id": "x", "command": "mail.list_recent", "args": {}})

    message = out["data"]["messages"][0]
    assert message["sender"] == "someone@example.com"
    assert message["unread"] is False


def test_an_empty_inbox_is_not_an_error(monkeypatch):
    _canned(monkeypatch, "")
    out = agent_mod.dispatch({"id": "x", "command": "mail.list_recent", "args": {}})

    assert out["ok"] is True
    assert out["data"]["messages"] == []


def test_truncated_records_are_skipped_rather_than_crashing(monkeypatch):
    """A half-written record loses one event; an exception loses all of them."""
    _canned(monkeypatch, f"u{FS}Broken{RS}u2{FS}Fine{FS}Home{FS}{FS}false{FS}2026,8,21,9,0{FS}2026,8,21,10,0{RS}")
    out = agent_mod.dispatch({"id": "x", "command": "calendar.list_events", "args": {}})

    assert [e["summary"] for e in out["data"]["events"]] == ["Fine"]


def test_mail_search_says_what_it_actually_searched(monkeypatch):
    _canned(monkeypatch, "")
    out = agent_mod.dispatch({"id": "x", "command": "mail.search", "args": {"query": "rent"}})

    assert "not message bodies" in out["data"]["searched"]


def test_an_empty_search_is_refused(monkeypatch):
    _canned(monkeypatch, "")
    out = agent_mod.dispatch({"id": "x", "command": "mail.search", "args": {"query": "   "}})

    assert out["ok"] is False


def test_a_refused_apple_event_names_the_settings_pane():
    explained = agent_mod._explain(
        "execution error: Not authorized to send Apple events to Mail. (-1743)"
    )
    assert "Automation" in explained
    assert "-1743" in explained          # the original is kept for searching


def test_a_missing_helper_script_says_so(monkeypatch):
    monkeypatch.setattr(agent_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(agent_mod, "SCRIPTS", Path("/nowhere/at/all"))
    out = agent_mod.dispatch({"id": "x", "command": "calendar.list_events", "args": {}})

    assert out["ok"] is False
    assert "Missing helper script" in out["error"]


def test_capabilities_that_need_macos_say_so_off_macos():
    """The agent is importable and testable on Linux; it just cannot do this."""
    out = agent_mod.dispatch({"id": "x", "command": "mail.list_recent", "args": {}})

    assert out["ok"] is False
    assert "macOS" in out["error"]


def test_the_applescripts_exist_and_take_their_arguments_from_argv():
    """No command string anywhere: arguments arrive through `on run argv`."""
    scripts = Path(__file__).resolve().parents[2] / "mac-agent" / "scripts"
    for name in ("calendar_list", "mail_recent", "mail_search"):
        source = (scripts / f"{name}.applescript").read_text()
        assert "on run argv" in source
        assert "do shell script" not in source


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


# ------------------------------------------------------------- the wake word


def test_the_mac_only_plays_clips_from_its_own_server(monkeypatch):
    """An agent that plays arbitrary URLs is an agent that fetches arbitrary
    URLs — on a machine holding the mail and the calendar."""
    played = []
    monkeypatch.setattr(agent_mod, "_afplay", lambda url: played.append(url) or True)
    monkeypatch.setitem(agent_mod.LISTENER, "server", "wss://michael-jarvis.duckdns.org/api/agent/ws")

    good = agent_mod.dispatch({"id": "x", "command": "audio.play", "args": {
        "url": "https://michael-jarvis.duckdns.org/api/voice/audio/abc?t=sig"}})
    assert good["ok"] is True
    assert len(played) == 1

    for hostile in (
        "https://evil.example.com/track.mp3",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
    ):
        refused = agent_mod.dispatch({"id": "x", "command": "audio.play", "args": {"url": hostile}})
        assert refused["ok"] is False, hostile
    assert len(played) == 1, "only the server's own clip should have played"


def test_muting_is_read_from_disk_every_time(tmp_path, monkeypatch):
    """The point of a hard mute is that editing the file works — while the agent
    is running, and while the socket is down."""
    config = tmp_path / ".jarvis-agent.json"
    config.write_text('{"secret": "x", "muted": false}')
    monkeypatch.setattr(agent_mod, "CONFIG", config)

    assert agent_mod._muted() is False
    config.write_text('{"secret": "x", "muted": true}')
    assert agent_mod._muted() is True


def test_an_unreadable_config_stays_muted(tmp_path, monkeypatch):
    """Unable to prove muting is off is not the same as it being on."""
    monkeypatch.setattr(agent_mod, "CONFIG", tmp_path / "does-not-exist.json")
    assert agent_mod._muted() is True


def test_muting_takes_effect_without_confirmation(tmp_path, monkeypatch):
    config = tmp_path / ".jarvis-agent.json"
    config.write_text('{"secret": "x", "muted": false}')
    monkeypatch.setattr(agent_mod, "CONFIG", config)

    reply = agent_mod.dispatch({"id": "x", "command": "voice.mute", "args": {}})
    assert reply["ok"] is True
    assert reply["data"]["muted"] is True          # acted, not proposed
    assert agent_mod._muted() is True


def test_unmuting_does_not(tmp_path, monkeypatch):
    """Turning a microphone on from a remote instruction is exactly what the
    confirmation gate is for."""
    config = tmp_path / ".jarvis-agent.json"
    config.write_text('{"secret": "x", "muted": true}')
    monkeypatch.setattr(agent_mod, "CONFIG", config)

    reply = agent_mod.dispatch({"id": "x", "command": "voice.unmute", "args": {}})
    assert reply["data"]["status"] == "pending_confirmation"
    assert agent_mod._muted() is True, "the microphone must still be closed"


async def test_a_wake_event_with_junk_audio_is_dropped(monkeypatch):
    """Anything arriving unasked gets checked before it reaches Whisper."""
    from jarvis.api import agent as agent_api

    called = []
    monkeypatch.setattr(
        "jarvis.api.voice.transcribe_bytes",
        lambda *a, **k: called.append(a) or "",
    )

    await agent_api.handle_wake({"event": "wake", "audio": "not base64 at all!!"})
    await agent_api.handle_wake({"event": "wake", "audio": ""})
    assert called == []


async def test_a_wake_event_is_transcribed_answered_and_played(monkeypatch):
    """The whole round trip: heard on the Mac, answered on the server, spoken
    back through the Mac's own speakers."""
    import base64

    from jarvis.api import agent as agent_api
    from jarvis.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "elevenlabs_api_key", "k")
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "v")

    async def fake_transcribe(data, settings, language=""):
        return "what is on my calendar"

    class FakeOutcome:
        reply = "Nothing at all today, sir."
        error = ""

    class FakeAgent:
        def __init__(self, *a, **k): pass
        async def run(self, said, ctx=None):
            assert said == "what is on my calendar"
            return FakeOutcome()

    monkeypatch.setattr("jarvis.api.voice.transcribe_bytes", fake_transcribe)
    monkeypatch.setattr("jarvis.agent.loop.Agent", FakeAgent)

    link = AgentLink()
    sent = []

    class Socket:
        async def send_json(self, payload): sent.append(payload)

    await link.attach(Socket(), "michaels-laptop")
    monkeypatch.setattr("jarvis.api.agent.get_link", lambda: link)

    async def answer():
        await agent_api.handle_wake({"event": "wake", "audio": base64.b64encode(b"pcm" * 400).decode()})

    task = asyncio.ensure_future(answer())
    await asyncio.sleep(0.05)
    assert sent, "nothing was sent to the Mac"
    assert sent[0]["command"] == "audio.play"
    url = sent[0]["args"]["url"]
    assert url.startswith("https://")
    assert "/api/voice/audio/" in url and "t=" in url

    link.resolve({"id": sent[0]["id"], "ok": True, "data": {"played": True}})
    await task


def test_the_mac_stamps_events_with_its_own_clock():
    """AppleScript sends wall-clock components with no offset. This runs on the
    Mac, so `.astimezone()` attaches the Mac's zone — which is the right one,
    and the reason the conversion belongs here rather than on the server."""
    stamped = agent_mod._iso("2026,8,21,20,0")

    assert stamped.startswith("2026-08-21T20:00:00")
    assert stamped[-6] in "+-", f"no offset attached: {stamped}"


def test_a_malformed_stamp_is_empty_rather_than_an_exception():
    assert agent_mod._iso("nonsense") == ""
    assert agent_mod._iso("") == ""
