"""File system tools: read, write, edit, and search."""

import re
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from teachclaw.agent.tools.base import FileSnapshot, Tool, ToolContext

_WORKSPACE_PREFIXES = ("skills", "common")


def _is_within(path: Path, root: Path) -> bool:
    """True if path is at or under root. Works for both file roots and dir roots.

    `Path.relative_to` succeeds when path == root (returning `.`) and when
    path is strictly under root, so a single check covers both shapes.
    """
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_path(path: str, ctx: ToolContext, *, write: bool = False) -> Path:
    """Resolve a tool-supplied path and enforce sandbox restrictions.

    - Absolute paths are rejected.
    - Relative paths resolve against ``storage_root``.
    - The post-resolve target must lie within at least one of the
      configured roots (``storage_root`` + ``read_roots``, or
      ``storage_root`` + ``write_roots`` when ``write=True``).
    """
    if ctx.storage_root is None:
        raise RuntimeError("ToolContext.storage_root is required for filesystem tools.")
    if path.startswith("/"):
        raise PermissionError(
            "Absolute paths are not allowed in this conversation. Use a relative path."
        )
    # `skills/` and `common/` are workspace-rooted prefixes so the model
    # can address shared resources without counting `..` segments.
    # Anything else resolves under the conversation's own storage root.
    first = path.split("/", 1)[0]
    base = ctx.workspace if first in _WORKSPACE_PREFIXES else ctx.storage_root
    resolved = (base / path).expanduser().resolve()
    if resolved in ctx.forbidden_files:
        raise PermissionError(f"Path '{path}' is not readable.")

    extra = ctx.write_roots if write else ctx.read_roots
    sandbox_roots = (ctx.storage_root.resolve(),) + tuple(r.resolve() for r in extra)
    if not any(_is_within(resolved, root) for root in sandbox_roots):
        kind = "write" if write else "read"
        raise PermissionError(
            f"Path '{path}' resolves outside this conversation's allowed {kind} roots."
        )
    return resolved


def _display_path(path: Path, ctx: ToolContext) -> str:
    """Return a sandbox- or workspace-relative display path when possible."""
    for root in [ctx.storage_root] + list(ctx.read_roots) + [ctx.workspace]:
        if root:
            try:
                return str(path.relative_to(root.resolve()))
            except ValueError:
                continue
    return str(path)


def _record_snapshot(ctx: ToolContext, path: Path) -> None:
    """Record the current metadata for a file in the session cache."""
    file_stat = path.stat()
    ctx.file_snapshots[path] = FileSnapshot(
        path=path, size=file_stat.st_size, mtime_ns=file_stat.st_mtime_ns
    )


def _require_fresh_snapshot(ctx: ToolContext, path: Path) -> None:
    """Require that an existing file was read and has not changed since then."""
    display_path = _display_path(path, ctx)
    snapshot = ctx.file_snapshots.get(path)
    if snapshot is None:
        raise RuntimeError(
            f"Refusing to modify existing file '{display_path}' because it has not been "
            "read in this session. Read it first with read_file."
        )

    try:
        current_stat = path.stat()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Refusing to modify '{display_path}' because it existed when read but no "
            "longer exists. Re-read the file and retry."
        )

    if current_stat.st_size != snapshot.size or current_stat.st_mtime_ns != snapshot.mtime_ns:
        raise RuntimeError(
            f"Refusing to modify '{display_path}' because it changed after it was read. "
            f"Expected size={snapshot.size} mtime_ns={snapshot.mtime_ns}, current "
            f"size={current_stat.st_size} mtime_ns={current_stat.st_mtime_ns}. Re-read the "
            "file and retry."
        )


class ReadFileTool(Tool):
    """Tool to read file contents."""

    class Params(BaseModel):
        path: str = Field(description="The file path to read, where . is the workspace dir.")

    Params: ClassVar[type[BaseModel]] = Params

    @classmethod
    def build(cls, _config: None, _ctx: ToolContext) -> "ReadFileTool":
        return cls()

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the complete contents of a file from inside the workspace directory. "
            "Use this tool when you need to examine the contents of a single file. "
            "Returns a detailed error if the file cannot be read or is not a regular file. "
            "Example: `{'path': 'README.md'}`."
        )

    async def execute(self, ctx: ToolContext, path: str, **kwargs: Any) -> str:
        file_path = _resolve_path(path, ctx)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if not file_path.is_file():
            raise ValueError(f"Not a file: {path}")

        content = file_path.read_text(encoding="utf-8")
        _record_snapshot(ctx, file_path)
        return content


class WriteFileTool(Tool):
    """Tool to write content to a file."""

    class Params(BaseModel):
        path: str = Field(description="The file path to write, where . is the workspace dir.")
        content: str = Field(description="The content to write")

    Params: ClassVar[type[BaseModel]] = Params

    @classmethod
    def build(cls, _config: None, ctx: ToolContext) -> "WriteFileTool":
        return cls()

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str | None:
        return (
            "Create a new file or completely overwrite an existing file from inside the workspace directory with new content. "
            "Use with caution because it replaces the full file contents. "
            "Creates parent directories as needed and returns a detailed error if the write fails. "
            "Example: `{'path': 'notes/output.txt', 'content': 'Hello, world!'}`."
        )

    async def execute(self, ctx: ToolContext, path: str, content: str, **kwargs: Any) -> str:
        file_path = _resolve_path(path, ctx, write=True)
        if file_path.exists():
            if not file_path.is_file():
                raise ValueError(f"Not a file: {path}")
            _require_fresh_snapshot(ctx, file_path)

        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        _record_snapshot(ctx, file_path)
        return "Success"


