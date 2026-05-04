"""Session management for conversation history."""

import json
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from loguru import logger
from pathvalidate import sanitize_filename

from benchclaw.bus import MediaMetadata, MessageAddress, ToolResult
from benchclaw.utils import _parse_timestamp, ensure_aware, now_aware

MAX_SESSIONS = 50
_MAX_REASONING_CHARS = 500

EventKind = Literal["user", "assistant", "tool", "system", "summary"]


@dataclass(frozen=True)
class RenderOptions:
    include_reasoning: bool = True
    max_inline_image_url_chars: int | None = None
    elide_tool_names: tuple[str, ...] = ()  # tool names whose past-turn results are elided


def _sender_label(metadata: dict[str, Any]) -> str | None:
    """Return a channel-provided display label for a sender, if present."""
    label = metadata.get("sender_label")
    return str(label) if label else None


def _format_prefix_time(sent_at: datetime | None) -> str | None:
    """Convert timestamp to HH:MM for compact user prefixes."""
    with suppress(ValueError, TypeError):
        if sent_at:
            return ensure_aware(sent_at).strftime("%H:%M")
    return None


def _user_prefix(sender: str | None, sent_at: datetime | None) -> str | None:
    """Build a user message prefix containing sender and/or timestamp."""
    short_time = _format_prefix_time(sent_at)
    if sender and short_time:
        return f"{sender} @{short_time}"
    if sender:
        return sender
    if short_time:
        return f"@{short_time}"
    return None


def _channel_display_name(channel: str) -> str:
    """Return a readable channel label for prompts."""
    known = {
        "telegram": "Telegram",
        "email": "Email",
    }
    if channel in known:
        return known[channel]
    return channel.replace("_", " ").title()


def _render_user_content(
    content: str,
    *,
    media: list[str] | None = None,
    sender: str | None = None,
    sent_at: datetime | None = None,
) -> str:
    """Render one user event into provider-visible text."""
    if prefix := _user_prefix(sender, sent_at):
        content = f"[{prefix}]: {content}"
    if media:
        stubs = "\n".join(
            (
                f"[media: {path}] "
                "(call annotate_media with this exact path before your final response if it has not been annotated yet)"
            )
            for path in media
        )
        content = f"{content}\n{stubs}" if content else stubs
    return content


def _truncate_inline_images(content: object, max_chars: int | None) -> object:
    if max_chars is None:
        return content
    if isinstance(content, list):
        return [_truncate_inline_images(item, max_chars) for item in content]
    if isinstance(content, dict):
        if content.get("type") == "image_url":
            url = (content.get("image_url") or {}).get("url", "")
            truncated = url[:max_chars] + "…" if len(url) > max_chars else url
            return {"type": "image_url", "image_url": {"url": truncated}}
        return {key: _truncate_inline_images(value, max_chars) for key, value in content.items()}
    return content


@dataclass
class BaseEvent:
    timestamp: datetime = field(default_factory=now_aware)

    def __post_init__(self) -> None:
        self.timestamp = ensure_aware(self.timestamp)

    @property
    def kind(self) -> EventKind:
        raise NotImplementedError

    def _record_base(self) -> dict[str, Any]:
        return {
            "_type": "event",
            "kind": self.kind,
            "timestamp": self.timestamp.isoformat(timespec="seconds"),
        }

    def to_record(self) -> dict[str, Any]:
        raise NotImplementedError

    def to_llm_message(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class UserEvent(BaseEvent):
    KIND: ClassVar[Literal["user"]] = "user"

    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    media: list[str] = field(default_factory=list)
    media_metadata: list[MediaMetadata] = field(default_factory=list)
    sender_id: str | None = None
    sender_label: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.sender_label is None:
            self.sender_label = _sender_label(self.metadata)

    @property
    def kind(self) -> Literal["user"]:
        return self.KIND

    def to_record(self) -> dict[str, Any]:
        record = {**self._record_base(), "content": self.content}
        optional_fields = {
            "metadata": self.metadata or None,
            "media": self.media or None,
            "media_metadata": self.media_metadata or None,
            "sender_id": self.sender_id,
            "sender_label": self.sender_label,
        }
        for key, value in optional_fields.items():
            if value is not None:
                record[key] = value
        return record

    def to_llm_message(self, **_: Any) -> dict[str, Any]:
        return {
            "role": "user",
            "content": _render_user_content(
                self.content,
                media=self.media,
                sender=self.sender_label,
                sent_at=self.timestamp,
            ),
        }


@dataclass
class AssistantEvent(BaseEvent):
    KIND: ClassVar[Literal["assistant"]] = "assistant"

    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] | None = None
    reasoning_content: str | None = None

    @property
    def kind(self) -> Literal["assistant"]:
        return self.KIND

    def to_record(self) -> dict[str, Any]:
        record = {**self._record_base(), "content": self.content}
        optional_fields = {
            "metadata": self.metadata or None,
            "tool_calls": self.tool_calls,
            "reasoning_content": self.reasoning_content,
        }
        for key, value in optional_fields.items():
            if value is not None:
                record[key] = value
        return record

    def to_llm_message(self, *, include_reasoning: bool = True, **_: Any) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        if include_reasoning and self.reasoning_content:
            reasoning_content = self.reasoning_content
            if len(reasoning_content) > _MAX_REASONING_CHARS:
                reasoning_content = reasoning_content[:_MAX_REASONING_CHARS] + " [truncated]"
            message["reasoning_content"] = reasoning_content
        return message


