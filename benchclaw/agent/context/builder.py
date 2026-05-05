"""System-prompt assembly. One call site: PromptBuilder.build."""

from __future__ import annotations

import platform
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, PackageLoader

from benchclaw.agent.skills import SkillsLoader

if TYPE_CHECKING:
    from benchclaw.agent.tools.base import Tool


BOOTSTRAP_FILES = ["AGENTS.md"]


def _xml_text(value: Any) -> str:
    text = str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_attr(value: Any) -> str:
    return _xml_text(value).replace('"', "&quot;").replace("'", "&apos;")


_jinja_env: Environment | None = None


def _env() -> Environment:
    global _jinja_env
    if _jinja_env is None:
        env = Environment(
            loader=PackageLoader("benchclaw.agent.context", "templates"),
            keep_trailing_newline=True,
        )
        env.filters["xml_text"] = _xml_text
        env.filters["xml_attr"] = _xml_attr
        _jinja_env = env
    return _jinja_env


def build_system_prompt(
    workspace: Path,
    *,
    tools: Iterable["Tool"] | None = None,
    channel: str | None = None,
    chat_id: str | None = None,
    session_label: str | None = None,
    chunk_elision_active: bool = False,
    profile_text: str | None = None,
    storage_path: str | None = None,
    model: str | None = None,
    context_window: int | None = None,
) -> str:
    bootstrap_files = [
        {"name": f, "content": (workspace / f).read_text(encoding="utf-8")}
        for f in BOOTSTRAP_FILES
        if (workspace / f).exists()
    ]
    skills = SkillsLoader(workspace).get_all_skills()
    system = platform.system()
    return (
        _env()
        .get_template("system_prompt.j2")
        .render(
            runtime=(
                f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, "
                f"Python {platform.python_version()}"
            ),
            workspace_path=str(workspace.expanduser().resolve()),
            bootstrap_files=bootstrap_files,
            skills=skills,
            tools=[
                {"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in (tools or [])
            ],
            channel=channel,
            chat_id=chat_id,
            session_label=session_label,
            chunk_elision_active=chunk_elision_active,
            profile_text=(profile_text or "").strip() or None,
            storage_path=storage_path,
            model=model,
            context_window=context_window,
        )
    )
