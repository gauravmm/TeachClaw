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
from benchclaw.agent.tools.message import MessageTool
from benchclaw.agent.tools.shell import ExecTool, ExecToolConfig
from benchclaw.agent.tools.web import WebFetchTool, WebSearchConfig, WebSearchTool

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
    ("message", MessageTool),
    ("exec", ExecTool),
    ("web_search", WebSearchTool),
    ("web_fetch", WebFetchTool),
)

TOOL_CONFIG_TYPES = {
    "exec": ExecToolConfig,
    "web_search": WebSearchConfig,
}
