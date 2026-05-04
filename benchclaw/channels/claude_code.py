"""Claude Code channel - MCP server implementing the Claude channels protocol.

Allows Claude Code sessions to connect to benchclaw for full bidirectional
communication, including role-play with a Claude instance.

Setup in Claude Code (.mcp.json):
  {
    "mcpServers": {
      "benchclaw": {
        "type": "http",
        "url": "http://localhost:18791/mcp"
      }
    }
  }

Claude Code will then have a `reply` tool it can call to send messages to
benchclaw, and benchclaw's responses arrive as <channel> tags in its context.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from loguru import logger
from mcp import types
from mcp.server.lowlevel.server import Server, request_ctx
from mcp.server.session import ServerSession
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.session import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification
from starlette.applications import Starlette
from starlette.routing import Route

from benchclaw.bus import MessageBus, OutboundMessage
from benchclaw.channels.attention import AttentionPolicy
from benchclaw.channels.base import BaseChannel, ChannelConfig


class ClaudeCodeConfig(ChannelConfig):
    """Configuration for the Claude Code channel."""

    host: str = "127.0.0.1"
    port: int = 18791
    channel_name: str = "benchclaw"
    instructions: str = (
        "You are connected to benchclaw via the Claude channels protocol. "
        "Call the reply tool to send a message to benchclaw. "
        "Messages from benchclaw arrive as <channel> tags in your context."
    )

    # AI-to-AI communication: always pay attention (no group summon needed)
    attention_policy: AttentionPolicy = AttentionPolicy.ALWAYS

    def make_channel(
        self, bus: MessageBus, media_repo: Any = None, **_: Any
    ) -> "ClaudeCodeChannel":
        return ClaudeCodeChannel(self, bus)

    def is_configured(self) -> bool:
        return True


class ClaudeCodeChannel(BaseChannel):
    """
    Claude Code channel via MCP server.

    Exposes an HTTP MCP endpoint that Claude Code can connect to. Inbound
    messages arrive when Claude Code calls the `reply` tool; outbound messages
    are pushed to the connected session as `notifications/claude/channel`.
    """

    name = "claude_code"

    def __init__(self, config: ClaudeCodeConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: ClaudeCodeConfig = config

        # Active session tracking (asyncio-safe: single-threaded event loop)
        self._session_to_chat: dict[int, str] = {}
        self._chat_to_session: dict[str, ServerSession] = {}

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _register_session(self, session: ServerSession) -> str:
        """Return the chat_id for this session, registering it on first call."""
        oid = id(session)
        if oid not in self._session_to_chat:
            chat_id = uuid.uuid4().hex[:12]
            self._session_to_chat[oid] = chat_id
            self._chat_to_session[chat_id] = session
            logger.info(f"Claude Code: new session → chat_id={chat_id}")
        return self._session_to_chat[oid]

    def _remove_session(self, chat_id: str) -> None:
        session = self._chat_to_session.pop(chat_id, None)
        if session is not None:
            self._session_to_chat.pop(id(session), None)

    # ------------------------------------------------------------------
    # MCP server construction
    # ------------------------------------------------------------------

    def _build_mcp_server(self) -> Server:
        """Create the low-level MCP server with channels capabilities."""
        server = Server(
            name=self.config.channel_name,
            instructions=self.config.instructions,
        )

        # Inject the experimental claude/channel capability into every
        # InitializationOptions that the session manager requests.
        _orig = server.create_initialization_options

        def _patched(
            notification_options=None,
            experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        ):
            ec: dict[str, dict[str, Any]] = {"claude/channel": {}}
            if experimental_capabilities:
                ec.update(experimental_capabilities)
            return _orig(notification_options, ec)

        server.create_initialization_options = _patched  # type: ignore[method-assign]

        # ---- tool: list_tools ----------------------------------------

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name="reply",
                    description=(
                        "Send a message to benchclaw. "
                        "Call this when you receive a <channel> tag and want to respond, "
                        "or to initiate contact with benchclaw's agent."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "The message text to send to benchclaw.",
                            },
                            "sender_id": {
                                "type": "string",
                                "description": (
                                    "Optional persona or role name for this message "
                                    "(e.g. 'assistant', 'pirate', 'advisor')."
                                ),
                            },
                        },
                        "required": ["content"],
                    },
                )
            ]

        # ---- tool: call_tool -----------------------------------------

        @server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
            if name != "reply":
                raise ValueError(f"Unknown tool: {name!r}")

            content = str(arguments.get("content", ""))
            sender_id = str(arguments.get("sender_id", "claude"))

            ctx = request_ctx.get()
            chat_id = self._register_session(ctx.session)

            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
            )

            return [types.TextContent(type="text", text="Delivered to benchclaw.")]

        return server

    # ------------------------------------------------------------------
    # BaseChannel interface
    # ------------------------------------------------------------------

    async def background(self) -> None:
        """Start the MCP HTTP server and run until cancelled."""
        mcp_server = self._build_mcp_server()
        session_manager = StreamableHTTPSessionManager(app=mcp_server)

        # Starlette treats plain functions as request handlers, not ASGI apps,
        # so wrap in a class so it's dispatched as a raw ASGI callable.
        class _MCPApp:
            async def __call__(self, scope, receive, send):
                await session_manager.handle_request(scope, receive, send)

        mcp_asgi = _MCPApp()

        @asynccontextmanager
        async def lifespan(app):
            async with session_manager.run():
                yield

        starlette_app = Starlette(
            routes=[Route("/mcp", endpoint=mcp_asgi, methods=["GET", "POST", "DELETE"])],
            lifespan=lifespan,
        )

        uv_config = uvicorn.Config(
            starlette_app,
            host=self.config.host,
            port=self.config.port,
            log_level="warning",
        )
        uv_server = uvicorn.Server(uv_config)

        logger.info(
            f"Claude Code channel listening at http://{self.config.host}:{self.config.port}/mcp"
        )

        try:
            await uv_server.serve()
        except asyncio.CancelledError:
            uv_server.should_exit = True
            raise

    async def send(self, msg: OutboundMessage) -> None:
        """Push an outbound message to the connected Claude Code session."""
        chat_id = msg.address.chat_id
        session = self._chat_to_session.get(chat_id)
        if session is None:
            logger.warning(f"Claude Code: no active session for chat_id={chat_id}")
            return

        notification = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params={"content": msg.content, "meta": {}},
        )
        try:
            await session._write_stream.send(  # type: ignore[attr-defined]
                SessionMessage(message=JSONRPCMessage(notification))
            )
        except Exception as exc:
            logger.warning(f"Claude Code: send failed for chat_id={chat_id}: {exc}")
            self._remove_session(chat_id)
