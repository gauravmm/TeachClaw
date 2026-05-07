"""Outbound send pipeline.

Three concerns split across module functions, each invoked from `send`:

* :func:`plan_segments` — content shape (mermaid / media / text) → typed
  :class:`OutboundSegment`s.
* :func:`dispatch` — segments → Telegram bot API calls (yields sent ids).
* :func:`record_reply` — sent ids → reply-record map + first-citation hint.

Adding a new content type means adding a dataclass in ``state.py`` and a
match arm in ``dispatch``; nothing else moves.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from teachclaw import citations as cit
from teachclaw.bus import OutboundMessage
from teachclaw.channels.telegrm.markdown_html import markdown_to_telegram_html, split_long
from teachclaw.channels.telegrm.state import (
    SOURCES_REACTION,
    DiagramSegment,
    MediaSegment,
    OutboundSegment,
    ReplyRecord,
    TextSegment,
)
from teachclaw.rendering import mermaid as mermaid_renderer

if TYPE_CHECKING:
    from teachclaw.channels.telegrm.channel import TelegramChannel


async def send(channel: "TelegramChannel", msg: OutboundMessage) -> None:
    if not channel._app:
        logger.warning("Telegram bot not running")
        return
    try:
        chat_id = int(msg.address.chat_id)
    except ValueError:
        logger.error(f"Invalid chat_id: {msg.address.chat_id}")
        return

    text, citations = cit.strip_citations(msg.content)
    record = ReplyRecord(content=msg.content, tool_calls=list(msg.tool_calls))

    segments = await plan_segments(channel, msg, text)
    sent_ids = [mid async for mid in dispatch(channel, chat_id, segments)]
    if sent_ids:
        await record_reply(channel, chat_id, sent_ids, record, has_citations=bool(citations))


async def plan_segments(
    channel: "TelegramChannel", msg: OutboundMessage, text: str
) -> list[OutboundSegment]:
    if msg.media:
        # Outbound media (e.g. send_media tool) bypasses mermaid routing.
        media_path, mime = resolve_outbound_media(channel, msg)
        return [MediaSegment(path=media_path, mime=mime, caption=text or None)]

    blocks = mermaid_renderer.extract_blocks(text)
    if not blocks:
        return [TextSegment(body=text)] if text.strip() else []

    rendered = await mermaid_renderer.render_blocks(
        blocks, channel.workspace, mmdc_path=channel.mermaid_mmdc_path
    )
    segments: list[OutboundSegment] = []
    cursor = 0
    for blk, rd in zip(blocks, rendered, strict=True):
        start, end = blk.span
        if start > cursor and text[cursor:start].strip():
            segments.append(TextSegment(body=text[cursor:start]))
        segments.append(DiagramSegment(rendered=rd))
        cursor = end
    if cursor < len(text) and text[cursor:].strip():
        segments.append(TextSegment(body=text[cursor:]))
    return segments


def resolve_outbound_media(channel: "TelegramChannel", msg: OutboundMessage) -> tuple[Path, str]:
    """Return (absolute_path, mime). Raises FileNotFoundError if missing.
    Uses media_repo when configured; otherwise probes via filetype."""
    ref = msg.media[0]
    if channel.media_repo and not Path(ref).is_absolute():
        return channel.media_repo.resolve_file(msg.address, ref)
    media_path = Path(ref)
    if not media_path.is_absolute():
        media_path = Path.cwd() / media_path
    if not media_path.is_file():
        raise FileNotFoundError(f"Telegram media not found: {ref}")
    import filetype

    return media_path, filetype.guess_mime(str(media_path)) or ""


async def dispatch(
    channel: "TelegramChannel", chat_id: int, segments: list[OutboundSegment]
) -> AsyncIterator[int]:
    for seg in segments:
        match seg:
            case TextSegment(body=body):
                for piece in split_long(body.strip(), channel.config.soft_split_chars):
                    if not piece:
                        continue
                    sent = await post(channel, chat_id, piece, markdown=True)
                    if sent is not None:
                        yield sent
            case DiagramSegment(rendered=rd):
                sent = await post_diagram(channel, chat_id, rd)
                if sent is not None:
                    yield sent
            case MediaSegment(path=path, mime=mime, caption=caption):
                sent = await post_media(channel, chat_id, path, mime, caption)
                if sent is not None:
                    yield sent


async def record_reply(
    channel: "TelegramChannel",
    chat_id: int,
    sent_ids: list[int],
    record: ReplyRecord,
    *,
    has_citations: bool,
) -> None:
    st = channel.user_state(chat_id)
    for mid in sent_ids:
        st.replies[mid] = record
    if has_citations and not st.seen_first_citation:
        st.seen_first_citation = True
        await post(channel, chat_id, f"(react {SOURCES_REACTION} to any reply for sources)")


async def post(
    channel: "TelegramChannel",
    chat_id: int,
    body: str,
    *,
    markdown: bool = False,
    parse_mode: str | None = None,
    reply_to_message_id: int | None = None,
) -> int | None:
    """Send one Telegram text message; return the new message_id, or None on
    failure. Noops when the bot isn't running, so callers don't have to gate
    on ``channel._app``.

    ``markdown=True``: convert ``body`` from markdown to Telegram HTML and set
    ``parse_mode='HTML'``; on HTML parse error, retry with the raw body and no
    markup. ``markdown=False``: send ``body`` verbatim. Set ``parse_mode``
    explicitly when sending pre-rendered HTML (e.g. the citation listing).
    """
    if not channel._app:
        return None
    kwargs: dict[str, Any] = {"chat_id": chat_id, "text": body}
    if markdown:
        kwargs["text"] = markdown_to_telegram_html(body)
        kwargs["parse_mode"] = "HTML"
    elif parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    if kwargs.get("parse_mode") == "HTML":
        # Don't let link previews bury the message body.
        kwargs["disable_web_page_preview"] = True
    if reply_to_message_id is not None:
        kwargs["reply_to_message_id"] = reply_to_message_id
        # Don't fail if the original was deleted in the meantime.
        kwargs["allow_sending_without_reply"] = True
    try:
        sent = await channel._app.bot.send_message(**kwargs)
        return sent.message_id
    except Exception as e:
        if not markdown:
            logger.warning(f"Failed to send Telegram message: {e}")
            return None
        logger.warning(f"HTML parse failed, falling back to plain text: {e}")
        try:
            sent = await channel._app.bot.send_message(chat_id=chat_id, text=body)
            return sent.message_id
        except Exception as e2:
            logger.error(f"Error sending Telegram message: {e2}")
            return None


async def post_diagram(
    channel: "TelegramChannel", chat_id: int, rendered: mermaid_renderer.RenderedDiagram
) -> int | None:
    if not channel._app:
        return None
    if rendered.status == "ok" and rendered.png_path and rendered.png_path.is_file():
        try:
            with rendered.png_path.open("rb") as fh:
                sent = await channel._app.bot.send_photo(chat_id=chat_id, photo=fh)
            return sent.message_id
        except Exception as e:
            logger.warning(f"Failed to send mermaid PNG, falling back to source: {e}")
    return await post(
        channel, chat_id, mermaid_renderer.format_failure(rendered.source), markdown=True
    )


async def post_media(
    channel: "TelegramChannel",
    chat_id: int,
    path: Path,
    mime: str,
    caption: str | None,
) -> int | None:
    assert channel._app
    kind = mime.split("/", 1)[0] if mime else ""
    html_caption = markdown_to_telegram_html(caption) if caption else None
    send_kwargs = {
        "chat_id": chat_id,
        "caption": html_caption,
        "parse_mode": "HTML" if html_caption else None,
    }
    try:
        with path.open("rb") as fh:
            if kind == "image":
                sent = await channel._app.bot.send_photo(photo=fh, **send_kwargs)
            elif kind == "video":
                sent = await channel._app.bot.send_video(video=fh, **send_kwargs)
            elif kind == "audio":
                sent = await channel._app.bot.send_audio(audio=fh, **send_kwargs)
            else:
                sent = await channel._app.bot.send_document(document=fh, **send_kwargs)
        return sent.message_id
    except Exception as e:
        logger.error(f"Error sending Telegram media: {e}")
        return None
