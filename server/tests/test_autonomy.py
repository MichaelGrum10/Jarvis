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


async def test_refuses_when_target_is_not_a_git_repo(tmp_path):
    """Branch isolation is the rail that makes autonomy recoverable. Without a
    repo there's no branch and no undo, so it must refuse rather than edit."""
    from jarvis.agent.autonomy import AutonomyEngine
    from jarvis.config import get_settings

    (tmp_path / "jarvis").mkdir()
    settings = get_settings()
    settings.autonomy_enabled = True
    settings.autonomy_repo_path = tmp_path
    try:
        result = await AutonomyEngine(settings).run("add a weather tool")
        assert result.success is False
        assert "not a git repository" in result.summary
        assert result.files_changed == []
    finally:
        settings.autonomy_enabled = False


async def test_the_checkout_is_returned_after_a_run(tmp_path, monkeypatch):
    """Leaving the repository on the work branch hijacks the user's own copy:
    their next update asks the remote for a branch that exists only locally and
    fails with "couldn't find remote ref", which reads as GitHub being down."""
    import subprocess

    from jarvis.agent.autonomy import AutonomyEngine

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], capture_output=True, text=True, check=False
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "T")
    (tmp_path / "f.txt").write_text("x")
    git("add", "-A")
    git("commit", "-qm", "base")

    engine = AutonomyEngine()
    logs: list[str] = []
    branch = await engine._create_branch(tmp_path, "fix something", logs.append)
    assert branch

    await engine._restore_branch(tmp_path, "main", logs.append)

    on = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert on == "main", "the working tree belongs to the user"
    # The branch is kept: reviewing it is the entire point of propose mode.
    assert branch in git("branch", "--list", branch).stdout
