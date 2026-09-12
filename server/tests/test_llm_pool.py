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
            assert "rate limited" in message.lower()
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
            with pytest.raises(LLMError, match="single endpoint"):
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


# ---------------------------------------------------------------- priority


def test_extra_providers_are_backups_by_default():
    """Position in the pool *is* the priority — the client stops at the first
    endpoint that answers, so anything after the primary is only ever reached
    when everything ahead of it is busy."""
    labels = [e.label for e in build_pool(settings(gemini_api_key="AIza")).endpoints]
    assert labels[0].startswith("groq:")
    assert labels[-1].startswith("gemini:")


def test_primary_provider_moves_a_backup_to_the_front():
    labels = [
        e.label
        for e in build_pool(settings(gemini_api_key="AIza", primary_provider="gemini")).endpoints
    ]
    assert labels[0].startswith("gemini:")
    assert any(label.startswith("groq:") for label in labels[1:])


def test_primary_provider_preserves_ladder_order_within_a_provider():
    """Promoting a provider must not scramble its own best-first model order."""
    pool = build_pool(settings(gemini_api_key="AIza", primary_provider="groq"))
    groq_models = [e.model for e in pool.endpoints if e.label.startswith("groq:")]
    assert groq_models == ["model-a", "model-b"]


def test_provider_order_sets_the_whole_sequence():
    """PROVIDER_ORDER is what scripts/bestmodels.sh writes: every provider in
    measured order. It replaces PRIMARY_PROVIDER, and providers it does not
    name keep their place after the named ones."""
    pool = build_pool(settings(
        gemini_api_key="AIza", openrouter_api_key="sk-or-x",
        primary_provider="groq", provider_order="gemini,groq",
    ))
    providers = [e.label.split(":", 1)[0] for e in pool.endpoints]
    assert providers[0] == "gemini"
    assert providers.index("groq") < providers.index("openrouter"), "unnamed providers go last"
    assert [e.model for e in pool.endpoints if e.label.startswith("groq:")] == ["model-a", "model-b"]


def test_provider_order_naming_a_missing_provider_is_not_fatal():
    pool = build_pool(settings(provider_order="nonexistent,groq"))
    assert pool.endpoints[0].label.startswith("groq:")


def test_unconfigured_primary_is_ignored_not_fatal():
    """Naming a provider you never set up shouldn't take the assistant down."""
    pool = build_pool(settings(primary_provider="nonexistent"))
    assert len(pool) > 0
    assert pool.endpoints[0].label.startswith("groq:")


async def test_a_model_that_cannot_tool_call_is_retired_not_merely_rested():
    """Measured on a real account: llama-3.3-70b-versatile returns 400 "Failed to
    call a function" on even a single-tool prompt.

    Backing off was the first attempt at this and wasn't enough. A cooldown
    assumes recovery, and this fault never recovers — so the endpoint kept being
    retried at slower and slower intervals while still counting as pool capacity
    that didn't exist. Twice in a row, with a correction in between, is proof
    enough: take it out for good."""
    client = GroqClient(settings(groq_model_ladder="broken,working"))
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                side_effect=[
                    # broken: initial malformed call, then the nudge fails too
                    httpx.Response(400, json=error_body(
                        "Failed to call a function. tool call validation failed")),
                    httpx.Response(400, json=error_body(
                        "Failed to call a function. tool call validation failed")),
                    # working: answers fine
                    httpx.Response(200, json=ok_body("answered by the working model")),
                ]
            )
            response = await client.complete(
                [{"role": "user", "content": "hi"}], tools=[{"type": "function"}]
            )
            assert response.content == "answered by the working model"

        broken = next(e for e in client.pool.endpoints if e.model == "broken")
        assert broken.retired, "a model that cannot tool-call must not be retried"
        assert broken not in client.pool.ready()

        # And the pool must stop claiming it as capacity that will return.
        working = next(e for e in client.pool.endpoints if e.model == "working")
        assert client.pool.soonest() is working
    finally:
        await client.aclose()


