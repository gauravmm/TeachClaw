"""Render a citation list into a channel's preferred dialect.

Three dialects today:

- ``PLAIN``: ``[1] title — section_path (URL)`` plus indented claims. No
  markup. Suitable for terminals.
- ``MARKDOWN``: ``[1] [title — section_path](URL)`` plus indented claims.
  Works for Claude Code and any markdown email.
- ``TELEGRAM_HTML``: ``[1] <a href="URL">title — section_path</a>`` plus
  indented claims. Goes alongside ``parse_mode="HTML"`` and
  ``disable_web_page_preview=True``.

All dialects share the same per-citation shape: one heading line, one
inline claim line when there's a single phrasing, a bulleted list when
there are multiple, and a fallback to the bare id when no kb_record
matches.
"""

from __future__ import annotations

import html
from enum import StrEnum

from teachclaw.citations.parsing import Citation

_CLAIM_MAX_CHARS = 240


class RenderFormat(StrEnum):
    PLAIN = "plain"
    MARKDOWN = "markdown"
    TELEGRAM_HTML = "telegram_html"


def render_list(
    citations: list[Citation],
    kb_records: dict[str, dict],
    *,
    fmt: RenderFormat,
) -> str:
    lines: list[str] = []
    for i, c in enumerate(citations, start=1):
        record = kb_records.get(c.id) or {}
        title = (record.get("title") or "").strip()
        section_path = (record.get("section_path") or "").strip()
        source = (record.get("source") or "").strip()

        label_parts = [p for p in (title, section_path) if p]
        label = " — ".join(label_parts) if label_parts else c.id

        head = _format_heading(i, c.id, label, source, fmt)
        claims = [s.strip() for s in c.claims if s.strip()]
        body = _format_claims(claims, fmt)
        lines.append(head + body if body else head)
    return "\n".join(lines)


def _format_heading(
    index: int,
    cid: str,
    label: str,
    source: str,
    fmt: RenderFormat,
) -> str:
    if fmt is RenderFormat.TELEGRAM_HTML:
        if source:
            return f'[{index}] <a href="{html.escape(source, quote=True)}">{html.escape(label)}</a>'
        return f"[{index}] <code>{html.escape(cid)}</code>"
    if fmt is RenderFormat.MARKDOWN:
        if source:
            # Escape ] in label and ) in URL so the link doesn't break early.
            safe_label = label.replace("]", "\\]")
            safe_source = source.replace(")", "\\)")
            return f"[{index}] [{safe_label}]({safe_source})"
        return f"[{index}] `{cid}`"
    # PLAIN
    if source:
        return f"[{index}] {label} ({source})"
    return f"[{index}] {cid}"


def _format_claims(claims: list[str], fmt: RenderFormat) -> str:
    if not claims:
        return ""
    indent = "    " if fmt is RenderFormat.TELEGRAM_HTML else "  "
    truncated = [c[:_CLAIM_MAX_CHARS] for c in claims]
    if fmt is RenderFormat.TELEGRAM_HTML:
        truncated = [html.escape(c) for c in truncated]
    if len(truncated) == 1:
        return f"\n{indent}{truncated[0]}"
    return "".join(f"\n{indent}• {c}" for c in truncated)
