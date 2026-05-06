"""Explicit built-in tool manifest."""

from teachclaw.agent.tools.base import Tool
from teachclaw.agent.tools.cron.tool import CronTool
from teachclaw.agent.tools.filesystem import (
    EditFileTool,
    GlobTool,
    GrepTool,
    ReadFileTool,
    WriteFileTool,
)
from teachclaw.agent.tools.media import (
    AnnotateMediaTool,
    ReadMediaTool,
    SendMediaTool,
)
from teachclaw.agent.tools.shell import ExecTool
from teachclaw.agent.tools.web import WebFetchTool, WebSearchTool

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
    ("exec", ExecTool),
    ("web_search", WebSearchTool),
    ("web_fetch", WebFetchTool),
)