# ---------------------------------------------------------------- error shapes


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"error": {"message": "rate limited", "code": "x"}}, "rate limited"),
        # Google's OpenAI-compatible endpoint wraps the error object in a list.
        # Assuming a dict and calling .get() on this crashed outright the first
        # time a Gemini key was configured.
        ([{"error": {"message": "API key not valid", "code": 400}}], "API key not valid"),
        ({"error": "something went wrong"}, "something went wrong"),
        ({"detail": "Not Found"}, "Not Found"),
        ({"message": "flat form"}, "flat form"),
    ],
)
def test_every_provider_error_shape_is_understood(body, expected):
    from jarvis.llm.client import _error_message

    assert expected in _error_message(httpx.Response(400, json=body))


@pytest.mark.parametrize("body", [[], ["nope"], None, {"error": []}, {"error": 42}])
def test_malformed_error_bodies_never_crash(body):
    """A provider returning something unexpected must degrade, not take down the
    request that was trying to report its failure."""
    from jarvis.llm.client import _error_message, error_payload

    assert isinstance(error_payload(httpx.Response(400, json=body)), dict)
    assert isinstance(_error_message(httpx.Response(400, json=body)), str)


def test_non_json_body_falls_back_to_text():
    from jarvis.llm.client import _error_message

    message = _error_message(httpx.Response(502, text="<html>gateway error</html>"))
    assert "gateway" in message


async def test_gemini_shaped_error_surfaces_through_the_pool():
    """End to end: a list-wrapped error must reach the user as a sentence."""
    client = GroqClient(settings(groq_model_ladder="model-a"))
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(
                    400, json=[{"error": {"message": "API key not valid", "code": 400}}]
                )
            )
            with pytest.raises(LLMError) as exc:
                await client.complete([{"role": "user", "content": "hi"}])
            assert "API key not valid" in str(exc.value)
    finally:
        await client.aclose()


# ---------------------------------------------------------------- exhaustion


async def test_exhaustion_separates_misconfigured_from_busy():
    """A rejected key is not a busy one. Telling someone to wait 20s for a
    credential that will never become valid sends them the wrong way entirely."""
    client = GroqClient(settings(groq_model_ladder="model-a", gemini_api_key="AIza_bad"))
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(429, json=error_body("rate limited"))
            )
            respx.post("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions").mock(
                return_value=httpx.Response(400, json=[{"error": {"message": "Please pass a valid API key"}}])
            )
            with pytest.raises(LLMError) as exc:
                await client.complete([{"role": "user", "content": "hi"}])

        message = str(exc.value)
        # A rejected key and a busy minute must not read the same. The key
        # problem is named as a provider error and points at the benchmark; the
        # busy one gets a wait estimate.
        assert "Provider errors" in message
        assert "valid API key" in message
        # The busy one still gets a wait estimate; the key one does not pretend
        # to be coming back on a timer.
        assert "Rate limited" in message
        assert "benchmark" in message
    finally:
        await client.aclose()


async def test_endpoints_still_cooling_are_not_counted_as_tried():
    """An endpoint skipped because it is resting was never attempted; reporting
    it as a failure overstates what happened and hides the real cause."""
    client = GroqClient(settings(groq_model_ladder="model-a,model-b"))
    try:
        # Put the first endpoint to sleep without it participating in this call.
        client.pool.endpoints[0].rest(60)

        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(429, json=error_body("rate limited"))
            )
            with pytest.raises(LLMError) as exc:
                await client.complete([{"role": "user", "content": "hi"}])

        message = str(exc.value)
        assert "tried 1 of 2" in message
        assert "still cooling" in message
        # Naming which endpoint, and for how long, is the difference between a
        # status line and something the user can act on.
        assert "model-a" in message or "model-b" in message
    finally:
        await client.aclose()


@pytest.mark.parametrize("raw", ["  gsk_padded  ", "gsk_padded\n", "\tgsk_padded"])
def test_keys_are_trimmed(raw):
    """A trailing newline from a phone paste survives .env quoting and reaches
    the Authorization header, where it is rejected as an invalid key with no
    hint that whitespace is the cause."""
    pool = build_pool(settings(groq_api_key=raw, groq_model_ladder="m"))
    assert pool.endpoints[0].api_key == "gsk_padded"


GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai"


def two_provider_client() -> GroqClient:
    """Groq plus Gemini, so a per-provider mistake has somewhere to show up."""
    client = GroqClient(settings())
    client.pool = Pool([
        Endpoint(model="model-a", api_key="gsk_primary", base_url=GROQ, label="groq:model-a"),
        Endpoint(
            model="gemini-2.0-flash", api_key="AQ.key", base_url=GEMINI,
            label="gemini:gemini-2.0-flash",
        ),
    ])
    return client