@dataclass
class ToolEvent(BaseEvent):
    KIND: ClassVar[Literal["tool"]] = "tool"

    content: ToolResult = ""
    tool_call_id: str = ""
    tool_name: str = ""

    @property
    def kind(self) -> Literal["tool"]:
        return self.KIND

    def to_record(self) -> dict[str, Any]:
        return {
            **self._record_base(),
            "content": self.content,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
        }

    def to_llm_message(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "name": self.tool_name,
            "content": self.content,
        }


@dataclass
class SystemEvent(BaseEvent):
    KIND: ClassVar[Literal["system"]] = "system"

    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> Literal["system"]:
        return self.KIND

    def to_record(self) -> dict[str, Any]:
        record = {**self._record_base(), "content": self.content}
        if self.metadata:
            record["metadata"] = self.metadata
        return record

    def to_llm_message(self, **kwargs: Any) -> dict[str, Any]:
        return {"role": "user", "content": f"<system_event>{self.content}</system_event>"}


@dataclass
class SummaryEvent(BaseEvent):
    KIND: ClassVar[Literal["summary"]] = "summary"

    content: str = ""

    @property
    def kind(self) -> Literal["summary"]:
        return self.KIND

    def to_record(self) -> dict[str, Any]:
        return {**self._record_base(), "content": self.content}

    def to_llm_message(self, **kwargs: Any) -> dict[str, Any]:
        return {"role": "user", "content": self.content}


ConversationEvent = UserEvent | AssistantEvent | ToolEvent | SystemEvent | SummaryEvent


def event_from_record(record: dict[str, Any]) -> ConversationEvent:
    kind = record["kind"]
    timestamp = _parse_timestamp(record["timestamp"])
    if kind == "user":
        return UserEvent(
            timestamp=timestamp,
            content=str(record.get("content", "")),
            metadata=dict(record.get("metadata") or {}),
            media=list(record.get("media") or []),
            media_metadata=list(record.get("media_metadata") or []),
            sender_id=record.get("sender_id"),
            sender_label=record.get("sender_label"),
        )
    if kind == "assistant":
        return AssistantEvent(
            timestamp=timestamp,
            content=str(record.get("content", "")),
            metadata=dict(record.get("metadata") or {}),
            tool_calls=record.get("tool_calls"),
            reasoning_content=record.get("reasoning_content"),
        )
    if kind == "tool":
        return ToolEvent(
            timestamp=timestamp,
            content=record.get("content", ""),
            tool_call_id=str(record["tool_call_id"]),
            tool_name=str(record["tool_name"]),
        )
    if kind == "system":
        return SystemEvent(
            timestamp=timestamp,
            content=str(record.get("content", "")),
            metadata=dict(record.get("metadata") or {}),
        )
    if kind == "summary":
        return SummaryEvent(timestamp=timestamp, content=str(record.get("content", "")))
    raise ValueError(f"Unsupported event kind: {kind}")


