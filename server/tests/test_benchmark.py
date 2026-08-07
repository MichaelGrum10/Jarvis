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
