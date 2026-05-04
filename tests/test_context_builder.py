from __future__ import annotations

from pathlib import Path
from typing import Any

from benchclaw.agent.context import build_system_prompt


class _DummyTool:
    def __init__(self, name: str, description: str, parameters: dict[str, Any]) -> None:
        self._name = name
        self._description = description
        self._parameters = parameters

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters


def test_build_system_prompt_uses_xml_safe_rendering(tmp_path: Path) -> None:
    tool = _DummyTool(
        name='quote"tool',
        description='Say "hi" & compare <values>.',
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": 'Path with "quotes" & symbols'},
            },
            "required": ["path"],
        },
    )

    prompt = build_system_prompt(
        tmp_path,
        tools=[tool],
        channel="whatsapp",
        chat_id="123&456",
        session_label='Alice "A" & Bob',
    )

    assert '<tool name="quote&quot;tool">' in prompt
    assert 'Say "hi" &amp; compare &lt;values&gt;.' in prompt
    assert "params=" not in prompt
    assert 'Session: Alice "A" &amp; Bob' in prompt


def test_build_system_prompt_lists_registered_tools(tmp_path: Path) -> None:
    tool = _DummyTool(
        name="annotate_media",
        description="Save image annotations.",
        parameters={"type": "object", "properties": {}, "required": []},
    )

    prompt = build_system_prompt(tmp_path, tools=[tool])

    assert "<private_tags>" not in prompt
    assert "annotate_media" in prompt


def test_build_system_prompt_threads_personality_overlay(tmp_path: Path) -> None:
    prompt = build_system_prompt(tmp_path, personality_overlay="Adopt a CFO voice.")
    assert "Adopt a CFO voice." in prompt
