"""Tests for the shell exec tool workspace restrictions."""

from pathlib import Path

import pytest

from teachclaw.agent.tools.base import ToolContext
from teachclaw.agent.tools.shell import ExecTool, ExecToolConfig


@pytest.mark.asyncio
async def test_exec_blocks_working_dir_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent

    ctx = ToolContext(workspace=tmp_path)
    tool = ExecTool.build(ExecToolConfig(restrict_to_workspace=True), ctx)

    with pytest.raises(PermissionError, match="outside workspace"):
        await tool.execute(ctx, command="echo hi", working_dir=str(outside))


@pytest.mark.asyncio
async def test_exec_allows_working_dir_inside_workspace(tmp_path: Path) -> None:
    subdir = tmp_path / "sub"
    subdir.mkdir()

    ctx = ToolContext(workspace=tmp_path)
    tool = ExecTool.build(ExecToolConfig(restrict_to_workspace=True), ctx)

    result = await tool.execute(ctx, command="echo hello", working_dir=str(subdir))

    assert "hello" in result


@pytest.mark.asyncio
async def test_exec_blocks_absolute_path_outside_workspace(tmp_path: Path) -> None:
    ctx = ToolContext(workspace=tmp_path)
    tool = ExecTool.build(ExecToolConfig(restrict_to_workspace=True), ctx)

    with pytest.raises(PermissionError, match="safety guard"):
        await tool.execute(ctx, command="cat /etc/hosts")


@pytest.mark.asyncio
async def test_exec_workspace_restriction_disabled_in_config(tmp_path: Path) -> None:
    ctx = ToolContext(workspace=tmp_path)
    tool = ExecTool.build(ExecToolConfig(restrict_to_workspace=False), ctx)

    # When restriction is off, working_dir outside workspace is accepted
    result = await tool.execute(ctx, command="echo open", working_dir=str(tmp_path.parent))

    assert "open" in result


@pytest.mark.asyncio
async def test_exec_default_config_restricts_to_workspace(tmp_path: Path) -> None:
    ctx = ToolContext(workspace=tmp_path)
    # Default ExecToolConfig has restrict_to_workspace=True
    tool = ExecTool.build(None, ctx)

    assert tool.restrict_to_workspace is True

    with pytest.raises(PermissionError, match="outside workspace"):
        await tool.execute(ctx, command="echo hi", working_dir=str(tmp_path.parent))
