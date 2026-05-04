"""Base class for agent tools."""

from abc import abstractmethod
from asyncio import Task
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

from benchclaw.bus import MessageAddress, MessageBus, ToolResult

if TYPE_CHECKING:
    from benchclaw.media import MediaRepository


@dataclass
class FileSnapshot:
    """Observed metadata for a file that was read in the current tool context."""

    path: Path
    size: int
    mtime_ns: int


@dataclass
class ToolContext:
    """Runtime context passed to Tool.build() and Tool.execute() during agent operation."""

    workspace: Path
    bus: MessageBus | None = None  # MessageBus; None for subagents/tests
    media_repo: "MediaRepository | None" = None
    address: MessageAddress | None = None  # Current session address; None for background/subagents
    background_tasks: dict[str, Task] | None = None  # Live task handles; master loop only
    file_snapshots: dict[Path, FileSnapshot] = field(default_factory=dict)
    allowed_dir: Path | None = None  # legacy single-dir restriction (no sandbox case)

    # Per-conversation sandbox. When storage_root is set the filesystem tools
    # operate in sandbox mode: relative paths resolve against storage_root,
    # absolute paths are rejected, and reads/writes are confined to the union
    # of read_roots / write_roots (which may include storage_root, shared
    # read-only directories like common/ and skills/, and per-user writable
    # paths). When storage_root is None the legacy workspace-rooted behaviour
    # applies — see _resolve_path in agent/tools/filesystem.py.
    storage_root: Path | None = None
    read_roots: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()


class ToolConfig(BaseModel):
    """Base config shared by every tool. Subclass to add tool-specific fields."""

    enabled: bool = True


class _NoParams(BaseModel):
    """Default Params for tools that take no arguments."""


class Tool:
    """
    Abstract base class for agent tools.

    Subclasses declare their argument schema by overriding ``Params`` with a
    ``pydantic.BaseModel``. The class derives ``parameters`` (the OpenAI
    tool-call JSON-schema dict) and validates incoming arguments against it
    in ``ToolRegistry.execute``. ``Tool.execute`` is then called with the
    validated kwargs.
    """

    Params: ClassVar[type[BaseModel]] = _NoParams
    _task: Task | None = None

    @classmethod
    def build(cls, config: Any, ctx: "ToolContext") -> "Tool":
        """Instantiate this tool from a config object and build context."""
        raise NotImplementedError(f"{cls.__name__}.build() is not implemented")

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name used in function calls."""
        pass

    @property
    def description(self) -> str | None:
        """Skill usage instruction to inject into agent context. Its always injected, so keep it short."""
        return None

    @property
    def parameters(self) -> dict[str, Any]:
        """JSON Schema for tool parameters, derived from ``cls.Params``.

        Pydantic-internal keys (``title``, ``$defs``) are stripped so the
        schema matches the OpenAI tool-call shape directly.
        """
        schema = self.Params.model_json_schema()
        schema.pop("title", None)
        for prop in (schema.get("properties") or {}).values():
            if isinstance(prop, dict):
                prop.pop("title", None)
        return schema

    @abstractmethod
    async def execute(self, ctx: "ToolContext", **kwargs: Any) -> ToolResult:
        """Execute the tool with validated parameters."""
        pass

    async def background(self, ctx: "ToolContext") -> None:
        """Optional long-running coroutine started by ToolRegistry.__aenter__. No-op by default."""
        pass

    def to_schema(self) -> dict[str, Any]:
        """Convert tool to OpenAI function schema format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
