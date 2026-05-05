"""Telegram channel for the lecture deployment.

Implements spec/TELEGRAM.md and integrates with spec/AUTH.md. Highlights:

- Slash commands wired through ``CommandHandler`` with auth middleware that
  short-circuits everything except ``/start``, ``/help``, ``/auth``.
- Reaction handler dispatches per-emoji from the generic ``SOURCES_REACTION``
  / ``TRACE_REACTION`` constants below; the former surfaces source citations
  (stub until RAG lands), the latter surfaces a tool-call trace.
- Per-message_id map (24h TTL) holds tool calls and parsed citations so a
  reaction on an old reply can recover them.
- Citation tags ``<citation id="..">..</citation>`` are stripped from the
  outbound text and remembered in the per-message_id map.
- Mermaid blocks in the outbound text are rendered to PNG via
  ``benchclaw.rendering.mermaid`` and posted in order.
- Rate limits: one in-flight per user (tied to the typing indicator), 30
  msgs/10min soft cap.
- DM-only: messages from group chats are refused with a one-line note.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger
from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)
from telegram.request import HTTPXRequest

from benchclaw import auth as auth_module
from benchclaw import citations as cit
from benchclaw import personalities
from benchclaw import storage as storage_layout
from benchclaw.bus import (
    MediaMetadata,
    MessageAddress,
    MessageBus,
    OutboundMessage,
    SessionControlEvent,
    SystemMessageEvent,
    ToolCallTrace,
    TypingEvent,
)
from benchclaw.channels.base import BaseChannel, ChannelConfig
from benchclaw.media import MediaRepository, extension_for_mime
from benchclaw.rendering import mermaid as mermaid_renderer

# ---------------------------------------------------------------------------
# Reaction emojis (edit here to change which reactions trigger which view)
# ---------------------------------------------------------------------------

# Reaction that asks the bot to reply with the source citations for a reply.
# Telegram's reaction API delivers emojis without the FE0F variant selector
# (e.g. "❤", not "❤️"); _normalize_emoji() below strips it on both sides
# so the constants here work whether or not you typed the variant selector.
SOURCES_REACTION = "❤"
# Reaction that asks the bot to reply with the tool-call trace for a reply.
TRACE_REACTION = "🔥"


_VARIATION_SELECTOR_16 = "️"


def _normalize_emoji(emoji: str) -> str:
    """Strip the U+FE0F variation selector so comparisons survive whether
    the emoji was written with or without the emoji-presentation modifier
    (e.g. ❤ vs. ❤️)."""
    return emoji.replace(_VARIATION_SELECTOR_16, "")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


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
        return TelegramChannel(
            self, bus, media_repo=media_repo, mermaid_mmdc_path=mermaid_mmdc_path
        )

    def is_configured(self) -> bool:
        return bool(self.token.strip())


# ---------------------------------------------------------------------------
# Per-user state
# ---------------------------------------------------------------------------


@dataclass
class _ReplyRecord:
    """Raw bits of an outbound assistant turn, kept under the Telegram
    message_id of the first segment we sent. Citations and kb_records are
    re-derived on demand in the reaction handlers (see _reaction_sources).

    This intentionally stores the *unstripped* content — i.e. what the LLM
    emitted, including any ``<citation>`` markers — so the SOURCES_REACTION
    can re-parse it without any TTL/tombstone state to manage.
    """

    content: str
    tool_calls: list[ToolCallTrace]


@dataclass
class _UserState:
    chat_id: int
    cite: bool = True
    in_flight: bool = False
    seen_first_citation: bool = False
    rate_window: deque[float] = field(default_factory=deque)
    rate_blocked_warned: bool = False
    last_user_message_id: int | None = None
    replies: dict[int, _ReplyRecord] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Markdown → Telegram HTML
# ---------------------------------------------------------------------------


def _markdown_to_telegram_html(text: str) -> str:
    """Convert markdown to Telegram-safe HTML."""
    if not text:
        return ""

    code_blocks: list[str] = []

    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\w]*\n?([\s\S]*?)```", save_code_block, text)

    inline_codes: list[str] = []

    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", save_inline_code, text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s*(.*)$", r"\1", text, flags=re.MULTILINE)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)

    for i, code in enumerate(inline_codes):
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")

    for i, code in enumerate(code_blocks):
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")

    return text


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < 0:
            cut = remaining.rfind("\n", 0, limit)
        if cut < 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


# ---------------------------------------------------------------------------
# Slash command set
# ---------------------------------------------------------------------------


_PUBLIC_COMMANDS: tuple[BotCommand, ...] = (
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

_ADMIN_COMMANDS: tuple[BotCommand, ...] = _PUBLIC_COMMANDS + (
    BotCommand("setsecret", "Rotate the shared auth secret"),
    BotCommand("whoauthed", "List authenticated user IDs"),
    BotCommand("stats", "Active users, query count, retrieval latency"),
)


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


class TelegramChannel(BaseChannel):
    """Telegram channel using long polling."""

    name = "telegram"

    def __init__(
        self,
        config: TelegramConfig,
        bus: MessageBus,
        media_repo: MediaRepository | None = None,
        mermaid_mmdc_path: str | None = None,
    ):
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self.media_repo = media_repo
        self.mermaid_mmdc_path = mermaid_mmdc_path
        self._app: Application | None = None
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._bot_username: str | None = None
        self._bot_user_id: int | None = None
        self._users: dict[str, _UserState] = {}
        self._auth_limiter = auth_module.AuthRateLimiter()

    @property
    def workspace(self) -> Path:
        if self.config.workspace:
            return Path(self.config.workspace).expanduser()
        if self.media_repo is not None:
            return self.media_repo.workspace
        return Path("./workspace")

    def _user_state(self, chat_id: int) -> _UserState:
        key = str(chat_id)
        st = self._users.get(key)
        if st is None:
            st = _UserState(chat_id=chat_id)
            self._users[key] = st
        return st

    def _addr(self, chat_id: int) -> MessageAddress:
        return MessageAddress(self.name, str(chat_id))

    def _is_admin(self, user_id: int) -> bool:
        return user_id in (self.config.admin_user_ids or [])

    def status(self) -> tuple[bool, str]:
        if self._app:
            return (True, "connected")
        return (False, "not connected")

    # ---- background lifecycle ---------------------------------------------

    async def background(self) -> None:
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return

        req = HTTPXRequest(
            connection_pool_size=16,
            pool_timeout=5.0,
            connect_timeout=30.0,
            read_timeout=30.0,
        )
        builder = (
            Application.builder().token(self.config.token).request(req).get_updates_request(req)
        )
        if self.config.proxy:
            builder = builder.proxy(self.config.proxy).get_updates_proxy(self.config.proxy)
        self._app = builder.build()
        self._app.add_error_handler(self._on_error)

        # Slash commands first; non-command messages second.
        cmds = {
            "start": self._cmd_start,
            "help": self._cmd_help,
            "auth": self._cmd_auth,
            "personality": self._cmd_personality,
            "cite": self._cmd_cite,
            "clear": self._cmd_clear,
            "forgetme": self._cmd_forgetme,
            "sources": self._cmd_sources,
            "scope": self._cmd_scope,
            "setsecret": self._cmd_setsecret,
            "whoauthed": self._cmd_whoauthed,
            "stats": self._cmd_stats,
        }
        for name, handler in cmds.items():
            self._app.add_handler(CommandHandler(name, handler))

        self._app.add_handler(CallbackQueryHandler(self._on_callback_query, pattern=r"^p:"))

        self._app.add_handler(
            MessageHandler(
                (
                    filters.TEXT
                    | filters.PHOTO
                    | filters.VOICE
                    | filters.AUDIO
                    | filters.Document.ALL
                )
                & ~filters.COMMAND,
                self._on_message,
            )
        )

        self._app.add_handler(MessageReactionHandler(self._on_reaction))

        logger.info("Starting Telegram bot (polling mode)...")

        await self._app.initialize()
        await self._app.start()

        bot_info = await self._app.bot.get_me()
        self._bot_username = bot_info.username
        self._bot_user_id = bot_info.id
        logger.info(f"Telegram bot @{bot_info.username} connected")

        await self._refresh_command_menu()
        self._ensure_secret_on_startup()

        assert self._app.updater
        await self._app.updater.start_polling(
            allowed_updates=["message", "message_reaction", "callback_query"],
            drop_pending_updates=True,
        )

        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            pass
        finally:
            for chat_id in list(self._typing_tasks):
                self._stop_typing(chat_id)
            if self._app:
                logger.info("Stopping Telegram bot...")
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
                self._app = None

    def _ensure_secret_on_startup(self) -> None:
        secret = auth_module.read_secret(self.workspace)
        if secret is not None:
            return
        record = auth_module.write_secret(self.workspace, auth_module.generate_code())
        logger.warning(
            "No auth secret on disk; generated a fresh code: {} (rotate with /setsecret).",
            record.code,
        )

    async def _refresh_command_menu(self) -> None:
        """(Re)publish the command menu, wiping stale per-scope lists first.

        Telegram resolves the menu by scope hierarchy (chat → all-private →
        default), and lists set under any scope persist across bot restarts
        until explicitly cleared. If a previous bot version published commands
        under a broader scope than we use now, those entries shadow the
        current default-scope list and the user sees the old menu. Clear the
        common scopes before re-setting so the active list always wins.
        """
        if not self._app:
            return
        bot = self._app.bot
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
            await bot.set_my_commands(
                commands=list(_PUBLIC_COMMANDS), scope=BotCommandScopeDefault()
            )
            for admin_id in self.config.admin_user_ids or []:
                # Clear then set so removed admin commands don't linger.
                try:
                    await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=admin_id))
                except Exception as e:
                    logger.debug(f"deleteMyCommands(chat={admin_id}) failed: {e}")
                await bot.set_my_commands(
                    commands=list(_ADMIN_COMMANDS),
                    scope=BotCommandScopeChat(chat_id=admin_id),
                )
        except Exception as e:
            logger.warning(f"setMyCommands failed: {e}")

    # ---- auth gate --------------------------------------------------------

    async def _gate(self, update: Update, *, allow_unauth: bool = False) -> bool:
        """Return True if the message may proceed."""
        if not update.effective_user or not update.effective_chat:
            return False
        if update.effective_chat.type != "private":
            await update.effective_chat.send_message(
                "I run as a DM-only bot for the lecture. Message me directly."
            )
            return False
        if allow_unauth:
            return True
        addr = self._addr(update.effective_chat.id)
        if auth_module.is_authenticated(self.workspace, addr):
            return True
        await update.effective_chat.send_message(
            "This is the class assistant. Send /auth <code> — the code is on the slide."
        )
        return False

    # ---- command handlers -------------------------------------------------

    async def _cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, allow_unauth=True):
            return
        msg = update.effective_message
        if not msg:
            return
        # Republish the menu so users who saw the old command list from a
        # previous deployment get the current one.
        await self._refresh_command_menu()
        text = (
            "Welcome to the AI-in-Business class assistant.\n\n"
            "Try one of these to get started:\n"
            "• What is a value chain, with an example from healthcare?\n"
            "• Map AI use cases to a 2x2 of effort vs. business impact.\n"
            "• Compare build vs. buy for a recommendation engine.\n\n"
            "Authenticate first: send /auth <code> using the code on the slide."
        )
        await msg.reply_text(text)

    async def _cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, allow_unauth=True):
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

    async def _cmd_auth(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, allow_unauth=True):
            return
        msg = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if not (msg and user and chat):
            return
        user_key = str(user.id)
        ok, lock_msg = self._auth_limiter.check(user_key)
        if not ok:
            await msg.reply_text(lock_msg or "Locked out.")
            return

        parts = (msg.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply_text("Send /auth <code> — the code is on the slide.")
            return
        provided = auth_module.normalize_code(parts[1])
        secret = auth_module.read_secret(self.workspace)
        if secret is None:
            await msg.reply_text("Auth is not configured yet — ask the prof to run /setsecret.")
            return
        if provided != secret.code:
            failures, locked = self._auth_limiter.record_failure(user_key)
            if locked:
                await msg.reply_text("Too many wrong codes. Locked out for the next hour.")
            else:
                await msg.reply_text(
                    f"Wrong code. ({failures}/{auth_module.RATE_LIMIT_FAILURES} tries in this window.)"
                )
            return
        addr = self._addr(chat.id)
        storage_layout.ensure_user_dirs(self.workspace, addr)
        auth_module.write_marker(self.workspace, addr, secret.code)
        self._auth_limiter.record_success(user_key)
        await msg.reply_text("Authenticated. Ask me anything.")

    async def _announce_persona_switch(
        self, addr: MessageAddress, chosen: personalities.Personality
    ) -> None:
        """Mark the persona switch in conversation history.

        The system prompt now leaves persona out (it lives in the synthetic
        tail message instead), so the only durable record of when a switch
        happened sits in the session as a SystemEvent.
        """
        await self.bus.publish_inbound(
            addr,
            SystemMessageEvent(
                content=(
                    f"User switched persona to {chosen.label}. Earlier assistant "
                    f"turns used a different voice; adopt the new persona from now on."
                )
            ),
        )

    async def _cmd_personality(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        msg = update.effective_message
        chat = update.effective_chat
        if not (msg and chat):
            return
        addr = self._addr(chat.id)
        text = (msg.text or "").split(maxsplit=1)
        if len(text) >= 2:
            name = text[1].strip().lower()
            chosen = personalities.write_personality(self.workspace, addr, name)
            if chosen is None:
                names = ", ".join(p.name for p in personalities.all_personalities())
                await msg.reply_text(f"Unknown personality. Pick one of: {names}.")
                return
            await self._announce_persona_switch(addr, chosen)
            await msg.reply_text(f"Personality set to {chosen.label}.")
            return

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(p.label, callback_data=f"p:{p.name}")]
                for p in personalities.all_personalities()
            ]
        )
        await msg.reply_text("Choose a persona:", reply_markup=keyboard)

    async def _on_callback_query(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
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
        addr = self._addr(chat.id)
        if not auth_module.is_authenticated(self.workspace, addr):
            await query.edit_message_text("Send /auth <code> first.")
            return
        chosen = personalities.write_personality(self.workspace, addr, name)
        if chosen is None:
            await query.edit_message_text("Unknown personality.")
            return
        await self._announce_persona_switch(addr, chosen)
        await query.edit_message_text(f"Personality set to {chosen.label}.")

    async def _cmd_cite(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        msg = update.effective_message
        chat = update.effective_chat
        if not (msg and chat):
            return
        st = self._user_state(chat.id)
        st.cite = not st.cite
        await msg.reply_text(f"Inline citations: {'on' if st.cite else 'off'}.")

    async def _cmd_clear(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        msg = update.effective_message
        chat = update.effective_chat
        if not (msg and chat):
            return
        addr = self._addr(chat.id)
        await self.bus.publish_inbound(addr, SessionControlEvent(action="reset"))
        st = self._user_state(chat.id)
        st.replies.clear()
        st.seen_first_citation = False
        await msg.reply_text("Conversation cleared.")

    async def _cmd_forgetme(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update, allow_unauth=True):
            return
        msg = update.effective_message
        chat = update.effective_chat
        if not (msg and chat):
            return
        addr = self._addr(chat.id)
        await self.bus.publish_inbound(addr, SessionControlEvent(action="forget"))
        st = self._user_state(chat.id)
        st.replies.clear()
        st.seen_first_citation = False
        await msg.reply_text(
            "Your storage has been deleted. Re-authenticate with /auth <code> to continue."
        )

    async def _cmd_sources(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        msg = update.effective_message
        if msg:
            await msg.reply_text("No corpus is wired up yet. Retrieval lands in a later iteration.")

    async def _cmd_scope(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        msg = update.effective_message
        if msg:
            await msg.reply_text("No corpus to scope yet. Retrieval lands in a later iteration.")

    async def _cmd_setsecret(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        user = update.effective_user
        if not (msg and user):
            return
        if not self._is_admin(user.id):
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
        record = auth_module.write_secret(self.workspace, code)
        await msg.reply_text(f"New secret: {record.code}")

    async def _cmd_whoauthed(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        user = update.effective_user
        if not (msg and user):
            return
        if not self._is_admin(user.id):
            await msg.reply_text("Admin command.")
            return
        ids = auth_module.authenticated_addresses(self.workspace, self.name)
        if not ids:
            await msg.reply_text("No authenticated users.")
            return
        await msg.reply_text(f"Authenticated ({len(ids)}): {', '.join(ids)}")

    async def _cmd_stats(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        user = update.effective_user
        if not (msg and user):
            return
        if not self._is_admin(user.id):
            await msg.reply_text("Admin command.")
            return
        active = sum(1 for st in self._users.values() if st.in_flight)
        total = len(self._users)
        await msg.reply_text(f"users seen: {total}, in-flight: {active}")

    # ---- non-command messages --------------------------------------------

    async def _on_message(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if not (message and user and chat):
            return

        st = self._user_state(chat.id)

        if not self._allow_rate(st, chat.id):
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

        if media_file and media_type and self._app:
            if not self.media_repo:
                logger.warning("Telegram received media but media_repo not configured; skipping")
            else:
                try:
                    file = await self._app.bot.get_file(media_file.file_id)
                    mime_type = getattr(media_file, "mime_type", None)
                    size_bytes = getattr(media_file, "file_size", None)
                    ext = extension_for_mime(mime_type)
                    file_path = self.media_repo.register(
                        MessageAddress(self.name, str(chat.id)),
                        sender_id=sender_id,
                        media_type=media_type,
                        ext=ext,
                        mime_type=mime_type,
                        timestamp=message.date,
                        original_name=getattr(media_file, "file_name", None),
                    )
                    await file.download_to_drive(str(file_path))
                    media_paths.append(self.media_repo.model_relpath(file_path))
                    media_metadata.append(
                        {
                            "path": str(file_path),
                            "media_type": media_type,
                            "mime_type": mime_type,
                            "size_bytes": size_bytes,
                            "saved_at": message.date.isoformat(timespec="seconds"),
                            "source_channel": self.name,
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

        await self._handle_message(
            sender_id=sender_id,
            chat_id=str(chat.id),
            content=content,
            media=media_paths,
            media_metadata=media_metadata,
            metadata=message_metadata,
            timestamp=message.date,
        )

    def _allow_rate(self, st: _UserState, chat_id: int) -> bool:
        now = time.monotonic()
        window = self.config.rate_limit_window_seconds
        while st.rate_window and now - st.rate_window[0] > window:
            st.rate_window.popleft()
        if len(st.rate_window) >= self.config.rate_limit_msgs:
            if not st.rate_blocked_warned and self._app:
                st.rate_blocked_warned = True
                asyncio.create_task(
                    self._safe_send_text(chat_id, "take a breath — too many messages just now.")
                )
            return False
        st.rate_window.append(now)
        st.rate_blocked_warned = False
        return True

    async def _safe_send_text(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> None:
        if not self._app:
            return
        try:
            kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}
            if reply_to_message_id is not None:
                kwargs["reply_to_message_id"] = reply_to_message_id
                # Don't fail if the original was deleted in the meantime.
                kwargs["allow_sending_without_reply"] = True
            if parse_mode is not None:
                kwargs["parse_mode"] = parse_mode
                # Don't let preview cards bury the citation list.
                kwargs["disable_web_page_preview"] = True
            await self._app.bot.send_message(**kwargs)
        except Exception:
            logger.debug("Failed to send text")

    # ---- outbound -- mermaid + citation aware -----------------------------

    async def send(self, msg: OutboundMessage) -> None:
        if not self._app:
            logger.warning("Telegram bot not running")
            return
        try:
            chat_id = int(msg.address.chat_id)
        except ValueError:
            logger.error(f"Invalid chat_id: {msg.address.chat_id}")
            return

        st = self._user_state(chat_id)
        # Strip <citation> markup for display, but keep the raw content so
        # the SOURCES_REACTION can re-derive citations on demand from
        # st.replies[message_id]. Same story for tool_calls.
        text, citations = cit.strip_citations(msg.content)
        raw_tool_calls = msg.metadata.get("tool_calls") if msg.metadata else None
        record = _ReplyRecord(content=msg.content, tool_calls=list(raw_tool_calls or []))

        if msg.media:
            # Outbound media (e.g. send_media tool) bypasses mermaid routing.
            sent_id = await self._send_media_message(msg, text, chat_id)
        else:
            sent_id = await self._send_text_with_mermaid(chat_id, text)

        if sent_id is not None:
            st.replies[sent_id] = record
            if citations and not st.seen_first_citation:
                st.seen_first_citation = True
                await self._safe_send_text(
                    chat_id, f"(react {SOURCES_REACTION} to any reply for sources)"
                )

    async def _send_text_with_mermaid(self, chat_id: int, text: str) -> int | None:
        blocks = mermaid_renderer.extract_blocks(text)
        # Build a list of segments: ('text', str) or ('mmd', RenderedDiagram)
        segments: list[tuple[str, Any]] = []
        if not blocks:
            segments.append(("text", text))
        else:
            cursor = 0
            kept = blocks[:2]
            for blk in kept:
                start, end = blk.span
                if start > cursor:
                    segments.append(("text", text[cursor:start]))
                rendered = await mermaid_renderer.render(
                    blk.source, self.workspace, mmdc_path=self.mermaid_mmdc_path
                )
                segments.append(("mmd", rendered))
                cursor = end
            if cursor < len(text):
                segments.append(("text", text[cursor:]))
            for extra in blocks[2:]:
                segments.append(
                    (
                        "text",
                        "\n_couldn't render this diagram, source below_\n```\n"
                        + extra.source
                        + "\n```\n",
                    )
                )

        first_msg_id: int | None = None
        for kind, payload in segments:
            if kind == "text":
                body = str(payload).strip()
                if not body:
                    continue
                pieces = _split_long(body, self.config.soft_split_chars)
                for piece in pieces:
                    sent = await self._send_html(chat_id, piece)
                    if sent and first_msg_id is None:
                        first_msg_id = sent
            else:
                rendered: mermaid_renderer.RenderedDiagram = payload
                sent = await self._send_diagram(chat_id, rendered)
                if sent and first_msg_id is None:
                    first_msg_id = sent

        return first_msg_id

    async def _send_html(self, chat_id: int, body: str) -> int | None:
        if not self._app:
            return None
        html = _markdown_to_telegram_html(body)
        try:
            sent = await self._app.bot.send_message(chat_id=chat_id, text=html, parse_mode="HTML")
            return sent.message_id
        except Exception as e:
            logger.warning(f"HTML parse failed, falling back to plain text: {e}")
            try:
                sent = await self._app.bot.send_message(chat_id=chat_id, text=body)
                return sent.message_id
            except Exception as e2:
                logger.error(f"Error sending Telegram message: {e2}")
                return None

    async def _send_diagram(
        self, chat_id: int, rendered: mermaid_renderer.RenderedDiagram
    ) -> int | None:
        if not self._app:
            return None
        if rendered.status == "ok" and rendered.png_path and rendered.png_path.is_file():
            try:
                with rendered.png_path.open("rb") as fh:
                    sent = await self._app.bot.send_photo(chat_id=chat_id, photo=fh)
                return sent.message_id
            except Exception as e:
                logger.warning(f"Failed to send mermaid PNG, falling back to source: {e}")
        body = "_couldn't render this diagram, source below_\n```\n" + rendered.source + "\n```"
        return await self._send_html(chat_id, body)

    async def _send_media_message(
        self,
        msg: OutboundMessage,
        text: str,
        chat_id: int,
    ) -> int | None:
        assert self._app
        try:
            if self.media_repo and not Path(msg.media[0]).is_absolute():
                media_path, mime = self.media_repo.resolve_file(msg.address, msg.media[0])
            else:
                media_path = Path(msg.media[0])
                if not media_path.is_absolute():
                    media_path = Path.cwd() / media_path
                if not media_path.is_file():
                    raise FileNotFoundError(f"Telegram media not found: {msg.media[0]}")
                import filetype

                mime = filetype.guess_mime(str(media_path))
            kind = (mime or "").split("/", 1)[0]
            caption = _markdown_to_telegram_html(text) if text else None
            send_kwargs = {
                "chat_id": chat_id,
                "caption": caption,
                "parse_mode": "HTML" if caption else None,
            }
            with media_path.open("rb") as fh:
                if kind == "image":
                    sent = await self._app.bot.send_photo(photo=fh, **send_kwargs)
                elif kind == "video":
                    sent = await self._app.bot.send_video(video=fh, **send_kwargs)
                elif kind == "audio":
                    sent = await self._app.bot.send_audio(audio=fh, **send_kwargs)
                else:
                    sent = await self._app.bot.send_document(document=fh, **send_kwargs)
            return sent.message_id
        except Exception as e:
            logger.error(f"Error sending Telegram media: {e}")
            return None

    # ---- typing indicator -------------------------------------------------

    async def notify_typing(self, event: TypingEvent) -> None:
        try:
            chat_id_int = int(event.address.chat_id)
        except ValueError:
            return
        st = self._user_state(chat_id_int)
        st.in_flight = bool(event.is_typing)
        if event.is_typing:
            self._start_typing(event.address.chat_id)
        else:
            self._stop_typing(event.address.chat_id)

    def _start_typing(self, chat_id: str) -> None:
        self._stop_typing(chat_id)
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    def _stop_typing(self, chat_id: str) -> None:
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _typing_loop(self, chat_id: str) -> None:
        try:
            for _ in range(8):
                if not self._app:
                    return
                await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Typing indicator stopped for {chat_id}: {e}")

    # ---- reactions --------------------------------------------------------

    async def _on_reaction(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        reaction = update.message_reaction
        if not reaction or not update.effective_chat or not update.effective_user:
            return
        if update.effective_chat.type != "private":
            return
        addr = self._addr(update.effective_chat.id)
        if not auth_module.is_authenticated(self.workspace, addr):
            return
        emojis = {r.emoji for r in (reaction.new_reaction or []) if hasattr(r, "emoji")}
        message_id = reaction.message_id
        st = self._user_state(update.effective_chat.id)
        for emoji in emojis:
            await self._dispatch_reaction(emoji, message_id, st, update.effective_chat.id)

    async def _dispatch_reaction(
        self, emoji: str, message_id: int, st: _UserState, chat_id: int
    ) -> None:
        normalized = _normalize_emoji(emoji)
        if normalized == _normalize_emoji(SOURCES_REACTION):
            await self._reaction_sources(message_id, st, chat_id)
        elif normalized == _normalize_emoji(TRACE_REACTION):
            await self._reaction_trace(message_id, st, chat_id)
        # 👍/👎/❓/🔁 reserved for future, no-op for now.

    async def _reaction_sources(self, message_id: int, st: _UserState, chat_id: int) -> None:
        if not self._app:
            return
        # All replies are threaded onto the original bot message the user
        # reacted on, so the response is anchored even after intervening
        # chatter. allow_sending_without_reply on the helper handles the case
        # where the original message has been deleted.
        record = st.replies.get(message_id)
        if record is None:
            # We don't track replies past process restart, /clear, or
            # /forgetme. Don't pretend we know more than that.
            await self._safe_send_text(
                chat_id,
                "I don't have a record of that message. It may be from a previous "
                "session or a message I didn't send.",
                reply_to_message_id=message_id,
            )
            return
        _, citations = cit.strip_citations(record.content)
        if not citations:
            await self._safe_send_text(
                chat_id,
                "That reply didn't cite any sources.",
                reply_to_message_id=message_id,
            )
            return
        kb_records = cit.extract_kb_records(record.tool_calls)
        rendered = cit.render_list(citations[:5], kb_records, fmt=cit.RenderFormat.TELEGRAM_HTML)
        await self._safe_send_text(
            chat_id,
            rendered,
            reply_to_message_id=message_id,
            parse_mode="HTML",
        )
        try:
            from telegram import ReactionTypeEmoji

            await self._app.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(SOURCES_REACTION)],
            )
        except Exception as e:
            logger.debug(f"setMessageReaction failed: {e}")

    async def _reaction_trace(self, message_id: int, st: _UserState, chat_id: int) -> None:
        record = st.replies.get(message_id)
        if record is None:
            await self._safe_send_text(
                chat_id,
                "I don't have a record of that message. It may be from a previous "
                "session or a message I didn't send.",
                reply_to_message_id=message_id,
            )
            return
        if not record.tool_calls:
            await self._safe_send_text(
                chat_id, "No tool calls for that reply.", reply_to_message_id=message_id
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
                # Agent loop no longer truncates; collapse newlines and cap
                # at display time so the bullet line stays one row.
                flat = tc.result.replace("\n", " ").strip()
                result_text = flat[:120] + "…" if len(flat) > 120 else flat
            lines.append(f"• {tc.name}({args_text}) → {result_text}")
        await self._safe_send_text(chat_id, "\n".join(lines), reply_to_message_id=message_id)

    # ---- misc -------------------------------------------------------------

    async def _on_error(self, _update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error(f"Telegram error: {context.error}")
