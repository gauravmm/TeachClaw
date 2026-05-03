"""Telegram channel implementation using python-telegram-bot."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import filetype
from loguru import logger
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from benchclaw.bus import MediaMetadata, MessageAddress, MessageBus, OutboundMessage, TypingEvent
from benchclaw.channels.base import BaseChannel, ChannelConfig
from benchclaw.media import MediaRepository


class TelegramConfig(ChannelConfig):
    """Telegram channel configuration."""

    token: str = ""  # Bot token from @BotFather
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )

    def make_channel(
        self, bus: MessageBus, media_repo: MediaRepository | None = None
    ) -> "TelegramChannel":
        return TelegramChannel(self, bus, media_repo=media_repo)

    def is_configured(self) -> bool:
        return bool(self.token.strip())


def _markdown_to_telegram_html(text: str) -> str:
    """
    Convert markdown to Telegram-safe HTML.
    """
    if not text:
        return ""

    # 1. Extract and protect code blocks (preserve content from other processing)
    code_blocks: list[str] = []

    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\w]*\n?([\s\S]*?)```", save_code_block, text)

    # 2. Extract and protect inline code
    inline_codes: list[str] = []

    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", save_inline_code, text)

    # 3. Headers # Title -> just the title text
    text = re.sub(r"^#{1,6}\s+(.+)$", r"\1", text, flags=re.MULTILINE)

    # 4. Blockquotes > text -> just the text (before HTML escaping)
    text = re.sub(r"^>\s*(.*)$", r"\1", text, flags=re.MULTILINE)

    # 5. Escape HTML special characters
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 6. Links [text](url) - must be before bold/italic to handle nested cases
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    # 7. Bold **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # 8. Italic _text_ (avoid matching inside words like some_var_name)
    text = re.sub(r"(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])", r"<i>\1</i>", text)

    # 9. Strikethrough ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # 10. Bullet lists - item -> • item
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)

    # 11. Restore inline code with HTML tags
    for i, code in enumerate(inline_codes):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")

    # 12. Restore code blocks with HTML tags
    for i, code in enumerate(code_blocks):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")

    return text


class TelegramChannel(BaseChannel):
    """
    Telegram channel using long polling.

    Simple and reliable - no webhook/public IP needed.
    """

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
        self.groq_api_key = None  # TODO: Remove
        self._app: Application | None = None
        self._chat_ids: dict[str, int] = {}  # Map sender_id to chat_id for replies
        self._typing_tasks: dict[str, asyncio.Task] = {}  # chat_id -> typing loop task
        self._bot_username: str | None = None
        self._bot_user_id: int | None = None

    def status(self) -> tuple[bool, str]:
        if self._app:
            return (True, "connected")
        return (False, "not connected")

    async def background(self) -> None:
        """Start the Telegram bot with long polling."""
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return

        # Build the application with larger connection pool to avoid pool-timeout on long runs
        req = HTTPXRequest(
            connection_pool_size=16, pool_timeout=5.0, connect_timeout=30.0, read_timeout=30.0
        )
        builder = (
            Application.builder().token(self.config.token).request(req).get_updates_request(req)
        )
        if self.config.proxy:
            builder = builder.proxy(self.config.proxy).get_updates_proxy(self.config.proxy)
        self._app = builder.build()
        self._app.add_error_handler(self._on_error)

        # Add message handler for text, photos, voice, documents
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

        logger.info("Starting Telegram bot (polling mode)...")

        # Initialize and start polling
        await self._app.initialize()
        await self._app.start()

        # Get bot info and register command menu
        bot_info = await self._app.bot.get_me()
        self._bot_username = bot_info.username
        self._bot_user_id = bot_info.id
        logger.info(f"Telegram bot @{bot_info.username} connected")

        # Start polling (this runs until cancelled)
        assert self._app.updater
        await self._app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=True,  # Ignore old messages on startup
        )

        try:
            await asyncio.Future()  # Wait forever until CancelledError
        except asyncio.CancelledError:
            pass
        finally:
            # Cancel all typing indicators
            for chat_id in list(self._typing_tasks):
                self._stop_typing(chat_id)

            if self._app:
                logger.info("Stopping Telegram bot...")
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
                self._app = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram."""
        if not self._app:
            logger.warning("Telegram bot not running")
            return

        try:
            chat_id = int(msg.address.chat_id)
        except ValueError:
            logger.error(f"Invalid chat_id: {msg.address.chat_id}")
            return

        try:
            if msg.media:
                if self.media_repo and not Path(msg.media[0]).is_absolute():
                    image_path, mime = self.media_repo.resolve_file(msg.media[0])
                else:
                    image_path = Path(msg.media[0])
                    if not image_path.is_absolute():
                        image_path = Path.cwd() / image_path
                    if not image_path.is_file():
                        raise FileNotFoundError(f"Telegram image not found: {msg.media[0]}")
                    mime = filetype.guess_mime(str(image_path))
                if not mime or not mime.startswith("image/"):
                    raise ValueError(f"Telegram outbound media is not an image: {msg.media[0]}")
                caption = _markdown_to_telegram_html(msg.content) if msg.content else None
                with image_path.open("rb") as photo:
                    await self._app.bot.send_photo(
                        chat_id=chat_id,
                        photo=photo,
                        caption=caption,
                        parse_mode="HTML" if caption else None,
                    )
                return
            # Convert markdown to Telegram HTML
            html_content = _markdown_to_telegram_html(msg.content)
            await self._app.bot.send_message(chat_id=chat_id, text=html_content, parse_mode="HTML")
        except Exception as e:
            # Fallback to plain text if HTML parsing fails
            logger.warning(f"HTML parse failed, falling back to plain text: {e}")
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=msg.content)
            except Exception as e2:
                logger.error(f"Error sending Telegram message: {e2}")

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return

        message = update.message
        user = update.effective_user
        chat_id = message.chat_id

        # Use stable numeric ID, but keep username for allowlist compatibility
        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"

        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id

        # Build content from text and/or media
        content_parts = []
        media_paths = []
        media_metadata: list[MediaMetadata] = []
        str_chat_id = str(chat_id)

        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(f"caption: {message.caption}")

        # Handle media files
        media_file = None
        media_type: str | None = None

        if message.photo:
            media_file = message.photo[-1]  # Largest photo
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

        # Download media if present
        if media_file and media_type and self._app:
            if not self.media_repo:
                logger.warning("Telegram received media but media_repo not configured; skipping")
            else:
                try:
                    file = await self._app.bot.get_file(media_file.file_id)
                    mime_type = getattr(media_file, "mime_type", None)
                    size_bytes = getattr(media_file, "file_size", None)
                    ext = self._get_extension(media_type, mime_type)

                    file_path = self.media_repo.register(
                        MessageAddress(self.name, str_chat_id),
                        sender_id=sender_id,
                        media_type=media_type,
                        ext=ext,
                        mime_type=mime_type,
                        timestamp=message.date,
                        original_name=getattr(media_file, "file_name", None),
                    )
                    await file.download_to_drive(str(file_path))
                    media_paths.append(self.media_repo.media_relpath(file_path))
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

                    logger.debug(f"Downloaded {media_type} to {file_path}")
                except Exception as e:
                    logger.error(f"Failed to download media: {e}")
                    media_metadata.append(
                        {
                            "path": None,
                            "media_type": media_type,
                            "mime_type": getattr(media_file, "mime_type", None),
                            "size_bytes": getattr(media_file, "file_size", None),
                            "saved_at": None,
                            "source_channel": self.name,
                            "original_name": getattr(media_file, "file_name", None),
                        }
                    )

        content = "\n".join(content_parts) if content_parts else "[empty message]"
        summon_source = self._detect_summon_source(message)
        message_metadata = {
            "message_id": message.message_id,
            "user_id": user.id,
            "username": user.username,
            "sender_label": user.first_name or user.username,
            "is_group": message.chat.type != "private",
        }
        if summon_source:
            message_metadata["summon"] = summon_source

        logger.debug(f"Telegram message from {sender_id}: {content[:50]}...")

        # Forward to the message bus
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str_chat_id,
            content=content,
            media=media_paths,
            media_metadata=media_metadata,
            metadata=message_metadata,
            timestamp=message.date,
        )

    def _detect_summon_source(self, message: Any) -> str | None:
        reply_to_message = getattr(message, "reply_to_message", None)
        reply_author = getattr(reply_to_message, "from_user", None)
        if (
            self._bot_user_id is not None
            and reply_author is not None
            and getattr(reply_author, "id", None) == self._bot_user_id
        ):
            return "reply"

        username = self._bot_username
        if not username:
            return None
        mention_re = re.compile(rf"(?<!\w)@{re.escape(username)}\b", re.IGNORECASE)
        for maybe_text in (getattr(message, "text", None), getattr(message, "caption", None)):
            if isinstance(maybe_text, str) and mention_re.search(maybe_text):
                return "mention"
        return None

    def _start_typing(self, chat_id: str) -> None:
        """Start sending 'typing...' indicator for a chat."""
        # Cancel any existing typing task for this chat
        self._stop_typing(chat_id)
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    def _stop_typing(self, chat_id: str) -> None:
        """Stop the typing indicator for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def notify_typing(self, event: TypingEvent) -> None:
        if event.is_typing:
            self._start_typing(event.address.chat_id)
        else:
            self._stop_typing(event.address.chat_id)

    async def _typing_loop(self, chat_id: str) -> None:
        """Repeatedly send 'typing' action, capped at 8 invocations (~32s)."""
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

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log polling / handler errors instead of silently swallowing them."""
        logger.error(f"Telegram error: {context.error}")

    def _get_extension(self, media_type: str | None, mime_type: str | None) -> str:
        """Get file extension based on media type."""
        if media_type is None:
            return ""

        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "audio/ogg": ".ogg",
                "audio/mpeg": ".mp3",
                "audio/mp4": ".m4a",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]

        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "file": ""}
        return type_map.get(media_type, "")
