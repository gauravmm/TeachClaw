"""Channel manager for coordinating chat channels."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

from loguru import logger

from teachclaw.bus import MessageBus, TypingEvent
from teachclaw.channels.base import BaseChannel
from teachclaw.config import Config
from teachclaw.media import MediaRepository


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels from config
    - Start/stop channels
    - Route outbound messages to each channel via per-channel bus queues
    """

    def __init__(self, config: Config, bus: MessageBus, media_repo: MediaRepository | None = None):
        self.config = config
        self.bus = bus
        self.channels: dict[str, BaseChannel] = {}
        self._stack = AsyncExitStack()

        mermaid_mmdc_path = config.mermaid.mmdc_path
        for name, chconfig in self.config.channels:
            if not chconfig.is_configured():
                continue
            self.channels[name] = chconfig.make_channel(
                self.bus,
                media_repo=media_repo,
                mermaid_mmdc_path=mermaid_mmdc_path,
            )

    async def __aenter__(self) -> "ChannelManager":
        await self._stack.__aenter__()

        if not self.channels:
            logger.warning("No channels configured")
            return self

        for name, channel in self.channels.items():
            dispatch_task = asyncio.create_task(
                self._dispatch_channel(name, channel), name=f"dispatch:{name}"
            )
            self._stack.callback(dispatch_task.cancel)
            await self._stack.enter_async_context(channel)

        return self

    async def __aexit__(self, *exc_info) -> None:
        logger.info("Stopping all channels...")
        await self._stack.__aexit__(*exc_info)

    async def _dispatch_channel(self, name: str, channel: BaseChannel) -> None:
        """Consume outbound events for a single channel and deliver them."""
        logger.info(f"Outbound dispatcher started for {name}")
        try:
            while True:
                msg = await self.bus.consume_outbound(channel=name)
                try:
                    if isinstance(msg, TypingEvent):
                        await channel.notify_typing(msg)
                    else:
                        await channel.send(msg)
                except Exception as e:
                    logger.error(f"Error dispatching to {name}: {e}")
        except asyncio.CancelledError:
            pass

    def get_status(self) -> dict[str, tuple[bool, str]]:
        """Get status of all channels as {name: (is_running, description)}."""
        return {name: channel.status() for name, channel in self.channels.items()}
