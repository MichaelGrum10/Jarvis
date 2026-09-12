"""Claude as the engine that edits this source.

The translation between the pool's OpenAI-shaped history and Claude's native
one is the whole risk surface here. Two of these tests guard failures that are
silent rather than loud: dropped thinking blocks and split tool results both
produce a working-looking request that degrades the model's behaviour.
"""

from __future__ import annotations

import pytest

from jarvis.config import get_settings
from jarvis.llm.client import LLMError


class _Usage:
    input_tokens = 120
    output_tokens = 30
    cache_read_input_tokens = 90


class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Thinking:
    type = "thinking"

    def __init__(self, thinking="considered it"):
        self.thinking = thinking


class _ToolUse:
    type = "tool_use"

    def __init__(self, id, name, input):  # noqa: A002 — mirrors the SDK's field
        self.id, self.name, self.input = id, name, input


class _Reply:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason
        self.model = "claude-opus-5"
        self.usage = _Usage()
        self.stop_details = None


class _Messages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


class _FakeClient:
    def __init__(self, replies):
        self.messages = _Messages(replies)


@pytest.fixture
def claude(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test-never-leaves-here")
    monkeypatch.setattr(settings, "anthropic_model", "claude-opus-5")
    monkeypatch.setattr(settings, "anthropic_effort", "high")
    return settings


def _coder(claude, replies):
    from jarvis.agent.coder import ClaudeCoder

    coder = ClaudeCoder(claude)
    coder._client = _FakeClient(replies)
    return coder


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
]


# ------------------------------------------------------------------ choosing


def test_claude_is_used_when_a_key_is_set(claude):
    from jarvis.agent import coder

    assert coder.available(claude) is True
    assert "claude-opus-5" in coder.describe(claude)


def test_without_a_key_it_falls_back_to_the_free_pool(claude, monkeypatch):
    """No key is a supported configuration, not an error — the engine still
    runs, just less well."""
    from jarvis.agent import coder

    monkeypatch.setattr(claude, "anthropic_api_key", "")
    assert coder.available(claude) is False
    assert "free pool" in coder.describe(claude)

    brain = coder.make_brain(claude)
    assert not isinstance(brain, coder.ClaudeCoder)


def test_each_run_gets_its_own_transcript(claude):
    """A coder holds one run's history. Handing the next run a used one would
    replay the last run's work as if it were context."""
    from jarvis.agent import coder

    first, second = coder.make_brain(claude), coder.make_brain(claude)
    assert first is not second


# -------------------------------------------------------------- translation


async def test_the_request_is_shaped_the_way_the_api_wants(claude):
    coder = _coder(claude, [_Reply([_Text("done")], stop_reason="end_turn")])
    await coder.complete(
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "fix it"}],
        TOOLS,
        temperature=0.1,
    )

    sent = coder._client.messages.calls[0]
    assert sent["model"] == "claude-opus-5"
    assert sent["system"] == "SYS", "the system prompt is a parameter, not a message"
    assert sent["messages"] == [{"role": "user", "content": "fix it"}]
    assert sent["output_config"] == {"effort": "high"}
    # Sampling parameters are rejected outright by the current models, so the
    # engine's temperature must be dropped rather than forwarded.
    assert "temperature" not in sent
    # Flat tool shape, not OpenAI's nested one.
    assert sent["tools"] == [
        {
            "name": "read_file",
            "description": "Read a file.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]


async def test_thinking_blocks_are_replayed_verbatim(claude):
    """The engine's history has nowhere to put a thinking block, so rebuilding
    the request from it would drop them and the next turn would be refused."""
    thinking = _Thinking()
    coder = _coder(
        claude,
        [
            _Reply([thinking, _ToolUse("t1", "read_file", {"path": "a.py"})]),
            _Reply([_Text("done")], stop_reason="end_turn"),
        ],
    )

    history = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "go"}]
    await coder.complete(history, TOOLS)

    # What the engine appends next: a reconstructed assistant turn (no thinking)
    # plus the tool result.
    history += [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "file body"},
    ]
    await coder.complete(history, TOOLS)

    second = coder._client.messages.calls[1]["messages"]
    assistant = [m for m in second if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert thinking in assistant[0]["content"], "the real thinking block must go back"


async def test_tool_results_go_back_in_one_message(claude):
    """Splitting parallel results across messages teaches the model to stop
    calling tools in parallel — a silent degradation, not an error."""
    coder = _coder(
        claude,
        [
            _Reply([
                _ToolUse("t1", "read_file", {"path": "a.py"}),
                _ToolUse("t2", "read_file", {"path": "b.py"}),
            ]),
            _Reply([_Text("done")], stop_reason="end_turn"),
        ],
    )

    history = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "go"}]
    await coder.complete(history, TOOLS)
    history += [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}, {"id": "t2"}]},
        {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "A"},
        {"role": "tool", "tool_call_id": "t2", "name": "read_file", "content": "B"},
    ]
    await coder.complete(history, TOOLS)

    sent = coder._client.messages.calls[1]["messages"]
    results = [m for m in sent if m["role"] == "user" and isinstance(m["content"], list)]
    assert len(results) == 1, "both results belong to one user message"
    assert [b["tool_use_id"] for b in results[0]["content"]] == ["t1", "t2"]


async def test_the_reply_looks_like_any_other_llm_response(claude):
    """The engine's loop is shared, so a Claude turn has to be indistinguishable
    from a pool turn at the call site."""
    coder = _coder(
        claude, [_Reply([_Text("reading it"), _ToolUse("t1", "read_file", {"path": "a.py"})])]
    )
    reply = await coder.complete(
        [{"role": "system", "content": "S"}, {"role": "user", "content": "go"}], TOOLS
    )

    assert reply.wants_tools is True
    assert reply.content == "reading it"
    call = reply.tool_calls[0]
    assert (call.id, call.name, call.arguments) == ("t1", "read_file", {"path": "a.py"})
    assert reply.usage["cached_tokens"] == 90


# ------------------------------------------------------------------ failures


async def test_a_refusal_is_raised_not_treated_as_an_empty_answer(claude):
    """A refusal is a 200 with nothing usable in it. Read as an empty reply it
    would loop until the step limit."""
    reply = _Reply([], stop_reason="refusal")
    reply.stop_details = type("D", (), {"category": "cyber", "explanation": "no"})()
    coder = _coder(claude, [reply])

    with pytest.raises(LLMError, match="declined"):
        await coder.complete([{"role": "user", "content": "go"}], TOOLS)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("AuthenticationError", "rejected the API key"),
        ("PermissionDeniedError", "not allowed"),
        ("NotFoundError", "no model called"),
    ],
)
async def test_api_failures_name_the_fix(claude, error, expected):
    import anthropic

    class _Boom:
        async def create(self, **kwargs):
            exc = getattr(anthropic, error)
            raise exc.__new__(exc)

    coder = _coder(claude, [])
    coder._client.messages = _Boom()

    with pytest.raises(LLMError, match=expected):
        await coder.complete([{"role": "user", "content": "go"}], TOOLS)


async def test_the_key_is_never_in_an_error_message(claude):
    import anthropic

    class _Boom:
        async def create(self, **kwargs):
            raise anthropic.AuthenticationError.__new__(anthropic.AuthenticationError)

    coder = _coder(claude, [])
    coder._client.messages = _Boom()

    with pytest.raises(LLMError) as caught:
        await coder.complete([{"role": "user", "content": "go"}], TOOLS)
    assert "sk-ant-test-never-leaves-here" not in str(caught.value)
