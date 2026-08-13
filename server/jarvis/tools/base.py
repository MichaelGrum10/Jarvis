"""Tool contract + registry.

A tool is a small async callable with a JSON-schema signature. The registry turns
them into the `tools` array Groq expects, and dispatches the model's tool calls
back to Python. Adding a capability means dropping a new file in this package —
nothing else needs to change.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Per-request state a tool may need: who's asking, and from where."""

    device_id: str = ""
    lat: float | None = None
    lon: float | None = None
    timezone: str = "UTC"
    conversation_id: int | None = None

    @property
    def has_location(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str = ""
    # Shown to the user as a card alongside the reply; the model only sees `data`.
    display: dict | None = None

    # Tool output goes straight into the next request — and into every request
    # after it in the same turn, so a result is paid for once per remaining
    # step, not once. At 4000 chars a two-tool turn spent ~2000 tokens of an
    # 8000-per-minute budget on raw JSON alone. 2000 chars is ~500 tokens and
    # still more than any result a person would read; the model asks again when
    # it needs the rest.
    MAX_MODEL_CHARS = 2000

    def for_model(self) -> str:
        if not self.ok:
            return json.dumps({"error": self.error or "tool failed"})
        try:
            rendered = json.dumps(self.data, default=str)
        except (TypeError, ValueError):
            rendered = str(self.data)
        if len(rendered) <= self.MAX_MODEL_CHARS:
            return rendered
        # Say it was cut, so the model reports "showing the first N" rather than
        # treating a truncated list as the whole truth.
        return (
            rendered[: self.MAX_MODEL_CHARS]
            + f'… [truncated at {self.MAX_MODEL_CHARS} chars — ask for more detail if needed]'
        )

    @classmethod
    def fail(cls, message: str) -> ToolResult:
        return cls(ok=False, error=message)

    @classmethod
    def success(cls, data: Any, display: dict | None = None) -> ToolResult:
        return cls(ok=True, data=data, display=display)


Handler = Callable[..., Awaitable[ToolResult]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler
    # Feature key from Settings.missing_for(); a tool whose creds are absent is
    # hidden from the model rather than failing mid-conversation.
    requires: str = ""
    needs_location: bool = False
    confirm: bool = False  # destructive — agent must ask before calling
    tags: list[str] = field(default_factory=list)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def tool(
        self,
        name: str,
        description: str,
        parameters: dict | None = None,
        **kwargs: Any,
    ) -> Callable[[Handler], Handler]:
        def decorator(fn: Handler) -> Handler:
            self.register(
                Tool(
                    name=name,
                    description=description,
                    parameters=parameters or {"type": "object", "properties": {}},
                    handler=fn,
                    **kwargs,
                )
            )
            return fn

        return decorator

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def available(self, settings, ctx: ToolContext | None = None) -> list[Tool]:
        """Tools whose credentials are present. Location-only tools stay visible so
        the model can ask the user to enable location rather than silently drop the
        capability."""
        out = []
        for tool in self._tools.values():
            if tool.requires and settings.missing_for(tool.requires):
                continue
            out.append(tool)
        return out

    def schemas(self, settings, ctx: ToolContext | None = None) -> list[dict]:
        return [t.schema() for t in self.available(settings, ctx)]

    async def dispatch(self, name: str, arguments: dict, ctx: ToolContext) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult.fail(f"Unknown tool '{name}'.")
        if tool.needs_location and not ctx.has_location:
            return ToolResult.fail(
                "No device location available. Ask the user to allow location access "
                "in the Jarvis app, or to name the city they're in."
            )
        try:
            kwargs = self._bind(tool.handler, arguments, ctx)
            return await tool.handler(**kwargs)
        except TypeError as exc:
            return ToolResult.fail(f"Bad arguments for {name}: {exc}")
        except Exception as exc:  # a tool blowing up must not kill the turn
            log.exception("Tool %s failed", name)
            return ToolResult.fail(f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _bind(handler: Handler, arguments: dict, ctx: ToolContext) -> dict:
        """Pass only the params the handler declares; inject `ctx` if it wants one."""
        sig = inspect.signature(handler)
        kwargs: dict[str, Any] = {}
        for param_name, param in sig.parameters.items():
            if param_name == "ctx":
                kwargs["ctx"] = ctx
            elif param_name in arguments:
                kwargs[param_name] = arguments[param_name]
            elif param.default is inspect.Parameter.empty:
                raise TypeError(f"missing required argument '{param_name}'")
        return kwargs


registry = ToolRegistry()


def load_all_tools() -> ToolRegistry:
    """Import every tool module so decorators run. Import errors are logged, not
    fatal — one broken integration shouldn't take the assistant offline."""
    from importlib import import_module

    modules = [
        "calendar_tool",
        "mail_tool",
        "messages_tool",
        "stocks_tool",
        "news_tool",
        "search_tool",
        "browser_tool",
        "places_tool",
        "device_tool",
        "memory_tool",
        "notes_tool",
        "system_tool",
    ]
    for mod in modules:
        try:
            import_module(f"jarvis.tools.{mod}")
        except Exception:
            log.exception("Could not load tool module %s", mod)
    log.info("Loaded %d tools", len(registry.all()))
    return registry
