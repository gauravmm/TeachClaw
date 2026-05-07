"""Per-conversation storage layout helpers.

The storage tree is laid out as follows:

    <workspace>/
      storage/
        _admin/                       # auth secret, prof-only; never exposed to tools
        <channel>/<chat_id>/          # this conversation's sandbox root
          profile.md                  # durable facts the bot has learned about the user
          media/                      # images and other media for this conversation
          ...                         # any other files the model writes
      common/                         # shared resources, read-only
      skills/                         # SKILL.md packages; read-only

Filesystem tools see this layout via ToolContext.storage_root /
read_roots / write_roots; see agent/tools/filesystem.py for enforcement.
"""

from pathlib import Path

from teachclaw.bus import MessageAddress


def storage_root(workspace: Path, addr: MessageAddress) -> Path:
    return workspace / "storage" / addr.channel / addr.chat_id


def media_dir(workspace: Path, addr: MessageAddress) -> Path:
    return storage_root(workspace, addr) / "media"


def common_dir(workspace: Path) -> Path:
    return workspace / "common"


def skills_dir(workspace: Path) -> Path:
    return workspace / "skills"


def admin_dir(workspace: Path) -> Path:
    return workspace / "storage" / "_admin"


def profile_path(workspace: Path, addr: MessageAddress) -> Path:
    return storage_root(workspace, addr) / "profile.md"


def ensure_user_dirs(workspace: Path, addr: MessageAddress) -> None:
    """Pre-create the per-conversation directories that the sandbox expects."""
    storage_root(workspace, addr).mkdir(parents=True, exist_ok=True)
    media_dir(workspace, addr).mkdir(parents=True, exist_ok=True)


def read_profile(workspace: Path, addr: MessageAddress) -> str | None:
    path = profile_path(workspace, addr)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def listing_for_user(workspace: Path, addr: MessageAddress) -> str:
    """Compact, deterministic listing of the user's own storage root."""
    root = storage_root(workspace, addr)
    header = str(root.relative_to(workspace) if root.is_relative_to(workspace) else root)
    return listing_for_dir(root, header=header)


def listing_for_dir(root: Path, *, header: str) -> str:
    """Compact, deterministic directory listing.

    Header line, then one line per child (sorted), with files showing a
    human size and directories showing a count of immediate children.
    Timestamps are deliberately omitted so writes that don't change
    names/sizes don't invalidate the prompt-cache prefix this listing
    sits next to.
    """
    if not root.exists():
        return f"{header}/: (empty)"
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        children = []
    if not children:
        return f"{header}/: (empty)"
    lines = [f"{header}/:"]
    for child in children:
        if child.is_file():
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            lines.append(f"  {child.name} ({_human_size(size)})")
        elif child.is_dir():
            try:
                count = sum(1 for _ in child.iterdir())
            except OSError:
                count = 0
            suffix = "" if count == 0 else f" ({count} item{'s' if count != 1 else ''})"
            lines.append(f"  {child.name}/{suffix}")
    return "\n".join(lines)


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024**2:.1f} MB"
