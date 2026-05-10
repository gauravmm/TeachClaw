"""Session management for conversation history."""

import json
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from loguru import logger
from pathvalidate import sanitize_filename
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from teachclaw.bus import MediaMetadata, MessageAddress, ToolResult
from teachclaw.utils import TimestampSerializer, _parse_timestamp, ensure_aware, now_aware

MAX_SESSIONS = 50
_MAX_REASONING_CHARS = 500


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


def _truncate_image_block(block: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Shorten an image_url data URL for display profiles. No-op on other blocks."""
    if block.get("type") != "image_url":
        return block
    url = (block.get("image_url") or {}).get("url", "")
    truncated = url[:max_chars] + "…" if len(url) > max_chars else url
    return {"type": "image_url", "image_url": {"url": truncated}}


class _EventBase(BaseModel):
    """Pydantic base for conversation events.

    Each subclass declares a ``kind: Literal[...]`` discriminator; the
    discriminated union :data:`ConversationEvent` is parsed/dumped via the
    :data:`_event_adapter` ``TypeAdapter`` so the on-disk JSONL is
    round-trip type-safe.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    timestamp: TimestampSerializer = Field(default_factory=now_aware)

    def to_record(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_llm_message(self, **_: Any) -> dict[str, Any]:
        raise NotImplementedError


class UserEvent(_EventBase):
    kind: Literal["user"] = "user"
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    media: list[str] = Field(default_factory=list)
    media_metadata: list[MediaMetadata] = Field(default_factory=list)
    sender_id: str | None = None
    sender_label: str | None = None

    def model_post_init(self, _: Any) -> None:
        if self.sender_label is None:
            self.sender_label = _sender_label(self.metadata)

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


class AssistantEvent(_EventBase):
    kind: Literal["assistant"] = "assistant"
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[dict[str, Any]] | None = None
    reasoning_content: str | None = None

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


class ToolEvent(_EventBase):
    kind: Literal["tool"] = "tool"
    content: ToolResult = ""
    tool_call_id: str = ""
    tool_name: str = ""

    def to_llm_message(
        self, *, max_inline_image_url_chars: int | None = None, **_: Any
    ) -> dict[str, Any]:
        content: ToolResult = self.content
        if max_inline_image_url_chars is not None and isinstance(content, list):
            content = [
                _truncate_image_block(block, max_inline_image_url_chars) for block in content
            ]
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "name": self.tool_name,
            "content": content,
        }


class SystemEvent(_EventBase):
    kind: Literal["system"] = "system"
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Ephemeral events stay in session.jsonl for replay fidelity but the
    # renderer hides them once a UserEvent appears at a later index. See
    # spec/TOOL_REMINDERS.md.
    ephemeral: bool = False

    def to_record(self) -> dict[str, Any]:
        record = super().to_record()
        if not self.ephemeral:
            record.pop("ephemeral", None)
        return record

    def to_llm_message(self, **_: Any) -> dict[str, Any]:
        return {"role": "user", "content": f"<system_event>{self.content}</system_event>"}


class SummaryEvent(_EventBase):
    kind: Literal["summary"] = "summary"
    content: str = ""

    def to_llm_message(self, **_: Any) -> dict[str, Any]:
        return {"role": "user", "content": self.content}


class ClearEvent(_EventBase):
    """Persistence-only marker for /clear and /forgetme.

    Written to the JSONL log so the file retains the prior conversation
    instead of being truncated. On load, events before the most recent
    ClearEvent are dropped from the rendered history.
    """

    kind: Literal["clear"] = "clear"
    action: Literal["reset", "forget"] = "reset"

    def to_llm_message(self, **_: Any) -> dict[str, Any]:
        raise NotImplementedError("ClearEvent is a persistence marker; not rendered to LLM")


ConversationEvent = Annotated[
    UserEvent | AssistantEvent | ToolEvent | SystemEvent | SummaryEvent,
    Field(discriminator="kind"),
]
_event_adapter: TypeAdapter[ConversationEvent] = TypeAdapter(ConversationEvent)


def event_from_record(record: dict[str, Any]) -> ConversationEvent:
    return _event_adapter.validate_python(record)


@dataclass
class Session:
    """
    A conversation session.

    Stores typed conversation events in JSONL format for persistence.
    When a log path is attached (see ``attach_log``), each ``append`` and
    ``clear`` writes a single line incrementally so the file stays current
    between shutdowns.
    """

    addr: MessageAddress
    events: list[ConversationEvent] = field(default_factory=list)
    created_at: datetime = field(default_factory=now_aware)
    updated_at: datetime = field(default_factory=now_aware)
    metadata: dict[str, Any] = field(default_factory=dict)
    _log_path: Path | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.created_at = ensure_aware(self.created_at)
        self.updated_at = ensure_aware(self.updated_at)

    def attach_log(self, path: Path, *, write_header: bool) -> None:
        """Bind this session to a JSONL file for incremental writes.

        When ``write_header`` is True (new session), the file is created
        with a metadata header line. When False (loaded session), the
        existing file is reused and subsequent events append to it.
        """
        self._log_path = path
        if write_header:
            self.save(path)

    def _append_log_line(self, record: dict[str, Any]) -> None:
        if self._log_path is None:
            return
        with open(self._log_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    @property
    def has_summary(self) -> bool:
        """True iff the first event is a SummaryEvent (i.e. compaction has run)."""
        return bool(self.events) and isinstance(self.events[0], SummaryEvent)

    def append(self, event: ConversationEvent) -> None:
        self.events.append(event)
        self.updated_at = now_aware()
        self._append_log_line(event.to_record())

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
        if self._log_path is not None:
            self.save(self._log_path)

    @staticmethod
    def _render_event_message(
        event: ConversationEvent,
        *,
        options: RenderOptions,
    ) -> dict[str, object]:
        return event.to_llm_message(
            include_reasoning=options.include_reasoning,
            max_inline_image_url_chars=options.max_inline_image_url_chars,
        )

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

    def render_history(
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
            if isinstance(event, SystemEvent) and event.ephemeral and i <= last_user_idx:
                continue
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
            *self.render_history(list(self.events), options=options),
        ]

    def clear(self, action: Literal["reset", "forget"] = "reset") -> None:
        """Clear all events from the in-memory history.

        Writes a ClearEvent marker to the log file so the prior conversation
        is retained on disk and can be sliced off at load time.
        """
        marker = ClearEvent(action=action)
        self.events = []
        self.updated_at = now_aware()
        self._append_log_line(marker.to_record())

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
        """Load a session from a JSONL file. Returns None if file missing or invalid.

        ClearEvent markers truncate the rendered history: events written
        before the most recent ClearEvent are dropped from the returned
        ``events`` list (they remain on disk as a record). ``updated_at``
        is taken from the most recent record's timestamp when present, so
        appends made after the metadata header was written are visible to
        archive eviction.
        """
        if not path.exists():
            return None

        try:
            events: list[ConversationEvent] = []
            metadata = {}
            created_at = None
            updated_at = None
            addr: MessageAddress | None = None
            slice_from: int = 0
            last_record_ts: datetime | None = None

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
                        continue

                    if ts_str := data.get("timestamp"):
                        with suppress(ValueError, TypeError):
                            last_record_ts = _parse_timestamp(ts_str)

                    if data.get("kind") == "clear":
                        slice_from = len(events)
                    else:
                        events.append(event_from_record(data))

            if addr is None:
                logger.warning(f"No address in session file {path}, skipping")
                return None

            events = events[slice_from:]

            return cls(
                addr=addr,
                events=events,
                created_at=created_at or now_aware(),
                updated_at=last_record_ts or updated_at or now_aware(),
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
        for session in self._cache.values():
            session.attach_log(self._get_session_path(session.addr), write_header=False)
        return self

    async def __aexit__(self, *_: Any) -> None:
        # Sessions persist incrementally via attach_log + append/clear, so
        # there's nothing to flush on shutdown. Keeping this hook so callers
        # can still use SessionManager as an async context manager.
        return None

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
            session = Session(addr=key)
            self._cache[key] = session
            if len(self._cache) > MAX_SESSIONS:
                oldest = min(
                    (s for s in self._cache.values() if s.addr != key),
                    key=lambda s: s.updated_at,
                )
                self._archive(oldest)
                del self._cache[oldest.addr]
            session.attach_log(self._get_session_path(key), write_header=True)
        return self._cache[key]

    def clear(self, key: MessageAddress) -> None:
        if s := self._cache.pop(key, None):
            self._archive(s)
