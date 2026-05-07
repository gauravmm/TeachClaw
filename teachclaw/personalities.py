"""Lesson personalities — system-prompt overlays only; tools/retrieval unchanged.

Definitions live at ``<workspace>/personalities.yaml`` (the workspace
*is* the lesson — see spec/SWITCHMODE.md). Schema and presence are
validated at boot by :mod:`teachclaw.lessons`; this module just loads.

The chosen name for each user persists at
``storage/<channel>/<chat_id>/personality.txt`` so it survives bot
restart but is wiped by /clear and /forgetme along with the rest of
the user's sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from teachclaw import storage as storage_layout
from teachclaw.bus import MessageAddress


@dataclass(frozen=True)
class Personality:
    name: str
    label: str
    description: str
    overlay: str


_CACHE: dict[Path, dict[str, Personality]] = {}

_DEFAULT_NAME = "default"
_FALLBACK_DEFAULT = Personality(_DEFAULT_NAME, "Default", "Neutral assistant.", "")


def _parse(path: Path) -> list[Personality]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[Personality] = []
    for raw in data.get("personalities") or []:
        out.append(
            Personality(
                name=str(raw["name"]),
                label=str(raw.get("label", raw["name"])),
                description=str(raw.get("description", "")),
                overlay=str(raw.get("overlay", "")).rstrip(),
            )
        )
    return out


def _load(workspace: Path) -> dict[str, Personality]:
    cached = _CACHE.get(workspace)
    if cached is not None:
        return cached
    path = workspace / "personalities.yaml"
    items = _parse(path) if path.exists() else [_FALLBACK_DEFAULT]
    by_name = {p.name: p for p in items}
    _CACHE[workspace] = by_name
    return by_name


def all_personalities(workspace: Path) -> tuple[Personality, ...]:
    return tuple(_load(workspace).values())


def _personality_path(workspace: Path, addr: MessageAddress) -> Path:
    return storage_layout.storage_root(workspace, addr) / "personality.txt"


def read_personality(workspace: Path, addr: MessageAddress) -> Personality:
    by_name = _load(workspace)
    path = _personality_path(workspace, addr)
    name = _DEFAULT_NAME
    if path.exists():
        try:
            name = path.read_text(encoding="utf-8").strip() or _DEFAULT_NAME
        except OSError:
            pass
    # Boot-time validate_workspace() guarantees the "default" persona
    # exists. If `name` was an unknown user-supplied value, fall back to
    # default; an absent default is a programmer error and KeyErrors.
    return by_name.get(name) or by_name[_DEFAULT_NAME]


def write_personality(workspace: Path, addr: MessageAddress, name: str) -> Personality | None:
    p = _load(workspace).get(name)
    if not p:
        return None
    path = _personality_path(workspace, addr)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(p.name, encoding="utf-8")
    return p


def clear_personality(workspace: Path, addr: MessageAddress) -> None:
    _personality_path(workspace, addr).unlink(missing_ok=True)
