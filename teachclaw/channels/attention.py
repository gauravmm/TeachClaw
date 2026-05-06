"""Inbound attention policy and summon filtering for channels."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from teachclaw.bus import InboundMessage, MediaMetadata, MessageAddress
from teachclaw.utils import ensure_aware, now_aware

SummonSource = Literal["mention", "reply"]


class AttentionPolicy(StrEnum):
    ALWAYS = "always"
    SUMMON_GROUP = "summon_group"


def _normalize_timestamp(ts: datetime | None) -> datetime:
    if ts is None:
        return now_aware()
    return ensure_aware(ts)


@dataclass
class _PendingMessage:
    sender_id: str
    chat_id: str
    content: str
    timestamp: datetime
    media: list[str]
    media_metadata: list[MediaMetadata]
    metadata: dict[str, Any]
    summon: SummonSource | None


@dataclass
class _ChatAttentionState:
    attention_active: bool = False
    last_seen: datetime | None = None
    history: deque[_PendingMessage] = field(default_factory=deque)


class InboundAttentionFilter:
    """Stateful filter for inbound channel attention policy."""

    def __init__(
        self,
        *,
        channel: str,
        policy: AttentionPolicy,
        lookback: timedelta,
        gap: timedelta,
    ):
        self._channel = channel
        self._policy = policy
        self._lookback = lookback
        self._gap = gap
        self._group_state: dict[str, _ChatAttentionState] = {}

    def apply(
        self,
        *,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None,
        media_metadata: list[MediaMetadata] | None,
        metadata: dict[str, Any],
        timestamp: datetime | None,
    ) -> list[InboundMessage]:
        ts = _normalize_timestamp(timestamp)
        clean_metadata = dict(metadata)
        raw_summon = clean_metadata.get("summon")
        summon = raw_summon if raw_summon in ("mention", "reply") else None
        pending = _PendingMessage(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            timestamp=ts,
            media=list(media or []),
            media_metadata=list(media_metadata or []),
            metadata=clean_metadata,
            summon=summon,
        )

        if self._policy == AttentionPolicy.ALWAYS:
            return [self._to_inbound(pending)]

        is_group = bool(clean_metadata.get("is_group", False))
        if not is_group:
            return [self._to_inbound(pending)]

        state = self._group_state.setdefault(chat_id, _ChatAttentionState())
        self._record_group_history(state, pending)
        self._expire_attention_if_needed(state, ts)

        to_forward: list[_PendingMessage] = []
        if state.attention_active:
            to_forward = [pending]
        elif summon:
            to_forward = self._replay_contiguous_history(state, ts)
            state.attention_active = True

        state.last_seen = ts
        return [self._to_inbound(item) for item in to_forward]

    def _record_group_history(self, state: _ChatAttentionState, pending: _PendingMessage) -> None:
        state.history.append(pending)
        cutoff = pending.timestamp - self._lookback
        while state.history and state.history[0].timestamp < cutoff:
            state.history.popleft()

    def _expire_attention_if_needed(self, state: _ChatAttentionState, ts: datetime) -> None:
        if not state.attention_active or state.last_seen is None:
            return
        if ts - state.last_seen > self._gap:
            state.attention_active = False

    def _replay_contiguous_history(
        self, state: _ChatAttentionState, current_ts: datetime
    ) -> list[_PendingMessage]:
        if not state.history:
            return []
        items = list(state.history)
        selected = [items[-1]]
        for idx in range(len(items) - 1, 0, -1):
            newer = items[idx]
            older = items[idx - 1]
            if current_ts - older.timestamp > self._lookback:
                break
            if newer.timestamp - older.timestamp > self._gap:
                break
            selected.append(older)
        selected.reverse()
        return selected

    def _to_inbound(self, pending: _PendingMessage) -> InboundMessage:
        metadata = dict(pending.metadata)
        metadata["summon"] = pending.summon
        return InboundMessage(
            address=MessageAddress(channel=self._channel, chat_id=pending.chat_id),
            sender_id=pending.sender_id,
            content=pending.content,
            timestamp=pending.timestamp,
            media=list(pending.media),
            media_metadata=list(pending.media_metadata),
            metadata=metadata,
        )
