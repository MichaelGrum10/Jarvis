"""Endpoint failover, and the three provider failures seen in real use."""

from __future__ import annotations

import httpx
import pytest
import respx

from jarvis.config import Settings
from jarvis.llm.client import GroqClient, LLMError, ToolCall
from jarvis.llm.pool import Endpoint, Pool, build_pool, split_keys

GROQ = "https://api.groq.com/openai/v1"
CEREBRAS = "https://api.cerebras.ai/v1"


def settings(**overrides) -> Settings:
    base = {
        "auth_secret": "x" * 32,
        "access_password": "y" * 12,
        "groq_api_key": "gsk_primary",
        "groq_model_ladder": "model-a,model-b",
    }
    base.update(overrides)
    return Settings(**base)


def ok_body(text="hello", model="model-a"):
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 10},
    }


def error_body(message, code="x"):
    return {"error": {"message": message, "code": code}}


TPM_MESSAGE = (
    "Request too large for model llama-3.1-8b-instant in organization "
    "org_01kz6namj6ez5r0dvtqrhv8nf2 service tier on_demand on tokens per minute "
    "(TPM): Limit 6000, Requested 11081, please reduce your message size and try "
    "again. Need more tokens? Upgrade to Dev Tier today at "
    "https://console.groq.com/settings/billing"
)


# ---------------------------------------------------------------- pool


def test_split_keys_handles_commas_whitespace_and_duplicates():
    assert split_keys("a, b  c,,a") == ["a", "b", "c"]
    assert split_keys("") == []


def test_pool_covers_every_model_and_key():
    pool = build_pool(settings(groq_api_keys="k1,k2"))
    assert len(pool) == 4  # 2 models x 2 keys
    assert len({e.label for e in pool.endpoints}) == 4


def test_extra_providers_join_the_pool():
    pool = build_pool(settings(cerebras_api_key="csk_1"))
    assert any(e.label.startswith("cerebras:") for e in pool.endpoints)


def test_primary_provider_is_tried_before_secondaries():
    """The user configured Groq deliberately; a secondary is a safety net, not a
    peer to be load-balanced onto."""
    pool = build_pool(settings(cerebras_api_key="csk_1"))
    labels = [e.label for e in pool.endpoints]
    assert all(label.startswith("groq:") for label in labels[:-1])
    assert labels[-1].startswith("cerebras:")


def test_cooldown_removes_and_restores_availability():
    endpoint = Endpoint(model="m", api_key="k", base_url=GROQ)
    assert endpoint.available
    endpoint.rest(60)
    assert not endpoint.available and endpoint.seconds_until_available > 1
    endpoint.succeeded()
    assert endpoint.available


def test_repeated_failures_back_off_further():
    endpoint = Endpoint(model="m", api_key="k", base_url=GROQ)
    endpoint.rest()
    first = endpoint.seconds_until_available
    endpoint.rest()
    assert endpoint.seconds_until_available > first


def test_pool_status_masks_keys():
    pool = Pool([Endpoint(model="m", api_key="gsk_supersecretkey", base_url=GROQ)])
    rendered = str(pool.status())
    assert "gsk_supersecretkey" not in rendered


# ---------------------------------------------------------------- failover


async def test_rate_limit_fails_over_instead_of_retrying():
    """The behaviour the old client got wrong: a busy endpoint should hand off
    immediately, not sleep and try the same thing again."""
    client = GroqClient(settings())
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                side_effect=[
                    httpx.Response(429, json=error_body("rate limited")),
                    httpx.Response(200, json=ok_body("second model answered", "model-b")),
                ]
            )
            response = await client.complete([{"role": "user", "content": "hi"}])
            assert response.content == "second model answered"
    finally:
        await client.aclose()


async def test_second_key_covers_for_a_busy_first_key():
    client = GroqClient(settings(groq_api_keys="k1,k2"))
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                side_effect=[
                    httpx.Response(429, json=error_body("rate limited")),
                    httpx.Response(200, json=ok_body("key two answered")),
                ]
            )
            response = await client.complete([{"role": "user", "content": "hi"}])
            assert response.content == "key two answered"
    finally:
        await client.aclose()


async def test_falls_over_to_a_second_provider():
    client = GroqClient(settings(groq_model_ladder="model-a", cerebras_api_key="csk_1"))
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(429, json=error_body("rate limited"))
            )
            respx.post(f"{CEREBRAS}/chat/completions").mock(
                return_value=httpx.Response(200, json=ok_body("cerebras answered"))
            )
            response = await client.complete([{"role": "user", "content": "hi"}])
            assert response.content == "cerebras answered"
    finally:
        await client.aclose()


