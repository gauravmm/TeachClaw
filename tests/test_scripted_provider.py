from __future__ import annotations

import json
from pathlib import Path

import pytest

import teachclaw.agent.loop  # noqa: F401  — break a pre-existing circular import
from teachclaw.providers.scripted import ScriptedProvider, ScriptedResponse


@pytest.mark.asyncio
async def test_scripted_provider_replays_in_order(tmp_path: Path) -> None:
    fixture = tmp_path / "script.yaml"
    fixture.write_text(
        "responses:\n"
        "  - content: hello\n"
        "    usage_total: 200\n"
        "  - content: ''\n"
        "    tool_calls:\n"
        "      - {name: search, arguments: {q: x}, id: tc1}\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider.from_fixture(fixture)

    r1 = await provider.chat(messages=[])
    r2 = await provider.chat(messages=[])
    r3 = await provider.chat(messages=[])  # past end → repeats last

    assert r1.content == "hello"
    assert r1.usage["total_tokens"] == 200
    assert r1.tool_calls == []

    assert r2.content == ""
    assert len(r2.tool_calls) == 1
    assert r2.tool_calls[0].name == "search"
    assert r2.tool_calls[0].arguments == {"q": "x"}
    assert r2.tool_calls[0].id == "tc1"

    assert r3.content == r2.content  # last entry repeats
    assert len(provider.calls) == 3


@pytest.mark.asyncio
async def test_scripted_provider_balloon_inflates_response(tmp_path: Path) -> None:
    fixture = tmp_path / "balloon.json"
    fixture.write_text(
        json.dumps({"responses": [{"content": "small", "balloon": 5000, "usage_total": 19000}]}),
        encoding="utf-8",
    )
    provider = ScriptedProvider.from_fixture(fixture)
    response = await provider.chat(messages=[])
    assert len(response.content) >= 5000
    assert response.usage["total_tokens"] == 19000


def test_scripted_provider_requires_responses(tmp_path: Path) -> None:
    fixture = tmp_path / "empty.yaml"
    fixture.write_text("responses: []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ScriptedProvider.from_fixture(fixture)


def test_scripted_response_from_dict_round_trip() -> None:
    sr = ScriptedResponse.from_dict(
        {
            "content": "hi",
            "tool_calls": [{"name": "n", "arguments": {"a": 1}}],
            "usage_total": 10,
        }
    )
    response = sr.to_response()
    assert response.content == "hi"
    assert response.tool_calls[0].name == "n"
    assert response.tool_calls[0].id == "tc0"
    assert response.usage["total_tokens"] == 10
