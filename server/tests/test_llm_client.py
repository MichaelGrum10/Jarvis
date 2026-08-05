"""Groq client: the 429/413 handling that stands between a rate limit and the
user seeing raw provider JSON in their chat."""

from __future__ import annotations

import httpx
import respx

from jarvis.config import get_settings
from jarvis.llm.client import GroqClient, LLMError

BASE = "https://api.groq.com/openai/v1"

GROQ_413_BODY = {
    "error": {
        "message": (
            "Request too large for model llama-3.1-8b-instant in organization "
            "org_x service tier on_demand on tokens per minute (TPM): Limit 6000, "
            "Requested 11081, please reduce your message size and try again. "
            "Need more tokens? Upgrade to Dev Tier today at "
            "https://console.groq.com/settings/billing"
        ),
        "type": "tokens",
        "code": "rate_limit_exceeded",
    }
}


def _client() -> GroqClient:
    settings = get_settings()
    settings.groq_model = "llama-3.3-70b-versatile"
    settings.groq_fast_model = "llama-3.1-8b-instant"
    return GroqClient(settings)


async def test_413_gives_clean_message_not_raw_json():
    """The exact failure reported live: primary rate-limited twice, fallback
    model rejects the same oversized payload with 413. The user must see a
    sentence, not Groq's JSON envelope and billing upsell."""
    client = _client()
    try:
        with respx.mock(base_url=BASE) as mock:
            mock.post("/chat/completions").mock(
                side_effect=[
                    httpx.Response(429, headers={"retry-after": "0"}, json={}),
                    httpx.Response(429, headers={"retry-after": "0"}, json={}),
                    httpx.Response(413, json=GROQ_413_BODY),
                ]
            )
            try:
                await client.complete([{"role": "user", "content": "hi"}], tools=[{"a": 1}])
                raise AssertionError("expected LLMError")
            except LLMError as exc:
                message = str(exc)
                assert "Upgrade to Dev Tier" not in message
                assert '{"error"' not in message
                assert "too large" in message.lower()
                assert "llama-3.1-8b-instant" in message
    finally:
        await client.aclose()


async def test_413_does_not_retry_forever():
    """A 413 must stop the ladder immediately rather than exhausting every
    remaining model with a request that cannot shrink on its own."""
    client = _client()
    try:
        with respx.mock(base_url=BASE) as mock:
            route = mock.post("/chat/completions").mock(
                return_value=httpx.Response(413, json=GROQ_413_BODY)
            )
            try:
                await client.complete([{"role": "user", "content": "hi"}])
                raise AssertionError("expected LLMError")
            except LLMError:
                pass
            assert route.call_count == 1
    finally:
        await client.aclose()


async def test_generic_4xx_also_avoids_raw_json():
    client = _client()
    try:
        with respx.mock(base_url=BASE) as mock:
            mock.post("/chat/completions").mock(
                return_value=httpx.Response(
                    400, json={"error": {"message": "model not found", "code": "bad_model"}}
                )
            )
            try:
                await client.complete([{"role": "user", "content": "hi"}])
                raise AssertionError("expected LLMError")
            except LLMError as exc:
                assert "model not found" in str(exc)
                assert '"code"' not in str(exc)
    finally:
        await client.aclose()


async def test_successful_call_still_works():
    client = _client()
    try:
        with respx.mock(base_url=BASE) as mock:
            mock.post("/chat/completions").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": "hello"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"total_tokens": 10},
                    },
                )
            )
            response = await client.complete([{"role": "user", "content": "hi"}])
            assert response.content == "hello"
            assert not response.wants_tools
    finally:
        await client.aclose()


async def test_history_window_is_bounded():
    """A generous history window is exactly what pushed the fallback model's
    tiny per-minute budget over its limit; keep it tight by default."""
    from jarvis.agent.loop import history_from_rows

    class Row:
        def __init__(self, i: int) -> None:
            self.role = "user" if i % 2 == 0 else "assistant"
            self.content = f"message {i}"

    rows = [Row(i) for i in range(40)]
    history = history_from_rows(rows)
    assert len(history) <= 12
    assert history[-1]["content"] == "message 39"
