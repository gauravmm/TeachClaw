"""Per-conversation citation storage with TTL tombstones and a hard cap.

Channels record outbound replies by whatever id they natively address
messages by (Telegram message_id is ``int``; SMTP would use a Message-ID
``str``). When the user later asks for sources for that reply, the
channel ``lookup``s by the same key.

Tombstone semantics: entries past TTL get their content cleared and
``expired=True`` left in place, so ``lookup`` can distinguish "we never
tracked this message" (returns ``None``) from "we did, but the content
aged out" (returns an entry with ``expired=True``). The hard cap then
evicts oldest-first, which means tombstones go before live entries.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from benchclaw.bus import ToolCallTrace
from benchclaw.citations.parsing import Citation, extract_kb_records


@dataclass
class CitationEntry:
    citations: list[Citation]
    tool_calls: list[ToolCallTrace]
    kb_records: dict[str, dict] = field(default_factory=dict)
    created_at: float = 0.0
    expired: bool = False


class CitationStore[KeyT]:
    """Per-conversation message → CitationEntry map with TTL/tombstone
    eviction. ``KeyT`` is whatever the channel uses (Telegram ``message_id``
    is ``int``; an SMTP channel might use ``Message-ID`` strings)."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 24 * 3600,
        hard_cap: int = 1000,
        kb_prefix: str = "kb__",
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._hard_cap = hard_cap
        self._kb_prefix = kb_prefix
        self._entries: dict[KeyT, CitationEntry] = {}

    def record(
        self,
        key: KeyT,
        *,
        citations: list[Citation],
        tool_calls: list[ToolCallTrace],
    ) -> None:
        """Store the entry, run the TTL tombstone sweep, and enforce the
        hard cap. ``kb_records`` is computed from ``tool_calls`` here so
        callers don't have to thread it through."""
        now = time.time()
        self._entries[key] = CitationEntry(
            citations=citations,
            tool_calls=tool_calls,
            kb_records=extract_kb_records(tool_calls, kb_prefix=self._kb_prefix),
            created_at=now,
        )
        cutoff = now - self._ttl_seconds
        for entry in self._entries.values():
            if not entry.expired and entry.created_at < cutoff:
                entry.citations = []
                entry.tool_calls = []
                entry.kb_records = {}
                entry.expired = True
        excess = len(self._entries) - self._hard_cap
        if excess > 0:
            for k, _ in sorted(self._entries.items(), key=lambda kv: kv[1].created_at)[:excess]:
                self._entries.pop(k, None)

    def lookup(self, key: KeyT) -> CitationEntry | None:
        return self._entries.get(key)

    def clear(self) -> None:
        self._entries.clear()
