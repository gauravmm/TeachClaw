"""Tests for filesystem search tools."""

from pathlib import Path

import pytest

from teachclaw.agent.tools.base import ToolContext
from teachclaw.agent.tools.builtins import BUILTIN_TOOLS
from teachclaw.agent.tools.filesystem import (
    EditFileTool,
    GlobTool,
    GrepTool,
    ReadFileTool,
    WriteFileTool,
    _resolve_path,
)


def test_list_dir_tool_is_not_registered() -> None:
    assert "list_dir" not in {name for name, _cls in BUILTIN_TOOLS}


@pytest.mark.asyncio
async def test_glob_returns_workspace_relative_matches(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "one.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "alpha" / "two.md").write_text("two\n", encoding="utf-8")

    tool = GlobTool()
    ctx = ToolContext(workspace=tmp_path, storage_root=tmp_path)

    result = await tool.execute(ctx, pattern="**/*.txt")

    assert result == "alpha/one.txt"


@pytest.mark.asyncio
async def test_grep_searches_workspace_relative_directory(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("register_tool('x')\nignore me\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text("no match here\n", encoding="utf-8")
    (tmp_path / "pkg" / "notes.txt").write_text("register_tool in text\n", encoding="utf-8")

    tool = GrepTool()
    ctx = ToolContext(workspace=tmp_path, storage_root=tmp_path)

    result = await tool.execute(ctx, pattern="register_tool", path="pkg", file_pattern="*.py")

    assert result == "pkg/a.py:1: register_tool('x')"


@pytest.mark.asyncio
async def test_grep_supports_regex_on_single_file(tmp_path: Path) -> None:
    file_path = tmp_path / "app.log"
    file_path.write_text("INFO start\nERROR failed\n", encoding="utf-8")

    tool = GrepTool()
    ctx = ToolContext(workspace=tmp_path, storage_root=tmp_path)

    result = await tool.execute(ctx, pattern="^ERROR", path="app.log", is_regex=True)

    assert result == "app.log:2: ERROR failed"


@pytest.mark.asyncio
async def test_write_existing_file_requires_prior_read(tmp_path: Path) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("old\n", encoding="utf-8")

    tool = WriteFileTool()
    ctx = ToolContext(workspace=tmp_path, storage_root=tmp_path)

    with pytest.raises(RuntimeError, match="has not been read in this session"):
        await tool.execute(ctx, path="notes.txt", content="new\n")


@pytest.mark.asyncio
async def test_write_existing_file_fails_if_changed_after_read(tmp_path: Path) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("old\n", encoding="utf-8")

    read_tool = ReadFileTool()
    write_tool = WriteFileTool()
    ctx = ToolContext(workspace=tmp_path, storage_root=tmp_path)

    assert await read_tool.execute(ctx, path="notes.txt") == "old\n"
    file_path.write_text("externally changed\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed after it was read"):
        await write_tool.execute(ctx, path="notes.txt", content="new\n")


@pytest.mark.asyncio
async def test_edit_existing_file_succeeds_after_read_and_refreshes_snapshot(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("hello world\n", encoding="utf-8")

    read_tool = ReadFileTool()
    edit_tool = EditFileTool()
    ctx = ToolContext(workspace=tmp_path, storage_root=tmp_path)

    assert await read_tool.execute(ctx, path="notes.txt") == "hello world\n"
    result = await edit_tool.execute(
        ctx,
        path="notes.txt",
        old_str="hello",
        new_str="goodbye",
    )

    assert result == "Successfully edited notes.txt"
    assert file_path.read_text(encoding="utf-8") == "goodbye world\n"

    second_result = await edit_tool.execute(
        ctx,
        path="notes.txt",
        old_str="goodbye",
        new_str="hello",
    )

    assert second_result == "Successfully edited notes.txt"


def _sandboxed_ctx(tmp_path):
    storage_root = tmp_path / "storage" / "telegram" / "1"
    storage_root.mkdir(parents=True)
    common = tmp_path / "common"
    skills = tmp_path / "skills"
    common.mkdir(parents=True)
    skills.mkdir(parents=True)
    return ToolContext(
        workspace=tmp_path,
        storage_root=storage_root,
        read_roots=(skills.resolve(), common.resolve()),
        write_roots=(),
    )


def test_sandbox_rejects_absolute_path(tmp_path):
    ctx = _sandboxed_ctx(tmp_path)
    with pytest.raises(PermissionError):
        _resolve_path("/etc/passwd", ctx)


def test_sandbox_rejects_traversal_outside_storage(tmp_path):
    ctx = _sandboxed_ctx(tmp_path)
    with pytest.raises(PermissionError):
        _resolve_path("../../../etc/passwd", ctx)


def test_sandbox_rejects_admin_dir_access(tmp_path):
    ctx = _sandboxed_ctx(tmp_path)
    with pytest.raises(PermissionError):
        _resolve_path("../../_admin/secret.json", ctx)


def test_sandbox_allows_storage_root_relative_path(tmp_path):
    ctx = _sandboxed_ctx(tmp_path)
    resolved = _resolve_path("notes.md", ctx)
    assert resolved == (ctx.storage_root / "notes.md").resolve()


def test_sandbox_allows_common_read_via_prefix(tmp_path):
    ctx = _sandboxed_ctx(tmp_path)
    resolved = _resolve_path("common/faq.md", ctx)
    assert resolved == (tmp_path / "common" / "faq.md").resolve()


def test_sandbox_allows_skills_read_via_prefix(tmp_path):
    ctx = _sandboxed_ctx(tmp_path)
    resolved = _resolve_path("skills/foo/SKILL.md", ctx)
    assert resolved == (tmp_path / "skills" / "foo" / "SKILL.md").resolve()


def test_sandbox_write_to_common_root_rejected(tmp_path):
    ctx = _sandboxed_ctx(tmp_path)
    with pytest.raises(PermissionError):
        _resolve_path("common/faq.md", ctx, write=True)


def test_sandbox_write_to_own_storage_root_allowed(tmp_path):
    ctx = _sandboxed_ctx(tmp_path)
    resolved = _resolve_path("notes.md", ctx, write=True)
    assert resolved == (ctx.storage_root / "notes.md").resolve()