def models_body(*ids):
    return {"data": [{"id": i} for i in ids]}


@pytest.mark.parametrize(
    "listed",
    ["gemini-2.0-flash", "models/gemini-2.0-flash"],
    ids=["bare", "namespaced"],
)
@pytest.mark.asyncio
async def test_google_namespace_prefix_still_matches_the_configured_model(listed):
    """Google lists `models/gemini-2.0-flash` but accepts `gemini-2.0-flash`.
    A literal comparison calls a working model missing."""
    from jarvis.llm.client import normalise_model_id

    client = two_provider_client()
    with respx.mock:
        respx.get(f"{GROQ}/models").mock(return_value=httpx.Response(200, json=models_body("model-a")))
        respx.get(f"{GEMINI}/models").mock(return_value=httpx.Response(200, json=models_body(listed)))
        by_provider = await client.list_models_by_provider()

    available = {normalise_model_id(m) for m in by_provider[GEMINI]}
    assert normalise_model_id("gemini-2.0-flash") in available


@pytest.mark.asyncio
async def test_one_providers_catalogue_never_answers_for_another():
    """Gemini's listing fails; Groq's succeeds. Gemini's model is unproven, not
    missing — merging the two catalogues would report it as unavailable."""
    client = two_provider_client()
    with respx.mock:
        respx.get(f"{GROQ}/models").mock(return_value=httpx.Response(200, json=models_body("model-a")))
        respx.get(f"{GEMINI}/models").mock(return_value=httpx.Response(401, json=error_body("bad key")))
        by_provider = await client.list_models_by_provider()

    assert set(by_provider[GROQ]) == {"model-a"}
    assert GEMINI not in by_provider, "an unreadable catalogue must be absent, not empty"


def _pool_of(n=3):
    return Pool([
        Endpoint(model=f"model-{i}", api_key="gsk_x", base_url=GROQ, label=f"groq:model-{i}")
        for i in range(n)
    ])


def test_a_retired_endpoint_is_not_offered_again():
    pool = _pool_of(2)
    pool.endpoints[0].retire("cannot produce valid tool calls")

    assert pool.endpoints[0] not in pool.ready()
    assert len(pool.ready()) == 1


def test_retiring_twice_keeps_the_first_reason():
    endpoint = _pool_of(1).endpoints[0]
    endpoint.retire("cannot produce valid tool calls")
    endpoint.retire("something else")

    assert endpoint.retired == "cannot produce valid tool calls"


def test_a_retired_endpoint_is_not_reported_as_coming_back_soon():
    """Its cooldown is zero, so it would win `soonest` and produce 'capacity
    returns shortly' for a pool where nothing is coming back."""
    pool = _pool_of(2)
    pool.endpoints[0].retire("cannot produce valid tool calls")
    pool.endpoints[1].rest(45.0)

    assert pool.wait_hint() > 30


def test_wait_hint_is_zero_when_everything_is_retired():
    pool = _pool_of(2)
    for endpoint in pool.endpoints:
        endpoint.retire("cannot produce valid tool calls")

    assert pool.soonest() is None
    assert pool.wait_hint() == 0.0


def test_endpoints_are_not_counted_as_both_tried_and_cooling():
    """The reported bug: 'tried 1 of 3 endpoints, 3 still cooling down' — an
    endpoint tried and failed is cooling by the time the message is built, so
    counting the two separately described four endpoints in a pool of three."""
    client = GroqClient(settings())
    client.pool = _pool_of(3)
    for endpoint in client.pool.endpoints:
        endpoint.rest(30.0)

    message = client._exhausted_message(["groq:model-0: rate limited"])

    assert "tried 1 of 3 endpoints" in message
    assert "3 still cooling" not in message
    assert "2 still cooling" in message


def test_a_retired_endpoint_is_named_and_not_counted_as_capacity():
    client = GroqClient(settings())
    client.pool = _pool_of(2)
    client.pool.endpoints[0].retire("cannot produce valid tool calls")

    message = client._exhausted_message(["groq:model-1: rate limited"])

    assert "1 retired as unusable" in message
    assert "groq:model-0" in message
    assert "single endpoint" in message, "one live endpoint left is a capacity warning"


