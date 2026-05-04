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


def test_build_system_prompt_escapes_session_label(tmp_path: Path) -> None:
    prompt = build_system_prompt(
        tmp_path,
        channel="whatsapp",
        chat_id="123&456",
        session_label='Alice "A" & Bob',
    )

    assert 'Session: Alice "A" &amp; Bob' in prompt


def test_build_system_prompt_omits_tools_listing(tmp_path: Path) -> None:
    """Tool definitions come through the chat template's tools=[...] field;
    the system prompt must not also embed a textual listing (otherwise small
    models double-count or drift from the canonical schema)."""
    tool = _DummyTool(
        name="annotate_media",
        description="Save image annotations.",
        parameters={"type": "object", "properties": {}, "required": []},
    )

    prompt = build_system_prompt(tmp_path, tools=[tool])

    assert "<tools>" not in prompt
    assert "annotate_media" not in prompt


def test_build_system_prompt_omits_personality_overlay(tmp_path: Path) -> None:
    """The persona lives in the synthetic tail message in AgentLoop, not
    the system prompt, so persona switches don't bust the cacheable
    system-prompt prefix."""
    prompt = build_system_prompt(tmp_path)
    assert "Persona for this conversation" not in prompt
