"""Structured media repository for workspace files and captions."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import filetype
from loguru import logger
from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from benchclaw.bus import MessageAddress
from benchclaw.utils import _parse_timestamp, ensure_aware, now_aware


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

    address: MessageAddress | None = None
    sender_id: str | None = None
    timestamp: str | None = None  # ISO
    media_type: str = "file"
    mime_type: str | None = None
    original_name: str | None = None
    caption: str | None = None

    @field_validator("address", mode="before")
    @classmethod
    def _parse_address(cls, value: Any) -> MessageAddress | None:
        if value is None or isinstance(value, MessageAddress):
            return value
        if isinstance(value, str):
            return MessageAddress.from_string(value)
        raise TypeError(f"Unsupported media address value: {value!r}")

    @field_serializer("address")
    def _serialize_address(self, value: MessageAddress | None) -> str | None:
        return str(value) if value else None


class MediaRepository:
    """
    Persistent store for inbound media records and captions for workspace files.

    Registered inbound media still lives under `workspace/media/...`.
    Metadata lives at `workspace/.media.json` and is keyed by workspace-relative path.
    """

    def __init__(self, workspace: Path, max_age_days: int = 30) -> None:
        self.workspace = workspace
        self.media_dir = workspace / "media"
        self.meta_path = workspace / ".media.json"
        self.max_age_days = max_age_days
        self._entries: dict[str, MediaEntry] = {}

    def load(self) -> None:
        """Load metadata from the workspace root metadata file."""
        if not self.meta_path.exists():
            return
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
            self._entries.clear()
            self._load_entries(data)
        except Exception as e:
            logger.warning(f"Failed to load media metadata: {e}")

    def save(self) -> None:
        """Write metadata to the workspace root metadata file."""
        self.workspace.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        for relpath, entry in sorted(self._entries.items()):
            node = data
            parts = Path(relpath).parts
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = {"_entry": entry.model_dump(mode="json")}
        self.meta_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _address_matches(record_addr: str, query_addr: MessageAddress) -> bool:
        if record_addr == str(query_addr):
            return True
        try:
            parsed = MessageAddress.from_string(record_addr)
        except ValueError:
            return False
        if parsed.channel != query_addr.channel:
            return False
        return parsed.chat_id == query_addr.chat_id

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
        """
        Allocate a new inbound media path, record the entry, save metadata, and return the abs path.
        Caller must write the file bytes to the returned path.
        """
        assert ext.startswith(".") or ext == "", "ext should include the dot, e.g. '.jpg'"
        ts = ensure_aware(timestamp or now_aware())
        hhmm = ts.strftime("%H%M")
        mmdd = ts.strftime("%m%d")
        hash8 = address.hash8
        serial = self._next_serial(hash8, mmdd, hhmm)
        relpath = Path("media") / hash8 / mmdd / f"{hhmm}-{serial}{ext}"
        abs_path = self.workspace / relpath
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        self._entries[str(relpath)] = MediaEntry(
            address=address,
            sender_id=sender_id,
            timestamp=ts.isoformat(timespec="seconds"),
            media_type=media_type,
            mime_type=mime_type,
            original_name=original_name,
            caption=None,
        )
        self.save()
        return abs_path

    def media_relpath(self, abs_path: Path) -> str:
        """Return a workspace-relative path."""
        return str(abs_path.relative_to(self.workspace))

    def resolve_file(self, path: str) -> tuple[Path, str | None]:
        """Resolve a workspace-relative path to (absolute_path, mime_type)."""
        relpath = self._normalize_relpath(path)
        abs_path = self.workspace / relpath
        if not abs_path.is_file():
            raise FileNotFoundError(f"Media file not found: {relpath}")

        entry = self._entries.get(relpath)
        mime_type = entry.mime_type if entry else None
        if not mime_type:
            mime_type = filetype.guess_mime(str(abs_path))
        return abs_path, mime_type

    def image_block(self, path: str) -> dict[str, object]:
        """Build a provider-ready image block for one workspace-relative file path."""
        abs_path, mime_type = self.resolve_file(path)
        if not mime_type or not mime_type.startswith("image/"):
            raise ValueError(f"Path is not an image: {path}")
        data = base64.b64encode(abs_path.read_bytes()).decode()
        return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{data}"}}

    def audio_block(self, path: str) -> dict[str, object]:
        """Build a provider-ready audio block for one workspace-relative file path."""
        abs_path, mime_type = self.resolve_file(path)
        if not mime_type or not mime_type.startswith("audio/"):
            raise ValueError(f"Path is not an audio file: {path}")
        # Strip codec parameter for the block media_type (e.g. "audio/ogg; codecs=opus" → "audio/ogg")
        block_mime = mime_type.split(";")[0].strip()
        data = base64.b64encode(abs_path.read_bytes()).decode()
        return {
            "type": "input_audio",
            "source": {"type": "base64", "media_type": block_mime, "data": data},
        }

    def build_media_blocks(self, paths: Iterable[str]) -> list[dict[str, object]]:
        """Build provider-ready content blocks for images and audio, skipping unsupported paths."""
        blocks: list[dict[str, object]] = []
        for path in paths:
            try:
                _, mime_type = self.resolve_file(path)
                if mime_type and mime_type.startswith("audio/"):
                    blocks.append(self.audio_block(path))
                else:
                    blocks.append(self.image_block(path))
            except (FileNotFoundError, ValueError):
                logger.warning(f"Skipping unsupported or missing media file: {path}")
        return blocks

    def set_caption(self, path: str, caption: str) -> None:
        """Update or create a caption for any workspace-relative file path."""
        relpath = self._normalize_relpath(path)
        abs_path, mime_type = self.resolve_file(relpath)
        entry = self._entries.get(relpath)
        if entry is None:
            entry = MediaEntry(
                timestamp=self._file_timestamp(abs_path),
                media_type=self._infer_media_type(mime_type),
                mime_type=mime_type,
                original_name=abs_path.name,
            )
            self._entries[relpath] = entry
        else:
            if entry.timestamp is None:
                entry.timestamp = self._file_timestamp(abs_path)
            if not entry.mime_type:
                entry.mime_type = mime_type
            if not entry.original_name:
                entry.original_name = abs_path.name
        entry.caption = caption
        self.save()

    def iter_records(self) -> Iterable[dict[str, Any]]:
        """Yield stored metadata records keyed by workspace-relative path."""
        for relpath, entry in sorted(self._entries.items()):
            yield {
                "path": relpath,
                "address": str(entry.address) if entry.address else None,
                "sender_id": entry.sender_id,
                "timestamp": entry.timestamp,
                "media_type": entry.media_type,
                "mime_type": entry.mime_type,
                "original_name": entry.original_name,
                "caption": entry.caption,
            }

    def search(
        self,
        *,
        query: str | None = None,
        address: MessageAddress | None = None,
        sender_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search saved file metadata and captions across the whole workspace."""
        limit = max(1, min(limit, 20))
        needle = (query or "").strip().casefold()
        lower_from = self._parse_date_bound(date_from, end=False)
        upper_to = self._parse_date_bound(date_to, end=True)
        matches: list[tuple[int, float, dict[str, Any]]] = []

        # TODO: Replace this unstructured data dict with a structured data class with proper types and validation.
        # ASSUMPTION: The repository will contain <50 entries. If that grows much larger, we'll want to build an index or use a real database instead of scanning all entries on each search.
        # TODO: Emit a warning only once using a lru-cache helper placed in utils.
        for record in self.iter_records():
            if record.get("caption") is None:
                continue
            record_addr = record["address"]
            if address is not None and not self._address_matches(record_addr, address):
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
        address = (record.get("address") or "").casefold()
        sender_id = (record.get("sender_id") or "").casefold()

        if any(value == needle for value in (path, name, address, sender_id)):
            return 300
        if needle in path or needle in name:
            return 200
        if needle in caption:
            return 100
        if any(needle in hay for hay in (mime, address, sender_id)):
            return 50
        return -1

    async def __aenter__(self) -> "MediaRepository":
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.load()
        if purged := self._purge_old():
            logger.info(f"Purged {purged} old media files")
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    def _purge_old(self) -> int:
        """Delete old registered media files and their metadata records."""
        cutoff = now_aware() - timedelta(days=self.max_age_days)
        deleted = 0
        for relpath, entry in list(self._entries.items()):
            if not relpath.startswith("media/"):
                continue
            if entry.timestamp is None:
                continue
            if _parse_timestamp(entry.timestamp) >= cutoff:
                continue
            (self.workspace / relpath).unlink(missing_ok=True)
            del self._entries[relpath]
            deleted += 1

        for d in sorted(self.media_dir.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()

        if deleted:
            self.save()
        return deleted

    def _normalize_relpath(self, path: str) -> str:
        rel = Path(path)
        if rel.is_absolute():
            raise ValueError(f"Path is outside the workspace: {path}")
        parts = []
        for part in rel.parts:
            if part in ("", "."):
                continue
            if part == "..":
                raise ValueError(f"Path is outside the workspace: {path}")
            parts.append(part)
        if not parts:
            raise ValueError(f"Invalid workspace-relative path: {path}")
        return str(Path(*parts))

    def _next_serial(self, hash8: str, mmdd: str, hhmm: str) -> str:
        parent = Path("media") / hash8 / mmdd
        max_serial = 0
        for relpath in self._entries:
            path = Path(relpath)
            if path.parent != parent or not path.stem.startswith(f"{hhmm}-"):
                continue
            _, serial = path.stem.split("-", 1)
            max_serial = max(max_serial, int(serial))
        media_bucket = self.media_dir / hash8 / mmdd
        if media_bucket.exists():
            for file_path in media_bucket.glob(f"{hhmm}-*"):
                stem = file_path.stem
                if "-" not in stem:
                    continue
                _, serial = stem.split("-", 1)
                if serial.isdigit():
                    max_serial = max(max_serial, int(serial))
        return f"{max_serial + 1:02d}"

    def _load_entries(self, node: dict[str, Any], prefix: tuple[str, ...] = ()) -> None:
        for name, value in node.items():
            if not isinstance(value, dict):
                raise TypeError(f"Invalid metadata node at {'/'.join(prefix + (name,))}")
            if "_entry" in value:
                relpath = str(Path(*prefix, name))
                self._entries[relpath] = MediaEntry.model_validate(value["_entry"])
                continue
            self._load_entries(value, prefix + (name,))

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
