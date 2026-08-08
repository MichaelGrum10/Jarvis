"""The provider benchmark's scoring — the part that decides what you'd switch to."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from jarvis.benchmark import benchmark_endpoint, padding_tools, tool_call_correct, verdict
from jarvis.llm.pool import Endpoint

BASE = "https://api.example.com/v1"


def api_tool_response(name="get_stock_quote", args='{"symbol": "NVDA"}'):
    """What a provider sends back over the wire."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [{"id": "1", "function": {"name": name, "arguments": args}}],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 3200},
    }


def parsed(name="get_stock_quote", args='{"symbol": "NVDA"}'):
    """What call() hands to the scorer, after unwrapping the envelope."""
    return {"ok": True, "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def api_text_response(text="ok"):
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20},
    }


# ---------------------------------------------------------------- scoring


def test_correct_tool_call_passes():
    ok, why = tool_call_correct(parsed())
    assert ok and why == "correct"


def test_arguments_crammed_into_the_name_is_caught():
    """The exact failure seen in real use. A benchmark that scored this as a pass
    would recommend the model that caused the outage."""
    ok, why = tool_call_correct(parsed(name='get_stock_quote {"symbol": "NVDA"}', args="{}"))
    assert not ok and why == "args inside name"


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"name": "search_web"}, "wrong tool"),
        ({"args": "{not json}"}, "arguments not valid JSON"),
        ({"args": '{"symbol": "TSLA"}'}, "wrong symbol"),
    ],
)
def test_bad_calls_are_rejected(kwargs, expected):
    ok, why = tool_call_correct(parsed(**kwargs))
    assert not ok and expected in why


def test_no_tool_call_is_a_failure():
    ok, why = tool_call_correct({"tool_calls": [], "content": "NVDA is a company."})
    assert not ok and why == "no tool call"


# ---------------------------------------------------------------- verdicts


def test_failing_at_full_size_is_disqualifying_however_fast():
    """This assistant is tool calls end to end; a model that can't manage a
    full-size turn is unusable regardless of latency."""
    fast_but_broken = {
        "reachable": True, "tools_small": True, "tools_large": False, "latency": 0.3,
    }
    assert "fails at full size" in verdict(fast_but_broken)


def test_speed_only_ranks_among_working_endpoints():
    quick = {"reachable": True, "tools_small": True, "tools_large": True, "large_latency": 1.0}
    slow = {"reachable": True, "tools_small": True, "tools_large": True, "large_latency": 8.0}
    assert "excellent" in verdict(quick)
    assert "slow" in verdict(slow)


def test_unreachable_is_reported_plainly():
    assert "unreachable" in verdict({"reachable": False})


# ---------------------------------------------------------------- full run


def test_padding_reproduces_a_realistic_request_size():
    """The large test only means something if it is genuinely large — this app
    sends ~29 schemas per turn."""
    tools = padding_tools(28)
    assert len(tools) == 28
    assert len(json.dumps(tools)) > 6000


async def test_endpoint_that_works_scores_well():
    endpoint = Endpoint(model="good-model", api_key="k", base_url=BASE)
    with respx.mock:
        route = respx.post(f"{BASE}/chat/completions")
        route.side_effect = [
            httpx.Response(200, json=api_text_response()),
            httpx.Response(200, json=api_tool_response()),
            httpx.Response(200, json=api_tool_response()),
        ]
        async with httpx.AsyncClient() as client:
            row = await benchmark_endpoint(client, endpoint)

    assert row["reachable"] and row["tools_small"] and row["tools_large"]
    assert row["prompt_tokens"] == 3200


async def test_rate_limited_at_full_size_is_surfaced():
    """The realistic free-tier failure: fine on a small request, refused on a
    real one. Reporting only the small result would be actively misleading."""
    endpoint = Endpoint(model="tight-limits", api_key="k", base_url=BASE)
    with respx.mock:
        route = respx.post(f"{BASE}/chat/completions")
        route.side_effect = [
            httpx.Response(200, json=api_text_response()),
            httpx.Response(200, json=api_tool_response()),
            httpx.Response(413, json={"error": {"message": "Limit 6000, Requested 11081"}}),
        ]
        async with httpx.AsyncClient() as client:
            row = await benchmark_endpoint(client, endpoint)

    assert row["tools_small"] is True
    assert row["tools_large"] is False
    assert "413" in row["tools_large_why"]
    assert "fails at full size" in verdict(row)


