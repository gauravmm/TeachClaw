"""Explicit built-in channel manifest."""

from benchclaw.channels.base import ChannelConfig
from benchclaw.channels.claude_code import ClaudeCodeConfig
from benchclaw.channels.smtp_email import EmailConfig
from benchclaw.channels.telegrm import TelegramConfig

BUILTIN_CHANNEL_CONFIGS: tuple[tuple[str, type[ChannelConfig]], ...] = (
    ("claude_code", ClaudeCodeConfig),
    ("email", EmailConfig),
    ("telegram", TelegramConfig),
)
