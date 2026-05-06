"""Explicit built-in channel manifest."""

from teachclaw.channels.base import ChannelConfig
from teachclaw.channels.claude_code import ClaudeCodeConfig
from teachclaw.channels.smtp_email import EmailConfig
from teachclaw.channels.telegrm import TelegramConfig

BUILTIN_CHANNEL_CONFIGS: tuple[tuple[str, type[ChannelConfig]], ...] = (
    ("claude_code", ClaudeCodeConfig),
    ("email", EmailConfig),
    ("telegram", TelegramConfig),
)
