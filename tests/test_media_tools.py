from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from benchclaw.agent.tools.base import ToolContext
from benchclaw.agent.tools.media import AnnotateMediaTool, ReadMediaTool, SendMediaTool
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

    assert "telegram:123" in result
    assert "follow-up" in result
    assert isinstance(outbound, OutboundMessage)
    assert outbound.address == addr
    assert outbound.media == [rel]
    assert outbound.content == "hello"


@pytest.mark.asyncio
async def test_send_media_resolves_shared_root(tmp_path: Path):
    shared = tmp_path / "cuteness"
    nested = shared / "cats"
    _write_png(nested / "fluffy.png")
    repo = MediaRepository(tmp_path, shared_roots={"cuteness": shared})
    addr = MessageAddress("telegram", "123")
    bus = MessageBus()
    ctx = ToolContext(workspace=tmp_path, bus=bus, media_repo=repo, address=addr)

    result = await SendMediaTool().execute(ctx, path="cuteness/cats/fluffy.png", caption="aww")
    outbound = await bus.consume_outbound(channel="telegram")

    assert "telegram:123" in result
    assert "follow-up" in result
    assert outbound.media == ["cuteness/cats/fluffy.png"]
    assert outbound.content == "aww"


@pytest.mark.asyncio
async def test_read_media_loads_shared_root(tmp_path: Path):
    shared = tmp_path / "assets"
    _write_png(shared / "logo.png")
    repo = MediaRepository(tmp_path, shared_roots={"assets": shared})
    addr = MessageAddress("telegram", "123")
    ctx = ToolContext(workspace=tmp_path, media_repo=repo, address=addr)

    [block] = await ReadMediaTool().execute(ctx, path="assets/logo.png")

    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_send_media_rejects_traversal(tmp_path: Path):
    shared = tmp_path / "cuteness"
    shared.mkdir()
    secret = tmp_path / "secret.png"
    _write_png(secret)
    repo = MediaRepository(tmp_path, shared_roots={"cuteness": shared})
    addr = MessageAddress("telegram", "123")
    ctx = ToolContext(workspace=tmp_path, bus=MessageBus(), media_repo=repo, address=addr)

    with pytest.raises(ValueError, match="must not contain '..'"):
        await SendMediaTool().execute(ctx, path="cuteness/../secret.png")


def test_shared_root_rejects_symlink_escape(tmp_path: Path):
    shared = tmp_path / "cuteness"
    shared.mkdir()
    outside = tmp_path / "outside.png"
    _write_png(outside)
    (shared / "escape.png").symlink_to(outside)
    repo = MediaRepository(tmp_path, shared_roots={"cuteness": shared})
    addr = MessageAddress("telegram", "123")

    with pytest.raises(ValueError, match="escapes shared root"):
        repo.resolve_file(addr, "cuteness/escape.png")


def test_unknown_alias_rejected(tmp_path: Path):
    repo = MediaRepository(tmp_path, shared_roots={})
    with pytest.raises(ValueError, match="Unknown media root"):
        repo.resolve_file(MessageAddress("telegram", "123"), "nope/file.png")


def test_alias_collision_with_media_reserved():
    from benchclaw.config import MediaConfig

    with pytest.raises(ValidationError):
        MediaConfig(shared_roots={"media": "/tmp"})


def test_alias_with_slash_rejected():
    from benchclaw.config import MediaConfig

    with pytest.raises(ValidationError):
        MediaConfig(shared_roots={"a/b": "/tmp"})


@pytest.mark.asyncio
async def test_annotate_rejects_shared_path(tmp_path: Path):
    shared = tmp_path / "assets"
    _write_png(shared / "logo.png")
    repo = MediaRepository(tmp_path, shared_roots={"assets": shared})
    addr = MessageAddress("telegram", "123")
    ctx = ToolContext(workspace=tmp_path, media_repo=repo, address=addr)

    with pytest.raises(ValueError, match="read-only shared root"):
        await AnnotateMediaTool().execute(ctx, path="assets/logo.png", caption="x")


def test_shared_root_listing(tmp_path: Path):
    shared = tmp_path / "assets"
    _write_png(shared / "logo.png")
    (shared / "icons").mkdir()
    _write_png(shared / "icons" / "a.png")
    repo = MediaRepository(tmp_path, shared_roots={"assets": shared})

    listing = repo.shared_root_listing()
    assert listing is not None
    assert listing.startswith("assets/:")
    assert "icons/ (1 item)" in listing
    assert "logo.png" in listing


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
