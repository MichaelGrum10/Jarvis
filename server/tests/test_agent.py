"""Agent loop behaviour, with the LLM stubbed so the control flow is what's tested."""

from __future__ import annotations

from jarvis.agent.loop import Agent, history_from_rows
from jarvis.llm.client import LLMError, LLMResponse, ToolCall


class FakeLLM:
    """Replays a scripted list of responses and records what it was sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools})
        if tools is None:
            # Mirrors a real model: with no tools offered it can only answer in text.
            return LLMResponse(content="Wrapping up with what I have.")
        if not self.responses:
            return LLMResponse(content="done")
        return self.responses.pop(0)


async def test_plain_answer_needs_no_tools(monkeypatch, ctx):
    agent = Agent()
    agent.llm = FakeLLM([LLMResponse(content="Hello.")])
    outcome = await agent.run("hi", [], ctx)
    assert outcome.reply == "Hello."
    assert outcome.tool_calls == []


async def test_tool_call_then_answer(ctx):
    agent = Agent()
    agent.llm = FakeLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="current_time", arguments={})]),
            LLMResponse(content="It is Tuesday."),
        ]
    )
    outcome = await agent.run("what day is it", [], ctx)
    assert outcome.reply == "It is Tuesday."
    assert len(outcome.tool_calls) == 1
    assert outcome.tool_calls[0]["tool"] == "current_time"
    assert outcome.tool_calls[0]["ok"] is True


async def test_tool_result_is_fed_back_to_the_model(ctx):
    """A tool that runs but whose output never reaches the model is worse than useless."""
    fake = FakeLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="current_time", arguments={})]),
            LLMResponse(content="ok"),
        ]
    )
    agent = Agent()
    agent.llm = fake
    await agent.run("time?", [], ctx)

    second_call = fake.calls[1]["messages"]
    tool_messages = [m for m in second_call if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "c1"
    assert "timezone" in tool_messages[0]["content"]


async def test_failed_tool_is_reported_but_loop_continues(ctx):
    agent = Agent()
    agent.llm = FakeLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="places_search",
                                             arguments={"category": "haircut"})]),
            LLMResponse(content="I need your location first."),
        ]
    )
    outcome = await agent.run("haircut near me", [], ctx)  # ctx has no coordinates
    assert outcome.tool_calls[0]["ok"] is False
    assert outcome.reply == "I need your location first."


async def test_parallel_tool_calls_all_execute(ctx):
    agent = Agent()
    agent.llm = FakeLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="a", name="current_time", arguments={}),
                    ToolCall(id="b", name="system_status", arguments={}),
                ]
            ),
            LLMResponse(content="both done"),
        ]
    )
    outcome = await agent.run("status", [], ctx)
    assert {c["tool"] for c in outcome.tool_calls} == {"current_time", "system_status"}


async def test_step_limit_forces_a_final_answer(ctx):
    """A model that loops on tools forever must still produce something for the user."""
    looping = [
        LLMResponse(tool_calls=[ToolCall(id=f"c{i}", name="current_time", arguments={})])
        for i in range(20)
    ]
    agent = Agent()
    agent.llm = FakeLLM(looping)
    outcome = await agent.run("loop", [], ctx)
    assert outcome.reply  # the wrap-up call produced text
    assert outcome.error == ""


async def test_llm_error_surfaces_as_error_event(ctx):
    from jarvis.llm.client import LLMError

    class Broken:
        async def complete(self, *a, **k):
            raise LLMError("no api key")

    agent = Agent()
    agent.llm = Broken()
    outcome = await agent.run("hi", [], ctx)
    assert "no api key" in outcome.error


async def test_system_prompt_reflects_location_state(ctx, located_ctx):
    from jarvis.agent.prompts import build_system_prompt
    from jarvis.config import get_settings

    settings = get_settings()
    without = build_system_prompt(settings, ctx)
    with_loc = build_system_prompt(settings, located_ctx)

    assert "has NOT shared location" in without
    assert "42.3603" in with_loc


async def test_history_drops_tool_noise():
    class Row:
        def __init__(self, role, content):
            self.role, self.content = role, content

    rows = [
        Row("user", "hi"),
        Row("assistant", "hello"),
        Row("tool", '{"junk": true}'),
        Row("assistant", ""),
    ]
    history = history_from_rows(rows)
    assert history == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]


async def test_waiting_is_capped_across_the_whole_turn(monkeypatch):
    """Eight steps each allowed their own wait is minutes of silence, and the
    browser drops the connection long before that — the user sees "Load failed",
    which is worse than the error waiting was meant to avoid."""
    from jarvis.agent.loop import Agent

    agent = Agent()
    budget = agent.settings.llm_turn_wait_budget_seconds
    seen: list[float | None] = []

    async def record(messages, tools=None, **kwargs):
        seen.append(kwargs.get("wait_budget"))
        raise LLMError("nothing available")

    monkeypatch.setattr(agent.llm, "complete", record)

    async for _ in agent.stream("hello"):
        pass

    assert seen, "the loop must pass a budget at all"
    assert seen[0] is not None
    assert seen[0] <= budget


def test_the_persona_forbids_the_habits_that_make_it_unbearable():
    """Terse, no closing offers, and "sir" only at the edges of a task."""
    from jarvis.agent.prompts import build_system_prompt
    from jarvis.config import Settings
    from jarvis.tools.base import ToolContext

    prompt = build_system_prompt(
        Settings(auth_secret="x" * 32, access_password="y" * 12),
        ToolContext(timezone="America/New_York"),
    )

    assert "only when greeting them or reporting a finished task" in prompt
    assert "No closing offers" in prompt
    assert "under forty words" in prompt, "spoken replies need their own budget"
