"""Telegram channel for the lecture deployment.

Implements spec/TELEGRAM.md and integrates with spec/AUTH.md. Highlights:

- Slash commands wired through ``CommandHandler`` with auth middleware that
  short-circuits everything except ``/start``, ``/help``, ``/auth``.
- Reaction handler dispatches per-emoji from a generic table; 👀 surfaces
  source citations (stub until RAG lands), 🔍 surfaces a tool-call trace.
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
    message_map_ttl_seconds: int = 24 * 3600

    def make_channel(
        self, bus: MessageBus, media_repo: MediaRepository | None = None
    ) -> "TelegramChannel":
        return TelegramChannel(self, bus, media_repo=media_repo)

    def is_configured(self) -> bool:
        return bool(self.token.strip())


# ---------------------------------------------------------------------------
# Per-user state
# ---------------------------------------------------------------------------


@dataclass
class _MessageMapEntry:
    citations: list[dict[str, str]]  # [{"id": "...", "claim": "..."}, ...]
    tool_calls: list[ToolCallTrace]
    created_at: float


@dataclass
class _UserState:
    chat_id: int
    cite: bool = True
    in_flight: bool = False
    seen_first_citation: bool = False
    rate_window: deque[float] = field(default_factory=deque)
    rate_blocked_warned: bool = False
    last_user_message_id: int | None = None
    message_map: dict[int, _MessageMapEntry] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Markdown → Telegram HTML
# ---------------------------------------------------------------------------


_CITATION_RE = re.compile(r"<citation\s+id=\"([^\"]+)\">(.*?)</citation>", re.DOTALL)


def _strip_citations(text: str) -> tuple[str, list[dict[str, str]]]:
    citations: list[dict[str, str]] = []

    def _replace(m: re.Match) -> str:
        citations.append({"id": m.group(1), "claim": m.group(2).strip()})
        return m.group(2)

    cleaned = _CITATION_RE.sub(_replace, text)
    return cleaned, citations


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
    BotCommand("reset", "Clear conversation history for this user"),
    BotCommand("forgetme", "Delete your storage and re-auth"),
    BotCommand("sources", "List available corpora (when wired)"),
    BotCommand("scope", "Restrict retrieval (when wired)"),
)

_ADMIN_COMMANDS: tuple[BotCommand, ...] = _PUBLIC_COMMANDS + (
    BotCommand("setsecret", "Rotate the shared auth secret"),
    BotCommand("whoauthed", "List authenticated user IDs"),
    BotCommand("reload_corpus", "Re-index the corpus from disk"),
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
    ):
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self.media_repo = media_repo
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
            "reset": self._cmd_reset,
            "forgetme": self._cmd_forgetme,
            "sources": self._cmd_sources,
            "scope": self._cmd_scope,
            "setsecret": self._cmd_setsecret,
            "whoauthed": self._cmd_whoauthed,
            "reload_corpus": self._cmd_reload_corpus,
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
        if not self._app:
            return
        try:
            await self._app.bot.set_my_commands(
                commands=list(_PUBLIC_COMMANDS), scope=BotCommandScopeDefault()
            )
            for admin_id in self.config.admin_user_ids or []:
                await self._app.bot.set_my_commands(
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
            "Commands: /auth, /personality, /cite, /reset, /forgetme, /sources, /scope.\n"
            "React 👀 to one of my replies to see the source chunks; "
            "react 🔍 to see the tool-call trace for that reply."
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

    async def _cmd_reset(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        msg = update.effective_message
        chat = update.effective_chat
        if not (msg and chat):
            return
        addr = self._addr(chat.id)
        await self.bus.publish_inbound(addr, SessionControlEvent(action="reset"))
        st = self._user_state(chat.id)
        st.message_map.clear()
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
        st.message_map.clear()
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

    async def _cmd_reload_corpus(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        user = update.effective_user
        if not (msg and user):
            return
        if not self._is_admin(user.id):
            await msg.reply_text("Admin command.")
            return
        await msg.reply_text("No corpus is wired up yet.")

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

    async def _safe_send_text(self, chat_id: int, text: str) -> None:
        if not self._app:
            return
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.debug("Failed to send rate-limit notice")

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
        text, citations = _strip_citations(msg.content)
        raw_tool_calls = msg.metadata.get("tool_calls") if msg.metadata else None
        tool_calls: list[ToolCallTrace] = list(raw_tool_calls or [])

        # Outbound media (e.g. send_media tool) bypasses mermaid routing.
        if msg.media:
            await self._send_media_message(msg, text, chat_id, st, citations, tool_calls)
            return

        await self._send_text_with_mermaid(chat_id, st, text, citations, tool_calls)

    async def _send_text_with_mermaid(
        self,
        chat_id: int,
        st: _UserState,
        text: str,
        citations: list[dict[str, str]],
        tool_calls: list[ToolCallTrace],
    ) -> None:
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
                rendered = await mermaid_renderer.render(blk.source, self.workspace)
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

        if first_msg_id is not None:
            self._record_message_map(st, first_msg_id, citations, tool_calls)
            if citations and not st.seen_first_citation:
                st.seen_first_citation = True
                await self._safe_send_text(chat_id, "(react 👀 to any reply for sources)")

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
        st: _UserState,
        citations: list[dict[str, str]],
        tool_calls: list[ToolCallTrace],
    ) -> None:
        assert self._app
        try:
            if self.media_repo and not Path(msg.media[0]).is_absolute():
                image_path, mime = self.media_repo.resolve_file(msg.address, msg.media[0])
            else:
                image_path = Path(msg.media[0])
                if not image_path.is_absolute():
                    image_path = Path.cwd() / image_path
                if not image_path.is_file():
                    raise FileNotFoundError(f"Telegram image not found: {msg.media[0]}")
                import filetype

                mime = filetype.guess_mime(str(image_path))
            if not mime or not mime.startswith("image/"):
                raise ValueError(f"Telegram outbound media is not an image: {msg.media[0]}")
            caption = _markdown_to_telegram_html(text) if text else None
            with image_path.open("rb") as photo:
                sent = await self._app.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    parse_mode="HTML" if caption else None,
                )
            self._record_message_map(st, sent.message_id, citations, tool_calls)
        except Exception as e:
            logger.error(f"Error sending Telegram media: {e}")

    def _record_message_map(
        self,
        st: _UserState,
        message_id: int,
        citations: list[dict[str, str]],
        tool_calls: list[ToolCallTrace],
    ) -> None:
        now = time.time()
        ttl = self.config.message_map_ttl_seconds
        st.message_map[message_id] = _MessageMapEntry(
            citations=citations,
            tool_calls=tool_calls,
            created_at=now,
        )
        # Prune
        cutoff = now - ttl
        stale = [mid for mid, entry in st.message_map.items() if entry.created_at < cutoff]
        for mid in stale:
            st.message_map.pop(mid, None)

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
        if emoji == "👀":
            await self._reaction_sources(message_id, st, chat_id)
        elif emoji == "🔍":
            await self._reaction_trace(message_id, st, chat_id)
        # 👍/👎/❓/🔁 reserved for future, no-op for now.

    async def _reaction_sources(self, message_id: int, st: _UserState, chat_id: int) -> None:
        entry = st.message_map.get(message_id)
        if not self._app:
            return
        if entry is None:
            await self._safe_send_text(
                chat_id,
                "Sources for that reply have expired — ask again and I'll re-cite.",
            )
            return
        if not entry.citations:
            await self._safe_send_text(
                chat_id,
                "No sources for that reply (retrieval not wired yet).",
            )
            return
        lines = [
            f"[{i + 1}] {c.get('id', '?')}: {c.get('claim', '')[:200]}"
            for i, c in enumerate(entry.citations[:5])
        ]
        await self._safe_send_text(chat_id, "\n".join(lines))
        try:
            from telegram import ReactionTypeEmoji

            await self._app.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji("👀")],
            )
        except Exception as e:
            logger.debug(f"setMessageReaction failed: {e}")

    async def _reaction_trace(self, message_id: int, st: _UserState, chat_id: int) -> None:
        entry = st.message_map.get(message_id)
        if entry is None:
            await self._safe_send_text(
                chat_id,
                "Tool trace for that reply has expired.",
            )
            return
        if not entry.tool_calls:
            await self._safe_send_text(chat_id, "No tool calls for that reply.")
            return
        lines: list[str] = []
        for tc in entry.tool_calls[:10]:
            args_text = ", ".join(f"{k}={v!r}" for k, v in list(tc.arguments.items())[:3])
            if len(args_text) > 120:
                args_text = args_text[:119] + "…"
            result_text = "(pending)" if tc.result is None else tc.result[:120]
            lines.append(f"• {tc.name}({args_text}) → {result_text}")
        await self._safe_send_text(chat_id, "\n".join(lines))

    # ---- misc -------------------------------------------------------------

    async def _on_error(self, _update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error(f"Telegram error: {context.error}")
