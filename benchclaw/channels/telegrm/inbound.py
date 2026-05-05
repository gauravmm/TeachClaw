"""Inbound non-command message handling.

Splits responsibility for one update across:
* :func:`on_message` — gate, rate-limit, in-flight check, then build
  the inbound payload (text + downloaded media) and hand off to the bus.
* :func:`allow_rate` — sliding-window soft cap; sends the user one
  "take a breath" warning per blocked window.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from benchclaw.bus import MediaMetadata, MessageAddress
from benchclaw.channels.telegrm.auth_gate import gate
from benchclaw.channels.telegrm.outbound import post
from benchclaw.channels.telegrm.state import UserState
from benchclaw.media import extension_for_mime

if TYPE_CHECKING:
    from benchclaw.channels.telegrm.channel import TelegramChannel


async def on_message(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await gate(channel, update):
        return
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not (message and user and chat):
        return

    st = channel.user_state(chat.id)

    # In groups, rate-limit each member independently so one chatty user
    # can't starve the room. In DMs there's only one member anyway, so
    # per-sender keying degenerates to per-chat behavior.
    sender_label = user.first_name or user.username or str(user.id)
    if not allow_rate(channel, st, chat.id, str(user.id), sender_label):
        return

    if st.in_flight:
        try:
            await message.reply_text("still thinking…")
        except Exception:
            pass
        return

    sender_id = str(user.id)
    if user.username:
        sender_id = f"{sender_id}|{user.username}"
    st.last_user_message_id = message.message_id

    content_parts: list[str] = []
    media_paths: list[str] = []
    media_metadata: list[MediaMetadata] = []

    if message.text:
        content_parts.append(message.text)
    if message.caption:
        content_parts.append(f"caption: {message.caption}")

    media_file = None
    media_type: str | None = None
    if message.photo:
        media_file = message.photo[-1]
        media_type = "image"
    elif message.voice:
        media_file = message.voice
        media_type = "voice"
    elif message.audio:
        media_file = message.audio
        media_type = "audio"
    elif message.document:
        media_file = message.document
        media_type = "file"

    if media_file and media_type and channel._app:
        if not channel.media_repo:
            logger.warning("Telegram received media but media_repo not configured; skipping")
        else:
            try:
                file = await channel._app.bot.get_file(media_file.file_id)
                mime_type = getattr(media_file, "mime_type", None)
                size_bytes = getattr(media_file, "file_size", None)
                ext = extension_for_mime(mime_type)
                file_path = channel.media_repo.register(
                    MessageAddress(channel.name, str(chat.id)),
                    sender_id=sender_id,
                    media_type=media_type,
                    ext=ext,
                    mime_type=mime_type,
                    timestamp=message.date,
                    original_name=getattr(media_file, "file_name", None),
                )
                await file.download_to_drive(str(file_path))
                media_paths.append(channel.media_repo.model_relpath(file_path))
                media_metadata.append(
                    {
                        "path": str(file_path),
                        "media_type": media_type,
                        "mime_type": mime_type,
                        "size_bytes": size_bytes,
                        "saved_at": message.date.isoformat(timespec="seconds"),
                        "source_channel": channel.name,
                        "original_name": getattr(media_file, "file_name", None),
                    }
                )
            except Exception as e:
                logger.error(f"Failed to download media: {e}")

    content = "\n".join(content_parts) if content_parts else "[empty message]"
    message_metadata = {
        "message_id": message.message_id,
        "user_id": user.id,
        "username": user.username,
        "sender_label": user.first_name or user.username,
        "is_group": chat.type != "private",
    }

    await channel._handle_message(
        sender_id=sender_id,
        chat_id=str(chat.id),
        content=content,
        media=media_paths,
        media_metadata=media_metadata,
        metadata=message_metadata,
        timestamp=message.date,
    )


def allow_rate(
    channel: "TelegramChannel",
    st: UserState,
    chat_id: int,
    sender_key: str,
    sender_label: str,
) -> bool:
    from collections import deque

    now = time.monotonic()
    window = channel.config.rate_limit_window_seconds
    rate_window = st.rate_windows.setdefault(sender_key, deque())
    while rate_window and now - rate_window[0] > window:
        rate_window.popleft()
    if len(rate_window) >= channel.config.rate_limit_msgs:
        if sender_key not in st.rate_blocked_warned and channel._app:
            st.rate_blocked_warned.add(sender_key)
            asyncio.create_task(
                post(
                    channel,
                    chat_id,
                    f"{sender_label}: take a breath — too many messages just now.",
                )
            )
        return False
    rate_window.append(now)
    st.rate_blocked_warned.discard(sender_key)
    return True
