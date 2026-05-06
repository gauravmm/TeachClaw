"""Tests for MCP server slot connect / reconnect behaviour.

The real MCP transport is replaced by a fake `_MCPLiveConnection` so the
tests exercise `_MCPServerSlot` directly without any IO.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import TextContent

from teachclaw.agent.tools import mcp_manager as mcp_module
from teachclaw.agent.tools.mcp_manager import MCPManager, MCPServerConfig


class _FakeTool:
    def __init__(self, name: str, description: str = "", input_schema: dict | None = None):
        self.name = name
        self.description = description
        self.inputSchema = input_schema


class _FakeSession:
    """Minimal session with the methods `_MCPLiveConnection` and `_MCPServerSlot`
    actually call: `list_tools` (during connect) and `call_tool` (during execute)."""

    def __init__(self, tool_name: str, text: str, fail_times: int = 0):
        self._tool_name = tool_name
        self._text = text
        self._fail_times = fail_times
        self.call_count = 0

    async def list_tools(self):
        return SimpleNamespace(
            tools=[_FakeTool(name=self._tool_name, description="fake", input_schema={})]
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        self.call_count += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("connection dropped")
        return SimpleNamespace(
            content=[TextContent(type="text", text=f"{name}:{arguments.get('value', self._text)}")]
        )


class _FakeLiveConnectionFactory:
    """Build fake `_MCPLiveConnection` classes that walk through a script.

    Each entry in ``script`` is either an ``Exception`` (raised from
    ``__aenter__``) or a ``_FakeSession`` (returned successfully). Iterates
    through the script in order; reaching the end raises ``StopIteration``.
    """

    def __init__(self, script: list[Exception | _FakeSession]) -> None:
        self._script = iter(script)
        self.constructed: list[_FakeLiveConnection] = []

    def __call__(self, config: MCPServerConfig, *, on_exit=None):
        connection = _FakeLiveConnection(config, on_exit, next(self._script))
        self.constructed.append(connection)
        return connection


class _FakeLiveConnection:
    def __init__(self, config, on_exit, outcome: Exception | _FakeSession) -> None:
        self.config = config
        self._on_exit = on_exit
        self._outcome = outcome
        self.session: _FakeSession | None = None
        self.tools: list = []
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> "_FakeLiveConnection":
        self.entered = True
        if isinstance(self._outcome, Exception):
            raise self._outcome
        self.session = self._outcome
        self.tools = list((await self.session.list_tools()).tools)
        return self

    async def __aexit__(self, *exc) -> None:
        self.exited = True

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]):
        assert self.session is not None
        return await self.session.call_tool(tool_name, arguments)


def _patch_live_connection(
    monkeypatch: pytest.MonkeyPatch, script: list[Exception | _FakeSession]
) -> _FakeLiveConnectionFactory:
    factory = _FakeLiveConnectionFactory(script)
    monkeypatch.setattr(mcp_module, "_MCPLiveConnection", factory)
    return factory


@pytest.mark.asyncio
async def test_mcp_manager_retries_initial_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_connect_with_retries` retries transient failures during startup."""
    cfg = MCPServerConfig(name="demo", transport="http", url="https://example.test/mcp")
    manager = MCPManager([cfg])

    factory = _patch_live_connection(
        monkeypatch,
        [
            RuntimeError("temporary connect failure"),
            RuntimeError("temporary connect failure"),
            _FakeSession(tool_name="echo", text="ok"),
        ],
    )
    monkeypatch.setattr(mcp_module.asyncio, "sleep", _no_sleep)

    async with manager:
        assert "demo__echo" in manager
        [definition] = manager.get_definitions()
        assert definition["function"]["name"] == "demo__echo"

    assert len(factory.constructed) == 3, "expected exactly three connect attempts"
    assert factory.constructed[-1].entered
    assert factory.constructed[-1].session is not None


@pytest.mark.asyncio
async def test_mcp_manager_drops_connection_on_tool_call_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing tool call propagates the error and drops the slot's connection."""
    cfg = MCPServerConfig(name="demo", transport="http", url="https://example.test/mcp")
    manager = MCPManager([cfg])
    failing_session = _FakeSession(tool_name="echo", text="x", fail_times=1)
    factory = _patch_live_connection(monkeypatch, [failing_session])

    async with manager:
        with pytest.raises(RuntimeError, match="connection dropped"):
            await manager.execute("demo__echo", {"value": "payload"})

        slot = manager._servers["demo"]
        assert slot.connection is None, "slot must drop its connection after a tool failure"

    assert failing_session.call_count == 1
    assert factory.constructed[0].exited


@pytest.mark.asyncio
async def test_mcp_manager_reconnects_on_next_execute_after_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a drop, the next `execute` opens a fresh connection."""
    cfg = MCPServerConfig(name="demo", transport="http", url="https://example.test/mcp")
    manager = MCPManager([cfg])
    failing_session = _FakeSession(tool_name="echo", text="x", fail_times=1)
    healthy_session = _FakeSession(tool_name="echo", text="ok")
    factory = _patch_live_connection(monkeypatch, [failing_session, healthy_session])

    async with manager:
        with pytest.raises(RuntimeError, match="connection dropped"):
            await manager.execute("demo__echo", {"value": "payload"})

        result = await manager.execute("demo__echo", {"value": "payload"})

    assert result == "echo:payload"
    assert len(factory.constructed) == 2, "second execute should open a new connection"
    assert healthy_session.call_count == 1


def test_mcp_manager_rejects_duplicate_server_names() -> None:
    configs = [
        MCPServerConfig(name="demo", transport="http", url="https://example.test/one"),
        MCPServerConfig(name="demo", transport="http", url="https://example.test/two"),
    ]

    with pytest.raises(ValueError, match="Duplicate MCP server names are not allowed: demo"):
        MCPManager(configs)


async def _no_sleep(_seconds: float) -> None:
    """Replace asyncio.sleep so retry backoff in `_connect_with_retries` is instant."""
    return None
