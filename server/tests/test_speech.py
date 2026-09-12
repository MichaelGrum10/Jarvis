"""The cloned voice: streaming, caching, tickets, and the fallback.

The thing this file is really guarding is the API key. It is billed per
character, it lives on the server, and every path here exists to keep it there —
the browser is handed a URL that plays one line, never a credential and never a
way to spend one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jarvis.config import get_settings
from jarvis.integrations import fish, tts
from jarvis.main import app
from jarvis.security import issue_speech_ticket

CLIP = b"ID3\x04\x00" + b"\xff\xfb\x90d" * 400        # enough to pass the size floor
KEY = "fish-test-key-never-leaves-here"
VOICE = "802e3bc2b27e49c2995d23ef70e6ac89"


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
def voice(monkeypatch, tmp_path):
    """Fish configured, with the cache in a scratch directory."""
    settings = get_settings()
    monkeypatch.setattr(settings, "fish_api_key", KEY)
    monkeypatch.setattr(settings, "fish_voice_id", VOICE)
    monkeypatch.setattr(settings, "fish_model", "s2-pro")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return settings


@pytest.fixture
def fake_api(monkeypatch, voice):
    """Stand in for tts.stream, and record what it was asked to say."""
    calls = []

    async def fake_stream(text, settings=None):
        calls.append(text)
        for i in range(0, len(CLIP), 512):
            yield CLIP[i:i + 512]

    monkeypatch.setattr("jarvis.api.voice.tts.stream", fake_stream)
    return calls


class _FakeHTTP:
    """Enough of httpx.AsyncClient to exercise the real streaming and caching."""

    def __init__(self, status=200, body=CLIP):
        self.calls = []
        self.status = status
        self.body = body
        self.gets = {}            # url suffix -> (status, json body) for verify()

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    def stream(self, method, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        outer = self

        class Response:
            status_code = outer.status

            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

            async def aiter_bytes(self):
                for i in range(0, len(outer.body), 512):
                    yield outer.body[i:i + 512]

            async def aread(self):
                return outer.body

        return Response()

    async def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        for suffix, (status, body) in self.gets.items():
            if url.endswith(suffix):
                return _Json(status, body)
        return _Json(404, {})


class _Json:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = b"{}"

    def json(self):
        return self._body


@pytest.fixture
def fake_http(monkeypatch):
    import httpx

    fake = _FakeHTTP()
    monkeypatch.setattr(httpx, "AsyncClient", fake)
    return fake


# ------------------------------------------------------------------ the key


def test_the_key_is_never_in_any_response(client, token, fake_api):
    prepared = client.post("/api/voice/speak", headers=auth(token), json={"text": "Good evening."})
    audio = client.get(prepared.json()["url"])
    status = client.get("/api/voice/speech-status", headers=auth(token))

    blob = prepared.text + status.text + repr(dict(audio.headers)) + repr(dict(prepared.headers))
    assert KEY not in blob
    assert VOICE not in blob          # the voice id is a credential too


def test_speaking_needs_a_device_token(client, fake_api):
    assert client.post("/api/voice/speak", json={"text": "hello"}).status_code == 401
    assert client.get("/api/voice/speech-status").status_code == 401


def test_audio_without_a_ticket_is_refused(client, token, fake_api):
    key = client.post(
        "/api/voice/speak", headers=auth(token), json={"text": "Good evening."}
    ).json()["url"].split("/")[-1].split("?")[0]

    assert client.get(f"/api/voice/audio/{key}").status_code == 401
    assert client.get(f"/api/voice/audio/{key}?t=forged").status_code == 401


def test_a_ticket_is_bound_to_one_line(client, token, fake_api, voice):
    """A leaked URL plays back one line the owner already heard. It is not a
    token, and it cannot be pointed at a different line."""
    first = client.post("/api/voice/speak", headers=auth(token), json={"text": "One."}).json()
    other_key = tts.voice_key("Something else entirely.", voice)
    stolen = first["url"].split("?t=")[1]

    assert client.get(f"/api/voice/audio/{other_key}?t={stolen}").status_code == 403


def test_an_expired_ticket_is_refused(client, token, fake_api, voice, monkeypatch):
    import jarvis.security as security

    monkeypatch.setattr(security, "SPEECH_TICKET_SECONDS", -1)
    key = tts.voice_key("Anything.", voice)
    ticket = issue_speech_ticket(key, voice)

    assert client.get(f"/api/voice/audio/{key}?t={ticket}").status_code == 401


# ---------------------------------------------------------------- streaming


def test_audio_streams_back_and_is_mp3(client, token, fake_api):
    prepared = client.post("/api/voice/speak", headers=auth(token), json={"text": "Good evening."})
    res = client.get(prepared.json()["url"])

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("audio/mpeg")
    assert res.content == CLIP


def test_the_fish_request_is_the_sdks_request(client, token, voice, fake_http):
    """Shape copied from fish-audio-sdk 1.3.0, not remembered. The model goes in
    a header — put it in the body and Fish silently uses its default."""
    import ormsgpack

    url = client.post("/api/voice/speak", headers=auth(token), json={"text": "Good evening, sir."}).json()["url"]
    res = client.get(url)
    assert res.status_code == 200
    assert res.headers["X-Speech-Source"] == "fish"

    call = fake_http.calls[0]
    assert call["url"] == "https://api.fish.audio/v1/tts"
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["headers"]["Content-Type"] == "application/msgpack"
    assert call["headers"]["model"] == "s2-pro"
    body = ormsgpack.unpackb(call["content"])
    assert body["text"] == "Good evening, sir."
    assert body["reference_id"] == VOICE
    assert body["format"] == "mp3"
    assert body["latency"] == "balanced"


def test_the_second_ask_comes_from_cache(client, token, voice, fake_http):
    """The boot greeting is said every day and is identical every time.

    Faked at the HTTP layer rather than at tts.stream, because stream() is
    where the caching lives — stubbing it would test the stub.
    """
    for _ in range(2):
        url = client.post(
            "/api/voice/speak", headers=auth(token), json={"text": "Good evening, sir."}
        ).json()["url"]
        assert client.get(url).status_code == 200

    assert len(fake_http.calls) == 1, "the same line was rendered twice"


def test_a_cached_clip_says_it_came_from_cache(client, token, voice, fake_http):
    url = client.post("/api/voice/speak", headers=auth(token), json={"text": "Twice."}).json()["url"]
    assert client.get(url).headers["X-Speech-Source"] == "fish"

    again = client.post("/api/voice/speak", headers=auth(token), json={"text": "Twice."}).json()
    assert again["cached"] is True
    assert client.get(again["url"]).headers["X-Speech-Source"] == "cache"


def test_whitespace_does_not_mint_a_second_cache_entry(voice):
    assert tts.voice_key(tts.speakable_length("Good   evening,\n sir."), voice) == \
           tts.voice_key(tts.speakable_length("Good evening, sir."), voice)


def test_changing_the_voice_or_model_re_renders(voice, monkeypatch):
    """The cache key carries both: a line rendered in one voice must not be
    served as if another said it."""
    line = tts.speakable_length("Good evening, sir.")
    first = tts.voice_key(line, voice)

    monkeypatch.setattr(voice, "fish_model", "s1")
    assert tts.voice_key(line, voice) != first

    monkeypatch.setattr(voice, "fish_model", "s2-pro")
    monkeypatch.setattr(voice, "fish_voice_id", "another-voice")
    assert tts.voice_key(line, voice) != first


def test_a_truncated_stream_is_not_left_in_the_cache(voice, monkeypatch):
    """A clip that dies halfway must not be served forever after."""
    import httpx

    class Boom:
        async def __aenter__(self): raise httpx.ConnectError("network gone")
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: Boom())

    async def drain():
        async for _ in tts.stream("Never finishes.", voice):
            pass

    with pytest.raises(tts.SpeechError):
        import asyncio
        asyncio.run(drain())

    assert list(tts.cache_dir(voice).glob("*")) == []


# ----------------------------------------------------------------- fallback


def test_unconfigured_says_so_rather_than_failing_obscurely(client, token, monkeypatch):
    """The browser keys its "settled, stop asking" fallback on this exact
    phrase — speech.js matches /not configured/."""
    settings = get_settings()
    monkeypatch.setattr(settings, "fish_api_key", "")

    res = client.post("/api/voice/speak", headers=auth(token), json={"text": "hello"})
    assert res.status_code == 503
    assert "not configured" in res.json()["detail"]


def test_a_credit_failure_is_flagged_as_one(client, token, voice, monkeypatch):
    """The browser stops asking on exhausted credit; it retries on a blip. It
    can only tell them apart if the server says which happened."""
    async def out_of_credit(text, settings=None):
        raise tts.SpeechError("Fish Audio credits are used up.", quota=True)
        yield b""                                     # pragma: no cover

    monkeypatch.setattr("jarvis.api.voice.tts.stream", out_of_credit)
    url = client.post("/api/voice/speak", headers=auth(token), json={"text": "Hello."}).json()["url"]
    res = client.get(url)

    assert res.status_code == 503
    assert res.headers.get("X-Speech-Quota") == "1"
    assert "credits" in res.json()["detail"].lower()


def test_fish_errors_name_the_fix_and_never_echo_the_body():
    assert "API key" in str(fish._explain(401, ""))
    exhausted = fish._explain(402, '{"detail":"insufficient credit"}')
    assert exhausted.quota is True
    assert "sk-" not in str(fish._explain(401, "invalid key sk-secret-thing"))


def test_status_reports_what_is_missing(client, token, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "fish_api_key", "")
    monkeypatch.setattr(settings, "fish_voice_id", "")

    body = client.get("/api/voice/speech-status", headers=auth(token)).json()
    assert body["configured"] is False
    assert body["provider"] == ""
    assert set(body["missing"]) == {"FISH_API_KEY", "FISH_VOICE_ID"}


def test_missing_names_only_what_is_missing(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "fish_api_key", "k")
    monkeypatch.setattr(settings, "fish_voice_id", "")
    assert tts.missing(settings) == ["FISH_VOICE_ID"]

    monkeypatch.setattr(settings, "fish_voice_id", "   ")
    assert tts.missing(settings) == ["FISH_VOICE_ID"], "whitespace is not a voice id"


def test_status_names_the_provider(client, token, voice):
    body = client.get("/api/voice/speech-status", headers=auth(token)).json()
    assert body == {**body, "configured": True, "provider": "fish", "label": "Fish Audio", "missing": []}
    assert "check" not in body, "no network call unless asked for one"


# ------------------------------------------------------------------- verify
#
# "Configured" and "working" are different claims. verify() asks Fish, with
# the two calls from its SDK that render nothing, so a wrong key or a
# mistyped voice id is named at boot rather than heard as the wrong voice.


def test_verify_accepts_a_real_key_and_a_trained_voice(client, token, voice, fake_http):
    """Fish's states are created/training/trained/failed — "trained" is the
    usable one. The first real check against Fish reported a working voice as
    not ready because this expected "ready", which Fish never says."""
    fake_http.gets["/wallet/self/api-credit"] = (200, {"credit": "4.20"})
    fake_http.gets[f"/model/{VOICE}"] = (200, {"title": "Michael", "state": "trained"})

    body = client.get("/api/voice/speech-status?verify=1", headers=auth(token)).json()
    check = body["check"]
    assert check["ok"] is True
    assert check["voice_title"] == "Michael"
    assert check["credit"] == 4.2
    assert KEY not in str(body) and VOICE not in str(body)
    # Bearer, like the SDK — and to the wallet and model endpoints, not /tts.
    assert all(c["headers"]["Authorization"] == f"Bearer {KEY}" for c in fake_http.calls)
    assert not any("/v1/tts" in c["url"] for c in fake_http.calls), "verify must not render audio"


@pytest.mark.parametrize(
    ("credit", "model", "expect"),
    [
        ((401, {}), (200, {"state": "trained"}), "rejected the API key"),
        ((200, {"credit": "1"}), (404, {}), "does not exist"),
        ((200, {"credit": "1"}), (200, {"title": "x", "state": "training"}), "still training"),
        ((200, {"credit": "1"}), (200, {"title": "x", "state": "created"}), "still training"),
        ((200, {"credit": "1"}), (200, {"title": "x", "state": "failed"}), "failed to train"),
        ((200, {"credit": "0"}), (200, {"title": "x", "state": "trained"}), "credits are used up"),
    ],
)
def test_verify_names_each_way_it_can_be_wrong(client, token, voice, fake_http, credit, model, expect):
    fake_http.gets["/wallet/self/api-credit"] = credit
    fake_http.gets[f"/model/{VOICE}"] = model

    check = client.get("/api/voice/speech-status?verify=1", headers=auth(token)).json()["check"]
    assert check["ok"] is False
    assert expect in check["error"]


def test_verify_without_configuration_does_not_call_out(client, token, monkeypatch, fake_http):
    settings = get_settings()
    monkeypatch.setattr(settings, "fish_api_key", "")

    check = client.get("/api/voice/speech-status?verify=1", headers=auth(token)).json()["check"]
    assert check["ok"] is False
    assert "not configured" in check["error"]
    assert fake_http.calls == []
