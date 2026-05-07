"""Explicit built-in channel manifest."""

from teachclaw.channels.base import ChannelConfig
from teachclaw.channels.telegrm import TelegramConfig

BUILTIN_CHANNEL_CONFIGS: tuple[tuple[str, type[ChannelConfig]], ...] = (
    ("telegram", TelegramConfig),
)
