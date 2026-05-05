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

from dataclasses import dataclass
from pathlib import Path

from benchclaw import personalities
from benchclaw import storage as storage_layout
from benchclaw.agent.context import build_system_prompt
from benchclaw.agent.tools.registry import ToolRegistry
from benchclaw.bus import MessageAddress
from benchclaw.config import AgentConfig
from benchclaw.media import MediaRepository
from benchclaw.session import RenderOptions, Session
from benchclaw.utils import now_aware


@dataclass(frozen=True)
class PromptBuild:
    """One turn's worth of LLM input.

    ``stable_prefix_end`` is the exclusive index up to which the prefix
    should be cache-stable across turns: everything before the synthetic
    ``<current_time>/<storage_listing>/<persona>`` injection (when one was
    added) or before the latest user message (when nothing was injected).
    The :mod:`benchclaw.agent.cache_monitor` watchdog reads this index to
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
