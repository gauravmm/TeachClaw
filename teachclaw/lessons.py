"""Lesson-pack loader. The workspace IS the lesson — see spec/SWITCHMODE.md.

Three workspace-resident files describe the lesson:

* ``personalities.yaml`` — persona overlays (loaded by ``teachclaw.personalities``).
* ``onboarding.yaml`` — welcome strings, example prompts, help text.
* ``infra.yaml`` (optional) — config overlay merging into the global
  ``Config`` (MCP servers and ``media.shared_roots``).

Every file is parsed and schema-checked at boot; any problem aggregates
into a single :class:`LessonValidationError` so a misconfigured workspace
surfaces every issue at once instead of one-at-a-time on first message.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from teachclaw.agent.tools.mcp_manager import MCPServerConfig

LESSON_SOURCE_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "personalities.yaml",
    "onboarding.yaml",
    "infra.yaml",
)

ONBOARDING_REQUIRED_KEYS: tuple[str, ...] = (
    "pre_auth_welcome",
    "group_welcome_pre_auth",
    "group_welcome_authed",
    "post_auth_welcome",
    "example_prompts",
    "help_text",
)

ONBOARDING_PLACEHOLDERS: frozenset[str] = frozenset(
    {"sources_reaction", "trace_reaction", "persona_pitch"}
)

EXAMPLE_PROMPTS_MIN = 1
EXAMPLE_PROMPTS_MAX = 4

INFRA_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"mcp_servers", "media"})

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


class LessonValidationError(Exception):
    """Aggregated lesson-pack validation failure.

    Carries the full list of problems found so the operator sees every
    issue at once, rather than fixing one and tripping on the next.
    """

    def __init__(self, workspace: Path, problems: list[str]) -> None:
        self.workspace = workspace
        self.problems = list(problems)
        body = "\n".join(f"  - {p}" for p in problems)
        super().__init__(
            f"Lesson pack at {workspace} failed validation ({len(problems)} problem(s)):\n{body}"
        )


@dataclass(frozen=True)
class ExamplePrompt:
    label: str
    prompt: str


@dataclass(frozen=True)
class Onboarding:
    pre_auth_welcome: str
    group_welcome_pre_auth: str
    group_welcome_authed: str
    post_auth_welcome: str
    example_prompts: tuple[ExamplePrompt, ...]
    help_text: str


@dataclass(frozen=True)
class InfraOverlay:
    """Parsed ``infra.yaml`` overlay; either field may be empty."""

    mcp_servers: tuple[MCPServerConfig, ...] = ()
    shared_roots: dict[str, str] | None = None  # None = key absent (don't override)

    @property
    def has_mcp(self) -> bool:
        return bool(self.mcp_servers)

    @property
    def has_shared_roots(self) -> bool:
        return self.shared_roots is not None


# ---- public API -----------------------------------------------------------


def validate_workspace(workspace: Path) -> None:
    """Parse and schema-check every lesson file under ``workspace``.

    Raises :class:`LessonValidationError` aggregating every problem found.
    Reads ``personalities.yaml`` here too so its problems (duplicate names,
    missing default) surface at boot alongside the others, even though the
    file is consumed by :mod:`teachclaw.personalities`.
    """
    problems: list[str] = []
    with _collect(problems):
        _check_layout(workspace, problems)
    with _collect(problems):
        _check_personalities(workspace / "personalities.yaml", problems)
    with _collect(problems):
        _check_onboarding(workspace / "onboarding.yaml", problems)
    with _collect(problems):
        infra_path = workspace / "infra.yaml"
        if infra_path.exists():
            _check_infra(infra_path, problems)
    if problems:
        raise LessonValidationError(workspace, problems)


def load_onboarding(workspace: Path) -> Onboarding:
    """Load ``onboarding.yaml`` from the workspace.

    Assumes ``validate_workspace`` already ran and left the file
    well-formed. Caller is responsible for placeholder substitution.
    """
    data = _read_yaml(workspace / "onboarding.yaml")
    examples = tuple(
        ExamplePrompt(label=str(e["label"]).strip(), prompt=str(e["prompt"]).strip())
        for e in data["example_prompts"]
    )
    return Onboarding(
        pre_auth_welcome=str(data["pre_auth_welcome"]).rstrip(),
        group_welcome_pre_auth=str(data["group_welcome_pre_auth"]).rstrip(),
        group_welcome_authed=str(data["group_welcome_authed"]).rstrip(),
        post_auth_welcome=str(data["post_auth_welcome"]).rstrip(),
        example_prompts=examples,
        help_text=str(data["help_text"]).rstrip(),
    )


def load_infra_overlay(workspace: Path) -> InfraOverlay:
    """Load ``infra.yaml`` overlay if present, else empty.

    Assumes ``validate_workspace`` already ran.
    """
    path = workspace / "infra.yaml"
    if not path.exists():
        return InfraOverlay()
    data = _read_yaml(path) or {}
    mcp_raw = data.get("mcp_servers") or []
    mcp = tuple(MCPServerConfig.model_validate(s) for s in mcp_raw)
    shared = None
    media = data.get("media")
    if isinstance(media, dict) and "shared_roots" in media:
        shared = {str(k): str(v) for k, v in (media.get("shared_roots") or {}).items()}
    return InfraOverlay(mcp_servers=mcp, shared_roots=shared)


def merge_infra_into_config(config: Any, overlay: InfraOverlay) -> None:
    """Apply ``overlay`` on top of a loaded :class:`teachclaw.config.Config`.

    Mutates ``config`` in place. Two merge policies:

    * ``mcp_servers``: keyed-merge by ``name``. Lesson entries replace any
      global server with the same name and append otherwise. Order: globals
      first (with overrides applied in place), then any new lesson servers.
    * ``media.shared_roots``: dict-merge. Lesson aliases override global
      aliases of the same name; new aliases are added.
    """
    if overlay.has_mcp:
        by_name = {s.name: s for s in overlay.mcp_servers}
        merged: list[MCPServerConfig] = []
        seen: set[str] = set()
        for server in config.mcp_servers:
            if server.name in by_name:
                merged.append(by_name[server.name])
            else:
                merged.append(server)
            seen.add(server.name)
        for server in overlay.mcp_servers:
            if server.name not in seen:
                merged.append(server)
        config.mcp_servers = merged
    if overlay.has_shared_roots:
        # validators on Config.media.shared_roots also apply to the merged dict
        merged_roots = {**config.media.shared_roots, **(overlay.shared_roots or {})}
        config.media.shared_roots = merged_roots


def lesson_forbidden_files(workspace: Path) -> tuple[Path, ...]:
    """Resolved paths of lesson source files in the workspace.

    Threaded into :class:`ToolContext` so the filesystem tools can refuse
    a read regardless of how the path is spelled (relative, absolute,
    ``skills/../foo``).
    """
    out: list[Path] = []
    for name in LESSON_SOURCE_FILES:
        path = workspace / name
        if path.exists():
            out.append(path.resolve())
    return tuple(out)


# ---- validation internals -------------------------------------------------


@contextmanager
def _collect(problems: list[str]) -> Iterator[None]:
    """Run a validation block; record raised problems instead of bailing."""
    try:
        yield
    except _ProblemListError as e:
        problems.extend(e.items)


class _ProblemListError(Exception):
    def __init__(self, items: list[str]) -> None:
        super().__init__("; ".join(items))
        self.items = items


def _check_layout(workspace: Path, problems: list[str]) -> None:
    if not workspace.exists():
        problems.append(f"workspace directory does not exist: {workspace}")
        return
    for name in ("AGENTS.md", "personalities.yaml", "onboarding.yaml"):
        if not (workspace / name).exists():
            problems.append(f"required lesson file is missing: {name}")
    if not (workspace / "skills").is_dir():
        problems.append("required directory 'skills/' is missing")


def _check_personalities(path: Path, problems: list[str]) -> None:
    if not path.exists():
        return  # already reported by _check_layout
    try:
        data = _read_yaml(path)
    except _ProblemListError as e:
        problems.extend(e.items)
        return
    items = (data or {}).get("personalities")
    if not isinstance(items, list) or not items:
        problems.append("personalities.yaml: missing or empty 'personalities:' list")
        return
    seen_names: set[str] = set()
    has_default = False
    for i, raw in enumerate(items):
        if not isinstance(raw, dict):
            problems.append(f"personalities.yaml: entry {i} is not a mapping")
            continue
        name = raw.get("name")
        label = raw.get("label")
        description = raw.get("description")
        overlay = raw.get("overlay")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"personalities.yaml: entry {i} has no non-empty 'name'")
            continue
        if name in seen_names:
            problems.append(f"personalities.yaml: duplicate persona name {name!r}")
        seen_names.add(name)
        if name == "default":
            has_default = True
        if not isinstance(label, str) or not label.strip():
            problems.append(f"personalities.yaml: persona {name!r} has no non-empty 'label'")
        if not isinstance(description, str) or not description.strip():
            problems.append(f"personalities.yaml: persona {name!r} has no non-empty 'description'")
        if not isinstance(overlay, str):
            problems.append(f"personalities.yaml: persona {name!r} has no 'overlay' string")
        elif name != "default" and not overlay.strip():
            problems.append(
                f"personalities.yaml: persona {name!r} has empty overlay (only 'default' may)"
            )
    if not has_default:
        problems.append("personalities.yaml: required persona 'default' is missing")


def _check_onboarding(path: Path, problems: list[str]) -> None:
    if not path.exists():
        return
    try:
        data = _read_yaml(path)
    except _ProblemListError as e:
        problems.extend(e.items)
        return
    if not isinstance(data, dict):
        problems.append("onboarding.yaml: top-level must be a mapping")
        return
    for key in ONBOARDING_REQUIRED_KEYS:
        if key not in data:
            problems.append(f"onboarding.yaml: missing required key {key!r}")
    for key in (
        "pre_auth_welcome",
        "group_welcome_pre_auth",
        "group_welcome_authed",
        "post_auth_welcome",
        "help_text",
    ):
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            problems.append(f"onboarding.yaml: {key!r} is empty")
        elif isinstance(value, str):
            for ph in _PLACEHOLDER_RE.findall(value):
                if ph not in ONBOARDING_PLACEHOLDERS:
                    problems.append(
                        f"onboarding.yaml: {key!r} uses unknown placeholder {{{ph}}} "
                        f"(allowed: {sorted(ONBOARDING_PLACEHOLDERS)})"
                    )
    examples = data.get("example_prompts")
    if examples is None:
        pass  # already reported as missing
    elif not isinstance(examples, list):
        problems.append("onboarding.yaml: 'example_prompts' must be a list")
    elif not (EXAMPLE_PROMPTS_MIN <= len(examples) <= EXAMPLE_PROMPTS_MAX):
        problems.append(
            f"onboarding.yaml: 'example_prompts' must have "
            f"{EXAMPLE_PROMPTS_MIN}-{EXAMPLE_PROMPTS_MAX} entries (got {len(examples)})"
        )
    else:
        for i, item in enumerate(examples):
            if not isinstance(item, dict):
                problems.append(f"onboarding.yaml: example_prompts[{i}] is not a mapping")
                continue
            label = item.get("label")
            prompt = item.get("prompt")
            if not isinstance(label, str) or not label.strip():
                problems.append(f"onboarding.yaml: example_prompts[{i}].label is empty")
            if not isinstance(prompt, str) or not prompt.strip():
                problems.append(f"onboarding.yaml: example_prompts[{i}].prompt is empty")


def _check_infra(path: Path, problems: list[str]) -> None:
    try:
        data = _read_yaml(path)
    except _ProblemListError as e:
        problems.extend(e.items)
        return
    if data is None:
        return  # empty file is allowed
    if not isinstance(data, dict):
        problems.append("infra.yaml: top-level must be a mapping")
        return
    for key in data:
        if key not in INFRA_TOP_LEVEL_KEYS:
            problems.append(
                f"infra.yaml: unknown top-level key {key!r} "
                f"(allowed: {sorted(INFRA_TOP_LEVEL_KEYS)})"
            )
    mcp_raw = data.get("mcp_servers")
    if mcp_raw is not None:
        if not isinstance(mcp_raw, list):
            problems.append("infra.yaml: 'mcp_servers' must be a list")
        else:
            for i, raw in enumerate(mcp_raw):
                if not isinstance(raw, dict):
                    problems.append(f"infra.yaml: mcp_servers[{i}] is not a mapping")
                    continue
                if not raw.get("name"):
                    problems.append(f"infra.yaml: mcp_servers[{i}] has no 'name'")
                try:
                    MCPServerConfig.model_validate(raw)
                except Exception as e:
                    problems.append(f"infra.yaml: mcp_servers[{i}] invalid: {e}")
    media = data.get("media")
    if media is not None:
        if not isinstance(media, dict):
            problems.append("infra.yaml: 'media' must be a mapping")
        else:
            extra = set(media) - {"shared_roots"}
            if extra:
                problems.append(
                    f"infra.yaml: media has unknown keys {sorted(extra)} "
                    "(only 'shared_roots' is supported in lesson overlays)"
                )
            shared = media.get("shared_roots")
            if shared is not None:
                if not isinstance(shared, dict):
                    problems.append("infra.yaml: 'media.shared_roots' must be a mapping")
                else:
                    for alias in shared:
                        if not isinstance(alias, str) or not alias:
                            problems.append(
                                "infra.yaml: media.shared_roots alias must be a non-empty string"
                            )
                            continue
                        if alias == "media":
                            problems.append(
                                "infra.yaml: media.shared_roots alias 'media' is reserved"
                            )
                        if "/" in alias or "\\" in alias:
                            problems.append(
                                f"infra.yaml: media.shared_roots alias {alias!r} contains a slash"
                            )
                        if alias in (".", ".."):
                            problems.append(
                                f"infra.yaml: media.shared_roots alias must not be {alias!r}"
                            )


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise _ProblemListError([f"{path.name}: failed to parse — {e}"])


__all__: Iterable[str] = (
    "ExamplePrompt",
    "InfraOverlay",
    "LessonValidationError",
    "Onboarding",
    "lesson_forbidden_files",
    "load_infra_overlay",
    "load_onboarding",
    "merge_infra_into_config",
    "validate_workspace",
)
