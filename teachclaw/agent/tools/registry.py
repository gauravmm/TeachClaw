"""Tool registry: manages tool lifecycle and execution."""

import contextlib
from collections.abc import Iterable
from typing import Any, Self

from teachclaw.agent.tools.base import Tool, ToolContext
from teachclaw.agent.tools.builtins import BUILTIN_TOOLS
from teachclaw.agent.tools.mcp_manager import MCPManager
from teachclaw.bus import ToolResult


class ToolRegistry:
    """
    Registry for agent tools.

    Manages tool construction and execution. Enter as an async context
    manager to enter any tool async context managers and the MCP manager.
    Raises RuntimeError if entered more than once on the same instance.
    """

    def __init__(self, tools_config: Any, ctx: ToolContext, mcp_manager: MCPManager | None = None):
        self._tools: dict[str, Tool] = {}
        self._mcp_manager = mcp_manager
        self._running = False
        self._exit_stack = contextlib.AsyncExitStack()

        for name, tool_cls in BUILTIN_TOOLS:
            tool_config = getattr(tools_config, name, None)
            if tool_config is not None and getattr(tool_config, "enabled", True) is False:
                continue
            tool = tool_cls.build(tool_config, ctx)
            self._tools[tool.name] = tool

    async def __aenter__(self) -> Self:
        if self._running:
            raise RuntimeError(
                "ToolRegistry is already running; cannot enter the same instance twice"
            )
        self._running = True
        await self._exit_stack.__aenter__()
        for tool in self._tools.values():
            if hasattr(tool, "__aenter__"):
                await self._exit_stack.enter_async_context(tool)  # type: ignore[arg-type]
        if self._mcp_manager:
            await self._exit_stack.enter_async_context(self._mcp_manager)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._exit_stack.__aexit__(*exc_info)
        self._running = False

    def get(self, name: str) -> Tool | None:
        """Look up a tool by name. Returns None if not registered."""
        return self._tools.get(name)

    def values(self) -> Iterable[Tool]:
        """Iterate over registered tools."""
        return self._tools.values()

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions in OpenAI format."""
        defs = [tool.to_schema() for tool in self.values()]
        if self._mcp_manager:
            defs.extend(self._mcp_manager.get_definitions())
        return defs

    async def execute(self, name: str, params: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Execute a tool by name with given parameters and call context."""
        if self._mcp_manager and name in self._mcp_manager:
            try:
                return await self._mcp_manager.execute(name, params)
            except Exception as e:
                return f"Error executing MCP tool '{name}': {e}"
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found"
        try:
            from pydantic import ValidationError

            try:
                validated = tool.Params.model_validate(params)
            except ValidationError as e:
                summary = "; ".join(
                    f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
                    for err in e.errors()
                )
                return f"Error: Invalid parameters for tool '{name}': {summary}"
            return await tool.execute(ctx, **validated.model_dump(exclude_none=False))
        except Exception as e:
            return f"Error executing {name}: {str(e)}"

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools or (self._mcp_manager is not None and name in self._mcp_manager)

    def is_terminal_when_lone(self, name: str) -> bool:
        """True if a turn whose only tool call is ``name`` should not be nudged."""
        tool = self._tools.get(name)
        return bool(tool and tool.terminal_when_lone)
