"""Groq chat-completions client (OpenAI-compatible wire format).

Groq's free tier is rate-limited, not feature-limited: tool calling works, so the
whole agent loop runs at zero cost. On a 429 we back off and retry; on repeated
429s we fall back to the smaller/faster model, which has a separate quota bucket.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings, get_settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    @classmethod
    def parse(cls, raw: dict) -> ToolCall:
        fn = raw.get("function", {})
        text = fn.get("arguments") or "{}"
        try:
            args = json.loads(text) if isinstance(text, str) else dict(text)
        except json.JSONDecodeError:
            log.warning("Model emitted non-JSON tool arguments: %s", text[:200])
            args = {}
        return cls(id=raw.get("id", ""), name=fn.get("name", ""), arguments=args)


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)
    model: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class GroqClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.settings.groq_base_url,
                timeout=httpx.Timeout(120.0, connect=15.0),
                headers={"Authorization": f"Bearer {self.settings.groq_api_key}"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        force_json: bool = False,
    ) -> LLMResponse:
        if not self.settings.groq_api_key:
            raise LLMError("GROQ_API_KEY is not set. Add it to .env and restart.")

        primary = model or self.settings.groq_model
        for attempt, candidate in enumerate(self._model_ladder(primary)):
            try:
                return await self._request(
                    messages, tools, candidate, temperature, max_tokens, force_json
                )
            except _RateLimited as exc:
                wait = min(2**attempt, 8) + exc.retry_after
                log.warning("Groq 429 on %s, retrying in %.1fs", candidate, wait)
                await asyncio.sleep(wait)
            except _TooLarge as exc:
                # A request too large for this model will not shrink on retry, and
                # the fallback model in the ladder has an even smaller token budget
                # than the primary — so falling through to it here would just trade
                # one guaranteed failure for another. Stop rather than cascade.
                log.warning("Groq 413 on %s: %s", candidate, exc.detail)
                raise LLMError(
                    f"That request is too large for {candidate} ({exc.detail}). "
                    "This usually happens when a conversation has grown long. "
                    "Starting a new conversation, or waiting a minute for the "
                    "main model's quota to reset, should fix it."
                ) from None
        raise LLMError("Groq is rate-limiting every model. Try again in a minute.")

    def _model_ladder(self, primary: str) -> list[str]:
        fast = self.settings.groq_fast_model
        return [primary, primary, fast] if fast != primary else [primary, primary, primary]

    @staticmethod
    def _extract_message(resp: httpx.Response) -> str:
        """Groq's error body is JSON with a nested message, plus marketing copy
        ("Upgrade to Dev Tier...") that has no business reaching the end user.
        Pull out just the useful sentence; fall back to raw text if parsing fails."""
        try:
            message = resp.json().get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            message = ""
        message = message.split("Need more tokens?")[0].strip()
        return message or resp.text[:300]

    async def _request(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        force_json: bool,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if force_json:
            payload["response_format"] = {"type": "json_object"}

        client = await self._http()
        resp = await client.post("/chat/completions", json=payload)

        if resp.status_code == 429:
            raise _RateLimited(float(resp.headers.get("retry-after", 1)))
        if resp.status_code == 413:
            raise _TooLarge(model, self._extract_message(resp))
        if resp.status_code >= 400:
            raise LLMError(f"Groq {resp.status_code}: {self._extract_message(resp)}")

        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        return LLMResponse(
            content=msg.get("content") or "",
            tool_calls=[ToolCall.parse(t) for t in (msg.get("tool_calls") or [])],
            finish_reason=choice.get("finish_reason", "stop"),
            usage=data.get("usage", {}),
            model=data.get("model", model),
        )


class _RateLimited(Exception):
    def __init__(self, retry_after: float = 1.0) -> None:
        self.retry_after = max(0.0, min(retry_after, 10.0))
        super().__init__("rate limited")


class _TooLarge(Exception):
    def __init__(self, model: str, detail: str) -> None:
        self.model = model
        self.detail = detail
        super().__init__(detail)


_client: GroqClient | None = None


def get_llm() -> GroqClient:
    global _client
    if _client is None:
        _client = GroqClient()
    return _client
