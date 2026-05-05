"""TelegramChannel — long-polling bot orchestrator.

The class is a thin wrapper that holds runtime state (the python-telegram-bot
:class:`Application`, per-user state, the auth rate-limiter, the typing-task
table) and wires telegram-python-bot handlers to module-level functions in
sibling files (commands, reactions, outbound, ...). Each binding goes through
Python's descriptor protocol — sibling-module functions take ``channel`` as
their first argument and become bound methods on the class.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger
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
from benchclaw.bus import MessageAddress, MessageBus
from benchclaw.channels.base import BaseChannel
from benchclaw.channels.telegrm import commands, inbound, outbound, reactions, typing_loop
from benchclaw.channels.telegrm.config import TelegramConfig
from benchclaw.channels.telegrm.state import UserState
from benchclaw.media import MediaRepository


class TelegramChannel(BaseChannel):
    """Telegram channel using long polling.

    Implements spec/TELEGRAM.md and integrates with spec/AUTH.md. Highlights:

    - Slash commands wired through ``CommandHandler`` with auth middleware
      that short-circuits everything except ``/start``, ``/help``, ``/auth``.
    - Reaction handler dispatches per-emoji from the ``SOURCES_REACTION`` /
      ``TRACE_REACTION`` constants in ``state.py``; the former surfaces
      source citations, the latter surfaces a tool-call trace.
    - Per-message_id reply-record map holds raw outbound content so a
      reaction on an old reply can re-derive citations on demand.
    - Mermaid blocks in the outbound text are rendered to PNG via
      ``benchclaw.rendering.mermaid`` and posted in order.
    - Rate limits: one in-flight per user (tied to the typing indicator),
      30 msgs/10min soft cap.
    - DM-only: messages from group chats are refused with a one-line note.
    """

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
        self._users: dict[str, UserState] = {}
        self._auth_limiter = auth_module.AuthRateLimiter()

    # ---- shared accessors used by sibling modules ------------------------

    @property
    def workspace(self) -> Path:
        if self.config.workspace:
            return Path(self.config.workspace).expanduser()
        if self.media_repo is not None:
            return self.media_repo.workspace
        return Path("./workspace")

    def user_state(self, chat_id: int) -> UserState:
        key = str(chat_id)
        st = self._users.get(key)
        if st is None:
            st = UserState(chat_id=chat_id)
            self._users[key] = st
        return st

    def addr(self, chat_id: int) -> MessageAddress:
        return MessageAddress(self.name, str(chat_id))

    def is_admin(self, user_id: int) -> bool:
        return user_id in (self.config.admin_user_ids or [])

    def status(self) -> tuple[bool, str]:
        if self._app:
            return (True, "connected")
        return (False, "not connected")

    # ---- handler bindings -------------------------------------------------
    # Module-level functions taking ``self`` as the first arg become bound
    # methods automatically via the descriptor protocol — calling
    # ``instance.send(msg)`` invokes ``outbound.send(instance, msg)``.

    send = outbound.send
    notify_typing = typing_loop.notify_typing

    _on_message = inbound.on_message
    _on_reaction = reactions.on_reaction
    _on_callback_query = commands.on_callback_query

    _cmd_start = commands.cmd_start
    _cmd_help = commands.cmd_help
    _cmd_auth = commands.cmd_auth
    _cmd_personality = commands.cmd_personality
    _cmd_cite = commands.cmd_cite
    _cmd_clear = commands.cmd_clear
    _cmd_forgetme = commands.cmd_forgetme
    _cmd_sources = commands.cmd_sources
    _cmd_scope = commands.cmd_scope
    _cmd_setsecret = commands.cmd_setsecret
    _cmd_whoauthed = commands.cmd_whoauthed
    _cmd_stats = commands.cmd_stats

    # ---- background lifecycle --------------------------------------------

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

        await commands.refresh_command_menu(self)
        commands.ensure_secret_on_startup(self)

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
                typing_loop.stop_typing(self, chat_id)
            if self._app:
                logger.info("Stopping Telegram bot...")
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
                self._app = None

    async def _on_error(self, _update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error(f"Telegram error: {context.error}")
