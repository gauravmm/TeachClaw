"""Media tools for re-reading, sending, and annotating stored media files."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from benchclaw.agent.tools.base import Tool, ToolContext
from benchclaw.bus import MessageAddress, OutboundMessage, ToolResult


def _require_address(ctx: ToolContext) -> MessageAddress:
    if ctx.address is None:
        raise RuntimeError("Media tools require a per-conversation address on the tool context")
    return ctx.address


def _shared_roots_blurb(ctx: ToolContext) -> str:
    """Suffix for tool descriptions listing operator-configured shared roots."""
    aliases = ctx.media_repo.shared_root_aliases if ctx.media_repo else ()
    if not aliases:
        return ""
    listed = ", ".join(f"'{a}/'" for a in aliases)
    return (
        f" Operator-configured shared roots are also available (read-only): {listed}. "
        "Use them as '<alias>/<subpath>'."
    )


class ReadMediaTool(Tool):
    """Tool to re-load a media file (image or audio) into the LLM context."""

    class Params(BaseModel):
        path: str = Field(
            description=(
                "Logical media path: 'media/<filename>' for this conversation's "
                "store, or '<alias>/<subpath>' for a configured shared root."
            )
        )

    Params: ClassVar[type[BaseModel]] = Params

    def __init__(self, shared_roots_blurb: str = "") -> None:
        self._shared_roots_blurb = shared_roots_blurb

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "ReadMediaTool":
        return cls(shared_roots_blurb=_shared_roots_blurb(ctx))

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
        ) + self._shared_roots_blurb

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

    terminal_when_lone: ClassVar[bool] = True

    class Params(BaseModel):
        path: str = Field(
            description=(
                "Logical media path: 'media/<filename>' for this conversation's "
                "store, or '<alias>/<subpath>' for a configured shared root."
            )
        )
        caption: str = Field(
            default="",
            description=(
                "Optional caption/body to send with the media. "
                "Put all required user-visible text here when applicable, instead of "
                "sending a separate plain-text acknowledgement."
            ),
        )

    Params: ClassVar[type[BaseModel]] = Params

    def __init__(self, shared_roots_blurb: str = "") -> None:
        self._shared_roots_blurb = shared_roots_blurb

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "SendMediaTool":
        return cls(shared_roots_blurb=_shared_roots_blurb(ctx))

    @property
    def name(self) -> str:
        return "send_media"

    @property
    def description(self) -> str:
        return (
            "Send one stored media file (image or audio) from this conversation's "
            "media store to the current chat. The caption is delivered to the user "
            "as the message body alongside the media; do NOT also emit a plain-text "
            "reply afterward — end the turn after this call."
        ) + self._shared_roots_blurb

    async def execute(self, ctx: ToolContext, path: str, caption: str = "", **_: Any) -> str:
        if not ctx.bus:
            raise RuntimeError("send_media requires message bus access")
        if not ctx.media_repo:
            raise RuntimeError("send_media requires media repository access")
        addr = _require_address(ctx)
        ctx.media_repo.resolve_file(addr, path)  # validates path exists
        await ctx.bus.publish_outbound(OutboundMessage(address=addr, content=caption, media=[path]))
        return (
            f"Media and caption delivered to {addr}. The caption was sent as the "
            "message text — do not send a follow-up text reply this turn."
        )


class AnnotateMediaTool(Tool):
    """Persist a caption or annotation for a stored media file."""

    class Params(BaseModel):
        path: str = Field(
            description=(
                "Sandbox media path to annotate, e.g. 'media/<filename>'. "
                "Shared roots are read-only and cannot be annotated."
            )
        )
        caption: str = Field(description="Caption or annotation text to persist with the file.")

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
            "in this conversation's sandbox. Use this after receiving media so future turns "
            "can answer follow-up questions without re-reading it. For audio, include a "
            "transcript summary, tone/intent notes, and language if non-English."
        )

    async def execute(self, ctx: ToolContext, path: str, caption: str, **_: Any) -> str:
        if not ctx.media_repo:
            raise RuntimeError("annotate_media requires media repository access")
        addr = _require_address(ctx)
        ctx.media_repo.set_caption(addr, path, caption)
        return f"Saved annotation for {path}"
