from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from benchclaw.bus import InboundMessage, MessageAddress, MessageBus, OutboundMessage
from benchclaw.channels.attention import AttentionPolicy, InboundAttentionFilter
from benchclaw.channels.base import BaseChannel, ChannelConfig
from benchclaw.channels.whatsapp import WhatsAppChannel, WhatsAppConfig
from benchclaw.channels.whatsapp.address import WhatsAppId
from benchclaw.channels.whatsapp.bridge import BridgeMessageEvent


def _ts(seconds: int) -> datetime:
    return datetime(2026, 3, 10, 0, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def test_channel_config_parses_attention_durations() -> None:
    cfg = ChannelConfig(
        attention_lookback="1h30m",
        attention_gap="2 min",
    )
    assert cfg.attention_lookback == timedelta(hours=1, minutes=30)
    assert cfg.attention_gap == timedelta(minutes=2)

    cfg2 = ChannelConfig(attention_lookback=300, attention_gap=timedelta(seconds=90))
    assert cfg2.attention_lookback == timedelta(minutes=5)
    assert cfg2.attention_gap == timedelta(seconds=90)

    cfg3 = ChannelConfig(
        attention_lookback=str(timedelta(days=1, seconds=1)),
        attention_gap=str(timedelta(minutes=2)),
    )
    assert cfg3.attention_lookback == timedelta(days=1, seconds=1)
    assert cfg3.attention_gap == timedelta(minutes=2)


def test_channel_config_serializes_attention_durations() -> None:
    cfg = ChannelConfig(
        attention_lookback=timedelta(hours=1, minutes=30),
        attention_gap=timedelta(seconds=75),
    )
    dumped = cfg.model_dump()
    assert dumped["attention_lookback"] == "1h30m"
    assert dumped["attention_gap"] == "1m15s"


def test_channel_config_rejects_negative_duration_string() -> None:
    with pytest.raises(ValueError, match="negative|greater than zero"):
        ChannelConfig(attention_lookback=str(timedelta(seconds=-1)))


def test_attention_filter_group_non_summon_dropped_when_off() -> None:
    filt = InboundAttentionFilter(
        channel="telegram",
        policy=AttentionPolicy.SUMMON_GROUP,
        lookback=timedelta(minutes=5),
        gap=timedelta(minutes=2),
    )
    out = filt.apply(
        sender_id="u1",
        chat_id="g1",
        content="hello",
        media=None,
        media_metadata=None,
        metadata={"is_group": True},
        timestamp=_ts(0),
    )
    assert out == []


def test_attention_filter_summon_replays_contiguous_history() -> None:
    filt = InboundAttentionFilter(
        channel="telegram",
        policy=AttentionPolicy.SUMMON_GROUP,
        lookback=timedelta(minutes=5),
        gap=timedelta(minutes=2),
    )
    for i, s in enumerate((0, 30), start=1):
        assert (
            filt.apply(
                sender_id="u1",
                chat_id="g1",
                content=f"m{i}",
                media=None,
                media_metadata=None,
                metadata={"is_group": True},
                timestamp=_ts(s),
            )
            == []
        )

    out = filt.apply(
        sender_id="u1",
        chat_id="g1",
        content="m3",
        media=None,
        media_metadata=None,
        metadata={"is_group": True, "summon": "mention"},
        timestamp=_ts(70),
    )
    assert [m.content for m in out] == ["m1", "m2", "m3"]
    assert [m.metadata.get("summon") for m in out] == [None, None, "mention"]


def test_attention_filter_replay_stops_at_gap() -> None:
    filt = InboundAttentionFilter(
        channel="telegram",
        policy=AttentionPolicy.SUMMON_GROUP,
        lookback=timedelta(minutes=10),
        gap=timedelta(minutes=2),
    )
    filt.apply(
        sender_id="u1",
        chat_id="g1",
        content="old-1",
        media=None,
        media_metadata=None,
        metadata={"is_group": True},
        timestamp=_ts(0),
    )
    filt.apply(
        sender_id="u1",
        chat_id="g1",
        content="old-2",
        media=None,
        media_metadata=None,
        metadata={"is_group": True},
        timestamp=_ts(30),
    )
    out = filt.apply(
        sender_id="u1",
        chat_id="g1",
        content="summon",
        media=None,
        media_metadata=None,
        metadata={"is_group": True, "summon": "reply"},
        timestamp=_ts(300),
    )
    assert [m.content for m in out] == ["summon"]


def test_attention_filter_attention_expires_after_long_gap() -> None:
    filt = InboundAttentionFilter(
        channel="telegram",
        policy=AttentionPolicy.SUMMON_GROUP,
        lookback=timedelta(minutes=5),
        gap=timedelta(minutes=2),
    )
    first = filt.apply(
        sender_id="u1",
        chat_id="g1",
        content="summon",
        media=None,
        media_metadata=None,
        metadata={"is_group": True, "summon": "mention"},
        timestamp=_ts(0),
    )
    assert [m.content for m in first] == ["summon"]

    within_gap = filt.apply(
        sender_id="u1",
        chat_id="g1",
        content="follow-up",
        media=None,
        media_metadata=None,
        metadata={"is_group": True},
        timestamp=_ts(60),
    )
    assert [m.content for m in within_gap] == ["follow-up"]

    expired = filt.apply(
        sender_id="u1",
        chat_id="g1",
        content="too-late",
        media=None,
        media_metadata=None,
        metadata={"is_group": True},
        timestamp=_ts(240),
    )
    assert expired == []


def test_attention_filter_always_policy_forwards_everything() -> None:
    filt = InboundAttentionFilter(
        channel="email",
        policy=AttentionPolicy.ALWAYS,
        lookback=timedelta(minutes=5),
        gap=timedelta(minutes=2),
    )
    out = filt.apply(
        sender_id="u1",
        chat_id="any",
        content="hello",
        media=None,
        media_metadata=None,
        metadata={"is_group": True},
        timestamp=_ts(0),
    )
    assert [m.content for m in out] == ["hello"]
    assert out[0].metadata.get("summon") is None


class _DummyChannel(BaseChannel):
    name = "dummy"

    async def send(self, msg: OutboundMessage) -> None:
        return


@pytest.mark.asyncio
async def test_allow_from_still_applies_before_publish() -> None:
    bus = MessageBus()
    cfg = ChannelConfig(allow_from=["allowed"], attention_policy=AttentionPolicy.ALWAYS)
    channel = _DummyChannel(cfg, bus)
    await channel._handle_message(sender_id="blocked", chat_id="c1", content="hello")
    assert bus.inbound == {}


@pytest.mark.asyncio
async def test_message_bus_publish_inbound_accepts_one_or_more() -> None:
    bus = MessageBus()
    address = MessageAddress(channel="dummy", chat_id="c1")

    m1 = InboundMessage(address=address, sender_id="u1", content="first", timestamp=_ts(0))
    m2 = InboundMessage(address=address, sender_id="u2", content="second", timestamp=_ts(1))
    await bus.publish_inbound(address, m1, m2)

    first = await bus.consume_inbound(address=address)
    second = await bus.consume_inbound(address=address)
    assert isinstance(first, InboundMessage)
    assert isinstance(second, InboundMessage)
    assert [first.content, second.content] == ["first", "second"]


def test_whatsapp_bridge_event_parses_typed_ids() -> None:
    event = BridgeMessageEvent.model_validate(
        {
            "type": "message",
            "id": "m-typed",
            "chatId": "222355137806442@lid",
            "content": "hello",
            "replyTo": "12025550123@s.whatsapp.net",
            "mentions": ["38818635882692@lid"],
            "botJids": ["38818635882692@lid"],
            "nameCache": {"38818635882692@lid": "benchbot"},
        }
    )

    assert event.chatId == WhatsAppId("222355137806442@lid")
    assert event.replyTo == WhatsAppId("12025550123@s.whatsapp.net")
    assert event.mentions == [WhatsAppId("38818635882692@lid")]
    assert event.botJids == [WhatsAppId("38818635882692@lid")]
    assert event.resolve_name(WhatsAppId("38818635882692@lid")) == "benchbot"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "type": "message",
                "id": "m1",
                "chatId": "12345-678@g.us",
                "content": "hello",
                "timestamp": 1_700_000_000,
                "isGroup": True,
                "botJids": ["15550001111:22@s.whatsapp.net"],
                "mentions": ["15550001111@s.whatsapp.net"],
            },
            "mention",
        ),
        (
            {
                "type": "message",
                "id": "m2",
                "chatId": "12345-679@g.us",
                "content": "hello",
                "timestamp": 1_700_000_010,
                "isGroup": True,
                "botJids": ["15550001111@s.whatsapp.net"],
                "replyTo": "15550001111:5@s.whatsapp.net",
            },
            "reply",
        ),
        (
            {
                "type": "message",
                "id": "m3",
                "chatId": "12345-680@g.us",
                "content": "@38818635882692",
                "timestamp": 1_700_000_020,
                "isGroup": True,
                "botJids": ["6580566418:2@s.whatsapp.net", "38818635882692@lid"],
                "mentions": ["38818635882692@lid"],
            },
            "mention",
        ),
    ],
)
async def test_whatsapp_bridge_mapping_sets_public_summon(
    payload: dict[str, object], expected: str
) -> None:
    bus = MessageBus()
    channel = WhatsAppChannel(WhatsAppConfig(), bus, media_repo=None)
    await channel._handle_bridge_message(json.dumps(payload))

    address = MessageAddress(channel="whatsapp", chat_id=str(payload["chatId"]))
    msg = await bus.consume_inbound(address=address)
    assert isinstance(msg, InboundMessage)
    assert msg.metadata.get("summon") == expected
    assert "_summon_source" not in msg.metadata
    assert msg.timestamp.timestamp() == pytest.approx(float(payload["timestamp"]))


