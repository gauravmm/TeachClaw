"""DM-only + auth gating for inbound updates.

`gate(channel, update, *, allow_unauth=False)` returns True if the message
may proceed. Group chats and unauthenticated users get a one-line note and
False; the caller short-circuits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Update

from benchclaw import auth as auth_module

if TYPE_CHECKING:
    from benchclaw.channels.telegrm.channel import TelegramChannel


async def gate(channel: "TelegramChannel", update: Update, *, allow_unauth: bool = False) -> bool:
    if not update.effective_user or not update.effective_chat:
        return False
    if update.effective_chat.type != "private":
        await update.effective_chat.send_message(
            "I run as a DM-only bot for the lecture. Message me directly."
        )
        return False
    if allow_unauth:
        return True
    addr = channel.addr(update.effective_chat.id)
    if auth_module.is_authenticated(channel.workspace, addr):
        return True
    await update.effective_chat.send_message(
        "This is the class assistant. Send /auth <code> — the code is on the slide."
    )
    return False
