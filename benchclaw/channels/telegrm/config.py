"""Telegram channel config + the slash-command lists published to Telegram."""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import BotCommand

from benchclaw.bus import MessageBus
from benchclaw.channels.base import ChannelConfig
from benchclaw.media import MediaRepository

if TYPE_CHECKING:
    from benchclaw.channels.telegrm.channel import TelegramChannel


class TelegramConfig(ChannelConfig):
    """Telegram channel configuration."""

    token: str = ""
    proxy: str | None = None
    admin_user_ids: list[int] = []
    workspace: str | None = None  # set via channel_manager / agent config
    rate_limit_msgs: int = 30
    rate_limit_window_seconds: int = 600
    soft_split_chars: int = 3500

    def make_channel(
        self,
        bus: MessageBus,
        media_repo: MediaRepository | None = None,
        mermaid_mmdc_path: str | None = None,
    ) -> "TelegramChannel":
        # Lazy import to avoid a circular dependency: channel.py imports the
        # rest of the package at module load, and several of those modules
        # import this file for TelegramConfig.
        from benchclaw.channels.telegrm.channel import TelegramChannel

        return TelegramChannel(
            self, bus, media_repo=media_repo, mermaid_mmdc_path=mermaid_mmdc_path
        )

    def is_configured(self) -> bool:
        return bool(self.token.strip())


PUBLIC_COMMANDS: tuple[BotCommand, ...] = (
    BotCommand("start", "Greeting + example questions"),
    BotCommand("help", "What I can do"),
    BotCommand("auth", "Authenticate with the code from the slide"),
    BotCommand("personality", "Switch reply persona"),
    BotCommand("cite", "Toggle inline citations on/off"),
    BotCommand("clear", "Clear conversation history"),
    BotCommand("forgetme", "Delete your storage and re-auth"),
    BotCommand("sources", "List available corpora (when wired)"),
    BotCommand("scope", "Restrict retrieval (when wired)"),
)

ADMIN_COMMANDS: tuple[BotCommand, ...] = PUBLIC_COMMANDS + (
    BotCommand("setsecret", "Rotate the shared auth secret"),
    BotCommand("whoauthed", "List authenticated user IDs"),
    BotCommand("stats", "Active users, query count, retrieval latency"),
)
