"""Tool registry: manages tool lifecycle and execution."""

import asyncio
import contextlib
from collections.abc import Iterable
from typing import Any, Self

from benchclaw.agent.tools.base import Tool, ToolContext
from benchclaw.agent.tools.builtins import BUILTIN_TOOLS
from benchclaw.agent.tools.mcp_manager import MCPManager
from benchclaw.bus import ToolResult


class ToolRegistry:
    """
    Registry for agent tools.

    Manages tool construction, lifecycle (background tasks and async context
    managers), and execution. Enter as an async context manager to start all
    tool background() tasks and enter any tool async context managers.
    Raises RuntimeError if entered more than once on the same instance.
    """

    def __init__(self, tools_config: Any, ctx: ToolContext, mcp_manager: MCPManager | None = None):
        self._tools: dict[str, Tool] = {}
        self._master_ctx = ctx
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
            if type(tool).background is not Tool.background:
                tool._task = asyncio.create_task(tool.background(self._master_ctx), name=tool.name)
        if self._mcp_manager:
            await self._exit_stack.enter_async_context(self._mcp_manager)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        for tool in self._tools.values():
            if tool._task:
                tool._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(tool._task), timeout=5.0)
                tool._task = None
        await self._exit_stack.__aexit__(*exc_info)
        self._running = False

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
