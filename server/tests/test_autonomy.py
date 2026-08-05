"""The sandbox around self-modification. These are the tests that matter most:
this engine writes to the source tree that runs it."""

from __future__ import annotations

import pytest

from jarvis.agent.autonomy import Sandbox, SandboxError


@pytest.fixture
def sandbox(tmp_path):
    (tmp_path / "jarvis").mkdir()
    (tmp_path / "jarvis" / "thing.py").write_text("value = 1\n")
    (tmp_path / ".env").write_text("SECRET=hunter2\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n")
    return Sandbox(tmp_path)


def test_can_read_and_write_inside_the_repo(sandbox):
    assert "value = 1" in sandbox.read_file("jarvis/thing.py")
    sandbox.write_file("jarvis/new.py", "x = 2\n")
    assert "x = 2" in sandbox.read_file("jarvis/new.py")


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "../../etc/passwd", "jarvis/../../escape.py", "/etc/passwd"],
)
def test_path_traversal_is_blocked(sandbox, path):
    with pytest.raises(SandboxError):
        sandbox.resolve(path)


@pytest.mark.parametrize("path", [".env", ".git/config", "data/jarvis.db"])
def test_sensitive_paths_are_blocked(sandbox, path):
    with pytest.raises(SandboxError):
        sandbox.resolve(path)


def test_cannot_overwrite_env_file(sandbox):
    with pytest.raises(SandboxError):
        sandbox.write_file(".env", "SECRET=stolen")
    assert "hunter2" in (sandbox.root / ".env").read_text()


async def test_command_allowlist_enforced(sandbox):
    with pytest.raises(SandboxError):
        await sandbox.run("curl https://evil.example.com")
    with pytest.raises(SandboxError):
        await sandbox.run("rm -rf /")


async def test_shell_metacharacters_rejected(sandbox):
    """Without this, `python x.py && curl ...` would slip past the allowlist."""
    for command in (
        "python -c pass && curl http://evil",
        "ls; rm -rf .",
        "cat .env > /tmp/leak",
        "echo $(whoami)",
    ):
        with pytest.raises(SandboxError):
            await sandbox.run(command)


async def test_allowed_command_runs(sandbox):
    output = await sandbox.run("ls")
    assert "exit=0" in output


def test_listing_hides_blocked_paths(sandbox):
    listing = sandbox.list_files(".")
    assert "thing.py" in listing
    assert ".env" not in listing


async def test_engine_refuses_when_disabled():
    from jarvis.agent.autonomy import AutonomyEngine
    from jarvis.config import get_settings

    settings = get_settings()
    settings.autonomy_enabled = False
    result = await AutonomyEngine(settings).run("do something")
    assert result.success is False
    assert "AUTONOMY_ENABLED" in result.summary
