"""Lecture personalities — system-prompt overlays only; tools/retrieval unchanged.

Definitions ship in ``teachclaw/data/personalities.yaml`` and can be
overridden by placing a ``personalities.yaml`` of the same shape in the
workspace root. The chosen name for each user persists at
``storage/<channel>/<chat_id>/personality.txt`` so it survives bot
restart but is wiped by /clear and /forgetme along with the rest of
the user's sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from loguru import logger

from teachclaw import storage as storage_layout
from teachclaw.bus import MessageAddress

_PACKAGED = Path(__file__).parent / "data" / "personalities.yaml"


@dataclass(frozen=True)
class Personality:
    name: str
    label: str
    description: str
    overlay: str


_CACHE: dict[Path, dict[str, Personality]] = {}


def _parse(path: Path) -> list[Personality]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning(f"Failed to load personalities from {path}: {e}")
        return []
    out: list[Personality] = []
    for raw in data.get("personalities") or []:
        try:
            out.append(
                Personality(
                    name=str(raw["name"]),
                    label=str(raw.get("label", raw["name"])),
                    description=str(raw.get("description", "")),
                    overlay=str(raw.get("overlay", "")).rstrip(),
                )
            )
        except (KeyError, TypeError) as e:
            logger.warning(f"Skipping malformed personality in {path}: {e}")
    return out


def _load(workspace: Path) -> dict[str, Personality]:
    cached = _CACHE.get(workspace)
    if cached is not None:
        return cached

    items = _parse(_PACKAGED)
    override = workspace / "personalities.yaml"
    if override.exists():
        items = _parse(override) or items

    if not any(p.name == "default" for p in items):
        items.insert(0, Personality("default", "Default", "Neutral, direct class assistant.", ""))

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
    name = "default"
    if path.exists():
        try:
            name = path.read_text(encoding="utf-8").strip() or "default"
        except OSError:
            pass
    return by_name.get(name) or by_name["default"]


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
