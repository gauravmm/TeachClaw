"""Context builder for assembling agent prompts."""

import platform
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, PackageLoader

from benchclaw.agent.skills import SkillsLoader
from benchclaw.agent.tools.registry import ToolRegistry
from benchclaw.utils import now_aware

if TYPE_CHECKING:
    from benchclaw.agent.tools.base import Tool

BOOTSTRAP_FILES = ["AGENTS.md"]


def _xml_text(value: Any) -> str:
    """Escape text for XML-like prompt blocks without mangling ordinary quotes."""
    text = str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_attr(value: Any) -> str:
    """Escape XML attribute values."""
    return _xml_text(value).replace('"', "&quot;").replace("'", "&apos;")


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.skills = SkillsLoader(workspace)
        self._jinja = Environment(
            loader=PackageLoader("benchclaw.agent.context", "templates"),
            keep_trailing_newline=True,
        )
        self._jinja.filters["xml_text"] = _xml_text
        self._jinja.filters["xml_attr"] = _xml_attr

    def build_system_prompt(
        self,
        tools: Iterable["Tool"] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        session_label: str | None = None,
        chunk_elision_active: bool = False,
        profile_text: str | None = None,
        storage_path: str | None = None,
        personality_overlay: str | None = None,
    ) -> str:
        system = platform.system()
        bootstrap_files = [
            {"name": f, "content": (self.workspace / f).read_text(encoding="utf-8")}
            for f in BOOTSTRAP_FILES
            if (self.workspace / f).exists()
        ]
        all_skills = self.skills.get_all_skills()
        return self._jinja.get_template("system_prompt.j2").render(
            now=now_aware().strftime("%Y-%m-%d %H:%M (%A) %z"),
            runtime=f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}",
            workspace_path=str(self.workspace.expanduser().resolve()),
            bootstrap_files=bootstrap_files,
            skills=all_skills,
            tools=[
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
                for t in (tools or [])
            ],
            channel=channel,
            chat_id=chat_id,
            session_label=session_label,
            chunk_elision_active=chunk_elision_active,
            profile_text=(profile_text or "").strip() or None,
            storage_path=storage_path,
            personality_overlay=(personality_overlay or "").strip() or None,
        )

    def build_context(
        self,
        history: list[dict[str, Any]],
        tools: ToolRegistry | None,
        channel: str | None = None,
        chat_id: str | None = None,
        session_label: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build the base context: system prompt followed by history. No user message appended."""
        return [
            {
                "role": "system",
                "content": self.build_system_prompt(
                    tools.values() if tools else None, channel, chat_id, session_label
                ),
            },
            *history,
        ]

    def tool_result(
        self, tool_call_id: str, tool_name: str, result: "str | list[dict[str, Any]]"
    ) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result}

    def assistant_message(
        self,
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": content or ""}

        if tool_calls:
            msg["tool_calls"] = tool_calls

        # Thinking models reject history without this
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content

        return msg
