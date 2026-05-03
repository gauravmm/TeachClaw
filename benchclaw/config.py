"""Configuration schema and loading utilities for nanobot."""

from collections.abc import Iterator
from pathlib import Path

import yaml
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings

from benchclaw.agent.tools.builtins import TOOL_CONFIG_TYPES
from benchclaw.agent.tools.mcp_manager import MCPServerConfig
from benchclaw.agent.tools.shell import ExecToolConfig
from benchclaw.agent.tools.web import WebSearchConfig
from benchclaw.channels.claude_code import ClaudeCodeConfig
from benchclaw.channels.smtp_email import EmailConfig
from benchclaw.channels.telegrm import TelegramConfig
from benchclaw.channels.whatsapp.channel import WhatsAppConfig


_DEFAULT_ELIDE_TOOLS: tuple[str, ...] = (
    "search",
    "fetch_doc",
    "wiki_lookup",
    "brave_search",
    "web_search",
    "web_fetch",
)


class CompactionConfig(BaseModel):
    """Compaction strategy. See spec/COMPACTION.md for the full design."""

    threshold: float = 0.82
    summarize_model: str | None = None  # null => same model as the agent
    elide_chunks_after_turn: bool = True
    elide_tool_names: tuple[str, ...] = _DEFAULT_ELIDE_TOOLS


class AgentConfig(BaseModel):
    """Default agent configuration."""

    workspace: str = "./workspace"
    model: str = "anthropic/claude-opus-4-5"
    max_tokens: int = 2048
    temperature: float = 0.7
    max_tool_iterations: int = 20
    memory_window: int = 50
    context_window: int = 24000
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)


class AgentsConfig(BaseModel):
    """Agent configuration."""

    master: AgentConfig = Field(default_factory=AgentConfig)


class ProviderConfig(BaseModel):
    """LLM provider configuration."""

    name: str = "anthropic"
    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None


class GatewayConfig(BaseModel):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790


class ToolsConfig(BaseModel):
    """Static built-in tool configuration."""

    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ChannelConfigs(BaseModel):
    """Optional built-in channel configuration."""

    claude_code: ClaudeCodeConfig | None = None
    email: EmailConfig | None = None
    telegram: TelegramConfig | None = None
    whatsapp: WhatsAppConfig | None = None

    def __iter__(self) -> Iterator[tuple[str, BaseModel]]:
        for name in type(self).model_fields:
            config = getattr(self, name)
            if config is not None:
                yield name, config


class Config(BaseSettings):
    """Root configuration for nanobot."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    channels: ChannelConfigs = Field(default_factory=ChannelConfigs)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.master.workspace).expanduser()

    model_config = ConfigDict(env_prefix="NANOBOT_", env_nested_delimiter="__")  # type: ignore


class ConfigManager:
    """Context manager that loads config on enter and saves it on exit."""

    def __init__(self, config_path: Path = Path("config") / "config.yaml"):
        self._path = config_path
        self.config: Config | None = None
        self._write_on_exit = False

    def __enter__(self) -> Config:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    data = yaml.safe_load(f)
                data = _migrate_config(data)
                self.config = Config.model_validate(data)
                return self.config
            except (yaml.YAMLError, ValueError) as e:
                logger.warning(f"Failed to load config from {self._path}: {e}")
                logger.warning("Using default configuration.")
        else:
            self._write_on_exit = True

        self.config = Config()
        return self.config

    def __exit__(self, *_) -> None:
        if self._write_on_exit and self.config:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w") as f:
                yaml.dump(self.config.model_dump(), f, default_flow_style=False, allow_unicode=True)


def _migrate_config(data: dict | None) -> dict:
    """Migrate old config formats to current."""
    data = data or {}

    channels = dict(data.get("channels") or {})
    telegram = channels.get("telegram")
    if isinstance(telegram, dict) and not telegram.get("enabled", True):
        channels.pop("telegram", None)
    elif isinstance(telegram, dict):
        telegram.pop("enabled", None)

    email = channels.get("email")
    if isinstance(email, dict):
        email.pop("consent_granted", None)

    tools = dict(data.get("tools") or {})
    data["channels"] = channels
    data["tools"] = {name: value for name, value in tools.items() if name in TOOL_CONFIG_TYPES}
    return data
