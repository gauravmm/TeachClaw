"""Lecture personalities — system-prompt overlays only; tools/retrieval unchanged.

The set is fixed and small; per-spec each user picks one and it persists for
their session. /clear clears it. The selection is stored on disk under
storage/<channel>/<chat_id>/personality.txt so it survives bot restart but
is still wiped by /forgetme along with the rest of the user's sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from benchclaw import storage as storage_layout
from benchclaw.bus import MessageAddress


@dataclass(frozen=True)
class Personality:
    name: str
    label: str
    description: str
    overlay: str


_PERSONALITIES: tuple[Personality, ...] = (
    Personality(
        name="default",
        label="Default",
        description="Neutral, direct class assistant.",
        overlay="",
    ),
    Personality(
        name="skeptical_cfo",
        label="Skeptical CFO",
        description="Penny-pinching CFO. Asks for the unit economics.",
        overlay=(
            "Adopt the voice of a skeptical CFO. Open with the cost question. "
            "Demand unit economics, payback period, and gross-margin impact "
            "before endorsing any AI initiative. Push back on vendor hype with "
            "specific numbers; if the user has not provided numbers, ask for "
            "the two or three figures you would need to decide. Keep replies "
            "short and unsentimental."
        ),
    ),
    Personality(
        name="vc_partner",
        label="VC Partner",
        description="Series-B VC partner. Pattern-matches to comparable deals.",
        overlay=(
            "Adopt the voice of a Series-B VC partner. Frame answers as theses "
            "and anti-theses. Reference comparable companies and recent deal "
            "patterns where useful. Probe for moat, distribution, and the "
            "founder's edge. Keep prose brisk; one or two crisp insights beats "
            "a comprehensive answer."
        ),
    ),
    Personality(
        name="mck_analyst",
        label="McKinsey Analyst",
        description="Structured analyst. MECE frameworks and 2x2s.",
        overlay=(
            "Adopt the voice of a McKinsey analyst. Default to MECE structure: "
            "two or three orthogonal dimensions and a short bullet under each. "
            "Offer a 2x2 or value-chain Mermaid diagram when it sharpens the "
            "argument. Avoid jargon for its own sake; prefer crisp business "
            "English."
        ),
    ),
    Personality(
        name="professor",
        label="Professor",
        description="Lecturer. Patient explanations with examples.",
        overlay=(
            "Adopt the voice of a patient business-school professor. Define "
            "key terms before using them. Anchor every claim with a concrete "
            "example. When useful, end with a one-sentence takeaway the "
            "student can write down."
        ),
    ),
)

_BY_NAME: dict[str, Personality] = {p.name: p for p in _PERSONALITIES}


def all_personalities() -> tuple[Personality, ...]:
    return _PERSONALITIES


def get(name: str) -> Personality | None:
    return _BY_NAME.get(name)


def default() -> Personality:
    return _BY_NAME["default"]


def _personality_path(workspace: Path, addr: MessageAddress) -> Path:
    return storage_layout.storage_root(workspace, addr) / "personality.txt"


def read_personality(workspace: Path, addr: MessageAddress) -> Personality:
    path = _personality_path(workspace, addr)
    if not path.exists():
        return default()
    try:
        name = path.read_text(encoding="utf-8").strip()
    except OSError:
        return default()
    return _BY_NAME.get(name) or default()


def write_personality(workspace: Path, addr: MessageAddress, name: str) -> Personality | None:
    p = _BY_NAME.get(name)
    if not p:
        return None
    path = _personality_path(workspace, addr)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(p.name, encoding="utf-8")
    return p


def clear_personality(workspace: Path, addr: MessageAddress) -> None:
    path = _personality_path(workspace, addr)
    path.unlink(missing_ok=True)
