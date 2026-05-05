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
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from loguru import logger

from benchclaw.agent.prompt import PromptBuild
from benchclaw.bus import MessageAddress


def _hash_message(msg: dict[str, object]) -> str:
    payload = json.dumps(
        {"role": msg.get("role"), "content": msg.get("content")},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _content_text(msg: dict[str, object]) -> str:
    """Best-effort plain-text extraction for diff context lines."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


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


@dataclass
class PromptCacheMonitor:
    _last: dict[MessageAddress, _Snapshot] = field(default_factory=dict)
    _warned: dict[MessageAddress, set[str]] = field(default_factory=dict)

    def observe(self, addr: MessageAddress, build: PromptBuild) -> None:
        """Compare the new stable prefix against the previous turn's snapshot."""
        messages = build.messages
        stable_prefix_end = build.stable_prefix_end
        if stable_prefix_end <= 0 or not messages:
            return

        new_system = _content_text(messages[0]) if messages[0].get("role") == "system" else ""
        history = messages[1:stable_prefix_end] if new_system else messages[:stable_prefix_end]
        new_hashes = tuple(_hash_message(m) for m in history)

        prev = self._last.get(addr)
        self._last[addr] = _Snapshot(new_system, new_hashes)
        if prev is None:
            return

        if new_system != prev.system_message:
            self._report_system_diff(addr, prev.system_message, new_system)

        for i, prev_hash in enumerate(prev.history_hashes):
            if i >= len(new_hashes):
                break
            if new_hashes[i] != prev_hash:
                # Index in the history slice is i; full-message index is i+1
                # when there's a system message, otherwise i.
                full_idx = i + (1 if new_system else 0)
                self._report_history_diff(addr, full_idx, prev_hash, new_hashes[i], messages)
                break

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
        content_preview = _content_text(msg)[:200].replace("\n", "\\n")
        logger.warning(
            f"Prompt cache: history diverged for {addr} at message index {full_idx} "
            f"(role={role}, was {prev_hash}, now {new_hash}). "
            f"new content: {content_preview}"
        )

    def forget(self, addr: MessageAddress) -> None:
        """Drop tracking state for an address (e.g. after /clear or /forgetme)."""
        self._last.pop(addr, None)
        self._warned.pop(addr, None)
