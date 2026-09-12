"""OmniRoute rides in the compose file on a profile, and doctor asks it questions.

The compose file is data this project ships; a typo there is a deployment that
fails on someone's server, not a test that fails here — unless it is checked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_omniroute_is_opt_in_and_never_on_the_internet(compose):
    service = compose["services"]["omniroute"]
    assert service["profiles"] == ["omniroute"], "must not start for people who did not ask for it"
    for port in service["ports"]:
        assert str(port).startswith("127.0.0.1:"), f"published beyond loopback: {port}"
    assert service["environment"]["REQUIRE_API_KEY"] == "false", (
        "the loopback binding is the security; if this flips, the pool's placeholder key stops working"
    )
    assert "omniroute-data" in compose["volumes"]


def test_omniroute_secrets_interpolate_to_empty_when_unset(compose):
    """`${VAR:?}` would make `docker compose up` fail for everyone, since compose
    interpolates the whole file even for inactive profiles."""
    env = compose["services"]["omniroute"]["environment"]
    for name in ("JWT_SECRET", "API_KEY_SECRET", "INITIAL_PASSWORD", "OMNIROUTE_WS_BRIDGE_SECRET"):
        assert ":?" not in str(env[name]), f"{name} would break compose for people without OmniRoute"
        assert env[name].startswith("${OMNIROUTE_"), f"{name} must come from an OMNIROUTE_-prefixed .env value"


def test_setkey_knows_the_omniroute_names():
    """setkey.sh warns on unknown names by reading config.py; the compose-only
    names must be on its allowlist or every install prints a false warning."""
    script = (ROOT / "scripts" / "setkey.sh").read_text()
    for name in ("COMPOSE_PROFILES", "OMNIROUTE_JWT_SECRET", "OMNIROUTE_API_KEY_SECRET",
                 "OMNIROUTE_PASSWORD", "OMNIROUTE_WS_BRIDGE_SECRET"):
        assert f'"{name}"' in script


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _Client:
    def __init__(self, status=200, body=None, error=None):
        self.status, self.body, self.error = status, body, error
        self.calls = []

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url, headers=None, **kwargs):
        import httpx

        self.calls.append((url, headers))
        if self.error:
            raise httpx.ConnectError(self.error)
        return _Response(self.status, self.body)


class _Report:
    def __init__(self):
        self.lines = []

    def ok(self, label, detail=""): self.lines.append(("ok", label, detail))
    def bad(self, label, problem, fix=""): self.lines.append(("bad", label, problem))
    def warn(self, label, problem, fix=""): self.lines.append(("warn", label, problem))


@pytest.fixture
def wired(monkeypatch):
    from jarvis.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "custom_base_url", "http://omniroute:20128/v1")
    monkeypatch.setattr(settings, "custom_api_key", "omniroute")
    monkeypatch.setattr(settings, "custom_model", "auto")
    return settings


async def test_doctor_asks_the_endpoint_from_inside(wired, monkeypatch):
    import httpx

    from jarvis.doctor import check_custom

    client = _Client(200, {"data": [{"id": "oc/gpt-5-mini"}, {"id": "groq/llama-3.3-70b"}]})
    monkeypatch.setattr(httpx, "AsyncClient", client)
    report = _Report()
    await check_custom(report)

    assert client.calls[0][0] == "http://omniroute:20128/v1/models"
    kinds = [k for k, *_ in report.lines]
    assert "bad" not in kinds and "warn" not in kinds
    assert any("2 models" in d for _, _, d in report.lines)


async def test_doctor_names_a_container_it_cannot_reach(wired, monkeypatch):
    import httpx

    from jarvis.doctor import check_custom

    monkeypatch.setattr(httpx, "AsyncClient", _Client(error="connection refused"))
    report = _Report()
    await check_custom(report)

    bad = [l for l in report.lines if l[0] == "bad"]
    assert bad and "connection refused" in bad[0][2]


async def test_doctor_does_not_flag_auto_as_an_unknown_model(wired, monkeypatch):
    """auto, auto/fast and friends are OmniRoute's routing aliases; they are
    never in /models and always work."""
    import httpx

    from jarvis.doctor import check_custom

    monkeypatch.setattr(wired, "custom_model", "auto/coding,oc/gpt-5-mini")
    monkeypatch.setattr(httpx, "AsyncClient", _Client(200, {"data": [{"id": "oc/gpt-5-mini"}]}))
    report = _Report()
    await check_custom(report)
    assert not [l for l in report.lines if l[0] == "warn"]

    monkeypatch.setattr(wired, "custom_model", "made-up/model")
    report = _Report()
    await check_custom(report)
    assert [l for l in report.lines if l[0] == "warn" and "made-up/model" in l[2]]


async def test_doctor_stays_quiet_when_nothing_is_wired(monkeypatch):
    from jarvis.config import get_settings
    from jarvis.doctor import check_custom

    settings = get_settings()
    for name in ("custom_base_url", "custom_api_key", "custom_model"):
        monkeypatch.setattr(settings, name, "")
    report = _Report()
    await check_custom(report)
    assert report.lines == []
