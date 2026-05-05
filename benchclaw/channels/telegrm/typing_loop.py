"""Typing-indicator background loop.

Telegram's typing indicator only lasts ~5s, so we re-send `chat_action`
every 4s for up to ~32s while the assistant is in flight.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from benchclaw.bus import TypingEvent

if TYPE_CHECKING:
    from benchclaw.channels.telegrm.channel import TelegramChannel


async def notify_typing(channel: "TelegramChannel", event: TypingEvent) -> None:
    try:
        chat_id_int = int(event.address.chat_id)
    except ValueError:
        return
    st = channel.user_state(chat_id_int)
    st.in_flight = bool(event.is_typing)
    if event.is_typing:
        start_typing(channel, event.address.chat_id)
    else:
        stop_typing(channel, event.address.chat_id)


def start_typing(channel: "TelegramChannel", chat_id: str) -> None:
    stop_typing(channel, chat_id)
    logger.info(f"Typing indicator: start chat={chat_id}")
    channel._typing_tasks[chat_id] = asyncio.create_task(_typing_loop(channel, chat_id))


def stop_typing(channel: "TelegramChannel", chat_id: str) -> None:
    task = channel._typing_tasks.pop(chat_id, None)
    if task and not task.done():
        logger.info(f"Typing indicator: stop chat={chat_id}")
        task.cancel()


async def _typing_loop(channel: "TelegramChannel", chat_id: str) -> None:
    try:
        for _ in range(8):
            if not channel._app:
                return
            await channel._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        # Bot lacking send-message permission, chat archived, etc.
        # Surface at warning so silently-failing typing indicators are
        # visible in logs instead of getting buried at debug level.
        logger.warning(f"Typing indicator stopped for {chat_id}: {e}")
