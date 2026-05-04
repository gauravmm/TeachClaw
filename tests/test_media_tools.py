from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.media import SearchMediaTool, SendMediaTool
from benchclaw.bus import MessageAddress, MessageBus, OutboundMessage
from benchclaw.channels.telegrm import TelegramChannel, TelegramConfig
from benchclaw.channels.whatsapp import WhatsAppChannel, WhatsAppConfig
from benchclaw.media import MediaRepository

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG_1X1)


@pytest.mark.asyncio
async def test_send_media_uses_current_address(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    addr = MessageAddress("telegram", "123")
    path = repo.register(
        addr,
        sender_id="123",
        media_type="image",
        ext=".png",
        mime_type="image/png",
        timestamp=datetime(2026, 3, 10, 14, 23, 0),
    )
    _write_png(path)
    rel = repo.model_relpath(path)
    bus = MessageBus()
    ctx = ToolContext(workspace=tmp_path, bus=bus, media_repo=repo, address=addr)

    result = await SendMediaTool().execute(ctx, path=rel, caption="hello")
    outbound = await bus.consume_outbound(channel="telegram")

    assert result == "Media sent to telegram:123"
    assert isinstance(outbound, OutboundMessage)
    assert outbound.address == addr
    assert outbound.media == [rel]
    assert outbound.content == "hello"


@pytest.mark.asyncio
async def test_search_media_returns_only_caller_records(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    alice = MessageAddress("telegram", "chat-1")
    bob = MessageAddress("telegram", "chat-2")
    p_alice = repo.register(alice, sender_id="alice", media_type="image", ext=".png", mime_type="image/png", timestamp=datetime(2026, 3, 10, 14, 23, 0))
    p_bob = repo.register(bob, sender_id="bob", media_type="image", ext=".png", mime_type="image/png", timestamp=datetime(2026, 3, 10, 14, 24, 0))
    _write_png(p_alice)
    _write_png(p_bob)
    repo.set_caption(alice, repo.model_relpath(p_alice), "alice receipt")
    repo.set_caption(bob, repo.model_relpath(p_bob), "bob receipt")

    ctx = ToolContext(workspace=tmp_path, media_repo=repo, address=alice)
    result = await SearchMediaTool().execute(ctx, query="receipt")
    parsed = json.loads(result)

    assert [item["path"] for item in parsed] == [repo.model_relpath(p_alice)]


@pytest.mark.asyncio
async def test_search_media_filters_by_media_type(tmp_path: Path):
    repo = MediaRepository(tmp_path)
    addr = MessageAddress("telegram", "chat-1")
    img_path = repo.register(addr, sender_id="alice", media_type="image", ext=".png", mime_type="image/png", timestamp=datetime(2026, 3, 10, 14, 23, 0))
    audio_path = repo.register(addr, sender_id="alice", media_type="audio", ext=".ogg", mime_type="audio/ogg", timestamp=datetime(2026, 3, 10, 14, 24, 0))
    _write_png(img_path)
    audio_path.write_bytes(b"fake ogg")
    repo.set_caption(addr, repo.model_relpath(img_path), "a photo")
    repo.set_caption(addr, repo.model_relpath(audio_path), "a voice note")

    ctx = ToolContext(workspace=tmp_path, media_repo=repo, address=addr)
    result = await SearchMediaTool().execute(ctx, media_type="audio")
    parsed = json.loads(result)

    assert len(parsed) == 1
    assert parsed[0]["media_type"] == "audio"


class _FakeTelegramBot:
    def __init__(self) -> None:
        self.sent_photo: dict | None = None
        self.sent_text: dict | None = None

    async def send_photo(self, **kwargs):
        self.sent_photo = kwargs

    async def send_message(self, **kwargs):
        self.sent_text = kwargs


@pytest.mark.asyncio
async def test_telegram_send_photo_uses_media(tmp_path: Path):
    image = tmp_path / "media" / "out.png"
    _write_png(image)
    channel = TelegramChannel(TelegramConfig(token="x"), MessageBus(), media_repo=None)
    bot = _FakeTelegramBot()
    channel._app = type("FakeApp", (), {"bot": bot})()

    await channel.send(
        OutboundMessage(
            address=MessageAddress("telegram", "123"),
            content="caption",
            media=[str(image)],
        )
    )

    assert bot.sent_photo is not None
    assert bot.sent_photo["chat_id"] == 123
    assert bot.sent_photo["caption"] == "caption"
    assert bot.sent_text is None


class _FakeWS:
    def __init__(self) -> None:
        self.payloads: list[str] = []

    async def send(self, payload: str) -> None:
        self.payloads.append(payload)


@pytest.mark.asyncio
async def test_whatsapp_send_serializes_image_payload(tmp_path: Path):
    image = tmp_path / "media" / "out.png"
    _write_png(image)
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus(), media_repo=None)
    channel._ws = _FakeWS()
    channel._connected = True

    await channel.send(
        OutboundMessage(
            address=MessageAddress("whatsapp", "123@s.whatsapp.net"),
            content="caption",
            media=[str(image)],
        )
    )

    [payload] = channel._ws.payloads
    parsed = json.loads(payload)
    assert parsed["type"] == "send"
    assert parsed["to"] == "123@s.whatsapp.net"
    assert parsed["text"] == "caption"
    assert parsed["imageMimeType"] == "image/png"
    assert isinstance(parsed["imageBase64"], str)


@pytest.mark.asyncio
async def test_whatsapp_send_normalizes_bare_chat_id(tmp_path: Path):
    image = tmp_path / "media" / "out.png"
    _write_png(image)
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus(), media_repo=None)
    channel._ws = _FakeWS()
    channel._connected = True

    await channel.send(
        OutboundMessage(
            address=MessageAddress("whatsapp", "123"),
            content="caption",
            media=[str(image)],
        )
    )

    [payload] = channel._ws.payloads
    parsed = json.loads(payload)
    assert parsed["to"] == "123@s.whatsapp.net"


@pytest.mark.asyncio
async def test_whatsapp_send_preserves_lid_chat_id(tmp_path: Path):
    image = tmp_path / "media" / "out.png"
    _write_png(image)
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus(), media_repo=None)
    channel._ws = _FakeWS()
    channel._connected = True

    await channel.send(
        OutboundMessage(
            address=MessageAddress("whatsapp", "222355137806442@lid"),
            content="caption",
            media=[str(image)],
        )
    )

    [payload] = channel._ws.payloads
    parsed = json.loads(payload)
    assert parsed["to"] == "222355137806442@lid"


@pytest.mark.asyncio
async def test_whatsapp_inbound_preserves_direct_chat_id(tmp_path: Path):
    bus = MessageBus()
    channel = WhatsAppChannel(WhatsAppConfig(), bus, media_repo=None)
    payload = {
        "type": "message",
        "id": "m-direct",
        "chatId": "222355137806442@lid",
        "content": "hello",
        "timestamp": 1_700_000_060,
        "isGroup": False,
    }

    await channel._handle_bridge_message(json.dumps(payload))

    msg = await bus.consume_inbound(address=MessageAddress("whatsapp", "222355137806442@lid"))
    assert msg.address == MessageAddress("whatsapp", "222355137806442@lid")