async def test_a_brief_cooldown_is_waited_out_rather_than_failed():
    """The reported bug: "No endpoint could answer (2 still cooling down)" for a
    pool whose capacity returned four seconds later. Failing instantly when
    everything is resting is a self-inflicted outage."""
    # Waiting is disabled suite-wide (see conftest); this test is about the
    # waiting, so it turns it back on.
    client = GroqClient(settings(groq_model_ladder="a", llm_wait_seconds=5.0))
    client.pool.endpoints[0].rest(0.3)
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(200, json=ok_body("answered after waiting"))
            )
            response = await client.complete([{"role": "user", "content": "hi"}])
        assert response.content == "answered after waiting"
    finally:
        await client.aclose()


async def test_a_long_cooldown_is_reported_instead_of_waited_out():
    """Beyond the ceiling the error is more use than the silence."""
    client = GroqClient(settings(groq_model_ladder="a"))
    client.pool.endpoints[0].rest(600.0)
    try:
        with pytest.raises(LLMError) as caught:
            await client.complete([{"role": "user", "content": "hi"}])
        assert "Capacity returns in" in str(caught.value)
    finally:
        await client.aclose()


async def test_a_rejected_key_is_retired_not_cooled():
    """A 401 never becomes a 200 by waiting. Cooling it means retrying on an
    ever-longer timer while it still counts as capacity."""
    client = GroqClient(settings(groq_model_ladder="a"))
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(401, json=error_body("Invalid API key"))
            )
            with pytest.raises(LLMError) as caught:
                await client.complete([{"role": "user", "content": "hi"}])

        assert client.pool.endpoints[0].retired == "key rejected"
        assert "key rejected" in str(caught.value)
        assert "Capacity returns" not in str(caught.value), (
            "nothing is coming back — saying otherwise sends the user off to wait"
        )
    finally:
        await client.aclose()


async def test_a_rejected_key_does_not_take_a_working_one_down_with_it():
    client = GroqClient(settings(groq_model_ladder="bad,good"))
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                side_effect=[
                    httpx.Response(401, json=error_body("Invalid API key")),
                    httpx.Response(200, json=ok_body("from the good one")),
                ]
            )
            response = await client.complete([{"role": "user", "content": "hi"}])
        assert response.content == "from the good one"
        assert client.pool.endpoints[0].retired
        assert not client.pool.endpoints[1].retired
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "key rejected"),
        (402, "needs a paid plan"),
        (403, "key has no access to this model"),
        (404, "model does not exist on this provider"),
    ],
)
async def test_permanent_faults_retire_with_their_own_reason(status, reason):
    """A 402 is not a busy minute. Cooling it retries a guaranteed failure while
    the endpoint still counts as capacity — and each of these needs a different
    action from the user, so the reason has to survive."""
    client = GroqClient(settings(groq_model_ladder="a"))
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(status, json=error_body("provider said no"))
            )
            with pytest.raises(LLMError) as caught:
                await client.complete([{"role": "user", "content": "hi"}])

        assert client.pool.endpoints[0].retired == reason
        assert reason in str(caught.value)
        assert "Capacity returns" not in str(caught.value)
    finally:
        await client.aclose()


async def test_a_billing_wall_does_not_stop_a_working_provider():
    client = GroqClient(settings(groq_model_ladder="paid,free"))
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                side_effect=[
                    httpx.Response(402, json=error_body("Payment required")),
                    httpx.Response(200, json=ok_body("the free one answered")),
                ]
            )
            response = await client.complete([{"role": "user", "content": "hi"}])
        assert response.content == "the free one answered"
        assert client.pool.endpoints[0].retired == "needs a paid plan"
    finally:
        await client.aclose()


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


