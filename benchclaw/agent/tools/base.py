"""Base class for agent tools."""

from abc import abstractmethod
from asyncio import Task
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


class Tool:
    """
    Abstract base class for agent tools.

    Tools are capabilities that the agent can use to interact with
    the environment, such as reading files, executing commands, etc.
    """

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
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema for tool parameters."""
        pass

    @abstractmethod
    async def execute(self, ctx: "ToolContext", **kwargs: Any) -> ToolResult:
        """
        Execute the tool with given parameters.

        Args:
            ctx: Runtime context including session address and bus.
            **kwargs: Tool-specific parameters.

        Returns:
            String result of the tool execution.
        """
        pass

    async def background(self, ctx: "ToolContext") -> None:
        """Optional long-running coroutine started by ToolRegistry.__aenter__. No-op by default."""
        pass

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Validate tool parameters against JSON schema. Returns error list (empty if valid)."""
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            raise ValueError(f"Schema must be object type, got {schema.get('type')!r}")
        return _validate_schema(params, {**schema, "type": "object"}, "")

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


_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _validate_schema(val: Any, schema: dict[str, Any], label: str = "") -> list[str]:
    t = schema.get("type")
    if t in _TYPE_MAP and not isinstance(val, _TYPE_MAP[t]):
        return [f"{label} should be {t}"]

    errors = []
    if "enum" in schema and val not in schema["enum"]:
        errors.append(f"{label} must be one of {schema['enum']}")
    elif t in ("integer", "number"):
        if "minimum" in schema and val < schema["minimum"]:
            errors.append(f"{label} must be >= {schema['minimum']}")
        if "maximum" in schema and val > schema["maximum"]:
            errors.append(f"{label} must be <= {schema['maximum']}")
    elif t == "string":
        if "minLength" in schema and len(val) < schema["minLength"]:
            errors.append(f"{label} must be at least {schema['minLength']} chars")
        if "maxLength" in schema and len(val) > schema["maxLength"]:
            errors.append(f"{label} must be at most {schema['maxLength']} chars")
    elif t == "object":
        for k in schema.get("required", []):
            if k not in val:
                errors.append(f"missing required {label + '.' + k}")
        props = schema.get("properties", {})
        for k, v in val.items():
            if k in props:
                errors.extend(_validate_schema(v, props[k], label + "." + k))
    if t == "array" and "items" in schema:
        for i, item in enumerate(val):
            errors.extend(_validate_schema(item, schema["items"], f"{label}[{i}]"))
    return errors
