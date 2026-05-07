"""Utility functions for teachclaw."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, TypeAlias

from pydantic import BeforeValidator, PlainSerializer
from pytimeparse.timeparse import timeparse

if TYPE_CHECKING:
    from teachclaw.bus import MessageAddress


def parse_duration(value: timedelta | int | float | str, positive: bool = True) -> timedelta:
    """Parse duration from timedelta, numeric seconds, or pytimeparse-compatible text."""
    assert not isinstance(value, bool), "Duration must not be a boolean."
    if isinstance(value, timedelta):
        result = value

    elif isinstance(value, int | float):
        assert math.isfinite(value), "Duration must be finite."
        result = timedelta(seconds=float(value))

    elif isinstance(value, str):
        parsed_seconds = timeparse(value)
        assert parsed_seconds is not None, "pytimeparse parsing failed."
        result = timedelta(seconds=parsed_seconds)

    assert not positive or result > timedelta(0), "Duration must be greater than zero."
    return result


def format_duration(delta: timedelta) -> str:
    """Format duration in compact human-readable form (e.g. 30m, 2h, 45s)."""
    total_seconds = delta.total_seconds()
    if not total_seconds.is_integer():
        return f"{total_seconds}s"

    seconds = int(total_seconds)
    sign = "-" if seconds < 0 else ""
    remaining = abs(seconds)
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, remaining = divmod(remaining, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    if remaining or not parts:
        parts.append(f"{remaining}s")
    return sign + "".join(parts)


DurationField: TypeAlias = Annotated[
    timedelta,
    BeforeValidator(parse_duration),
    PlainSerializer(format_duration),
]


def local_timezone():
    """Return the process-local timezone object."""
    tz = datetime.now().astimezone().tzinfo
    assert tz is not None
    return tz


def now_aware() -> datetime:
    """Return the current time as a timezone-aware datetime in local timezone."""
    return datetime.now(local_timezone())


def ensure_aware(value: datetime) -> datetime:
    """Coerce a datetime to a timezone-aware value in local timezone."""
    if value.tzinfo is None:
        return value.replace(tzinfo=local_timezone())
    return value.astimezone(local_timezone())


def _encode_timestamp(dt: datetime | None) -> str | None:
    return None if dt is None else ensure_aware(dt).isoformat(timespec="seconds")


def _parse_timestamp(value: datetime | str | int | float) -> datetime:
    """Parse datetime, ISO string, or Unix seconds into an aware datetime in system timezone."""
    if isinstance(value, datetime):
        return ensure_aware(value)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=local_timezone())
    return ensure_aware(datetime.fromisoformat(value))


def parse_optional_timestamp(value: datetime | str | int | float | None) -> datetime | None:
    """Parse an optional timestamp into an aware datetime in system timezone."""
    if value is None:
        return None
    return _parse_timestamp(value)


TimestampSerializer: TypeAlias = Annotated[
    datetime,
    BeforeValidator(_parse_timestamp),
    PlainSerializer(_encode_timestamp),
]


OptionalTimestampSerializer: TypeAlias = Annotated[
    datetime | None,
    BeforeValidator(parse_optional_timestamp),
    PlainSerializer(_encode_timestamp),
]


def parse_optional_message_address(value: MessageAddress | dict | None) -> MessageAddress | None:
    """Parse optional MessageAddress from object/dict form."""
    from teachclaw.bus import MessageAddress

    if value is None or isinstance(value, MessageAddress):
        return value
    return MessageAddress(**value)


def _encode_message_address(value: MessageAddress | None) -> dict | None:
    return None if value is None else {"channel": value.channel, "chat_id": value.chat_id}


MessageAddressField: TypeAlias = Annotated[
    Any,
    BeforeValidator(parse_optional_message_address),
    PlainSerializer(_encode_message_address),
]