@dataclass
class Session:
    """
    A conversation session.

    Stores typed conversation events in JSONL format for persistence.
    """

    addr: MessageAddress
    events: list[ConversationEvent] = field(default_factory=list)
    created_at: datetime = field(default_factory=now_aware)
    updated_at: datetime = field(default_factory=now_aware)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.created_at = ensure_aware(self.created_at)
        self.updated_at = ensure_aware(self.updated_at)

    @property
    def has_summary(self) -> bool:
        """True iff the first event is a SummaryEvent (i.e. compaction has run)."""
        return bool(self.events) and isinstance(self.events[0], SummaryEvent)

    def append(self, event: ConversationEvent) -> None:
        self.events.append(event)
        self.updated_at = now_aware()

    def compact_with_summary(self, summary: str, *, keep_from_index: int = -1) -> None:
        """Replace conversation history with a SummaryEvent + optional verbatim tail.

        If keep_from_index >= 0, ``events[keep_from_index:]`` is preserved after
        the summary; this is normally the index of the latest UserEvent so the
        current question stays verbatim and attached media still resolves.
        """
        kept: list[ConversationEvent] = (
            list(self.events[keep_from_index:]) if 0 <= keep_from_index < len(self.events) else []
        )
        self.events = [SummaryEvent(content=summary), *kept]
        self.updated_at = now_aware()

    @staticmethod
    def _render_event_message(
        event: ConversationEvent,
        *,
        options: RenderOptions,
    ) -> dict[str, object]:
        if isinstance(event, AssistantEvent):
            message = event.to_llm_message(include_reasoning=options.include_reasoning)
        else:
            message = event.to_llm_message()
        message["content"] = _truncate_inline_images(
            message.get("content", ""),
            options.max_inline_image_url_chars,
        )
        return message

    @staticmethod
    def _find_last_reasoning_index(history: list[ConversationEvent]) -> int | None:
        for i, event in reversed(list(enumerate(history))):
            if isinstance(event, AssistantEvent) and event.reasoning_content:
                return i
        return None

    @staticmethod
    def _last_user_index(history: list[ConversationEvent]) -> int:
        for i in range(len(history) - 1, -1, -1):
            if isinstance(history[i], UserEvent):
                return i
        return -1

    @staticmethod
    def _maybe_elide_tool_event(
        event: ConversationEvent,
        index: int,
        last_user_index: int,
        elide_tool_names: tuple[str, ...],
    ) -> ConversationEvent:
        """Return an elided copy of an old retrieval ToolEvent, or the event unchanged.

        Elision applies only to ToolEvents whose tool_name is listed in
        elide_tool_names AND which sit before the most recent user message in
        the rendered history. The current turn's retrieval result is left
        verbatim so the model can reason over it; older results are replaced
        with a short stub to save context. The underlying event in the session
        is not mutated — only this rendering view is changed.
        """
        if not elide_tool_names or not isinstance(event, ToolEvent):
            return event
        if index >= last_user_index:
            return event
        if event.tool_name not in elide_tool_names:
            return event
        return ToolEvent(
            timestamp=event.timestamp,
            content=f"[{event.tool_name} result elided to save context; call again to re-fetch]",
            tool_call_id=event.tool_call_id,
            tool_name=event.tool_name,
        )

    def _render_history(
        self,
        history: list[ConversationEvent],
        *,
        options: RenderOptions | None = None,
    ) -> list[dict[str, object]]:
        options = options or RenderOptions()
        last_reasoning_idx = self._find_last_reasoning_index(history)
        last_user_idx = self._last_user_index(history)
        messages: list[dict[str, object]] = []
        for i, event in enumerate(history):
            rendered_event = self._maybe_elide_tool_event(
                event, i, last_user_idx, options.elide_tool_names
            )
            messages.append(
                self._render_event_message(
                    rendered_event,
                    options=RenderOptions(
                        include_reasoning=options.include_reasoning and i == last_reasoning_idx,
                        max_inline_image_url_chars=options.max_inline_image_url_chars,
                    ),
                )
            )
        return messages

    def render_llm_messages(
        self,
        system_prompt: str,
        options: RenderOptions | None = None,
    ) -> list[dict[str, object]]:
        return [
            {"role": "system", "content": system_prompt},
            *self._render_history(list(self.events), options=options),
        ]

    def clear(self) -> None:
        """Clear all events."""
        self.events = []
        self.updated_at = now_aware()

    def describe_current_session(self) -> str:
        """Return a readable prompt label for the current chat when possible."""
        channel_name = _channel_display_name(self.addr.channel)
        last_user = next(
            (event for event in reversed(self.events) if isinstance(event, UserEvent)),
            None,
        )
        if not last_user:
            return f"{channel_name} chat {self.addr.chat_id}"

        sender = str(last_user.sender_label or "").strip() or None
        is_group = bool(last_user.metadata.get("is_group"))

        if sender and not is_group:
            return f"{sender} on {channel_name}"
        if sender and is_group:
            return f"{channel_name} group chat (recent sender: {sender})"
        return f"{channel_name} chat {self.addr.chat_id}"

    @classmethod
    def load(cls, path: Path) -> "Session | None":
        """Load a session from a JSONL file. Returns None if file missing or invalid."""
        if not path.exists():
            return None

        try:
            events: list[ConversationEvent] = []
            metadata = {}
            created_at = None
            updated_at = None
            addr: MessageAddress | None = None

            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = (
                            _parse_timestamp(data["created_at"]) if data.get("created_at") else None
                        )
                        updated_at = (
                            _parse_timestamp(data["updated_at"]) if data.get("updated_at") else None
                        )
                        if data.get("address"):
                            addr = MessageAddress.from_string(data["address"])
                    else:
                        events.append(event_from_record(data))

            if addr is None:
                logger.warning(f"No address in session file {path}, skipping")
                return None

            return cls(
                addr=addr,
                events=events,
                created_at=created_at or now_aware(),
                updated_at=updated_at or now_aware(),
                metadata=metadata,
            )
        except Exception as e:
            logger.warning(f"Failed to load session from {path}: {e}")
            return None

    def save(self, path: Path) -> None:
        """Save this session to a JSONL file."""
        with open(path, "w") as f:
            metadata_line = {
                "_type": "metadata",
                "address": str(self.addr),
                "created_at": self.created_at.isoformat(timespec="seconds"),
                "updated_at": self.updated_at.isoformat(timespec="seconds"),
                "metadata": self.metadata,
            }
            f.write(json.dumps(metadata_line) + "\n")
            for event in self.events:
                f.write(json.dumps(event.to_record()) + "\n")


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    Use as an async context manager: all sessions are loaded on enter and flushed on exit.
    """

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        self._archive_dir = sessions_dir / ".archive"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[MessageAddress, Session] = {}

    def _get_session_path(self, key: MessageAddress) -> Path:
        return self.sessions_dir / f"{sanitize_filename(str(key).replace(':', ''))}.jsonl"

    async def __aenter__(self) -> "SessionManager":
        sessions: list[Session] = []
        for path in self.sessions_dir.glob("*.jsonl"):
            if (session := Session.load(path)) is not None:
                sessions.append(session)

        if len(sessions) > MAX_SESSIONS:
            sessions.sort(key=lambda s: s.updated_at, reverse=True)
            for old_session in sessions[MAX_SESSIONS:]:
                self._archive(old_session)
            sessions = sessions[:MAX_SESSIONS]

        self._cache = {s.addr: s for s in sessions}
        return self

    async def __aexit__(self, *_: Any) -> None:
        for session in self._cache.values():
            session.save(self._get_session_path(session.addr))

    def _archive(self, s: Session) -> None:
        path = self._get_session_path(s.addr)
        archive_path = (
            self._archive_dir / f"{path.stem}_{now_aware().strftime('%Y%m%dT%H%M%S')}{path.suffix}"
        )

        self._archive_dir.mkdir(parents=True, exist_ok=True)
        s.save(archive_path)
        path.unlink(missing_ok=True)

    def save(self, session: Session) -> None:
        session.save(self._get_session_path(session.addr))

    def get(self, key: MessageAddress) -> Session:
        if key not in self._cache:
            self._cache[key] = Session(addr=key)
            if len(self._cache) > MAX_SESSIONS:
                oldest = min(
                    (s for s in self._cache.values() if s.addr != key),
                    key=lambda s: s.updated_at,
                )
                self._archive(oldest)
                del self._cache[oldest.addr]
        return self._cache[key]

    def clear(self, key: MessageAddress) -> None:
        if s := self._cache.pop(key, None):
            self._archive(s)
