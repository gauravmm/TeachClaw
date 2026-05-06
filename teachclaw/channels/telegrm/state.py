"""Per-user state, reply records, outbound segment types, reaction constants.

Pure data — no Telegram API calls — so this file is safe to import from
anywhere in the package without cycles.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from teachclaw.bus import ToolCallTrace
from teachclaw.rendering import mermaid as mermaid_renderer

# ---- Reaction emojis -------------------------------------------------------
# Edit here to change which reactions trigger which view. Telegram's reaction
# API delivers emojis without the FE0F variant selector (e.g. "❤", not "❤️");
# normalize_emoji() strips it on both sides so the constants here work
# whether or not you typed the variant selector.

SOURCES_REACTION = "❤"
TRACE_REACTION = "🔥"

_VARIATION_SELECTOR_16 = "️"


def normalize_emoji(emoji: str) -> str:
    return emoji.replace(_VARIATION_SELECTOR_16, "")


SOURCES_NORM = normalize_emoji(SOURCES_REACTION)
TRACE_NORM = normalize_emoji(TRACE_REACTION)


# ---- Reply record ----------------------------------------------------------


@dataclass
class ReplyRecord:
    """Raw bits of an outbound assistant turn, indexed under every Telegram
    message_id we sent for it. Citations and kb_records are re-derived on
    demand in the reaction handlers (see reactions.reaction_sources).

    This intentionally stores the *unstripped* content — i.e. what the LLM
    emitted, including any ``<citation>`` markers — so SOURCES_REACTION can
    re-parse it without any TTL/tombstone state to manage. Multiple
    message_ids point at the same record when a reply spans several segments
    (text + diagrams, or media + caption-as-reply), so a reaction on any one
    segment finds the same sources.
    """

    content: str
    tool_calls: list[ToolCallTrace]


# ---- Outbound segments -----------------------------------------------------
# A reply is planned as an ordered list of OutboundSegments and dispatched by
# one loop. Adding a new content type (audio, system banner, …) means adding
# a dataclass + one branch in outbound.dispatch — no other changes.


@dataclass
class TextSegment:
    body: str  # markdown; converted to Telegram HTML at send time


@dataclass
class DiagramSegment:
    rendered: mermaid_renderer.RenderedDiagram


@dataclass
class MediaSegment:
    path: Path
    mime: str
    caption: str | None  # markdown; converted at send time


OutboundSegment = TextSegment | DiagramSegment | MediaSegment


# ---- Per-user state --------------------------------------------------------


@dataclass
class UserState:
    chat_id: int
    cite: bool = True
    in_flight: bool = False
    seen_first_citation: bool = False
    last_user_message_id: int | None = None
    replies: dict[int, ReplyRecord] = field(default_factory=dict)
    # Per-sender rate-limit windows. The key is the Telegram user id (as
    # str) for the sender; in DMs there's only ever one key, in groups one
    # per member. Keeping the map flat here works for both shapes.
    rate_windows: dict[str, deque[float]] = field(default_factory=dict)
    rate_blocked_warned: set[str] = field(default_factory=set)
    # First-wins gate for reaction handlers in groups: every (message_id,
    # normalized_emoji) we've already responded to. Reactions in DMs can
    # also use this so repeat ❤ taps don't re-render the citation block.
    served_reactions: set[tuple[int, str]] = field(default_factory=set)