async def test_exhausted_pool_explains_itself_without_raw_json():
    client = GroqClient(settings())
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(429, json=error_body("rate limited"))
            )
            with pytest.raises(LLMError) as exc:
                await client.complete([{"role": "user", "content": "hi"}])
            message = str(exc.value)
            assert '{"error"' not in message
            assert "rate limited" in message
    finally:
        await client.aclose()


async def test_single_key_exhaustion_suggests_a_second_provider():
    """The actionable advice when one key runs dry is more capacity, not 'retry'."""
    client = GroqClient(settings(groq_model_ladder="model-a"))
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(429, json=error_body("rate limited"))
            )
            with pytest.raises(LLMError, match="single API key"):
                await client.complete([{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()


# ---------------------------------------------------------------- 413


async def test_413_moves_on_and_never_shows_the_upsell():
    client = GroqClient(settings())
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                side_effect=[
                    httpx.Response(413, json=error_body(TPM_MESSAGE)),
                    httpx.Response(200, json=ok_body("roomier model answered", "model-b")),
                ]
            )
            response = await client.complete([{"role": "user", "content": "hi"}])
            assert response.content == "roomier model answered"
    finally:
        await client.aclose()


async def test_413_everywhere_reports_cleanly():
    client = GroqClient(settings())
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(413, json=error_body(TPM_MESSAGE))
            )
            with pytest.raises(LLMError) as exc:
                await client.complete([{"role": "user", "content": "hi"}])
            message = str(exc.value)
            for noise in ("Upgrade to Dev Tier", "org_01kz", "service tier", "https://"):
                assert noise not in message, f"leaked provider noise: {noise}"
    finally:
        await client.aclose()


# ---------------------------------------------------------------- tool calls


def test_malformed_tool_name_is_recovered():
    """Seen live: 'calendar_list {"start": "today"}' as the function name, which
    providers reject as an unknown tool. Recover rather than lose the turn."""
    call = ToolCall.parse(
        {"id": "1", "function": {"name": 'calendar_list {"start": "today"}', "arguments": "{}"}}
    )
    assert call.name == "calendar_list"
    assert call.arguments == {"start": "today"}


def test_well_formed_tool_call_is_untouched():
    call = ToolCall.parse(
        {"id": "1", "function": {"name": "stock_quote", "arguments": '{"symbols": ["AAPL"]}'}}
    )
    assert call.name == "stock_quote"
    assert call.arguments == {"symbols": ["AAPL"]}


def test_non_dict_arguments_do_not_crash():
    call = ToolCall.parse({"id": "1", "function": {"name": "x", "arguments": "[1,2,3]"}})
    assert call.arguments == {}


async def test_tool_validation_400_is_nudged_then_recovers():
    client = GroqClient(settings())
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                side_effect=[
                    httpx.Response(
                        400,
                        json=error_body(
                            "tool call validation failed: attempted to call tool "
                            "'calendar_list {\"start\": \"today\"}' which was not in request.tools"
                        ),
                    ),
                    httpx.Response(200, json=ok_body("recovered after nudge")),
                ]
            )
            response = await client.complete(
                [{"role": "user", "content": "what's on today"}], tools=[{"type": "function"}]
            )
            assert response.content == "recovered after nudge"
    finally:
        await client.aclose()


async def test_no_endpoints_configured_says_so():
    client = GroqClient(settings(groq_api_key="", groq_api_keys=""))
    try:
        with pytest.raises(LLMError, match="No LLM endpoints"):
            await client.complete([{"role": "user", "content": "hi"}])
    finally:
        await client.aclose()


async def test_generic_4xx_also_avoids_raw_json():
    """Not every provider error is a rate limit; none of them should surface as
    an API payload."""
    client = GroqClient(settings(groq_model_ladder="model-a"))
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(
                    400, json=error_body("model not found", code="bad_model")
                )
            )
            with pytest.raises(LLMError) as exc:
                await client.complete([{"role": "user", "content": "hi"}])
            assert "model not found" in str(exc.value)
            assert '"code"' not in str(exc.value)
    finally:
        await client.aclose()


def test_history_window_is_bounded():
    """A generous history window is what pushed a request past the token budget
    in the first place; keep it tight by default."""
    from jarvis.agent.loop import history_from_rows

    class Row:
        def __init__(self, i: int) -> None:
            self.role = "user" if i % 2 == 0 else "assistant"
            self.content = f"message {i}"

    history = history_from_rows([Row(i) for i in range(40)])
    assert len(history) <= 12
    assert history[-1]["content"] == "message 39"
