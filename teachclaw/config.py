"""Configuration schema and loading utilities for teachclaw."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings

from teachclaw.agent.tools.base import ToolConfig
from teachclaw.agent.tools.mcp_manager import MCPServerConfig
from teachclaw.agent.tools.shell import ExecToolConfig
from teachclaw.agent.tools.web import WebSearchConfig
from teachclaw.channels.telegrm import TelegramConfig

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
    top_p: float | None = None
    top_k: int | None = None
    enable_thinking: bool | None = None
    max_tool_iterations: int = 20
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


class ToolsConfig(BaseModel):
    """Static built-in tool configuration.

    Tools without their own config use a plain ``ToolConfig`` so that
    ``enabled: false`` still works uniformly. Add an entry here for any
    built-in tool the user should be able to toggle.
    """

    cron: ToolConfig = Field(default_factory=ToolConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class MediaConfig(BaseModel):
    """Media subsystem configuration.

    ``shared_roots`` maps an alias the model uses (e.g. ``cuteness``) to an
    absolute or user-relative directory the alias resolves to. Files under
    those directories are read-only and reachable as ``<alias>/<subpath>``.
    """

    shared_roots: dict[str, str] = Field(default_factory=dict)
    max_age_days: int = 30

    @field_validator("shared_roots")
    @classmethod
    def _validate_aliases(cls, value: dict[str, str]) -> dict[str, str]:
        for alias in value:
            if not alias:
                raise ValueError("media.shared_roots alias must be non-empty")
            if alias == "media":
                raise ValueError(
                    "media.shared_roots alias 'media' is reserved for the per-conversation sandbox"
                )
            if "/" in alias or "\\" in alias:
                raise ValueError(f"media.shared_roots alias must not contain a slash: {alias!r}")
            if alias in (".", ".."):
                raise ValueError(f"media.shared_roots alias must not be {alias!r}")
        return value


class MermaidConfig(BaseModel):
    """Mermaid renderer configuration.

    ``mmdc_path`` overrides ``shutil.which("mmdc")`` lookup. Useful when the
    bot runs in an environment whose PATH doesn't include nvm/npm bin dirs
    (e.g. systemd unit, or python launched outside an interactive shell).
    """

    mmdc_path: str | None = None


class ToolReminder(BaseModel):
    """A nudge appended to session history right after a configured tool returns.

    See spec/TOOL_REMINDERS.md. ``ephemeral`` reminders are hidden by the
    renderer once a UserEvent appears at a later index.
    """

    text: str
    ephemeral: bool = False

    @field_validator("text")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("tool_reminders text must be non-empty")
        return v


class ChannelConfigs(BaseModel):
    """Optional built-in channel configuration."""

    telegram: TelegramConfig | None = None

    def __iter__(self) -> Iterator[tuple[str, BaseModel]]:
        for name in type(self).model_fields:
            config = getattr(self, name)
            if config is not None:
                yield name, config


class Config(BaseSettings):
    """Root configuration for teachclaw."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    channels: ChannelConfigs = Field(default_factory=ChannelConfigs)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    mermaid: MermaidConfig = Field(default_factory=MermaidConfig)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    tool_reminders: dict[str, ToolReminder] = Field(default_factory=dict)

    @field_validator("tool_reminders", mode="before")
    @classmethod
    def _coerce_reminder_strings(cls, v: Any) -> Any:
        # Accept bare strings as shorthand for {"text": "...", "ephemeral": False}.
        if not v:
            return {}
        return {k: ({"text": entry} if isinstance(entry, str) else entry) for k, entry in v.items()}

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.master.workspace).expanduser()

    model_config = ConfigDict(env_prefix="BENCHCLAW_", env_nested_delimiter="__")  # type: ignore


def _save_config(config: Config, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(config.model_dump(), f, default_flow_style=False, allow_unicode=True)


def load_config(path: Path = Path("config.yaml")) -> Config:
    """Load and validate config, writing a defaults file on first run.

    1. Read ``path`` (or instantiate :class:`Config` with defaults when the
       file is absent — and immediately persist those defaults so the user
       has something to edit).
    2. Validate the lesson-pack workspace and merge its infra overlay,
       so a misconfigured workspace blocks startup with a clear error
       rather than failing later in the agent loop. See
       ``spec/SWITCHMODE.md``.
    """
    if path.exists():
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            config = Config.model_validate(data)
        except (yaml.YAMLError, ValueError) as e:
            logger.warning(f"Failed to load config from {path}: {e}")
            logger.warning("Using default configuration.")
            config = Config()
    else:
        config = Config()
        _save_config(config, path)

    from teachclaw import lessons

    lessons.validate_workspace(config.workspace_path)
    overlay = lessons.load_infra_overlay(config.workspace_path)
    lessons.merge_infra_into_config(config, overlay)
    return config