async def test_unreachable_endpoint_stops_early():
    endpoint = Endpoint(model="dead", api_key="k", base_url=BASE)
    with respx.mock:
        respx.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
        )
        async with httpx.AsyncClient() as client:
            row = await benchmark_endpoint(client, endpoint)

    assert row["reachable"] is False
    assert "401" in row["error"]


# ---------------------------------------------------------------- regressions


async def test_latency_probe_omits_tool_choice_entirely():
    """The bug that made every endpoint report 'unreachable'. With no tools,
    sending "tool_choice": null is rejected outright:
    400 "Only allowed string values for 'tool_choice' are [none, auto, required]".
    Both keys must be absent, not null."""
    endpoint = Endpoint(model="m", api_key="k", base_url=BASE)
    seen: list[dict] = []

    def capture(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=api_text_response())

    with respx.mock:
        respx.post(f"{BASE}/chat/completions").mock(side_effect=capture)
        async with httpx.AsyncClient() as client:
            from jarvis.benchmark import call
            await call(client, endpoint, [{"role": "user", "content": "hi"}], tools=None)

    assert "tool_choice" not in seen[0], "null tool_choice is a 400 on Groq"
    assert "tools" not in seen[0]


async def test_tool_probe_does_send_tool_choice():
    endpoint = Endpoint(model="m", api_key="k", base_url=BASE)
    seen: list[dict] = []

    def capture(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=api_tool_response())

    with respx.mock:
        respx.post(f"{BASE}/chat/completions").mock(side_effect=capture)
        async with httpx.AsyncClient() as client:
            from jarvis.benchmark import TEST_TOOL, call
            await call(client, endpoint, [{"role": "user", "content": "hi"}], tools=[TEST_TOOL])

    assert seen[0]["tool_choice"] == "auto"


@pytest.mark.parametrize(
    "error,expected",
    [
        ("HTTP 404: The model `x` does not exist", "model not available"),
        ("HTTP 401: invalid api key", "key rejected"),
        ("HTTP 400: Only allowed string values for 'tool_choice'", "request rejected"),
        ("ConnectError", "unreachable"),
    ],
)
def test_config_errors_are_not_reported_as_unreachable(error, expected):
    """Calling a retired model or a bad key 'unreachable' sends people debugging
    their network instead of their .env."""
    assert expected in verdict({"reachable": False, "error": error})


async def test_one_broken_provider_does_not_discard_the_whole_run():
    """A crash mid-run previously lost the results for every endpoint tested
    before it — the expensive part of the exercise."""
    from jarvis.benchmark import benchmark_endpoint

    endpoint = Endpoint(model="weird", api_key="k", base_url=BASE)
    with respx.mock:
        respx.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(400, json=[{"error": {"message": "list-shaped"}}])
        )
        async with httpx.AsyncClient() as client:
            row = await benchmark_endpoint(client, endpoint)

    assert row["reachable"] is False
    assert "list-shaped" in row["error"]


def test_a_missing_model_names_its_own_providers_setting():
    """Telling someone to edit GROQ_MODEL_LADDER because a Cerebras model was
    retired sends them to a setting unrelated to the failure."""
    from jarvis.benchmark import _setting_for
    from jarvis.llm.pool import Endpoint

    def at(url):
        return Endpoint(model="m", api_key="k", base_url=url)

    assert _setting_for(at("https://api.groq.com/openai/v1")) == "GROQ_MODEL_LADDER"
    assert _setting_for(at("https://api.cerebras.ai/v1")) == "CEREBRAS_MODEL"
    assert _setting_for(
        at("https://generativelanguage.googleapis.com/v1beta/openai")
    ) == "GEMINI_MODEL"
    assert _setting_for(at("https://models.inference.ai.azure.com")) == "GITHUB_MODELS_MODEL"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("HTTP 402: Payment required to access this resource", "needs a paid plan"),
        ("HTTP 429: You exceeded your current quota, check your plan and billing",
         "no free quota for this model"),
        ("HTTP 429: Rate limit reached, please try again", "rate limited right now"),
        ("HTTP 401: Wrong API Key", "key rejected"),
    ],
)
def test_each_refusal_is_named_for_what_it_actually_is(error, expected):
    """"Unreachable" for a billing wall or a model with no free allowance sends
    people debugging their network instead of their config."""
    from jarvis.benchmark import verdict

    assert expected in verdict({"reachable": False, "error": error})


