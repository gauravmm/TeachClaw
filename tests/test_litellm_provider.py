"""Tests for ``LiteLLMProvider._parse_response``.

The parser turns a ``litellm.ModelResponse`` into our internal
``LLMResponse`` and is the only piece of the provider that's pure
logic — every reply funnels through it, so silent bugs corrupt the
agent's view of every turn. Network paths (``__init__``, ``chat``)
are not exercised here.
"""

from __future__ import annotations

import litellm
from litellm.types.utils import (
    ChatCompletionMessageToolCall,
    Choices,
    Function,
    Message,
    ModelResponse,
    Usage,
)

import teachclaw.agent.loop  # noqa: F401  — break a pre-existing circular import
from teachclaw.providers.litellm_provider import LiteLLMProvider


def _response(
    *,
    content: str | None = "hello",
    tool_calls: list[ChatCompletionMessageToolCall] | None = None,
    finish_reason: str = "stop",
    usage: Usage | None = None,
    reasoning_content: str | None = None,
) -> ModelResponse:
    msg = Message(content=content, role="assistant", tool_calls=tool_calls)
    if reasoning_content is not None:
        # ``Message`` is a pydantic model; reasoning_content arrives as an
        # extra field from providers like Kimi/DeepSeek-R1 and is read via
        # getattr in the parser.
        object.__setattr__(msg, "reasoning_content", reasoning_content)
    kwargs: dict = {"choices": [Choices(finish_reason=finish_reason, index=0, message=msg)]}
    if usage is not None:
        kwargs["usage"] = usage
    return ModelResponse(**kwargs)


def test_parse_plain_text_response():
    parsed = LiteLLMProvider._parse_response(_response(content="hi there"))
    assert parsed.content == "hi there"
    assert parsed.tool_calls == []
    assert parsed.finish_reason == "stop"
    assert parsed.usage == {}
    assert parsed.reasoning_content is None
    assert parsed.has_tool_calls is False


def test_parse_none_content_becomes_empty_string():
    """Some providers omit content when only tool_calls are present."""
    parsed = LiteLLMProvider._parse_response(_response(content=None))
    assert parsed.content == ""


def test_parse_strips_leading_double_newline_qwen_workaround():
    parsed = LiteLLMProvider._parse_response(_response(content="\n\nactual reply"))
    assert parsed.content == "actual reply"


def test_parse_does_not_strip_single_leading_newline():
    parsed = LiteLLMProvider._parse_response(_response(content="\nkeep me"))
    assert parsed.content == "\nkeep me"


def test_parse_extracts_tool_call_with_json_arguments():
    tc = ChatCompletionMessageToolCall(
        id="call_42",
        function=Function(name="search", arguments='{"q": "puppies", "n": 3}'),
    )
    parsed = LiteLLMProvider._parse_response(
        _response(content=None, tool_calls=[tc], finish_reason="tool_calls")
    )
    assert parsed.has_tool_calls is True
    assert len(parsed.tool_calls) == 1
    call = parsed.tool_calls[0]
    assert call.id == "call_42"
    assert call.name == "search"
    assert call.arguments == {"q": "puppies", "n": 3}
    assert parsed.finish_reason == "tool_calls"


def test_parse_falls_back_to_raw_for_invalid_json_arguments():
    """Malformed tool arguments must not crash the parser; the agent sees
    a {'raw': ...} dict so it can decide how to handle the bogus call."""
    tc = ChatCompletionMessageToolCall(
        id="call_bad", function=Function(name="search", arguments="not-json{")
    )
    parsed = LiteLLMProvider._parse_response(_response(content=None, tool_calls=[tc]))
    assert parsed.tool_calls[0].arguments == {"raw": "not-json{"}


def test_parse_handles_missing_tool_function_name():
    tc = ChatCompletionMessageToolCall(id="call_x", function=Function(name=None, arguments="{}"))
    parsed = LiteLLMProvider._parse_response(_response(content=None, tool_calls=[tc]))
    assert parsed.tool_calls[0].name == "(no name)"


def test_parse_collects_usage_when_present():
    parsed = LiteLLMProvider._parse_response(
        _response(usage=Usage(prompt_tokens=12, completion_tokens=34, total_tokens=46))
    )
    assert parsed.usage == {
        "prompt_tokens": 12,
        "completion_tokens": 34,
        "total_tokens": 46,
    }


def test_parse_handles_missing_finish_reason():
    msg = Message(content="x", role="assistant")
    response = ModelResponse(choices=[Choices(finish_reason=None, index=0, message=msg)])
    parsed = LiteLLMProvider._parse_response(response)
    assert parsed.finish_reason == "stop"


def test_parse_carries_reasoning_content():
    parsed = LiteLLMProvider._parse_response(
        _response(content="answer", reasoning_content="step 1; step 2")
    )
    assert parsed.reasoning_content == "step 1; step 2"


def test_parse_multiple_tool_calls_preserve_order():
    calls = [
        ChatCompletionMessageToolCall(
            id=f"call_{i}", function=Function(name=f"t{i}", arguments="{}")
        )
        for i in range(3)
    ]
    parsed = LiteLLMProvider._parse_response(_response(content=None, tool_calls=calls))
    assert [c.id for c in parsed.tool_calls] == ["call_0", "call_1", "call_2"]
    assert [c.name for c in parsed.tool_calls] == ["t0", "t1", "t2"]


def test_litellm_choices_alias_is_real():
    """Sanity guard: the parser asserts ``isinstance(choice, litellm.Choices)``;
    fail loudly here if litellm renames the alias instead of in production."""
    assert litellm.Choices is Choices
