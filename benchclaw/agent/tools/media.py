"""Media tools for re-reading, searching, and sending stored media files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from benchclaw.agent.tools.base import Tool, ToolContext
from benchclaw.bus import MessageAddress, OutboundMessage, ToolResult


def _require_address(ctx: ToolContext) -> MessageAddress:
    if ctx.address is None:
        raise RuntimeError("Media tools require a per-conversation address on the tool context")
    return ctx.address


class ReadMediaTool(Tool):
    """Tool to re-load a media file (image or audio) into the LLM context."""

    class Params(BaseModel):
        path: str = Field(description="Sandbox-relative media path, e.g. 'media/<filename>'.")

    Params: ClassVar[type[BaseModel]] = Params

    @classmethod
    def build(cls, _config: None, _ctx: ToolContext) -> "ReadMediaTool":
        return cls()

    @property
    def name(self) -> str:
        return "read_media"

    @property
    def description(self) -> str:
        return (
            "Load a media file (image or audio) from this conversation's media store. "
            "Use the path shown in the inbound media listing, for example "
            "'media/20260504T182300-01.jpg'. Only call this when you need to examine "
            "a media file to answer a follow-up question."
        )

    async def execute(self, ctx: ToolContext, path: str, **kwargs: Any) -> ToolResult:
        if not ctx.media_repo:
            raise RuntimeError("read_media requires media repository access")
        addr = _require_address(ctx)
        _, mime_type = ctx.media_repo.resolve_file(addr, path)
        if mime_type and mime_type.startswith("audio/"):
            return [ctx.media_repo.audio_block(addr, path)]
        return [ctx.media_repo.image_block(addr, path)]


class SendMediaTool(Tool):
    """Send a stored media file to the current chat."""

    class Params(BaseModel):
        path: str = Field(description="Sandbox-relative media path, e.g. 'media/<filename>'.")
        caption: str = Field(
            default="",
            description=(
                "Optional caption/body to send with the media. "
                "Put all required user-visible text here when applicable, instead of "
                "sending a separate plain-text acknowledgement."
            ),
        )

    Params: ClassVar[type[BaseModel]] = Params

    @classmethod
    def build(cls, _config: None, _ctx: ToolContext) -> "SendMediaTool":
        return cls()

    @property
    def name(self) -> str:
        return "send_media"

    @property
    def description(self) -> str:
        return (
            "Send one stored media file (image or audio) from this conversation's "
            "media store to the current chat. Put the user-visible text in the caption "
            "rather than also saying in plain text that you sent it."
        )

    async def execute(
        self, ctx: ToolContext, path: str, caption: str = "", **_: Any
    ) -> str:
        if not ctx.bus:
            raise RuntimeError("send_media requires message bus access")
        if not ctx.media_repo:
            raise RuntimeError("send_media requires media repository access")
        addr = _require_address(ctx)
        ctx.media_repo.resolve_file(addr, path)  # validates path exists
        await ctx.bus.publish_outbound(
            OutboundMessage(address=addr, content=caption, media=[path])
        )
        return f"Media sent to {addr}"


class SearchMediaTool(Tool):
    """Search this conversation's captioned media."""

    class Params(BaseModel):
        query: str = Field(default="", description="Free-text search over captions and metadata.")
        media_type: str | None = Field(
            default=None,
            description="Optional filter by media type: 'image', 'audio', 'voice', etc.",
        )
        sender_id: str | None = Field(default=None, description="Optional sender_id filter.")
        date_from: str | None = Field(
            default=None, description="Optional inclusive lower timestamp/date bound (ISO)."
        )
        date_to: str | None = Field(
            default=None, description="Optional inclusive upper timestamp/date bound (ISO)."
        )
        limit: int = Field(default=10, ge=1, le=20, description="Maximum number of results.")

    Params: ClassVar[type[BaseModel]] = Params

    @classmethod
    def build(cls, _config: None, _ctx: ToolContext) -> "SearchMediaTool":
        return cls()

    @property
    def name(self) -> str:
        return "search_media"

    @property
    def description(self) -> str:
        return (
            "Search this conversation's captioned media (images, audio) using stored "
            "metadata and model-authored captions. Use this when you remember a media "
            "file but not its exact path."
        )

    async def execute(
        self,
        ctx: ToolContext,
        query: str = "",
        media_type: str | None = None,
        sender_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 10,
        **_: Any,
    ) -> str:
        if not ctx.media_repo:
            raise RuntimeError("search_media requires media repository access")
        addr = _require_address(ctx)
        results = ctx.media_repo.search(
            addr,
            query=query or None,
            sender_id=sender_id,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )
        if media_type:
            results = [r for r in results if r.get("media_type") == media_type]
        return json.dumps(results, ensure_ascii=False)


class AnnotateMediaTool(Tool):
    """Persist a caption or annotation for a stored media file."""

    class Params(BaseModel):
        path: str = Field(description="Sandbox-relative media path to annotate.")
        caption: str = Field(description="Searchable caption or annotation text.")

    Params: ClassVar[type[BaseModel]] = Params

    @classmethod
    def build(cls, _config: None, _ctx: ToolContext) -> "AnnotateMediaTool":
        return cls()

    @property
    def name(self) -> str:
        return "annotate_media"

    @property
    def description(self) -> str:
        return (
            "Save a concise caption or annotation for a stored media file (image or audio) "
            "in this conversation. Use this after receiving media so future turns can "
            "search or answer follow-up questions without re-reading it. For audio, "
            "include a transcript summary, tone/intent notes, and language if non-English."
        )

    async def execute(self, ctx: ToolContext, path: str, caption: str, **_: Any) -> str:
        if not ctx.media_repo:
            raise RuntimeError("annotate_media requires media repository access")
        addr = _require_address(ctx)
        ctx.media_repo.set_caption(addr, path, caption)
        return f"Saved annotation for {path}"