def _signed_tool_call():
    """A Gemini tool call, complete with the field it insists on getting back."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "c1",
            "type": "function",
            "function": {"name": "calendar_list", "arguments": '{"start": "today"}'},
            "thought_signature": "sig-abc123",
        }],
        "_origin": GEMINI_URL,
    }


def test_a_providers_own_fields_survive_a_round_trip_home():
    """Gemini 3 refuses a follow-up whose tool call has lost its
    thought_signature — the field it added and we discarded."""
    from jarvis.llm.client import messages_for

    sent = messages_for([_signed_tool_call()], GEMINI_URL)[0]

    assert sent["tool_calls"][0]["thought_signature"] == "sig-abc123"
    assert "_origin" not in sent, "internal bookkeeping must never reach the API"


def test_provider_specific_fields_are_stripped_when_the_message_travels():
    """The pool fails over mid-conversation, so Gemini's fields can end up
    being sent to Groq, which has never heard of them."""
    from jarvis.llm.client import messages_for

    sent = messages_for([_signed_tool_call()], GROQ)[0]

    assert "thought_signature" not in sent["tool_calls"][0]
    assert sent["tool_calls"][0]["function"]["name"] == "calendar_list"
    assert "_origin" not in sent


def test_ordinary_messages_pass_through_untouched():
    from jarvis.llm.client import messages_for

    history = [
        {"role": "system", "content": "you are jarvis"},
        {"role": "user", "content": "what's on today"},
        {"role": "tool", "tool_call_id": "c1", "name": "calendar_list", "content": "[]"},
    ]

    assert messages_for(history, GROQ) == history


def _two_provider(**over):
    client = GroqClient(settings(**over))
    client.pool = Pool([
        Endpoint(model="groqm", api_key="k", base_url=GROQ, label="groq:groqm"),
        Endpoint(model="gemm", api_key="k", base_url=GEMINI_URL, label="gemini:gemm"),
    ])
    return client


SIGNATURE_ERROR = (
    "Function call is missing a thought_signature in functionCall parts. "
    "This is required for this model."
)


def test_a_started_tool_call_continues_on_the_provider_that_started_it():
    """Gemini refuses to continue a tool call it did not sign, so failing over
    mid-conversation breaks a conversation where nothing is actually wrong."""
    client = _two_provider()
    history = [
        {"role": "user", "content": "what's on today"},
        {"role": "assistant", "tool_calls": [{"id": "c1"}], "_origin": GEMINI_URL},
        {"role": "tool", "tool_call_id": "c1", "content": "[]"},
    ]

    order = [e.label for e in client._candidates(None, history)]

    assert order[0] == "gemini:gemm", "the follow-up belongs to whoever made the call"


def test_ordering_is_untouched_when_no_tool_call_is_in_flight():
    client = _two_provider()
    plain = [{"role": "user", "content": "hello"}]

    assert [e.label for e in client._candidates(None, plain)] == ["groq:groqm", "gemini:gemm"]


async def test_declining_a_history_neither_rests_nor_retires_the_endpoint():
    """The endpoint is healthy — it just cannot pick up someone else's tool
    call. Resting it would remove a working provider over a conversation."""
    client = _two_provider()
    try:
        with respx.mock:
            respx.post(f"{GEMINI_URL}/chat/completions").mock(
                return_value=httpx.Response(400, json=error_body(SIGNATURE_ERROR))
            )
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(200, json=ok_body("groq finished it"))
            )
            history = [
                {"role": "assistant", "tool_calls": [{"id": "c1"}], "_origin": GEMINI_URL},
                {"role": "tool", "tool_call_id": "c1", "content": "[]"},
            ]
            response = await client.complete(history, tools=[{"type": "function"}])

        assert response.content == "groq finished it"
        gemini = client.pool.endpoints[1]
        assert not gemini.retired
        assert gemini.available, "a healthy endpoint must stay in rotation"
    finally:
        await client.aclose()


async def test_a_declined_history_is_not_called_misconfigured():
    """It sent people to the benchmark, which passes, for a fault that is not
    theirs to fix."""
    client = _two_provider()
    try:
        with respx.mock:
            respx.post(f"{GEMINI_URL}/chat/completions").mock(
                return_value=httpx.Response(400, json=error_body(SIGNATURE_ERROR))
            )
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(429, json=error_body("rate limited"))
            )
            with pytest.raises(LLMError) as caught:
                await client.complete(
                    [{"role": "assistant", "tool_calls": [{"id": "c1"}], "_origin": GEMINI_URL}],
                    tools=[{"type": "function"}],
                )

        assert "misconfigured" not in str(caught.value)
        assert "cannot continue" in str(caught.value)
    finally:
        await client.aclose()


def test_flattening_lets_any_provider_pick_up_a_tool_call():
    """The stranding case: Groq calls a tool, then hits its per-minute limit,
    and Gemini won't continue Groq's call. Restated as plain text, it can."""
    from jarvis.llm.client import flatten_tool_exchange

    flat = flatten_tool_exchange([
        {"role": "user", "content": "what's on today"},
        {"role": "assistant", "content": None, "_origin": GROQ, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "calendar_list", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "calendar_list",
         "content": '[{"summary": "Dentist"}]'},
    ])

    assert not any(m.get("tool_calls") for m in flat), "no structure left to refuse"
    assert not any(m.get("role") == "tool" for m in flat)
    assert "calendar_list" in flat[1]["content"]
    assert "Dentist" in flat[2]["content"], "the results must survive the rewrite"
    assert flat[0] == {"role": "user", "content": "what's on today"}


def test_flattening_leaves_a_plain_conversation_alone():
    from jarvis.llm.client import flatten_tool_exchange

    plain = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]

    assert flatten_tool_exchange(plain) == plain


async def test_a_stranded_turn_is_finished_by_the_other_provider():
    """End to end: Groq is rate limited, Gemini declines the structured history,
    and the flattened retry gets an answer instead of an error."""
    client = _two_provider()
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(429, json=error_body("rate limited"))
            )
            respx.post(f"{GEMINI_URL}/chat/completions").mock(
                side_effect=[
                    httpx.Response(400, json=error_body(SIGNATURE_ERROR)),
                    httpx.Response(200, json=ok_body("Nothing on today.")),
                ]
            )
            response = await client.complete(
                [
                    {"role": "assistant", "content": None, "_origin": GROQ, "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "calendar_list", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "c1", "name": "calendar_list",
                     "content": "[]"},
                ],
                tools=[{"type": "function"}],
            )

        assert response.content == "Nothing on today."
    finally:
        await client.aclose()


def test_a_rate_limit_does_not_get_worse_each_time_it_is_hit():
    """A per-minute bucket refills on a fixed schedule. Doubling the wait turns
    a cleared 20-second limit into "capacity returns in about 39s"."""
    endpoint = Endpoint(model="m", api_key="k", base_url=GROQ)

    endpoint.rest(escalate=False)
    first = endpoint.seconds_until_available
    endpoint.succeeded()
    endpoint.rest(escalate=False)

    assert abs(endpoint.seconds_until_available - first) < 1


def test_repeated_rate_limits_stay_flat_while_real_faults_still_back_off():
    limited = Endpoint(model="m", api_key="k", base_url=GROQ)
    broken = Endpoint(model="m", api_key="k", base_url=GROQ)

    for _ in range(3):
        limited.rest(escalate=False)
        broken.rest()

    assert limited.seconds_until_available < 25
    assert broken.seconds_until_available > 60, "a failing provider should still back off"


def test_groqs_own_reset_header_is_used_when_retry_after_is_absent():
    """Groq sends no Retry-After on a token-per-minute refusal — it sends
    x-ratelimit-reset-tokens, and ignoring it meant guessing."""
    from jarvis.llm.client import _retry_after

    class R:
        headers = {"x-ratelimit-reset-tokens": "19.222s"}

    assert 19 <= _retry_after(R()) <= 20


def test_retry_after_still_wins_when_present():
    from jarvis.llm.client import _retry_after

    class R:
        headers = {"retry-after": "5", "x-ratelimit-reset-tokens": "19.222s"}

    assert _retry_after(R()) == 5.0


def test_a_cooling_endpoint_says_why_and_for_how_long():
    """"1 still cooling down from earlier failures" cannot distinguish a busy
    provider from a broken one without reading the server log."""
    client = GroqClient(settings(groq_model_ladder="a,b"))
    client.pool.endpoints[0].rest(30.0, escalate=False, why="rate limited")

    message = client._exhausted_message(["groq:b: request too large"])

    assert "rate limited" in message
    assert "30s" in message or "29s" in message


async def test_a_turn_that_already_ran_tools_waits_rather_than_discarding_the_work():
    """Failing here throws away a completed calendar read and makes the user ask
    again from scratch, so the longer silence is the cheaper option."""
    client = GroqClient(settings(
        groq_model_ladder="a", llm_retry_wait_mid_turn_seconds=5.0
    ))
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                side_effect=[
                    httpx.Response(429, json=error_body("rate limited"),
                                   headers={"retry-after": "1"}),
                    httpx.Response(200, json=ok_body("Nothing on today.")),
                ]
            )
            response = await client.complete([
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "calendar_list", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "name": "calendar_list", "content": "[]"},
            ])
        assert response.content == "Nothing on today."
    finally:
        await client.aclose()


def test_the_custom_slot_joins_the_pool_like_any_other_provider():
    """Free tiers appear and close faster than a hardcoded list keeps up, so
    adding one must not require a code change."""
    pool = build_pool(settings(
        custom_api_key="k",
        custom_base_url="https://api.example.com/v1",
        custom_model="some-model",
    ))

    assert any(e.label == "custom:some-model" for e in pool.endpoints)


def test_a_half_configured_custom_slot_is_ignored():
    """It has no defaults to fall back on, so a key with no URL would build an
    endpoint that fails every request with a confusing error."""
    for partial in (
        {"custom_api_key": "k"},
        {"custom_api_key": "k", "custom_base_url": "https://api.example.com/v1"},
        {"custom_api_key": "k", "custom_model": "m"},
    ):
        pool = build_pool(settings(**partial))
        assert not any(e.label.startswith("custom:") for e in pool.endpoints), partial


def test_new_providers_are_reachable_from_settings():
    pool = build_pool(settings(nvidia_api_key="k", huggingface_api_key="k"))
    labels = {e.label.split(":", 1)[0] for e in pool.endpoints}

    assert {"nvidia", "huggingface"} <= labels


def test_a_retired_model_names_its_own_providers_setting():
    """"Remove them from your model ladder" is Groq's setting. Saying it about
    an OpenRouter model sends the user somewhere unrelated."""
    client = GroqClient(settings())
    client.pool = Pool([
        Endpoint(model="m", api_key="k", base_url="https://openrouter.ai/api/v1",
                 label="openrouter:m"),
        Endpoint(model="n", api_key="k", base_url=GROQ, label="groq:n"),
    ])
    client.pool.endpoints[0].retire("model does not exist on this provider")

    message = client._exhausted_message(["openrouter:m: model does not exist on this provider"])

    assert "OPENROUTER_MODEL" in message
    assert "GROQ_MODEL_LADDER" not in message
    assert "benchmark" in message, "a missing model is the case the search can fix"


def test_a_rejected_key_is_not_sent_to_the_model_search():
    """No model name fixes a bad key, so proposing a search would waste time."""
    client = GroqClient(settings(groq_model_ladder="a"))
    client.pool.endpoints[0].retire("key rejected")

    message = client._exhausted_message(["groq:a: key rejected"])

    assert "Clear their keys" in message
    assert "benchmark to find a working model" not in message


def test_null_content_is_normalised_when_a_message_travels():
    """Groq emits content: null on every tool-call message. Gemini refuses it —
    "Value is not a string: null" — so a Groq turn continued on Gemini died on
    a message that was never Gemini's."""
    from jarvis.llm.client import messages_for

    from_groq = {
        "role": "assistant", "content": None, "_origin": GROQ,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "calendar_list", "arguments": "{}"}}],
    }

    sent = messages_for([from_groq], GEMINI_URL)[0]

    assert sent["content"] == ""
    assert sent["tool_calls"][0]["function"]["name"] == "calendar_list"


