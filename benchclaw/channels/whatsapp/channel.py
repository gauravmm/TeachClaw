"""WhatsApp channel implementation using the Node.js bridge."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import filetype
import websockets
from loguru import logger
from pydantic import ValidationError

from benchclaw.bus import MediaMetadata, MessageAddress, MessageBus, OutboundMessage, TypingEvent
from benchclaw.channels.base import BaseChannel, ChannelConfig
from benchclaw.channels.whatsapp.address import (
    WhatsAppId,
)
from benchclaw.channels.whatsapp.bridge import (
    BRIDGE_EVENT_ADAPTER,
    BridgeErrorEvent,
    BridgeMessageEvent,
    BridgeQrEvent,
    BridgeSentEvent,
    BridgeStatusEvent,
)
from benchclaw.media import MediaRepository, extension_for_mime
from benchclaw.utils import now_aware, parse_optional_timestamp


class WhatsAppConfig(ChannelConfig):
    """WhatsApp channel configuration."""

    bridge_url: str = "ws://localhost:3001"
    bridge_token: str = ""  # Shared token for bridge auth (optional, recommended)

    def make_channel(
        self, bus: MessageBus, media_repo: MediaRepository | None = None, **_: Any
    ) -> "WhatsAppChannel":
        return WhatsAppChannel(self, bus, media_repo=media_repo)

    def is_configured(self) -> bool:
        return bool(self.bridge_url.strip())


class WhatsAppChannel(BaseChannel):
    """
    WhatsApp channel that connects to a Node.js bridge.

    The bridge uses @whiskeysockets/baileys to handle the WhatsApp Web protocol.
    Communication between Python and Node.js is via WebSocket.
    """

    name = "whatsapp"

    def __init__(
        self, config: WhatsAppConfig, bus: MessageBus, media_repo: MediaRepository | None = None
    ):
        super().__init__(config, bus)
        self.config: WhatsAppConfig = config
        self.media_repo = media_repo
        self._ws = None
        self._connected = False

    def status(self) -> tuple[bool, str]:
        return (
            self._connected,
            f"bridge {'connected' if self._connected else 'disconnected'} ({self.config.bridge_url})",
        )

    async def background(self) -> None:
        """Start the WhatsApp channel by connecting to the bridge."""
        logger.info(f"Connecting to WhatsApp bridge at {self.config.bridge_url}...")
        while True:
            try:
                async with websockets.connect(self.config.bridge_url) as ws:
                    self._ws = ws
                    if self.config.bridge_token:
                        await ws.send(
                            json.dumps({"type": "auth", "token": self.config.bridge_token})
                        )
                    self._connected = True
                    logger.info("Connected to WhatsApp bridge")

                    async for message in ws:
                        try:
                            await self._handle_bridge_message(str(message))
                        except Exception as e:
                            logger.error(f"Error handling bridge message: {e}")

            except Exception as e:
                logger.warning(f"WhatsApp bridge connection error: {e}. Retrying...")
                await asyncio.sleep(5)

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WhatsApp."""
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return

        try:
            payload = {
                "type": "send",
                "to": WhatsAppId.from_raw(msg.address.chat_id).outbound_jid(),
                "text": msg.content,
            }
            if msg.media:
                media_path = msg.media[0]
                if self.media_repo and not Path(media_path).is_absolute():
                    image_path, mime = self.media_repo.resolve_file(msg.address, media_path)
                else:
                    image_path = Path(media_path)
                    if not image_path.is_absolute():
                        image_path = Path.cwd() / image_path
                    if not image_path.is_file():
                        raise FileNotFoundError(f"WhatsApp image not found: {media_path}")
                    mime = filetype.guess_mime(str(image_path))
                if not mime or not mime.startswith("image/"):
                    raise ValueError(f"WhatsApp outbound media is not an image: {media_path}")
                payload["imageBase64"] = base64.b64encode(image_path.read_bytes()).decode()
                payload["imageMimeType"] = mime
            await self._ws.send(json.dumps(payload))
        except Exception as e:
            logger.error(f"Error sending WhatsApp message: {e}")

    async def notify_typing(self, event: TypingEvent) -> None:
        if not self._ws or not self._connected:
            return
        try:
            await self._ws.send(
                json.dumps(
                    {
                        "type": "typing",
                        "to": WhatsAppId.from_raw(event.address.chat_id).outbound_jid(),
                        "is_typing": event.is_typing,
                    }
                )
            )
        except Exception as e:
            logger.debug(f"Failed to send typing indicator: {e}")

    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a message from the bridge."""
        try:
            event = BRIDGE_EVENT_ADAPTER.validate_json(raw)
        except ValidationError as e:
            logger.warning(
                f"Invalid WhatsApp bridge payload: {e.errors()[0]['msg']}; raw={raw[:160]}"
            )
            return

        match event:
            case BridgeMessageEvent():
                await self._handle_bridge_inbound(event)
            case BridgeStatusEvent():
                logger.info(f"WhatsApp status: {event.status}")
                self._connected = event.status == "connected"
            case BridgeQrEvent():
                logger.error("Scan QR code in the bridge terminal to connect WhatsApp")
            case BridgeErrorEvent():
                logger.error(f"WhatsApp bridge error: {event.error}")
            case BridgeSentEvent():
                pass

    async def _handle_bridge_inbound(self, event: BridgeMessageEvent) -> None:
        chat_id = str(event.chatId)
        sender_id = event.chatId.localpart
        content = self._replace_mentions(event.content, event)
        source_ts = parse_optional_timestamp(event.timestamp)
        summon_source = self._detect_summon_source(event)

        media_metadata = [
            item.to_media_metadata(source_channel=self.name) for item in event.mediaMetadata
        ]
        media_paths = self._save_bridge_media(event, sender_id, source_ts, media_metadata)

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            media=media_paths or None,
            media_metadata=media_metadata,
            metadata=self._message_metadata(event, sender_id, summon_source),
            timestamp=source_ts,
        )

    def _replace_mentions(self, content: str, payload: BridgeMessageEvent) -> str:
        for person_id in payload.mentions or []:
            name = payload.resolve_name(person_id) or ""
            if name and person_id.localpart:
                replacement = name if name.startswith("@") else f"@{name}"
                content = re.sub(
                    rf"(?<!\w)@{re.escape(person_id.localpart)}\b", replacement, content
                )
        return content

    def _detect_summon_source(
        self, payload: BridgeMessageEvent
    ) -> Literal["mention", "reply"] | None:
        bot_jids = set(payload.botJids or [])
        if not payload.isGroup or not bot_jids:
            return None
        if payload.replyTo and payload.replyTo in bot_jids:
            return "reply"
        if any(item in bot_jids for item in payload.mentions or []):
            return "mention"
        return None

    def _message_metadata(
        self,
        event: BridgeMessageEvent,
        sender_id: str,
        summon_source: Literal["mention", "reply"] | None,
    ) -> dict[str, str | int | float | None | bool]:
        metadata: dict[str, str | int | float | None | bool] = {
            "message_id": event.id,
            "timestamp": event.timestamp,
            "is_group": event.isGroup,
            "sender_label": event.senderName or event.pushName or sender_id,
            "bot_name": next(
                (
                    resolved
                    for item in event.botJids or []
                    if (resolved := event.resolve_name(item))
                ),
                None,
            ),
        }
        if metadata["bot_name"] is None:
            metadata.pop("bot_name")
        if summon_source:
            metadata["summon"] = summon_source
        return metadata

    def _save_bridge_media(
        self,
        event: BridgeMessageEvent,
        sender_id: str,
        source_ts: datetime | None,
        media_metadata: list[MediaMetadata],
    ) -> list[str]:
        if not event.mediaBase64:
            return []
        if not self.media_repo:
            logger.warning("WhatsApp received media but media_repo not configured; skipping")
            return []

        try:
            mime_type = event.mediaType or "application/octet-stream"
            ext = extension_for_mime(mime_type) or ".bin"
            # Derive broad media_type from the MIME prefix, falling back to the
            # existing metadata value so voice/audio distinction is preserved.
            mime_prefix = mime_type.split("/")[0]
            if mime_prefix in {"image", "audio", "video"}:
                media_type = mime_prefix
            elif media_metadata:
                media_type = media_metadata[0].get("media_type") or "file"
            else:
                media_type = "file"
            file_path = self.media_repo.register(
                MessageAddress("whatsapp", event.chatId.comparable_id),
                sender_id=sender_id,
                media_type=media_type,
                ext=ext,
                mime_type=mime_type,
                timestamp=source_ts,
                original_name=media_metadata[0].get("original_name") if media_metadata else None,
            )
            file_path.write_bytes(base64.b64decode(event.mediaBase64))
            saved_at = now_aware().isoformat(timespec="seconds")
            if media_metadata:
                media_metadata[0]["path"] = str(file_path)
                media_metadata[0]["saved_at"] = saved_at
            else:
                media_metadata.append(
                    {
                        "path": str(file_path),
                        "media_type": media_type,
                        "mime_type": mime_type,
                        "size_bytes": None,
                        "saved_at": saved_at,
                        "source_channel": self.name,
                    }
                )
            logger.debug(f"Saved WhatsApp media to {file_path}")
            return [self.media_repo.model_relpath(file_path)]
        except Exception as e:
            logger.error(f"Failed to save WhatsApp media: {e}")
            return []
