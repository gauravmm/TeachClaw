"""Media tools for re-reading, searching, and sending stored media files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from benchclaw.agent.tools.base import Tool, ToolContext
from benchclaw.bus import MessageAddress, OutboundMessage, ToolResult
from benchclaw.media import MediaRepository


def _resolve_target_address(ctx: ToolContext, address: str | None) -> MessageAddress | None:
    """Resolve an explicit or implicit target address."""
    return MessageAddress.from_string(address) if address else ctx.address


class ReadMediaTool(Tool):
    """Tool to re-load a media file (image or audio) into the LLM context."""

    class Params(BaseModel):
        path: str = Field(description="Workspace-relative path to the media file.")

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
            "Load a workspace media file (image or audio) by its relative path so you can inspect or re-listen to it. "
            "Use the exact stored path, for example 'media/a3f7b2c1/0310/1423-01.jpg' or 'media/a3f7b2c1/0310/1423-01.ogg'. "
            "Only call this when you need to examine a media file to answer a follow-up question."
        )

    async def execute(self, ctx: ToolContext, path: str, **kwargs: Any) -> ToolResult:
        if Path(path).is_absolute():
            raise ValueError(f"Path is outside the workspace: {path}")
        media_repo = ctx.media_repo or MediaRepository(ctx.workspace)
        _, mime_type = media_repo.resolve_file(path)
        if mime_type and mime_type.startswith("audio/"):
            return [media_repo.audio_block(path)]
        return [media_repo.image_block(path)]


class SendMediaTool(Tool):
    """Send a stored workspace media file to the current or another chat."""

    class Params(BaseModel):
        path: str = Field(description="Workspace-relative media path.")
        caption: str = Field(
            default="",
            description=(
                "Optional caption/body to send with the media. "
                "Put all required user-visible text here when applicable, instead of sending a separate plain-text acknowledgement."
            ),
        )
        address: str | None = Field(
            default=None,
            description=(
                "Optional target address as channel:chat_id. Defaults to the current chat; "
                "omit this when sending to the current chat."
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
            "Send one stored workspace media file (image or audio) to the current chat by default, or to another chat "
            "when you provide an explicit address like 'telegram:12345'. "
            "When sending media, put the user-visible text in the caption/body rather than also saying in plain text that you sent it. "
            "Strongly prefer omitting `address` when sending to the current chat."
        )

    async def execute(
        self, ctx: ToolContext, path: str, caption: str = "", address: str | None = None, **_: Any
    ) -> str:
        if not ctx.bus:
            raise RuntimeError("send_media requires message bus access")
        target = _resolve_target_address(ctx, address)
        if target is None:
            raise ValueError("No target address available")

        if Path(path).is_absolute():
            raise ValueError(f"Path is outside the workspace: {path}")
        media_repo = ctx.media_repo or MediaRepository(ctx.workspace)
        media_repo.resolve_file(path)  # validates path exists

        await ctx.bus.publish_outbound(
            OutboundMessage(address=target, content=caption, media=[path])
        )
        return f"Media sent to {target}"


class SearchMediaTool(Tool):
    """Search stored media using saved metadata and captions."""

    class Params(BaseModel):
        query: str = Field(default="", description="Free-text search over captions and metadata.")
        media_type: str | None = Field(
            default=None,
            description="Optional filter by media type: 'image', 'audio', 'voice', etc.",
        )
        address: str | None = Field(
            default=None, description="Optional address filter as channel:chat_id."
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
            "Search captioned workspace media (images, audio) using stored metadata and model-authored captions. "
            "Use this when you remember a media file but not its exact path."
        )

    async def execute(
        self,
        ctx: ToolContext,
        query: str = "",
        media_type: str | None = None,
        address: str | None = None,
        sender_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 10,
        **_: Any,
    ) -> str:
        if not ctx.media_repo:
            raise RuntimeError("search_media requires media repository access")
        resolved_address = MessageAddress.from_string(address) if address else None

        results = ctx.media_repo.search(
            query=query or None,
            address=resolved_address,
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
        path: str = Field(description="Workspace-relative media path to annotate.")
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
            "Save a concise caption or annotation for a stored workspace media file (image or audio). "
            "Use this after receiving media so future turns can search or answer follow-up questions without re-reading it. "
            "For audio, include a transcript summary, tone/intent notes, and language if non-English."
        )

    async def execute(self, ctx: ToolContext, path: str, caption: str, **_: Any) -> str:
        if not ctx.media_repo:
            raise RuntimeError("annotate_media requires media repository access")
        ctx.media_repo.set_caption(path, caption)
        return f"Saved annotation for {path}"