@pytest.mark.asyncio
async def test_whatsapp_bridge_rewrites_bot_id_mentions_to_bot_name() -> None:
    bus = MessageBus()
    channel = WhatsAppChannel(WhatsAppConfig(), bus, media_repo=None)
    payload = {
        "type": "message",
        "id": "m4",
        "chatId": "12345-681@g.us",
        "content": "hello @38818635882692",
        "timestamp": 1_700_000_030,
        "isGroup": True,
        "botJids": ["6580566418:2@s.whatsapp.net", "38818635882692@lid"],
        "nameCache": {"38818635882692@lid": "benchbot"},
        "mentions": ["38818635882692@lid"],
    }

    await channel._handle_bridge_message(json.dumps(payload))

    address = MessageAddress(channel="whatsapp", chat_id=str(payload["chatId"]))
    msg = await bus.consume_inbound(address=address)
    assert isinstance(msg, InboundMessage)
    assert msg.content == "hello @benchbot"
    assert msg.metadata["bot_name"] == "benchbot"


@pytest.mark.asyncio
async def test_whatsapp_bridge_uses_name_cache_for_bot_name() -> None:
    bus = MessageBus()
    channel = WhatsAppChannel(WhatsAppConfig(), bus, media_repo=None)
    payload = {
        "type": "message",
        "id": "m4b",
        "chatId": "12345-681@g.us",
        "content": "hello @38818635882692",
        "timestamp": 1_700_000_035,
        "isGroup": True,
        "botJids": ["38818635882692@lid"],
        "nameCache": {"38818635882692@lid": "benchbot"},
        "mentions": ["38818635882692@lid"],
    }

    await channel._handle_bridge_message(json.dumps(payload))

    address = MessageAddress(channel="whatsapp", chat_id=str(payload["chatId"]))
    msg = await bus.consume_inbound(address=address)
    assert isinstance(msg, InboundMessage)
    assert msg.content == "hello @benchbot"
    assert msg.metadata["bot_name"] == "benchbot"


