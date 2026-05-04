"""Explicit built-in tool manifest."""

from benchclaw.agent.tools.base import Tool
from benchclaw.agent.tools.cron.tool import CronTool
from benchclaw.agent.tools.filesystem import (
    EditFileTool,
    GlobTool,
    GrepTool,
    ReadFileTool,
    WriteFileTool,
)
from benchclaw.agent.tools.media import (
    AnnotateMediaTool,
    ReadMediaTool,
    SearchMediaTool,
    SendMediaTool,
)
from benchclaw.agent.tools.shell import ExecTool
from benchclaw.agent.tools.web import WebFetchTool, WebSearchTool

BUILTIN_TOOLS: tuple[tuple[str, type[Tool]], ...] = (
    ("cron", CronTool),
    ("read_file", ReadFileTool),
    ("write_file", WriteFileTool),
    ("edit_file", EditFileTool),
    ("glob", GlobTool),
    ("grep", GrepTool),
    ("read_media", ReadMediaTool),
    ("annotate_media", AnnotateMediaTool),
    ("send_media", SendMediaTool),
    ("search_media", SearchMediaTool),
    ("exec", ExecTool),
    ("web_search", WebSearchTool),
    ("web_fetch", WebFetchTool),
)
