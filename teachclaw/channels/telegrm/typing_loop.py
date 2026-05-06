"""Typing-indicator background loop.

Telegram's typing indicator only lasts ~5s, so we re-send `chat_action`
every 4s for up to ~32s while the assistant is in flight.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from teachclaw.bus import TypingEvent

if TYPE_CHECKING:
    from teachclaw.channels.telegrm.channel import TelegramChannel


async def notify_typing(channel: "TelegramChannel", event: TypingEvent) -> None:
    try:
        chat_id_int = int(event.address.chat_id)
    except ValueError:
        return
    st = channel.user_state(chat_id_int)
    st.in_flight = bool(event.is_typing)
    if event.is_typing:
        await start_typing(channel, event.address.chat_id)
    else:
        stop_typing(channel, event.address.chat_id)


async def start_typing(channel: "TelegramChannel", chat_id: str) -> None:
    """Fire the initial typing chat_action synchronously, then schedule
    a 4-second refresh loop.

    Sending the first action inline (awaited by the dispatcher) is what
    makes the indicator survive fast LLM responses: if start were just
    ``create_task(...)``, a quick stop_typing could cancel the refresh
    task before its first ``send_chat_action`` HTTP request reached
    Telegram, and the bubble would never appear in the client.
    """
    stop_typing(channel, chat_id)
    if not channel._app:
        return
    try:
        await channel._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
    except Exception as e:
        logger.warning(f"Typing indicator initial action failed for {chat_id}: {e}")
        return
    channel._typing_tasks[chat_id] = asyncio.create_task(_typing_loop(channel, chat_id))


def stop_typing(channel: "TelegramChannel", chat_id: str) -> None:
    task = channel._typing_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


async def _typing_loop(channel: "TelegramChannel", chat_id: str) -> None:
    """Refresh the typing chat_action every 4s for ~32s.

    The initial action is sent synchronously by :func:`start_typing`;
    this loop only handles the periodic refreshes that keep the bubble
    visible past Telegram's ~5s window.
    """
    try:
        for _ in range(7):
            await asyncio.sleep(4)
            if not channel._app:
                return
            await channel._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        # Bot lacking send-message permission, chat archived, etc.
        # Surface at warning so silently-failing typing indicators are
        # visible in logs instead of getting buried at debug level.
        logger.warning(f"Typing indicator stopped for {chat_id}: {e}")