def test_null_content_is_left_alone_going_home():
    """The provider that produced it accepts its own output."""
    from jarvis.llm.client import messages_for

    from_groq = {"role": "assistant", "content": None, "_origin": GROQ, "tool_calls": [{"id": "c"}]}

    assert messages_for([from_groq], GROQ)[0]["content"] is None


async def test_a_provider_error_is_not_called_a_misconfiguration():
    """"Won't recover on their own" is wrong for upstream trouble, and sends the
    user to check a configuration that is fine."""
    client = GroqClient(settings(groq_model_ladder="a"))
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                return_value=httpx.Response(502, json=error_body("Provider returned error"))
            )
            with pytest.raises(LLMError) as caught:
                await client.complete([{"role": "user", "content": "hi"}])

        message = str(caught.value)
        assert "misconfigured" not in message
        assert "won't recover" not in message
        assert "transient" in message
    finally:
        await client.aclose()


def test_every_null_is_dropped_when_a_message_travels():
    """OpenAI-shaped responses carry optional fields as explicit nulls, and
    Gemini rejects any of them: "Value is not a string: null". Normalising only
    `content` left refusal, audio and the rest to fail the same way."""
    from jarvis.llm.client import messages_for

    from_groq = {
        "role": "assistant",
        "content": None,
        "refusal": None,
        "audio": None,
        "_origin": GROQ,
        "tool_calls": [{
            "id": None, "type": "function",
            "function": {"name": "calendar_list", "arguments": None},
        }],
    }

    sent = messages_for([from_groq], GEMINI_URL)[0]

    assert None not in sent.values(), f"a null survived: {sent}"
    assert sent["content"] == ""
    call = sent["tool_calls"][0]
    assert None not in call.values()
    assert None not in call["function"].values()
    assert call["function"]["arguments"] == "{}", "an absent argument object is empty, not null"


