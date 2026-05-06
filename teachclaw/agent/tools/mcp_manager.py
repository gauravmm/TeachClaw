"""MCP (Model Context Protocol) server manager for teachclaw."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal

from loguru import logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from pydantic import BaseModel, model_validator


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server."""

    name: str
    transport: Literal["stdio", "http"]

    # stdio fields
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}

    # http field
    url: str | None = None

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "MCPServerConfig":
        if self.transport == "stdio" and not self.command:
            raise ValueError("MCP stdio server requires 'command'")
        if self.transport == "http" and not self.url:
            raise ValueError("MCP http server requires 'url'")
        return self


class _MCPLiveConnection:
    """Runtime state for one live MCP session.

    This object is intentionally single-use: each instance may be started at
    most once. `_MCPServerSlot` creates a fresh `_MCPLiveConnection` for every
    reconnect attempt instead of trying to restart an old one in place.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        on_exit: Callable[["_MCPLiveConnection"], None] | None = None,
    ) -> None:
        self.config = config
        self.session: ClientSession | None = None
        self.tools: list[Any] = []
        self._task: asyncio.Task[None] | None = None
        self._on_exit = on_exit

    @asynccontextmanager
    async def _create_transport(self) -> AsyncIterator[tuple[Any, Any]]:
        """Yield a normalized `(read, write)` transport pair for this connection."""
        if self.config.transport == "stdio":
            params = StdioServerParameters(
                command=self.config.command,  # type: ignore[arg-type]
                args=self.config.args,
                env=self.config.env or None,
            )
            async with stdio_client(params) as transport:
                read, write = transport
                yield read, write
        elif self.config.transport == "http":
            assert self.config.url is not None
            async with streamable_http_client(self.config.url) as transport:
                read, write, *_ = transport
                yield read, write
        else:
            raise ValueError(f"unsupported MCP transport: {self.config.transport}")

    async def __aenter__(self) -> "_MCPLiveConnection":
        """Start the background task that owns this live connection.

        Instances are single-use by design. Reconnects should allocate a new
        `_MCPLiveConnection` rather than entering the same instance twice.
        """
        if self._task:
            raise RuntimeError(f"MCP connection '{self.config.name}' can only be started once")

        startup_future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._run(startup_future), name=f"mcp:{self.config.name}")
        self._task = task

        try:
            await startup_future
        except Exception:
            try:
                await task
            except Exception:
                pass
            raise

        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        """Stop the task that owns this live connection."""
        try:
            if self._task:
                if not self._task.done():
                    self._task.cancel()
                await self._task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"MCP: error while stopping '{self.config.name}': {e}")

    async def _run(self, startup_future: asyncio.Future[None]) -> None:
        """Own this connection for its full lifetime in a single task."""
        try:
            async with self._create_transport() as (read, write):
                async with ClientSession(read, write) as session:
                    self.session = session
                    await session.initialize()
                    self.tools = list((await session.list_tools()).tools)

                    # Signal successful startup to the `__aenter__()` method, if it's still waiting.
                    if not startup_future.done():
                        startup_future.set_result(None)

                    logger.info(f"MCP: connected to '{self.config.name}', {len(self.tools)} tools")

                    await asyncio.Future()
        except Exception as e:
            # Signal startup failure to the `__aenter__()` method, if it's still waiting.
            if not startup_future.done():
                startup_future.set_exception(e)
            raise
        finally:
            self.tools = []
            self.session = None
            if self._task is asyncio.current_task():
                self._task = None
            if self._on_exit is not None:
                try:
                    self._on_exit(self)
                except Exception as e:
                    logger.debug(f"MCP: error while handling exit for '{self.config.name}': {e}")

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        assert self.session is not None, "MCP session is not connected"
        return await self.session.call_tool(tool_name, arguments=arguments)


class _MCPServerSlot:
    """Restartable controller for one configured MCP server."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.connection: _MCPLiveConnection | None = None
        self._known_tools: list[Any] = []
        self.lock = asyncio.Lock()

    def get_tools(self) -> list[Any]:
        return self.connection.tools if self.connection else self._known_tools

    def has_tool(self, tool_name: str) -> bool:
        return any(tool.name == tool_name for tool in self.get_tools())

    def _handle_connection_exit(self, connection: _MCPLiveConnection) -> None:
        asyncio.create_task(
            self._clear_connection_if_current(connection),
            name=f"mcp-clear:{self.config.name}",
        )

    async def _clear_connection_if_current(self, connection: _MCPLiveConnection) -> None:
        async with self.lock:
            if self.connection is connection:
                self.connection = None

    async def _drop_connection(self) -> None:
        """Stop and clear the current live connection, if any."""
        connection, self.connection = self.connection, None
        if connection is not None:
            await connection.__aexit__(None, None, None)

    async def _connect_with_retries(
        self, reason: str, attempts: int = 3, delay: float = 2.0
    ) -> None:
        """Connect or reconnect this server with bounded retries and backoff."""

        for attempt in range(1, attempts + 1):
            try:
                await self._drop_connection()
                # `_MCPLiveConnection` instances are single-use, so each retry gets
                # a fresh object with a fresh owner task.
                connection = _MCPLiveConnection(self.config, on_exit=self._handle_connection_exit)
                try:
                    await connection.__aenter__()
                except Exception:
                    await connection.__aexit__(None, None, None)
                    raise
                self.connection = connection
                self._known_tools = list(connection.tools)
                return

            except Exception as e:
                logger.warning(
                    f"MCP: {reason} failed for '{self.config.name}' (attempt {attempt}/{attempts}): {e}"
                )
                if attempt < attempts:
                    await asyncio.sleep(delay * 2 ** (attempt - 1))
                else:
                    raise RuntimeError(
                        f"MCP: failed to connect to '{self.config.name}' after {attempts} attempts"
                    ) from e

    async def start(self, reason: str) -> None:
        async with self.lock:
            await self._connect_with_retries(reason)

    async def stop(self) -> None:
        async with self.lock:
            await self._drop_connection()

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        async with self.lock:
            if self.connection is None:
                await self._connect_with_retries("session restore")

            try:
                connection = self.connection
                assert connection is not None, (
                    "connection not established after reconnect attempt 1, cannot call tool"
                )
                return await connection.call_tool(tool_name, arguments)
            except Exception as e:
                logger.warning(f"MCP: call to '{tool_name}' on '{self.config.name}' failed: {e}")
                await self._drop_connection()
                raise


