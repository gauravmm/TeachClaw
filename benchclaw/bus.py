"""Async message bus for decoupled channel-agent communication."""

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, NotRequired, TypedDict

from benchclaw.utils import now_aware

# Return type for Tool.execute() and ToolResultEvent.result.
ToolResult = str | list[dict[str, Any]]


class MediaMetadata(TypedDict):
    """Structured metadata for one inbound media attachment."""

    path: str | None
    media_type: str
    mime_type: str | None
    size_bytes: int | None
    saved_at: str | None
    source_channel: str
    original_name: NotRequired[str | None]


@dataclass(frozen=True)
class MessageAddress:
    """Identifies a conversation endpoint (channel + chat_id)."""

    channel: str
    chat_id: str

    def __str__(self) -> str:
        return f"{self.channel}:{self.chat_id}"

    @property
    def hash8(self) -> str:
        """8-char hex digest of this address, for use as a short stable directory name."""
        return hashlib.sha256(str(self).encode()).hexdigest()[:8]

    @classmethod
    def from_string(cls, s: str) -> "MessageAddress":
        channel, chat_id = s.split(":", 1)
        return cls(channel=channel, chat_id=chat_id)


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    address: MessageAddress  # Channel + chat_id endpoint
    sender_id: str  # User identifier (the specific person within the group chat)
    content: str  # Message text
    timestamp: datetime = field(default_factory=now_aware)
    media: list[str] = field(default_factory=list)  # Media URLs
    media_metadata: list[MediaMetadata] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""

    address: MessageAddress  # Destination endpoint
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultEvent:
    """A completed background tool call, routed through bus.inbound[addr]."""

    tool_call_id: str
    tool_name: str
    result: ToolResult


@dataclass
class SystemMessageEvent:
    """An internal system prompt injected into the agent's conversation without user involvement."""

    content: str


@dataclass
class ToolCallTrace:
    """One dispatched tool call, optionally with its captured result.

    Channels record these via ``OutboundMessage.metadata['tool_calls']`` so a
    🔥 reaction can replay what the agent actually did for that turn.
    """

    id: str
    name: str
    arguments: dict[str, object]
    result: str | None = None


@dataclass
class SessionControlEvent:
    """Out-of-band channel directive to the agent loop for one address.

    `action="reset"` clears the in-memory session events. `action="forget"`
    additionally deletes the user's storage directory recursively. The
    channel publishes this for /clear and /forgetme respectively.
    """

    action: Literal["reset", "forget"]


@dataclass(frozen=True)
class TypingEvent:
    """Signal from the agent that typing state has changed for an address."""

    address: MessageAddress
    is_typing: bool  # True = start indicator, False = stop


@dataclass(frozen=True)
class AttentionEvent:
    """Attention (awake/asleep) state change for one address."""

    address: MessageAddress
    awake: bool  # True = just became awake, False = just went asleep


# All events that flow through bus.inbound[addr]
AddressEvent = InboundMessage | ToolResultEvent | SystemMessageEvent | SessionControlEvent
# All events that flow through bus.outbound[channel]
OutboundEvent = OutboundMessage | TypingEvent | AttentionEvent