def test_no_null_survives_anywhere_in_a_travelled_message():
    """Checked structurally rather than field by field: the last two attempts at
    this each fixed the null they knew about and shipped the next one."""
    from jarvis.llm.client import messages_for

    def nulls(value, path="msg"):
        if value is None:
            yield path
        elif isinstance(value, dict):
            for k, v in value.items():
                yield from nulls(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                yield from nulls(v, f"{path}[{i}]")

    groq_shaped = {
        "role": "assistant", "content": None, "refusal": None, "audio": None,
        "reasoning": None, "annotations": [], "_origin": GROQ,
        "tool_calls": [{
            "id": "call_abc", "type": "function", "index": None,
            "function": {"name": "calendar_find_free", "arguments": '{"date":"tomorrow"}'},
        }],
    }

    sent = messages_for([groq_shaped], GEMINI_URL)[0]

    assert list(nulls(sent)) == []
    assert sent["tool_calls"][0]["function"]["name"] == "calendar_find_free"


async def test_a_pool_seconds_from_ready_waits_even_when_nothing_was_rate_limited():
    """The reported case: Groq cooling with three seconds left, OpenRouter
    erroring, Gemini declining the history. Nothing recorded a rate limit
    because the cooling endpoint was never tried, so the retry never fired for
    a pool that was about to be fine."""
    client = GroqClient(settings(
        groq_model_ladder="cooling,broken", llm_retry_wait_seconds=5.0
    ))
    cooling, broken = client.pool.endpoints
    cooling.rest(0.3, escalate=False, why="rate limited")
    try:
        with respx.mock:
            respx.post(f"{GROQ}/chat/completions").mock(
                side_effect=[
                    httpx.Response(502, json=error_body("Provider returned error")),
                    httpx.Response(200, json=ok_body("answered after the wait")),
                ]
            )
            response = await client.complete([{"role": "user", "content": "hi"}])

        assert response.content == "answered after the wait"
    finally:
        await client.aclose()


async def test_a_fully_retired_pool_does_not_wait_for_nothing():
    client = GroqClient(settings(groq_model_ladder="a"))
    client.pool.endpoints[0].retire("key rejected")
    try:
        with pytest.raises(LLMError) as caught:
            await client.complete([{"role": "user", "content": "hi"}])
        assert "Capacity returns" not in str(caught.value)
    finally:
        await client.aclose()


def test_any_provider_can_carry_a_list_of_models():
    """One key reaches a whole catalogue. Listing several models behind it is
    several independent chances to answer for the cost of one signup."""
    pool = build_pool(settings(
        nvidia_api_key="nvapi-x",
        nvidia_model="meta/llama-3.3-70b-instruct, qwen/qwen2.5-72b, deepseek-ai/deepseek-r1",
    ))

    nvidia = [e for e in pool.endpoints if e.label.startswith("nvidia:")]
    assert len(nvidia) == 3
    assert [e.model for e in nvidia] == [
        "meta/llama-3.3-70b-instruct", "qwen/qwen2.5-72b", "deepseek-ai/deepseek-r1"
    ]


def test_a_single_model_still_works_unchanged():
    pool = build_pool(settings(nvidia_api_key="nvapi-x", nvidia_model="only/one"))
    assert len([e for e in pool.endpoints if e.label.startswith("nvidia:")]) == 1