class MCPManager:
    """
    Manages connections to one or more MCP servers.

    Enter as an async context manager to connect to all configured servers
    and discover their tools. On exit, all connections are cleanly closed.
    """

    def __init__(self, server_configs: list[MCPServerConfig]) -> None:
        server_names = [cfg.name for cfg in server_configs]
        duplicate_names = sorted({name for name in server_names if server_names.count(name) > 1})
        if duplicate_names:
            duplicates = ", ".join(duplicate_names)
            raise ValueError(f"Duplicate MCP server names are not allowed: {duplicates}")

        self._servers = {cfg.name: _MCPServerSlot(cfg) for cfg in server_configs}

    async def __aenter__(self) -> "MCPManager":
        servers = list(self._servers.values())
        results = await asyncio.gather(
            *(server.start(reason="initial connect") for server in servers),
            return_exceptions=True,
        )
        for server, result in zip(servers, results):
            if isinstance(result, Exception):
                logger.warning(f"MCP: failed to connect to '{server.config.name}': {result}")

        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await asyncio.gather(*(server.stop() for server in self._servers.values()))

    def get_definitions(self) -> list[dict[str, Any]]:
        """Return OpenAI-format tool schemas for all MCP tools."""
        definitions: list[dict[str, Any]] = []
        for server_name, server in self._servers.items():
            for tool in server.get_tools():
                public_name = f"{server_name}__{tool.name}"
                definitions.append(
                    {
                        "type": "function",
                        "function": {
                            "name": public_name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema,
                        },
                    }
                )
        return definitions

    def _split_public_name(self, public_name: str) -> tuple[_MCPServerSlot, str]:
        server_name, sep, tool_name = public_name.partition("__")
        if sep and (server := self._servers.get(server_name)):
            return server, tool_name
        raise KeyError(public_name)

    async def execute(self, public_name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool and return its result as a string."""
        server, tool_name = self._split_public_name(public_name)

        result = await server.execute(tool_name, arguments)
        parts = [item.text for item in result.content if isinstance(item, TextContent)]
        return "\n".join(parts) if parts else "(no output)"

    def __contains__(self, name: str) -> bool:
        try:
            server, tool_name = self._split_public_name(name)
            return server.has_tool(tool_name)
        except KeyError:
            return False
