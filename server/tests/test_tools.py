"""Tool registry, dispatch, and the guards that keep tools honest."""

from __future__ import annotations

import pytest

from jarvis.tools.base import Tool, ToolContext, ToolRegistry, ToolResult


async def test_all_tools_load(registry):
    names = {t.name for t in registry.all()}
    for expected in (
        "calendar_list", "calendar_create", "calendar_delete",
        "mail_summary", "mail_read", "messages_recent",
        "stock_quote", "wsj_headlines", "web_search",
        "places_search", "device_open_app", "memory_save", "current_time",
    ):
        assert expected in names, f"{expected} did not register"


async def test_every_tool_has_a_valid_schema(registry):
    for tool in registry.all():
        schema = tool.schema()
        assert schema["function"]["name"] == tool.name
        assert schema["function"]["description"].strip(), f"{tool.name} has no description"
        params = schema["function"]["parameters"]
        assert params["type"] == "object"
        # Every declared required field must actually exist in properties.
        for required in params.get("required", []):
            assert required in params["properties"], f"{tool.name}: '{required}' not in properties"


async def test_unknown_tool_is_reported_not_raised(registry, ctx):
    result = await registry.dispatch("does_not_exist", {}, ctx)
    assert result.ok is False
    assert "Unknown tool" in result.error


async def test_location_tools_refuse_without_coordinates(registry, ctx):
    """The whole point of device location: without it we must not guess a city."""
    result = await registry.dispatch("places_search", {"category": "haircut"}, ctx)
    assert result.ok is False
    assert "location" in result.error.lower()


async def test_missing_required_argument_is_a_clean_error(registry, ctx):
    result = await registry.dispatch("mail_read", {}, ctx)
    assert result.ok is False
    assert "missing required argument" in result.error.lower()


async def test_tool_exception_does_not_kill_the_turn(ctx):
    local = ToolRegistry()

    @local.tool(name="explode", description="Always fails.")
    async def explode():
        raise RuntimeError("boom")

    result = await local.dispatch("explode", {}, ctx)
    assert result.ok is False
    assert "boom" in result.error


async def test_hidden_when_credentials_absent(registry):
    """Tools whose integration isn't configured must not be offered to the model —
    otherwise it promises capabilities that will fail mid-conversation."""
    from jarvis.config import get_settings

    settings = get_settings()
    settings.icloud_email = ""
    settings.icloud_app_password = ""
    get_settings.cache_clear()

    available = {t.name for t in registry.available(settings)}
    assert "mail_summary" not in available
    assert "stock_quote" in available  # no credentials needed


async def test_current_time_uses_context_timezone(registry):
    tokyo = ToolContext(timezone="Asia/Tokyo")
    result = await registry.dispatch("current_time", {}, tokyo)
    assert result.ok
    assert result.data["timezone"] == "Asia/Tokyo"


async def test_tool_result_truncates_for_model():
    result = ToolResult.success({"blob": "x" * 50_000})
    assert len(result.for_model()) <= 12_000


async def test_duplicate_registration_rejected():
    local = ToolRegistry()
    tool = Tool(name="dup", description="d", parameters={"type": "object", "properties": {}},
                handler=lambda: None)
    local.register(tool)
    with pytest.raises(ValueError):
        local.register(tool)
