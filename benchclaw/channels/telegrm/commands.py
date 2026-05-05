"""Slash-command handlers, command-menu publishing, persona switching.

Each `cmd_*` function takes (channel, update, ctx) and is bound onto
``TelegramChannel`` so Python's descriptor protocol passes the instance
correctly when ``telegram.ext.CommandHandler`` invokes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from telegram import (
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from benchclaw import auth as auth_module
from benchclaw import personalities
from benchclaw import storage as storage_layout
from benchclaw.bus import MessageAddress, SessionControlEvent, SystemMessageEvent
from benchclaw.channels.telegrm.auth_gate import gate
from benchclaw.channels.telegrm.config import ADMIN_COMMANDS, PUBLIC_COMMANDS
from benchclaw.channels.telegrm.state import SOURCES_REACTION, TRACE_REACTION

if TYPE_CHECKING:
    from benchclaw.channels.telegrm.channel import TelegramChannel


# ---- menu + startup --------------------------------------------------------


def ensure_secret_on_startup(channel: "TelegramChannel") -> None:
    secret = auth_module.read_secret(channel.workspace)
    if secret is not None:
        return
    record = auth_module.write_secret(channel.workspace, auth_module.generate_code())
    logger.warning(
        "No auth secret on disk; generated a fresh code: {} (rotate with /setsecret).",
        record.code,
    )


async def refresh_command_menu(channel: "TelegramChannel") -> None:
    """(Re)publish the command menu, wiping stale per-scope lists first.

    Telegram resolves the menu by scope hierarchy (chat → all-private →
    default), and lists set under any scope persist across bot restarts until
    explicitly cleared. If a previous bot version published commands under a
    broader scope than we use now, those entries shadow the current
    default-scope list and the user sees the old menu. Clear the common
    scopes before re-setting so the active list always wins.
    """
    if not channel._app:
        return
    bot = channel._app.bot
    scopes_to_clear = (
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllChatAdministrators(),
    )
    try:
        for scope in scopes_to_clear:
            try:
                await bot.delete_my_commands(scope=scope)
            except Exception as e:
                logger.debug(f"deleteMyCommands({type(scope).__name__}) failed: {e}")
        await bot.set_my_commands(commands=list(PUBLIC_COMMANDS), scope=BotCommandScopeDefault())
        for admin_id in channel.config.admin_user_ids or []:
            # Clear then set so removed admin commands don't linger.
            try:
                await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception as e:
                logger.debug(f"deleteMyCommands(chat={admin_id}) failed: {e}")
            await bot.set_my_commands(
                commands=list(ADMIN_COMMANDS),
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
    except Exception as e:
        logger.warning(f"setMyCommands failed: {e}")


async def announce_persona_switch(
    channel: "TelegramChannel",
    addr: MessageAddress,
    chosen: personalities.Personality,
) -> None:
    """Mark the persona switch in conversation history.

    The system prompt now leaves persona out (it lives in the synthetic tail
    message instead), so the only durable record of when a switch happened
    sits in the session as a SystemEvent.
    """
    await channel.bus.publish_inbound(
        addr,
        SystemMessageEvent(
            content=(
                f"User switched persona to {chosen.label}. Earlier assistant "
                f"turns used a different voice; adopt the new persona from now on."
            )
        ),
    )


# ---- public commands -------------------------------------------------------


async def cmd_start(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await gate(channel, update, allow_unauth=True):
        return
    msg = update.effective_message
    if not msg:
        return
    # Republish the menu so users who saw the old command list from a
    # previous deployment get the current one.
    await refresh_command_menu(channel)
    text = (
        "Welcome to the AI-in-Business class assistant.\n\n"
        "Try one of these to get started:\n"
        "• What is a value chain, with an example from healthcare?\n"
        "• Map AI use cases to a 2x2 of effort vs. business impact.\n"
        "• Compare build vs. buy for a recommendation engine.\n\n"
        "Authenticate first: send /auth <code> using the code on the slide."
    )
    await msg.reply_text(text)


async def cmd_help(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await gate(channel, update, allow_unauth=True):
        return
    msg = update.effective_message
    if not msg:
        return
    text = (
        "I'm a small assistant for the AI-in-Business lecture.\n\n"
        "Commands: /auth, /personality, /cite, /clear, /forgetme, /sources, /scope.\n"
        f"React {SOURCES_REACTION} to one of my replies to see the source chunks; "
        f"react {TRACE_REACTION} to see the tool-call trace for that reply."
    )
    await msg.reply_text(text)


async def cmd_auth(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await gate(channel, update, allow_unauth=True):
        return
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not (msg and user and chat):
        return
    user_key = str(user.id)
    ok, lock_msg = channel._auth_limiter.check(user_key)
    if not ok:
        await msg.reply_text(lock_msg or "Locked out.")
        return

    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Send /auth <code> — the code is on the slide.")
        return
    provided = auth_module.normalize_code(parts[1])
    secret = auth_module.read_secret(channel.workspace)
    if secret is None:
        await msg.reply_text("Auth is not configured yet — ask the prof to run /setsecret.")
        return
    if provided != secret.code:
        failures, locked = channel._auth_limiter.record_failure(user_key)
        if locked:
            await msg.reply_text("Too many wrong codes. Locked out for the next hour.")
        else:
            await msg.reply_text(
                f"Wrong code. ({failures}/{auth_module.RATE_LIMIT_FAILURES} tries in this window.)"
            )
        return
    addr = channel.addr(chat.id)
    storage_layout.ensure_user_dirs(channel.workspace, addr)
    auth_module.write_marker(channel.workspace, addr, secret.code)
    channel._auth_limiter.record_success(user_key)
    await msg.reply_text("Authenticated. Ask me anything.")


async def cmd_personality(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await gate(channel, update):
        return
    msg = update.effective_message
    chat = update.effective_chat
    if not (msg and chat):
        return
    addr = channel.addr(chat.id)
    text = (msg.text or "").split(maxsplit=1)
    if len(text) >= 2:
        name = text[1].strip().lower()
        chosen = personalities.write_personality(channel.workspace, addr, name)
        if chosen is None:
            names = ", ".join(p.name for p in personalities.all_personalities())
            await msg.reply_text(f"Unknown personality. Pick one of: {names}.")
            return
        await announce_persona_switch(channel, addr, chosen)
        await msg.reply_text(f"Personality set to {chosen.label}.")
        return

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(p.label, callback_data=f"p:{p.name}")]
            for p in personalities.all_personalities()
        ]
    )
    await msg.reply_text("Choose a persona:", reply_markup=keyboard)


async def on_callback_query(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    if not query.data.startswith("p:"):
        return
    name = query.data[2:]
    chat = update.effective_chat
    if not chat:
        return
    addr = channel.addr(chat.id)
    if not auth_module.is_authenticated(channel.workspace, addr):
        await query.edit_message_text("Send /auth <code> first.")
        return
    chosen = personalities.write_personality(channel.workspace, addr, name)
    if chosen is None:
        await query.edit_message_text("Unknown personality.")
        return
    await announce_persona_switch(channel, addr, chosen)
    await query.edit_message_text(f"Personality set to {chosen.label}.")


async def cmd_cite(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await gate(channel, update):
        return
    msg = update.effective_message
    chat = update.effective_chat
    if not (msg and chat):
        return
    st = channel.user_state(chat.id)
    st.cite = not st.cite
    await msg.reply_text(f"Inline citations: {'on' if st.cite else 'off'}.")


async def cmd_clear(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await gate(channel, update):
        return
    msg = update.effective_message
    chat = update.effective_chat
    if not (msg and chat):
        return
    addr = channel.addr(chat.id)
    await channel.bus.publish_inbound(addr, SessionControlEvent(action="reset"))
    st = channel.user_state(chat.id)
    st.replies.clear()
    st.seen_first_citation = False
    await msg.reply_text("Conversation cleared.")


async def cmd_forgetme(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await gate(channel, update, allow_unauth=True):
        return
    msg = update.effective_message
    chat = update.effective_chat
    if not (msg and chat):
        return
    addr = channel.addr(chat.id)
    await channel.bus.publish_inbound(addr, SessionControlEvent(action="forget"))
    st = channel.user_state(chat.id)
    st.replies.clear()
    st.seen_first_citation = False
    await msg.reply_text(
        "Your storage has been deleted. Re-authenticate with /auth <code> to continue."
    )


async def cmd_sources(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await gate(channel, update):
        return
    msg = update.effective_message
    if msg:
        await msg.reply_text("No corpus is wired up yet. Retrieval lands in a later iteration.")


async def cmd_scope(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await gate(channel, update):
        return
    msg = update.effective_message
    if msg:
        await msg.reply_text("No corpus to scope yet. Retrieval lands in a later iteration.")


# ---- admin commands --------------------------------------------------------


async def cmd_setsecret(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not (msg and user):
        return
    if not channel.is_admin(user.id):
        await msg.reply_text("Admin command.")
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) >= 2:
        code = auth_module.normalize_code(parts[1])
        if not auth_module.is_valid_code_shape(code):
            await msg.reply_text(
                "Codes use the alphabet ABCDEFGHJKLMNPQRSTUVWXYZ23456789 (no 0/O/1/I/L)."
            )
            return
    else:
        code = auth_module.generate_code()
    record = auth_module.write_secret(channel.workspace, code)
    await msg.reply_text(f"New secret: {record.code}")


async def cmd_whoauthed(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not (msg and user):
        return
    if not channel.is_admin(user.id):
        await msg.reply_text("Admin command.")
        return
    ids = auth_module.authenticated_addresses(channel.workspace, channel.name)
    if not ids:
        await msg.reply_text("No authenticated users.")
        return
    await msg.reply_text(f"Authenticated ({len(ids)}): {', '.join(ids)}")


async def cmd_stats(
    channel: "TelegramChannel", update: Update, _ctx: ContextTypes.DEFAULT_TYPE
) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not (msg and user):
        return
    if not channel.is_admin(user.id):
        await msg.reply_text("Admin command.")
        return
    active = sum(1 for st in channel._users.values() if st.in_flight)
    total = len(channel._users)
    await msg.reply_text(f"users seen: {total}, in-flight: {active}")