def test_replacement_candidates_skip_models_that_cannot_do_the_job():
    """A provider's catalogue is mostly noise here — transcription, embeddings,
    and models too small to hold a 30-schema turn."""
    from jarvis.benchmark import _candidate_models

    catalogue = {
        "whisper-large-v3", "text-embedding-3", "llama-guard-4",
        "llama-3.1-8b-instant", "gpt-oss-120b", "gemini-2.5-flash", "current-model",
    }

    candidates = _candidate_models(catalogue, exclude="current-model")

    assert "whisper-large-v3" not in candidates
    assert "text-embedding-3" not in candidates
    assert "llama-guard-4" not in candidates
    assert "llama-3.1-8b-instant" not in candidates, "8b cannot hold a full-size turn"
    assert "current-model" not in candidates, "the failing model is not its own replacement"
    assert candidates[0] == "gpt-oss-120b", "measured-good models are tried first"
    assert "gemini-2.5-flash" in candidates, (
        "a substring test drops every Gemini model, because 'gemini' contains 'mini'"
    )


def test_gemini_survives_the_small_model_filter():
    """The bug this replaces: filtering by substring discarded the entire Gemini
    catalogue while trying to repair a Gemini endpoint, so the provider was
    reported dead when every one of its models was fine."""
    from jarvis.benchmark import _candidate_models

    catalogue = {
        "gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-pro",
        "gemini-2.5-flash-lite", "gpt-4o-mini", "text-embedding-004",
    }

    candidates = _candidate_models(catalogue, exclude="gemini-2.0-flash")

    assert "gemini-2.5-flash" in candidates
    assert "gemini-2.5-pro" in candidates
    assert "gemini-2.5-flash-lite" not in candidates, "'lite' is a real token here"
    assert "gpt-4o-mini" not in candidates, "'mini' is a real token here"
    assert "text-embedding-004" not in candidates


def test_the_key_setting_is_derived_from_the_model_setting():
    """Groq's model setting is a ladder, so a naive suffix swap produces
    GROQ_MODEL_LADDER_API_KEY."""
    from jarvis.benchmark import _key_setting_for
    from jarvis.llm.pool import Endpoint

    groq = Endpoint(model="m", api_key="k", base_url="https://api.groq.com/openai/v1")
    gemini = Endpoint(
        model="m", api_key="k",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
    )

    assert _key_setting_for(groq) == "GROQ_API_KEY"
    assert _key_setting_for(gemini) == "GEMINI_API_KEY"


def test_a_base_url_setting_is_derivable_for_every_provider():
    """The 404-with-no-catalogue advice names a _BASE_URL setting; it has to be
    the real one, or the remedy sends people to a setting that does not exist."""
    from jarvis.benchmark import _base_url_setting_for, _key_setting_for, _setting_for
    from jarvis.config import Settings
    from jarvis.llm.pool import Endpoint

    fields = set(Settings.model_fields)
    for url in (
        "https://api.groq.com/openai/v1",
        "https://api.cerebras.ai/v1",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "https://models.github.ai/inference",
        "https://api.mistral.ai/v1",
        "https://openrouter.ai/api/v1",
        "https://api.together.xyz/v1",
    ):
        endpoint = Endpoint(model="m", api_key="k", base_url=url)
        for setting in (
            _key_setting_for(endpoint),
            _base_url_setting_for(endpoint),
            _setting_for(endpoint),
        ):
            assert setting.lower() in fields, f"{setting} is not a real setting"
