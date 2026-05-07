"""System-prompt assembly. One call site: PromptBuilder.build."""

from __future__ import annotations

import platform
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from jinja2 import Environment, PackageLoader

if TYPE_CHECKING:
    from teachclaw.agent.tools.base import Tool


BOOTSTRAP_FILES = ["AGENTS.md"]
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def _load_skills(workspace: Path) -> list[dict[str, str]]:
    """Enumerate ``workspace/skills/<name>/SKILL.md`` and read each frontmatter
    description, in stable directory order.

    Returns one dict per skill with ``name``, ``path`` (relative to workspace),
    and ``description`` (empty if frontmatter is missing or has no
    ``description`` key). Frontmatter parse errors are tolerated — a broken
    SKILL.md still surfaces in the system prompt with its directory name."""
    skills_dir = workspace / "skills"
    if not skills_dir.exists():
        return []
    out: list[dict[str, str]] = []
    for skill_dir in sorted(skills_dir.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_dir.is_dir() or not skill_file.exists():
            continue
        description = ""
        text = skill_file.read_text(encoding="utf-8")
        if (m := _FRONTMATTER_RE.match(text)) is not None:
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except yaml.YAMLError:
                meta = {}
            if isinstance(meta, dict):
                description = str(meta.get("description") or "").strip()
        out.append(
            {
                "name": skill_dir.name,
                "path": str(skill_file.relative_to(workspace)),
                "description": description or skill_dir.name,
            }
        )
    return out


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
            loader=PackageLoader("teachclaw.agent.context", "templates"),
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
    skills = _load_skills(workspace)
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
