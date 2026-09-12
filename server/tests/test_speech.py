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
from jarvis.integrations import elevenlabs, fish, tts
from jarvis.main import app
from jarvis.security import issue_speech_ticket

CLIP = b"ID3\x04\x00" + b"\xff\xfb\x90d" * 400        # enough to pass the size floor


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
    """ElevenLabs configured, Fish not — the original shape."""
    settings = get_settings()
    monkeypatch.setattr(settings, "tts_provider", "")
    monkeypatch.setattr(settings, "fish_api_key", "")
    monkeypatch.setattr(settings, "fish_voice_id", "")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "sk-test-key-never-leaves-here")
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "voice-123")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return settings


@pytest.fixture
def fish_voice(monkeypatch, tmp_path):
    """Fish configured, ElevenLabs not."""
    settings = get_settings()
    monkeypatch.setattr(settings, "tts_provider", "")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "")
    monkeypatch.setattr(settings, "fish_api_key", "fish-test-key-never-leaves-here")
    monkeypatch.setattr(settings, "fish_voice_id", "802e3bc2b27e49c2995d23ef70e6ac89")
    monkeypatch.setattr(settings, "fish_model", "s2-pro")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return settings


@pytest.fixture
def fake_api(monkeypatch, voice):
    """Stand in for ElevenLabs, and record what it was sent."""
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
    assert "sk-test-key-never-leaves-here" not in blob
    assert "voice-123" not in blob          # the voice id is a credential too


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


def test_the_second_ask_comes_from_cache(client, token, voice, fake_http):
    """The boot greeting is said every day and is identical every time.

    Faked at the HTTP layer rather than at elevenlabs.stream, because
    stream() is where the caching lives — stubbing it would test the stub.
    """
    for _ in range(2):
        url = client.post(
            "/api/voice/speak", headers=auth(token), json={"text": "Good evening, sir."}
        ).json()["url"]
        assert client.get(url).status_code == 200

    assert len(fake_http.calls) == 1, "the same line was rendered twice"
    assert fake_http.calls[0]["json"]["text"] == "Good evening, sir."
    # The key travels in a header to ElevenLabs and nowhere else.
    assert fake_http.calls[0]["headers"]["xi-api-key"] == "sk-test-key-never-leaves-here"


def test_a_cached_clip_says_it_came_from_cache(client, token, voice, fake_http):
    url = client.post("/api/voice/speak", headers=auth(token), json={"text": "Twice."}).json()["url"]
    assert client.get(url).headers["X-Speech-Source"] == "elevenlabs"

    again = client.post("/api/voice/speak", headers=auth(token), json={"text": "Twice."}).json()
    assert again["cached"] is True
    assert client.get(again["url"]).headers["X-Speech-Source"] == "cache"


def test_the_streaming_endpoint_is_used_not_the_batch_one(client, token, voice, fake_http):
    """Batch returns nothing until the whole clip is rendered — seconds of dead
    air before he starts."""
    client.get(client.post(
        "/api/voice/speak", headers=auth(token), json={"text": "Latency matters."}
    ).json()["url"])

    call = fake_http.calls[0]
    assert call["url"].endswith("/stream")
    assert call["params"]["optimize_streaming_latency"] == elevenlabs.LATENCY_MODE


def test_whitespace_does_not_mint_a_second_cache_entry(voice):
    assert tts.voice_key(tts.speakable_length("Good   evening,\n sir."), voice) == \
           tts.voice_key(tts.speakable_length("Good evening, sir."), voice)


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
    settings = get_settings()
    monkeypatch.setattr(settings, "elevenlabs_api_key", "")
    monkeypatch.setattr(settings, "fish_api_key", "")

    res = client.post("/api/voice/speak", headers=auth(token), json={"text": "hello"})
    assert res.status_code == 503
    assert "not configured" in res.json()["detail"]


def test_a_quota_failure_is_flagged_as_one(client, token, voice, monkeypatch):
    """The browser stops asking on quota; it retries on a blip. It can only
    tell them apart if the server says which happened."""
    async def out_of_credit(text, settings=None):
        raise tts.SpeechError("ElevenLabs quota is used up.", quota=True)
        yield b""                                     # pragma: no cover

    monkeypatch.setattr("jarvis.api.voice.tts.stream", out_of_credit)
    url = client.post("/api/voice/speak", headers=auth(token), json={"text": "Hello."}).json()["url"]
    res = client.get(url)

    assert res.status_code == 503
    assert res.headers.get("X-Speech-Quota") == "1"
    assert "quota" in res.json()["detail"].lower()


def test_errors_never_echo_the_remote_body_verbatim(voice):
    error = elevenlabs._explain(401, '{"detail":"invalid api key sk-test-key-never-leaves-here"}')
    assert "sk-test" not in str(error)