@pytest.mark.asyncio
async def test_whatsapp_bridge_rewrites_all_resolved_mentions_to_names() -> None:
    bus = MessageBus()
    channel = WhatsAppChannel(
        WhatsAppConfig(attention_policy=AttentionPolicy.ALWAYS), bus, media_repo=None
    )
    payload = {
        "type": "message",
        "id": "m5",
        "chatId": "12345-682@g.us",
        "content": "hello @38818635882692 and @12025550123",
        "timestamp": 1_700_000_040,
        "isGroup": True,
        "nameCache": {
            "38818635882692@lid": "alice",
            "12025550123@s.whatsapp.net": "bob",
        },
        "mentions": ["38818635882692@lid", "12025550123@s.whatsapp.net"],
    }

    await channel._handle_bridge_message(json.dumps(payload))

    address = MessageAddress(channel="whatsapp", chat_id=str(payload["chatId"]))
    msg = await bus.consume_inbound(address=address)
    assert isinstance(msg, InboundMessage)
    assert msg.content == "hello @alice and @bob"


@pytest.mark.asyncio
async def test_whatsapp_bridge_prefers_resolved_sender_name_for_sender_label() -> None:
    bus = MessageBus()
    channel = WhatsAppChannel(
        WhatsAppConfig(attention_policy=AttentionPolicy.ALWAYS), bus, media_repo=None
    )
    payload = {
        "type": "message",
        "id": "m6",
        "chatId": "12345-683@g.us",
        "content": "hello",
        "timestamp": 1_700_000_050,
        "isGroup": True,
        "pushName": "Push Name",
        "senderName": "Resolved Name",
    }

    await channel._handle_bridge_message(json.dumps(payload))

    address = MessageAddress(channel="whatsapp", chat_id=str(payload["chatId"]))
    msg = await bus.consume_inbound(address=address)
    assert isinstance(msg, InboundMessage)
    assert msg.metadata["sender_label"] == "Resolved Name"


@pytest.mark.asyncio
async def test_whatsapp_bridge_invalid_payload_is_dropped() -> None:
    bus = MessageBus()
    channel = WhatsAppChannel(WhatsAppConfig(), bus, media_repo=None)
    payload = {
        "type": "message",
        "id": "bad1",
        "content": "hello",
        "timestamp": 1_700_000_055,
        "isGroup": True,
    }

    await channel._handle_bridge_message(json.dumps(payload))

    assert bus.inbound == {}


@pytest.mark.asyncio
async def test_whatsapp_bridge_uses_mention_names_for_bot_rewrite() -> None:
    bus = MessageBus()
    channel = WhatsAppChannel(WhatsAppConfig(), bus, media_repo=None)
    payload = {
        "type": "message",
        "id": "m7",
        "chatId": "12345-684@g.us",
        "content": "@38818635882692 please check",
        "timestamp": 1_700_000_060,
        "isGroup": True,
        "botJids": ["38818635882692@lid"],
        "nameCache": {"38818635882692@lid": "realbot"},
        "mentions": ["38818635882692@lid"],
    }

    await channel._handle_bridge_message(json.dumps(payload))

    address = MessageAddress(channel="whatsapp", chat_id=str(payload["chatId"]))
    msg = await bus.consume_inbound(address=address)
    assert isinstance(msg, InboundMessage)
    assert msg.content == "@realbot please check"
    assert msg.metadata["bot_name"] == "realbot"