@dataclass
class InboundMessageBatch:
    """A drained batch of inbound events, sorted by type."""

    tool_results: list[ToolResultEvent] = field(default_factory=list)
    system_events: list[SystemMessageEvent] = field(default_factory=list)
    user_messages: list[InboundMessage] = field(default_factory=list)
    control_events: list[SessionControlEvent] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(
            self.tool_results or self.system_events or self.user_messages or self.control_events
        )


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Inbound events (user messages and tool results) are queued per-address;
    the agent subscribes to new-address notifications via subscribe_new_addresses()
    and spawns a handler per address.

    Outbound messages are queued per-channel; any consumer for that channel
    receives the next message regardless of which chat it came from.

    Usage:
        await bus.publish_inbound(addr, msg)              # enqueues one InboundMessage
        await bus.publish_inbound(addr, msg1, msg2, ...)  # enqueues multiple
        await bus.publish_inbound(addr, tool_result)      # enqueues ToolResultEvent
        await bus.publish_inbound(addr, system_event)     # enqueues SystemEvent
        await bus.consume_inbound(address=addr)           # next AddressEvent for that address
        new_addrs = bus.subscribe_new_addresses()         # Queue[MessageAddress] of new addresses

        await bus.publish_outbound(msg)             # enqueues to msg.address.channel queue
        await bus.consume_outbound(channel="x")     # next message for channel x
    """

    def __init__(self):
        self.inbound: dict[MessageAddress, asyncio.Queue[AddressEvent]] = {}
        self._address_subscribers: list[asyncio.Queue[MessageAddress]] = []
        self.outbound: dict[str, asyncio.Queue[OutboundEvent]] = {}
        self._channel_created = asyncio.Condition()

    def subscribe_new_addresses(self) -> asyncio.Queue[MessageAddress]:
        """Return a queue that receives each new MessageAddress as it first appears.

        The caller owns the returned queue; it must be consumed to avoid memory growth.
        All registered subscribers receive every new address.
        """
        q: asyncio.Queue[MessageAddress] = asyncio.Queue()
        self._address_subscribers.append(q)
        return q

    async def publish_inbound(self, addr: MessageAddress, *events: AddressEvent) -> None:
        """Publish one or more events to an address's inbound queue.

        Creates a new per-address queue on first use and notifies all subscribers.
        No lock needed: asyncio is single-threaded so the check-then-set is atomic.
        """
        if addr not in self.inbound:
            self.inbound[addr] = asyncio.Queue()
            for sub in self._address_subscribers:
                sub.put_nowait(addr)
        for event in events:
            await self.inbound[addr].put(event)

    async def consume_inbound(self, *, address: MessageAddress) -> AddressEvent:
        """Consume the next inbound event for the given address (blocks until available)."""
        return await self.inbound[address].get()

    async def consume_inbound_batch(self, *, address: MessageAddress) -> InboundMessageBatch:
        """Block until at least one inbound event is available, then drain the queue.

        Returns an InboundMessageBatch with events sorted into tool_results,
        system_events, and user_messages. Raises TypeError on unknown event types.
        """
        events: list[AddressEvent] = [await self.inbound[address].get()]
        try:
            while True:
                events.append(self.inbound[address].get_nowait())
        except asyncio.QueueEmpty:
            pass

        batch = InboundMessageBatch()
        for event in events:
            match event:
                case ToolResultEvent():
                    batch.tool_results.append(event)
                case SystemMessageEvent():
                    batch.system_events.append(event)
                case InboundMessage():
                    batch.user_messages.append(event)
                case SessionControlEvent():
                    batch.control_events.append(event)
        return batch

    async def publish_outbound(self, msg: OutboundEvent) -> None:
        """Enqueue a response or typing event into the channel's outbound queue, creating it if needed."""
        ch = msg.address.channel
        if ch not in self.outbound:
            # NOTE: The check (ch not in self.outbound) is NOT redundant.
            # There's a race condition where two coroutines fail the check above, then one clobbers
            # the other's queue assignment. The fix is to protect the check with the lock. To avoid
            # the slowdown of acquiring locks on every publish, we guard the lock itself with the check.
            async with self._channel_created:
                if ch not in self.outbound:
                    self.outbound[ch] = asyncio.Queue()
                    self._channel_created.notify_all()
        await self.outbound[ch].put(msg)

    async def consume_outbound(self, *, channel: str) -> OutboundEvent:
        """Block until the next outbound event for the given channel is available.

        If the channel queue does not exist yet, waits until it is created by
        the first publish_outbound call for that channel.
        """
        if channel not in self.outbound:
            async with self._channel_created:
                await self._channel_created.wait_for(lambda: channel in self.outbound)
        return await self.outbound[channel].get()
