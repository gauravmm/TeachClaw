"""Shell execution tool."""

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from teachclaw.agent.tools.base import Tool, ToolConfig, ToolContext


class ExecToolConfig(ToolConfig):
    """Shell exec tool configuration."""

    timeout: int = 300
    restrict_to_workspace: bool = True


class ExecTool(Tool):
    """Tool to execute shell commands."""

    class Params(BaseModel):
        command: str = Field(description="The shell command to execute")
        working_dir: str | None = Field(
            default=None, description="Optional working directory for the command"
        )

    @classmethod
    def build(cls, config: "ExecToolConfig | None", ctx: ToolContext) -> "ExecTool":
        resolved_config = config or ExecToolConfig()
        return cls(
            config=resolved_config,
            working_dir=str(ctx.workspace),
            restrict_to_workspace=resolved_config.restrict_to_workspace,
        )

    def __init__(
        self,
        config: ExecToolConfig,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
    ):
        self.timeout = config.timeout
        self.working_dir = working_dir
        self._workspace_root = Path(working_dir).resolve() if working_dir else None
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",  # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",  # del /f, del /q
            r"\brmdir\s+/s\b",  # rmdir /s
            r"\b(format|mkfs|diskpart)\b",  # disk operations
            r"\bdd\s+if=",  # dd
            r">\s*/dev/sd",  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",  # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str | None:
        return (
            "Execute a shell command and return its combined stdout and stderr. "
            "Dangerous patterns (e.g. `rm -rf`, disk writes) are blocked; subagents are restricted to the workspace directory; output is truncated at 10,000 characters. "
            "Example: `{'command': 'git log --oneline -5', 'working_dir': '/home/user/project'}`."
        )

    async def execute(
        self, ctx: ToolContext, command: str, working_dir: str | None = None, **kwargs: Any
    ) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()

        # When workspace-restricted, reject a caller-supplied working_dir that is
        # outside the workspace before running any command.
        if self.restrict_to_workspace and working_dir and self._workspace_root:
            try:
                requested = Path(working_dir).resolve()
            except Exception:
                requested = None
            if requested is not None and (
                requested != self._workspace_root and self._workspace_root not in requested.parents
            ):
                raise PermissionError(
                    "Command blocked by safety guard (working_dir outside workspace)"
                )

        self._guard_command(command, cwd)

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                process.kill()
                raise TimeoutError(f"Command timed out after {self.timeout} seconds")

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            if process.returncode != 0:
                output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # Truncate very long output
            max_len = 10000
            if len(result) > max_len:
                result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"

            return result

        except Exception as e:
            raise RuntimeError(f"Error executing command: {e}") from e

    def _guard_command(self, command: str, cwd: str) -> None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                raise PermissionError(
                    "Command blocked by safety guard (dangerous pattern detected)"
                )

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                raise PermissionError("Command blocked by safety guard (not in allowlist)")

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                raise PermissionError("Command blocked by safety guard (path traversal detected)")

            cwd_path = Path(cwd).resolve()

            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", cmd)
            # Only match absolute paths — avoid false positives on relative
            # paths like ".venv/bin/python" where "/bin/python" would be
            # incorrectly extracted by the old pattern.
            posix_paths = re.findall(r"(?:^|[\s|>])(/[^\s\"'>]+)", cmd)

            for raw in win_paths + posix_paths:
                try:
                    p = Path(raw.strip()).resolve()
                except Exception:
                    continue
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    raise PermissionError(
                        "Command blocked by safety guard (path outside working dir)"
                    )
