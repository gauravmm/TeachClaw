"""LLM-prompt assembly for one address turn.

The agent loop only ever needs one thing from this module: a
:class:`PromptBuild` for the current session — the rendered message list
plus the index up to which the cacheable prefix is supposed to be
byte-stable across turns. Everything else (system-prompt rendering,
synthetic context injection, media-block prepending, render options for
elision) is private detail and lives here so :class:`AgentLoop` stays
focused on orchestration.
"""

from __future__ import annotations

import platform
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, PackageLoader

from teachclaw import personalities
from teachclaw import storage as storage_layout
from teachclaw.agent.tools.base import Tool
from teachclaw.agent.tools.registry import ToolRegistry
from teachclaw.bus import MessageAddress
from teachclaw.config import AgentConfig
from teachclaw.media import MediaRepository
from teachclaw.session import RenderOptions, Session
from teachclaw.utils import now_aware

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
            loader=PackageLoader("teachclaw.agent", "templates"),
            keep_trailing_newline=True,
        )
        env.filters["xml_text"] = _xml_text
        env.filters["xml_attr"] = _xml_attr
        _jinja_env = env
    return _jinja_env


def build_system_prompt(
    workspace: Path,
    *,
    tools: Iterable[Tool] | None = None,
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


@dataclass(frozen=True)
class PromptBuild:
    """One turn's worth of LLM input.

    ``stable_prefix_end`` is the exclusive index up to which the prefix
    should be cache-stable across turns: everything before the synthetic
    ``<current_time>/<storage_listing>/<persona>`` injection (when one was
    added) or before the latest user message (when nothing was injected).
    The :mod:`teachclaw.agent.cache_monitor` watchdog reads this index to
    fingerprint the cacheable prefix.
    """

    messages: list[dict[str, object]]
    stable_prefix_end: int


def _last_user_message_index(messages: list[dict[str, object]]) -> int | None:
    return next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"),
        None,
    )


def _prepend_media_to_last_user(
    messages: list[dict[str, object]],
    media_blocks: list[dict[str, object]] | None,
) -> list[dict[str, object]]:
    """Prepend ``media_blocks`` to the latest user message's content.

    Promotes plain-text content into a content-block list when needed.
    Returns a new list — the caller's input is not mutated.
    """
    if not media_blocks:
        return list(messages)
    last_user_idx = _last_user_message_index(messages)
    if last_user_idx is None:
        return list(messages)
    out = list(messages)
    user_msg = dict(out[last_user_idx])
    existing = user_msg.get("content", "")
    if isinstance(existing, list):
        user_msg["content"] = [*media_blocks, *existing]
    else:
        user_msg["content"] = [*media_blocks, {"type": "text", "text": existing}]
    out[last_user_idx] = user_msg
    return out


def _insert_synthetic_context(
    messages: list[dict[str, object]],
    *,
    listing: str | None,
    current_time: str | None,
    persona_overlay: str | None,
) -> tuple[list[dict[str, object]], int]:
    """Insert a synthetic ``<current_time>/<storage_listing>/<persona>`` user
    message right before the latest user turn.

    Returns ``(messages, stable_prefix_end)``. Keeping the turn-local
    context in a separate message just before the user turn — rather than
    appending to the system prompt — is what lets the cacheable
    system-prompt prefix stay byte-identical across turns.
    """
    last_user_idx = _last_user_message_index(messages)
    persona_text = (persona_overlay or "").strip() or None
    has_synthetic = bool(listing or current_time or persona_text)
    if last_user_idx is None:
        return list(messages), len(messages)
    if not has_synthetic:
        return list(messages), last_user_idx

    parts: list[str] = []
    if current_time:
        parts.append(f"<current_time>{current_time}</current_time>")
    if listing:
        parts.append(f"<storage_listing>\n{listing}\n</storage_listing>")
    if persona_text:
        parts.append(f"<persona>\n{persona_text}\n</persona>")
    ctx_msg: dict[str, object] = {"role": "user", "content": "\n".join(parts)}
    out = list(messages)
    out.insert(last_user_idx, ctx_msg)
    return out, last_user_idx


class PromptBuilder:
    """Assembles the per-turn prompt from session + workspace state.

    Held by :class:`AgentLoop` for the lifetime of the process. ``build``
    is called every turn; ``render_options`` is also reused by the
    summarization path for symmetry with the main render.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        tools: ToolRegistry,
        media_repo: MediaRepository,
        agent_config: AgentConfig,
    ) -> None:
        self.workspace = workspace
        self.tools = tools
        self.media_repo = media_repo
        self.agent_config = agent_config

    def render_options(self) -> RenderOptions:
        compaction = self.agent_config.compaction
        elide = tuple(compaction.elide_tool_names) if compaction.elide_chunks_after_turn else ()
        return RenderOptions(elide_tool_names=elide)

    def build(
        self,
        session: Session,
        addr: MessageAddress,
        pending_media: list[str] | None,
    ) -> PromptBuild:
        storage_root = storage_layout.storage_root(self.workspace, addr)
        persona = personalities.read_personality(self.workspace, addr)
        system_prompt = build_system_prompt(
            self.workspace,
            tools=self.tools.values(),
            channel=addr.channel,
            chat_id=addr.chat_id,
            session_label=session.describe_current_session(),
            chunk_elision_active=self.agent_config.compaction.elide_chunks_after_turn,
            profile_text=storage_layout.read_profile(self.workspace, addr),
            storage_path=str(storage_root.expanduser().resolve()),
            model=self.agent_config.model,
            context_window=self.agent_config.context_window,
        )
        messages = session.render_llm_messages(system_prompt, self.render_options())
        listing = storage_layout.listing_for_user(self.workspace, addr)
        current_time = now_aware().strftime("%Y-%m-%d %H:%M (%A) %z")
        media_blocks = (
            self.media_repo.build_media_blocks(addr, pending_media)
            if (pending_media and self.media_repo)
            else None
        )
        out = _prepend_media_to_last_user(messages, media_blocks)
        out, stable_prefix_end = _insert_synthetic_context(
            out,
            listing=listing,
            current_time=current_time,
            persona_overlay=persona.overlay,
        )
        return PromptBuild(messages=out, stable_prefix_end=stable_prefix_end)
