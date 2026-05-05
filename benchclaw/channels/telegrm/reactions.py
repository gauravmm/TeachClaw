"""Reaction handlers — sources view and tool-call trace view.

Telegram delivers reaction updates via ``MessageReactionHandler``. We dispatch
per-emoji from the cached, normalized constants in :mod:`state` so the cost
of comparing emojis stays trivial.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from benchclaw import auth as auth_module
from benchclaw import citations as cit
from benchclaw.channels.telegrm.outbound import post
from benchclaw.channels.telegrm.state import (
    SOURCES_NORM,
    SOURCES_REACTION,
    TRACE_NORM,
    UserState,
    normalize_emoji,
)

if TYPE_CHECKING:
    from benchclaw.channels.telegrm.channel import TelegramChannel


async def on_reaction(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    reaction = update.message_reaction
    if not reaction or not update.effective_chat or not update.effective_user:
        return
    addr = channel.addr(update.effective_chat.id)
    if not auth_module.is_authenticated(channel.workspace, addr):
        return
    emojis = {r.emoji for r in (reaction.new_reaction or []) if hasattr(r, "emoji")}
    message_id = reaction.message_id
    st = channel.user_state(update.effective_chat.id)
    for emoji in emojis:
        await dispatch_reaction(channel, emoji, message_id, st, update.effective_chat.id)


async def dispatch_reaction(
    channel: "TelegramChannel",
    emoji: str,
    message_id: int,
    st: UserState,
    chat_id: int,
) -> None:
    normalized = normalize_emoji(emoji)
    if normalized != SOURCES_NORM and normalized != TRACE_NORM:
        # 👍/👎/❓/🔁 reserved for future, no-op for now.
        return
    # First-wins: once we've already posted the citation/trace block for
    # a given message + emoji, ignore subsequent reactions (especially in
    # groups, where multiple members may react to the same reply).
    served_key = (message_id, normalized)
    if served_key in st.served_reactions:
        return
    st.served_reactions.add(served_key)
    if normalized == SOURCES_NORM:
        await reaction_sources(channel, message_id, st, chat_id)
    elif normalized == TRACE_NORM:
        await reaction_trace(channel, message_id, st, chat_id)


async def reaction_sources(
    channel: "TelegramChannel", message_id: int, st: UserState, chat_id: int
) -> None:
    # All replies are threaded onto the original bot message the user reacted
    # on, so the response is anchored even after intervening chatter.
    # allow_sending_without_reply on `post` handles the case where the
    # original message has been deleted.
    record = st.replies.get(message_id)
    if record is None:
        # We don't track replies past process restart, /clear, or /forgetme.
        # Don't pretend we know more than that.
        await post(
            channel,
            chat_id,
            "I don't have a record of that message. It may be from a previous "
            "session or a message I didn't send.",
            reply_to_message_id=message_id,
        )
        return
    _, citations = cit.strip_citations(record.content)
    if not citations:
        await post(
            channel,
            chat_id,
            "That reply didn't cite any sources.",
            reply_to_message_id=message_id,
        )
        return
    kb_records = cit.extract_kb_records(record.tool_calls)
    rendered = cit.render_list(citations[:5], kb_records, fmt=cit.RenderFormat.TELEGRAM_HTML)
    await post(
        channel,
        chat_id,
        rendered,
        parse_mode="HTML",
        reply_to_message_id=message_id,
    )
    if channel._app:
        try:
            from telegram import ReactionTypeEmoji

            await channel._app.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(SOURCES_REACTION)],
            )
        except Exception as e:
            logger.debug(f"setMessageReaction failed: {e}")


async def reaction_trace(
    channel: "TelegramChannel", message_id: int, st: UserState, chat_id: int
) -> None:
    record = st.replies.get(message_id)
    if record is None:
        await post(
            channel,
            chat_id,
            "I don't have a record of that message. It may be from a previous "
            "session or a message I didn't send.",
            reply_to_message_id=message_id,
        )
        return
    if not record.tool_calls:
        await post(
            channel, chat_id, "No tool calls for that reply.", reply_to_message_id=message_id
        )
        return
    lines: list[str] = []
    for tc in record.tool_calls[:10]:
        args_text = ", ".join(f"{k}={v!r}" for k, v in list(tc.arguments.items())[:3])
        if len(args_text) > 120:
            args_text = args_text[:119] + "…"
        if tc.result is None:
            result_text = "(pending)"
        else:
            # Agent loop no longer truncates; collapse newlines and cap at
            # display time so the bullet line stays one row.
            flat = tc.result.replace("\n", " ").strip()
            result_text = flat[:120] + "…" if len(flat) > 120 else flat
        lines.append(f"• {tc.name}({args_text}) → {result_text}")
    await post(channel, chat_id, "\n".join(lines), reply_to_message_id=message_id)
