"""Channel-agnostic citation parsing and rendering.

Pure-string transforms with no per-message state. Channels keep their
own minimal `message_id → raw_outbound_record` index so a reaction
on an old reply can re-derive citations on demand by running
``strip_citations`` on the original assistant content and
``extract_kb_records`` on the tool-call trace.
"""

from teachclaw.citations.parsing import (
    Citation,
    extract_kb_records,
    strip_citations,
)
from teachclaw.citations.render import RenderFormat, render_list
from teachclaw.citations.validation import (
    kb_records_from_events,
    unverified_postscript,
    validate_citations,
)

__all__ = [
    "Citation",
    "RenderFormat",
    "extract_kb_records",
    "kb_records_from_events",
    "render_list",
    "strip_citations",
    "unverified_postscript",
    "validate_citations",
]