def test_status_reports_what_is_missing(client, token, monkeypatch):
    settings = get_settings()
    for name in ("tts_provider", "elevenlabs_api_key", "elevenlabs_voice_id", "fish_api_key", "fish_voice_id"):
        monkeypatch.setattr(settings, name, "")

    body = client.get("/api/voice/speech-status", headers=auth(token)).json()
    assert body["configured"] is False
    # Nothing set at all: point at Fish, the preferred provider.
    assert set(body["missing"]) == {"FISH_API_KEY", "FISH_VOICE_ID"}


def test_missing_names_the_provider_closest_to_working(monkeypatch, tmp_path):
    """A Fish key with no voice id wants FISH_VOICE_ID — not a list of every
    variable either provider could take."""
    settings = get_settings()
    for name in ("tts_provider", "elevenlabs_api_key", "elevenlabs_voice_id", "fish_voice_id"):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "fish_api_key", "k")
    assert tts.missing(settings) == ["FISH_VOICE_ID"]

    monkeypatch.setattr(settings, "fish_api_key", "")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "k")
    assert tts.missing(settings) == ["ELEVENLABS_VOICE_ID"]


# --------------------------------------------------------------- fish audio


def test_fish_is_chosen_when_it_is_the_one_configured(fish_voice):
    assert tts.provider(fish_voice) == "fish"
    assert tts.label(fish_voice) == "Fish Audio"


def test_fish_is_preferred_when_both_are_configured(fish_voice, monkeypatch):
    monkeypatch.setattr(fish_voice, "elevenlabs_api_key", "k")
    monkeypatch.setattr(fish_voice, "elevenlabs_voice_id", "v")
    assert tts.provider(fish_voice) == "fish"

    monkeypatch.setattr(fish_voice, "tts_provider", "elevenlabs")
    assert tts.provider(fish_voice) == "elevenlabs"


def test_forcing_an_unconfigured_provider_does_not_fall_through(fish_voice, monkeypatch):
    """TTS_PROVIDER=elevenlabs with no ElevenLabs key must not quietly use Fish:
    "I set ElevenLabs and it is still Fish" is the wrong surprise."""
    monkeypatch.setattr(fish_voice, "tts_provider", "elevenlabs")
    assert tts.provider(fish_voice) == ""
    assert tts.configured(fish_voice) is False


def test_the_fish_request_is_the_sdks_request(client, token, fish_voice, fake_http):
    """Shape copied from fish-audio-sdk 1.3.0, not remembered. The model goes in
    a header — put it in the body and Fish silently uses its default."""
    import ormsgpack

    url = client.post("/api/voice/speak", headers=auth(token), json={"text": "Good evening, sir."}).json()["url"]
    res = client.get(url)
    assert res.status_code == 200
    assert res.headers["X-Speech-Source"] == "fish"

    call = fake_http.calls[0]
    assert call["url"] == "https://api.fish.audio/v1/tts"
    assert call["headers"]["Authorization"] == "Bearer fish-test-key-never-leaves-here"
    assert call["headers"]["Content-Type"] == "application/msgpack"
    assert call["headers"]["model"] == "s2-pro"
    body = ormsgpack.unpackb(call["content"])
    assert body["text"] == "Good evening, sir."
    assert body["reference_id"] == "802e3bc2b27e49c2995d23ef70e6ac89"
    assert body["format"] == "mp3"
    assert body["latency"] == "balanced"


def test_the_fish_key_is_never_in_any_response(client, token, fish_voice, fake_http):
    prepared = client.post("/api/voice/speak", headers=auth(token), json={"text": "Hello."})
    audio = client.get(prepared.json()["url"])
    status = client.get("/api/voice/speech-status", headers=auth(token))

    blob = prepared.text + status.text + repr(dict(audio.headers)) + repr(dict(prepared.headers))
    assert "fish-test-key-never-leaves-here" not in blob
    assert "802e3bc2b27e49c2995d23ef70e6ac89" not in blob     # the voice id is a credential too


def test_switching_provider_does_not_replay_the_old_voice_from_cache(voice, monkeypatch):
    """The cache key carries the provider: a line rendered by ElevenLabs must
    not be served as if Fish said it."""
    line = tts.speakable_length("Good evening, sir.")
    eleven_key = tts.voice_key(line, voice)

    monkeypatch.setattr(voice, "fish_api_key", "k")
    monkeypatch.setattr(voice, "fish_voice_id", "v")
    monkeypatch.setattr(voice, "tts_provider", "fish")
    assert tts.voice_key(line, voice) != eleven_key


def test_fish_errors_name_the_fix_and_flag_credits():
    assert "API key" in str(fish._explain(401, ""))
    exhausted = fish._explain(402, '{"detail":"insufficient credit"}')
    assert exhausted.quota is True
    assert "sk-" not in str(fish._explain(401, "invalid key sk-secret-thing"))
