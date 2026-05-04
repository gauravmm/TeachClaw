"""Channel-agnostic citation parsing, storage, and rendering.

See spec/CITATIONS.md. Telegram is the first consumer; other channels
(Claude Code, SMTP email, WhatsApp) wire up the same surface by
calling ``strip_citations`` on outbound text, recording the parsed
citations in a per-channel ``CitationStore``, and ``render_list``-ing
them in their preferred dialect when the user asks for sources.
"""

from benchclaw.citations.parsing import (
    Citation,
    extract_kb_records,
    strip_citations,
)
from benchclaw.citations.render import RenderFormat, render_list
from benchclaw.citations.store import CitationEntry, CitationStore

__all__ = [
    "Citation",
    "CitationEntry",
    "CitationStore",
    "RenderFormat",
    "extract_kb_records",
    "render_list",
    "strip_citations",
]
