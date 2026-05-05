"""Base channel interface for chat platforms."""

import asyncio
from abc import abstractmethod
from asyncio import Task
from datetime import datetime, timedelta
from typing import Any, Self

from anyio import AsyncContextManagerMixin
from loguru import logger
from pydantic import BaseModel

from benchclaw.bus import (
    MediaMetadata,
    MessageBus,
    OutboundMessage,
    TypingEvent,
)
from benchclaw.channels.attention import (
    AttentionPolicy,
    InboundAttentionFilter,
)
from benchclaw.utils import DurationField


class ChannelConfig(BaseModel):
    allow_from: list[str] | None = None
    attention_policy: AttentionPolicy = AttentionPolicy.SUMMON_GROUP
    attention_lookback: DurationField = timedelta(minutes=10)
    attention_gap: DurationField = timedelta(minutes=5)

    def make_channel(
        self,
        bus: "MessageBus",
        media_repo: Any = None,
        mermaid_mmdc_path: str | None = None,
    ) -> "BaseChannel":
        raise NotImplementedError(f"{type(self).__name__} must implement make_channel()")

    def is_configured(self) -> bool:
        return True


class BaseChannel(AsyncContextManagerMixin):
    """
    Abstract base class for chat channel implementations.

    Each channel (Telegram, etc.) should implement this interface
    to integrate with the benchclaw message bus.
    """

    name: str = "base"

    def __init__(self, config: ChannelConfig, bus: MessageBus):
        """
        Initialize the channel.

        Args:
            config: Channel-specific configuration.
            bus: The message bus for communication.
        """
        self.config = config
        self.bus = bus

        self._task: Task | None = None  # Background task
        # Per-chat typing-indicator dedupe: True iff our last published
        # state for that chat was is_typing=True. Per-chat (not per-
        # channel) so a typing event for chat B doesn't get suppressed
        # because chat A is already mid-reply.
        self._typing_active: dict[str, bool] = {}
        self._inbound_attention = InboundAttentionFilter(
            channel=self.name,
            policy=self.config.attention_policy,
            lookback=self.config.attention_lookback,
            gap=self.config.attention_gap,
        )

    async def background(self) -> None:
        """
        Start the channel and begin listening for messages.

        This should be a long-running async task that:
        1. Connects to the chat platform
        2. Listens for incoming messages
        3. Forwards messages to the bus via _handle_message()
        4. Terminates cleanly on CancelledError
        """

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self.background())
        return self

    async def __aexit__(self, *args):
        if self._task:
            try:
                async with asyncio.timeout(5):
                    self._task.cancel()
            except TimeoutError:
                print(f"{self.__qualname__} timed out while exiting")

    def status(self) -> tuple[bool, str]:
        """Return (is_running, description) for this channel."""
        running = bool(self._task and not self._task.done())
        return (running, "running" if running else "stopped")

    async def _handle_typing(self, event: TypingEvent) -> None:
        """Deduplicate per-chat typing state changes and delegate to notify_typing.

        State is keyed by chat_id so a typing-true event for chat B isn't
        suppressed while chat A is already typing — the prior per-channel
        bool masked the indicator any time the same channel served more
        than one chat at once (DM + group, two groups, etc.).
        """
        chat_id = event.address.chat_id
        if event.is_typing != self._typing_active.get(chat_id, False):
            self._typing_active[chat_id] = event.is_typing
            await self.notify_typing(event)

    async def notify_typing(self, event: TypingEvent) -> None:
        """Called when typing state changes. Override to send platform-specific indicators."""
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """
        Send a message through this channel.

        Args:
            msg: The message to send.
        """
        pass

    def is_allowed(self, sender_id: str) -> bool:
        """
        Check if a sender is allowed to use this bot.

        Args:
            sender_id: The sender's identifier.

        Returns:
            True if allowed, False otherwise.
        """
        allow_list = self.config.allow_from
        if not allow_list:
            # If no allow list, allow everyone
            return True

        if sender_id in allow_list:
            return True

        if "|" in sender_id:
            if any(part in allow_list for part in sender_id.split("|")):
                return True
        return False

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        media_metadata: list[MediaMetadata] | None = None,
        metadata: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        """
        Handle an incoming message from the chat platform.

        This method checks permissions and forwards to the bus.

        Args:
            sender_id: The sender's identifier.
            chat_id: The chat/channel identifier.
            content: Message text content.
            media: Optional list of media URLs.
            media_metadata: Optional structured metadata for media attachments.
            metadata: Optional channel-specific metadata.
            timestamp: Optional source timestamp for attention decisions.
        """
        if not self.is_allowed(sender_id):
            logger.warning(
                f"Access denied for sender {sender_id} on channel {self.name}. "
                f"Add them to allow_from list in config to grant access."
            )
            return

        inbound = self._inbound_attention.apply(
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media,
            media_metadata=media_metadata,
            metadata=metadata or {},
            timestamp=timestamp,
        )
        if inbound:
            await self.bus.publish_inbound(inbound[0].address, *inbound)
