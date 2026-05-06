"""Per-address watchdog for prompt-cache busting.

The agent loop builds a fresh prompt every turn. The leading portion —
system message plus everything in conversation history before the
synthetic <current_time>/<storage_listing>/<persona> injection — is
supposed to be byte-identical from one turn to the next so that any
upstream prefix cache (vLLM, Anthropic, etc.) actually hits.

`PromptCacheMonitor.observe` is called after each render with the full
message list and the index of the synthetic injection (or where the
latest user message starts when nothing was injected). It compares the
"stable prefix" against the previous turn's stable prefix and logs a
warning on the first divergence — naming the offset, the message index,
and a short context window — so a regression is immediately visible.

Warn-only. Repeated identical fingerprints are de-duplicated per
address to avoid log spam when a divergence persists across turns.

When constructed with a ``log_dir``, every ``observe`` call also
appends a JSONL record (cached length, new length, invalidated count)
to ``<log_dir>/cache_log_<addr>.jsonl`` for offline analysis.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from pathvalidate import sanitize_filename

from teachclaw.agent.prompt import PromptBuild
from teachclaw.bus import MessageAddress


def _hash_message(msg: dict[str, object]) -> str:
    payload = json.dumps(
        {"role": msg.get("role"), "content": msg.get("content")},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _first_diff_offset(a: str, b: str) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def _excerpt(text: str, offset: int, window: int = 120) -> str:
    start = max(0, offset - window // 2)
    end = min(len(text), offset + window // 2)
    snippet = text[start:end].replace("\n", "\\n")
    return f"…{snippet}…" if start > 0 or end < len(text) else snippet


@dataclass
class _Snapshot:
    system_message: str
    history_hashes: tuple[str, ...]


class PromptCacheMonitor:
    def __init__(self, log_dir: Path | None = None) -> None:
        self._last: dict[MessageAddress, _Snapshot] = {}
        self._warned: dict[MessageAddress, set[str]] = {}
        self._log_dir = log_dir

    def observe(self, addr: MessageAddress, build: PromptBuild) -> None:
        """Compare the new stable prefix against the previous turn's snapshot."""
        messages = build.messages
        stable_prefix_end = build.stable_prefix_end
        if stable_prefix_end <= 0 or not messages:
            return

        first = messages[0]
        if first.get("role") == "system":
            content = first.get("content")
            new_system = (
                content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            )
        else:
            new_system = ""
        history = messages[1:stable_prefix_end] if new_system else messages[:stable_prefix_end]
        new_hashes = tuple(_hash_message(m) for m in history)

        prev = self._last.get(addr)
        self._last[addr] = _Snapshot(new_system, new_hashes)

        common = 0
        if prev is not None:
            for i, prev_hash in enumerate(prev.history_hashes):
                if i >= len(new_hashes) or new_hashes[i] != prev_hash:
                    break
                common = i + 1
        prev_len = len(prev.history_hashes) if prev is not None else 0
        system_changed = prev is not None and prev.system_message != new_system
        self._record_log(addr, prev_len, len(new_hashes), common, system_changed)

        if prev is None:
            return

        if system_changed:
            self._report_system_diff(addr, prev.system_message, new_system)

        if common < prev_len and common < len(new_hashes):
            full_idx = common + (1 if new_system else 0)
            self._report_history_diff(
                addr, full_idx, prev.history_hashes[common], new_hashes[common], messages
            )

    def _seen(self, addr: MessageAddress, fingerprint: str) -> bool:
        seen = self._warned.setdefault(addr, set())
        if fingerprint in seen:
            return True
        seen.add(fingerprint)
        return False

    def _report_system_diff(self, addr: MessageAddress, prev: str, new: str) -> None:
        prev_hash = hashlib.sha256(prev.encode("utf-8")).hexdigest()[:8]
        new_hash = hashlib.sha256(new.encode("utf-8")).hexdigest()[:8]
        if self._seen(addr, f"system:{prev_hash}->{new_hash}"):
            return
        offset = _first_diff_offset(prev, new)
        logger.warning(
            f"Prompt cache: system message diverged for {addr} at offset {offset} "
            f"(was {prev_hash}, now {new_hash}).\n"
            f"  prev: {_excerpt(prev, offset)}\n"
            f"  new:  {_excerpt(new, offset)}"
        )

    def _report_history_diff(
        self,
        addr: MessageAddress,
        full_idx: int,
        prev_hash: str,
        new_hash: str,
        messages: list[dict[str, object]],
    ) -> None:
        if self._seen(addr, f"history:{full_idx}:{prev_hash}->{new_hash}"):
            return
        msg = messages[full_idx]
        role = msg.get("role", "?")
        content = msg.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        content_preview = text[:200].replace("\n", "\\n")
        logger.warning(
            f"Prompt cache: history diverged for {addr} at message index {full_idx} "
            f"(role={role}, was {prev_hash}, now {new_hash}). "
            f"new content: {content_preview}"
        )

    def _record_log(
        self,
        addr: MessageAddress,
        prev_len: int,
        new_len: int,
        common: int,
        system_changed: bool,
    ) -> None:
        if self._log_dir is None:
            return
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            filename = sanitize_filename(str(addr).replace(":", "_")) + ".cache.jsonl"
            record = {
                "ts": time.time(),
                "addr": str(addr),
                "prev_len": prev_len,
                "new_len": new_len,
                "cached_len": common,
                "invalidated_len": max(0, prev_len - common),
                "system_changed": system_changed,
            }
            with (self._log_dir / filename).open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"Failed to write cache log: {e}")

    def forget(self, addr: MessageAddress) -> None:
        """Drop tracking state for an address (e.g. after /clear or /forgetme)."""
        self._last.pop(addr, None)
        self._warned.pop(addr, None)
