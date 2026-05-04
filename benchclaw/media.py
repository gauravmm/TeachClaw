"""Per-user media repository.

Each conversation owns its own media tree under
``storage/<channel>/<chat_id>/media/`` with a flat naming scheme:
``<YYYYMMDDTHHMMSS>-<NN>.<ext>``. The serial disambiguates files
registered in the same second. Per-user metadata lives at
``storage/<channel>/<chat_id>/.media.json``.

The model sees media via the sandbox-relative path ``media/<filename>``;
nothing outside the per-user storage tree is reachable.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import filetype
from loguru import logger
from pydantic import BaseModel, ConfigDict

from benchclaw import storage as storage_layout
from benchclaw.bus import MessageAddress
from benchclaw.utils import _parse_timestamp, ensure_aware, now_aware

MEDIA_PREFIX = "media"


def extension_for_mime(mime_type: str | None) -> str:
    """Return a leading-dot file extension inferred from a MIME type.

    Strips any codec parameters (e.g. ``audio/ogg; codecs=opus`` → ``audio/ogg``)
    before lookup. Returns the empty string when the MIME type is missing or
    unrecognized; callers should fall back to whatever channel-specific naming
    they already use in that case.
    """
    if not mime_type:
        return ""
    head = mime_type.split(";", 1)[0].strip()
    t = filetype.get_type(mime=head)
    if t is None or not t.extension:
        return ""
    return f".{t.extension}"


class MediaEntry(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sender_id: str | None = None
    timestamp: str | None = None  # ISO
    media_type: str = "file"
    mime_type: str | None = None
    original_name: str | None = None
    caption: str | None = None


class MediaRepository:
    """Per-user inbound-media store.

    Files for ``<channel>:<chat_id>`` live at
    ``<workspace>/storage/<channel>/<chat_id>/media/<filename>``; metadata
    at ``<workspace>/storage/<channel>/<chat_id>/.media.json``. Entries are
    keyed by the sandbox-relative path the model sees, e.g.
    ``media/20260504T182300-01.jpg``.
    """

    def __init__(self, workspace: Path, max_age_days: int = 30) -> None:
        self.workspace = workspace
        self.max_age_days = max_age_days
        self._cache: dict[MessageAddress, dict[str, MediaEntry]] = {}

    def _meta_path(self, address: MessageAddress) -> Path:
        return storage_layout.storage_root(self.workspace, address) / ".media.json"

    def _media_dir(self, address: MessageAddress) -> Path:
        return storage_layout.media_dir(self.workspace, address)

    def _entries(self, address: MessageAddress) -> dict[str, MediaEntry]:
        cached = self._cache.get(address)
        if cached is not None:
            return cached
        entries: dict[str, MediaEntry] = {}
        meta_path = self._meta_path(address)
        if meta_path.exists():
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                for relpath, payload in data.items():
                    entries[relpath] = MediaEntry.model_validate(payload)
            except Exception as e:
                logger.warning(f"Failed to load media metadata for {address}: {e}")
        self._cache[address] = entries
        return entries

    def _save(self, address: MessageAddress) -> None:
        entries = self._cache.get(address)
        if entries is None:
            return
        meta_path = self._meta_path(address)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        data = {rel: entry.model_dump(mode="json") for rel, entry in sorted(entries.items())}
        meta_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def model_relpath(abs_path: Path) -> str:
        """Sandbox-relative path the model sees for an absolute media path."""
        return f"{MEDIA_PREFIX}/{abs_path.name}"

    def register(
        self,
        address: MessageAddress,
        sender_id: str,
        media_type: str,
        ext: str,
        mime_type: str | None,
        timestamp: datetime | None = None,
        original_name: str | None = None,
    ) -> Path:
        """Allocate a new inbound media path under this user's media dir.

        Returns the absolute path; the caller writes file bytes to it.
        """
        assert ext.startswith(".") or ext == "", "ext should include the dot, e.g. '.jpg'"
        ts = ensure_aware(timestamp or now_aware())
        prefix = ts.strftime("%Y%m%dT%H%M%S")
        entries = self._entries(address)
        media_dir = self._media_dir(address)
        serial = self._next_serial(entries, media_dir, prefix)
        filename = f"{prefix}-{serial}{ext}"
        relpath = f"{MEDIA_PREFIX}/{filename}"
        abs_path = media_dir / filename
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        entries[relpath] = MediaEntry(
            sender_id=sender_id,
            timestamp=ts.isoformat(timespec="seconds"),
            media_type=media_type,
            mime_type=mime_type,
            original_name=original_name,
            caption=None,
        )
        self._save(address)
        return abs_path

    def resolve_file(self, address: MessageAddress, path: str) -> tuple[Path, str | None]:
        """Resolve a sandbox-relative media path under this user's media dir."""
        relpath = self._normalize_relpath(path)
        abs_path = self._media_dir(address) / Path(relpath).name
        if not abs_path.is_file():
            raise FileNotFoundError(f"Media file not found: {relpath}")
        entry = self._entries(address).get(relpath)
        mime_type = entry.mime_type if entry else None
        if not mime_type:
            mime_type = filetype.guess_mime(str(abs_path))
        return abs_path, mime_type

    def image_block(self, address: MessageAddress, path: str) -> dict[str, object]:
        abs_path, mime_type = self.resolve_file(address, path)
        if not mime_type or not mime_type.startswith("image/"):
            raise ValueError(f"Path is not an image: {path}")
        data = base64.b64encode(abs_path.read_bytes()).decode()
        return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{data}"}}

    def audio_block(self, address: MessageAddress, path: str) -> dict[str, object]:
        abs_path, mime_type = self.resolve_file(address, path)
        if not mime_type or not mime_type.startswith("audio/"):
            raise ValueError(f"Path is not an audio file: {path}")
        block_mime = mime_type.split(";")[0].strip()
        data = base64.b64encode(abs_path.read_bytes()).decode()
        return {
            "type": "input_audio",
            "source": {"type": "base64", "media_type": block_mime, "data": data},
        }

    def build_media_blocks(
        self, address: MessageAddress, paths: Iterable[str]
    ) -> list[dict[str, object]]:
        blocks: list[dict[str, object]] = []
        for path in paths:
            try:
                _, mime_type = self.resolve_file(address, path)
                if mime_type and mime_type.startswith("audio/"):
                    blocks.append(self.audio_block(address, path))
                else:
                    blocks.append(self.image_block(address, path))
            except FileNotFoundError, ValueError:
                logger.warning(f"Skipping unsupported or missing media file: {path}")
        return blocks

    def set_caption(self, address: MessageAddress, path: str, caption: str) -> None:
        relpath = self._normalize_relpath(path)
        abs_path, mime_type = self.resolve_file(address, relpath)
        entries = self._entries(address)
        entry = entries.get(relpath)
        if entry is None:
            entry = MediaEntry(
                timestamp=self._file_timestamp(abs_path),
                media_type=self._infer_media_type(mime_type),
                mime_type=mime_type,
                original_name=abs_path.name,
            )
            entries[relpath] = entry
        else:
            if entry.timestamp is None:
                entry.timestamp = self._file_timestamp(abs_path)
            if not entry.mime_type:
                entry.mime_type = mime_type
            if not entry.original_name:
                entry.original_name = abs_path.name
        entry.caption = caption
        self._save(address)

    def iter_records(self, address: MessageAddress) -> Iterable[dict[str, Any]]:
        for relpath, entry in sorted(self._entries(address).items()):
            yield {
                "path": relpath,
                "sender_id": entry.sender_id,
                "timestamp": entry.timestamp,
                "media_type": entry.media_type,
                "mime_type": entry.mime_type,
                "original_name": entry.original_name,
                "caption": entry.caption,
            }

    def search(
        self,
        address: MessageAddress,
        *,
        query: str | None = None,
        sender_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search this user's captioned media."""
        limit = max(1, min(limit, 20))
        needle = (query or "").strip().casefold()
        lower_from = self._parse_date_bound(date_from, end=False)
        upper_to = self._parse_date_bound(date_to, end=True)
        matches: list[tuple[int, float, dict[str, Any]]] = []

        for record in self.iter_records(address):
            if record.get("caption") is None:
                continue
            if sender_id is not None and record["sender_id"] != sender_id:
                continue
            record_ts = self._record_timestamp(record)
            if lower_from is not None and (record_ts is None or record_ts < lower_from):
                continue
            if upper_to is not None and (record_ts is None or record_ts > upper_to):
                continue
            score = self._score_record(record, needle)
            if needle and score < 0:
                continue
            ts_key = record_ts.timestamp() if record_ts else float("-inf")
            matches.append((score, ts_key, record))

        matches.sort(key=lambda item: (-item[0], -item[1], item[2]["path"]))
        return [record for _, _, record in matches[:limit]]

    @staticmethod
    def _parse_date_bound(value: str | None, *, end: bool) -> datetime | None:
        if not value:
            return None
        parsed = _parse_timestamp(value)
        if "T" in value:
            return parsed
        if end:
            return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
        return parsed.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _record_timestamp(record: dict[str, Any]) -> datetime | None:
        value = record.get("timestamp")
        return _parse_timestamp(value) if value else None

    @staticmethod
    def _score_record(record: dict[str, Any], needle: str) -> int:
        if not needle:
            return 0
        path = str(record["path"]).casefold()
        name = (record.get("original_name") or "").casefold()
        caption = (record.get("caption") or "").casefold()
        mime = (record.get("mime_type") or "").casefold()
        sender_id = (record.get("sender_id") or "").casefold()

        if any(value == needle for value in (path, name, sender_id)):
            return 300
        if needle in path or needle in name:
            return 200
        if needle in caption:
            return 100
        if any(needle in hay for hay in (mime, sender_id)):
            return 50
        return -1

    async def __aenter__(self) -> "MediaRepository":
        purged = self._purge_old()
        if purged:
            logger.info(f"Purged {purged} old media files")
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    def _purge_old(self) -> int:
        """Walk every per-user storage dir and prune expired media."""
        cutoff = now_aware() - timedelta(days=self.max_age_days)
        storage_root = self.workspace / "storage"
        if not storage_root.is_dir():
            return 0
        deleted = 0
        for channel_dir in storage_root.iterdir():
            if not channel_dir.is_dir() or channel_dir.name == "_admin":
                continue
            for chat_dir in channel_dir.iterdir():
                if not chat_dir.is_dir():
                    continue
                addr = MessageAddress(channel=channel_dir.name, chat_id=chat_dir.name)
                entries = self._entries(addr)
                changed = False
                for relpath, entry in list(entries.items()):
                    if entry.timestamp is None:
                        continue
                    if _parse_timestamp(entry.timestamp) >= cutoff:
                        continue
                    (self._media_dir(addr) / Path(relpath).name).unlink(missing_ok=True)
                    del entries[relpath]
                    deleted += 1
                    changed = True
                if changed:
                    self._save(addr)
        return deleted

    @staticmethod
    def _normalize_relpath(path: str) -> str:
        rel = Path(path)
        if rel.is_absolute():
            raise ValueError(f"Path is outside the media sandbox: {path}")
        parts = []
        for part in rel.parts:
            if part in ("", "."):
                continue
            if part == "..":
                raise ValueError(f"Path is outside the media sandbox: {path}")
            parts.append(part)
        if len(parts) != 2 or parts[0] != MEDIA_PREFIX:
            raise ValueError(f"Media paths must look like 'media/<filename>': {path}")
        return f"{MEDIA_PREFIX}/{parts[1]}"

    @staticmethod
    def _next_serial(entries: dict[str, MediaEntry], media_dir: Path, prefix: str) -> str:
        max_serial = 0
        for relpath in entries:
            stem = Path(relpath).stem
            if not stem.startswith(f"{prefix}-"):
                continue
            _, serial = stem.split("-", 1)
            if serial.isdigit():
                max_serial = max(max_serial, int(serial))
        if media_dir.exists():
            for file_path in media_dir.glob(f"{prefix}-*"):
                stem = file_path.stem
                if "-" not in stem:
                    continue
                _, serial = stem.split("-", 1)
                if serial.isdigit():
                    max_serial = max(max_serial, int(serial))
        return f"{max_serial + 1:02d}"

    @staticmethod
    def _infer_media_type(mime_type: str | None) -> str:
        if not mime_type:
            return "file"
        prefix = mime_type.split("/", 1)[0]
        if prefix in {"image", "audio", "video"}:
            return prefix
        return "file"

    @staticmethod
    def _file_timestamp(path: Path) -> str:
        return ensure_aware(datetime.fromtimestamp(path.stat().st_mtime)).isoformat(
            timespec="seconds"
        )
