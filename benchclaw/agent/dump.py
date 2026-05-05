"""Pretty-printer for the LLM-prompt debug dump.

The agent loop optionally writes every prompt list to disk for
inspection. Tool message ``content`` and tool-call ``arguments`` live
in the wire schema as opaque JSON strings; rendering those literally
yields a wall of escaped quotes and ``\\n`` markers. The helpers here
inflate JSON-string fields into structured objects (with a JSONL
fallback for tools like ``kb__search`` that emit one record per line)
so the dump stays readable. The wire messages themselves are never
mutated — only the dump copy is rewritten.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger
from pathvalidate import sanitize_filename

from benchclaw.bus import MessageAddress


def dump_messages(
    dir_path: Path | None,
    addr: MessageAddress,
    messages: list[dict[str, object]],
) -> None:
    """Write ``messages`` as pretty JSON into ``dir_path`` under a
    per-conversation filename derived from ``addr``. Inflates any
    string-encoded JSON payloads first. No-op when ``dir_path`` is
    None so the fast path doesn't pay any inflation cost.
    """
    if dir_path is None:
        return
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
        filename = sanitize_filename(str(addr).replace(":", "_")) + ".json"
        (dir_path / filename).write_text(
            json.dumps(
                [_inflate(m) for m in messages],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Failed to write debug dump: {e}")


def _try_parse_json(value: object) -> object:
    """Parse a string that's actually JSON or newline-delimited JSON.

    Returns the parsed object on success, or the original value on
    anything else (including non-strings, plain prose, or partial
    JSON). The JSONL fallback covers tool results with multiple
    records concatenated by newlines.
    """
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if len(lines) < 2:
        return value
    parsed: list[object] = []
    for ln in lines:
        try:
            parsed.append(json.loads(ln))
        except json.JSONDecodeError:
            return value
    return parsed


def _inflate(message: dict[str, object]) -> dict[str, object]:
    out = dict(message)
    if out.get("role") == "tool":
        out["content"] = _try_parse_json(out.get("content"))
    tool_calls = out.get("tool_calls")
    if isinstance(tool_calls, list):
        inflated_calls: list[object] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                inflated_calls.append(tc)
                continue
            tc_copy = dict(tc)
            fn = tc_copy.get("function")
            if isinstance(fn, dict):
                fn_copy = dict(fn)
                fn_copy["arguments"] = _try_parse_json(fn_copy.get("arguments"))
                tc_copy["function"] = fn_copy
            inflated_calls.append(tc_copy)
        out["tool_calls"] = inflated_calls
    return out