class EditFileTool(Tool):
    """Tool to edit a file by replacing text."""

    class Params(BaseModel):
        path: str = Field(description="The file path to edit, where . is the workspace dir.")
        old_str: str = Field(description="The exact text to find and replace")
        new_str: str = Field(description="The text to replace with")

    Params: ClassVar[type[BaseModel]] = Params

    @classmethod
    def build(cls, _config: None, _ctx: ToolContext) -> "EditFileTool":
        return cls()

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Make a targeted edit to an existing text file by replacing an exact string match, from inside the workspace directory by default. "
            "This is safer than overwriting the whole file when you only need to change part of it. "
            "The edit is rejected if the original text is missing or appears more than once. "
            "Example: `{'path': 'USER.md', 'old_str': 'port: 8080', 'new_str': 'port: 9090'}`."
        )

    async def execute(
        self, ctx: ToolContext, path: str, old_str: str, new_str: str, **kwargs: Any
    ) -> str:
        file_path = _resolve_path(path, ctx, write=True)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if not file_path.is_file():
            raise ValueError(f"Not a file: {path}")

        _require_fresh_snapshot(ctx, file_path)

        content = file_path.read_text(encoding="utf-8")

        if old_str not in content:
            raise ValueError("old_str not found in file. Make sure it matches exactly.")

        count = content.count(old_str)
        if count > 1:
            raise ValueError(
                f"old_str appears {count} times. Please provide more context to make it unique."
            )

        new_content = content.replace(old_str, new_str, 1)
        file_path.write_text(new_content, encoding="utf-8")
        _record_snapshot(ctx, file_path)

        return f"Successfully edited {path}"


class GlobTool(Tool):
    """Tool to match filesystem paths with a glob pattern."""

    class Params(BaseModel):
        pattern: str = Field(description="The glob pattern to match")
        path: str = Field(
            default=".",
            description="The directory to search from, where . is the workspace dir.",
        )
        max_results: int = Field(
            default=200, ge=1, description="Maximum number of matches to return"
        )

    Params: ClassVar[type[BaseModel]] = Params

    @classmethod
    def build(cls, _config: None, _ctx: ToolContext) -> "GlobTool":
        return cls()

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "Find files and directories that match a glob pattern, from inside the workspace directory by default. "
            "Use this tool when you know the shape of the path but not the exact filename, or when you want to list a directory by using patterns like '*' or '**/*'. "
            "Returns matching paths relative to the workspace when possible. "
            "Example: `{'pattern': 'teachclaw/**/*.py', 'path': '.'}`."
        )

    async def execute(
        self,
        ctx: ToolContext,
        pattern: str,
        path: str = ".",
        max_results: int = 200,
        **kwargs: Any,
    ) -> str:
        root = _resolve_path(path, ctx)
        if not root.exists():
            raise FileNotFoundError(f"Directory not found: {path}")
        if not root.is_dir():
            raise ValueError(f"Not a directory: {path}")

        matches = sorted(root.glob(pattern))
        if not matches:
            return f"No paths matched pattern: {pattern}"

        limited_matches = matches[:max_results]
        lines = [_display_path(match, ctx) for match in limited_matches]
        if len(matches) > max_results:
            lines.append(f"... and {len(matches) - max_results} more")
        return "\n".join(lines)


class GrepTool(Tool):
    """Tool to search file contents for matching lines."""

    class Params(BaseModel):
        pattern: str = Field(description="The text or regex pattern to search for")
        path: str = Field(
            default=".",
            description="The file or directory to search, where . is the workspace dir.",
        )
        file_pattern: str = Field(
            default="*",
            description="Glob filter for files when searching a directory",
        )
        is_regex: bool = Field(
            default=False,
            description="Whether pattern should be treated as a regular expression",
        )
        case_sensitive: bool = Field(
            default=False,
            description="Whether matching should be case-sensitive",
        )
        max_results: int = Field(
            default=200, ge=1, description="Maximum number of matching lines to return"
        )

    Params: ClassVar[type[BaseModel]] = Params

    @classmethod
    def build(cls, _config: None, _ctx: ToolContext) -> "GrepTool":
        return cls()

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search for matching text in a file or directory tree, from inside the workspace directory by default. "
            "Supports plain-text matching or regular expressions and returns matching lines with file and line numbers. "
            "Use `file_pattern` to limit which files are searched when scanning directories. "
            "Example: `{'pattern': 'register_tool', 'path': 'teachclaw', 'file_pattern': '*.py'}`."
        )

    async def execute(
        self,
        ctx: ToolContext,
        pattern: str,
        path: str = ".",
        file_pattern: str = "*",
        is_regex: bool = False,
        case_sensitive: bool = False,
        max_results: int = 200,
        **kwargs: Any,
    ) -> str:
        target = _resolve_path(path, ctx)
        if not target.exists():
            raise FileNotFoundError(f"Path not found: {path}")

        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            matcher = re.compile(pattern if is_regex else re.escape(pattern), flags)
        except re.error as e:
            raise ValueError(f"Invalid regular expression: {e}") from e

        if target.is_file():
            files = [target]
        else:
            files = [
                candidate for candidate in sorted(target.rglob(file_pattern)) if candidate.is_file()
            ]

        results: list[str] = []
        for file_path in files:
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue

            for line_number, line in enumerate(lines, start=1):
                if matcher.search(line):
                    results.append(f"{_display_path(file_path, ctx)}:{line_number}: {line}")
                    if len(results) >= max_results:
                        return "\n".join(results)

        if not results:
            return f"No matches found for pattern: {pattern}"

        return "\n".join(results)
