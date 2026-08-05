from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Settings are read at import time, so the environment must be set before any
# jarvis module loads.
_TMP = tempfile.mkdtemp(prefix="jarvis-test-")
os.environ.update(
    {
        "AUTH_SECRET": "test-secret-not-used-in-production-0123456789",
        "ACCESS_PASSWORD": "test-password",
        "GROQ_API_KEY": "test-key",
        "BRIDGE_TOKEN": "test-bridge-token",
        "DATA_DIR": _TMP,
        "TIMEZONE": "America/New_York",
        "OWNER_NAME": "Tester",
    }
)


@pytest.fixture(scope="session", autouse=True)
def _isolate_data_dir():
    from jarvis.config import get_settings

    settings = get_settings()
    settings.data_dir = Path(_TMP)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    yield


@pytest.fixture(autouse=True)
async def _fresh_db():
    from jarvis.db import init_db
    from jarvis.tools.base import load_all_tools

    await init_db()
    # The agent resolves tools through the module-level registry, which is only
    # populated by load_all_tools(). In production the app lifespan does this;
    # tests that exercise the agent directly need it too. Idempotent: importlib
    # caches the modules, so decorators run exactly once.
    load_all_tools()
    yield


@pytest.fixture
def registry():
    from jarvis.tools.base import load_all_tools

    return load_all_tools()


@pytest.fixture
def ctx():
    from jarvis.tools.base import ToolContext

    return ToolContext(device_id="test-device", timezone="America/New_York")


@pytest.fixture
def located_ctx():
    from jarvis.tools.base import ToolContext

    # Boston City Hall — a real point, so radius maths is meaningful.
    return ToolContext(
        device_id="test-device", timezone="America/New_York", lat=42.3603, lon=-71.0580
    )
