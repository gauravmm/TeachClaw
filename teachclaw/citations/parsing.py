"""Citation parsing and kb-record extraction.

Pure-string transforms with no channel coupling. ``strip_citations``
turns model output containing ``<citation id="…">…</citation>`` markers
into displayable text plus a deduped citation list. ``extract_kb_records``
walks ``ToolCallTrace`` results from kb-style MCP tools and pulls the
``{id, title, source, section_path, …}`` records that the renderer uses
to build clickable source headers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from teachclaw.bus import ToolCallTrace


@dataclass
class Citation:
    """One cited source plus every distinct phrasing the model used for it.

    ``claims`` is in first-appearance order; repeated phrasings are dropped
    so the rendered list shows each unique claim once.
    """

    id: str
    claims: list[str] = field(default_factory=list)


_CITATION_RE = re.compile(r"<citation\s+id=\"([^\"]+)\">(.*?)</citation>", re.DOTALL)
# Bare-tag fallback: small models often emit `<citation id="X">` as a
# footnote marker at the end of a sentence with no closing tag. We accept
# both the self-closing variant `<citation id="X"/>` and the open-only form.
_CITATION_BARE_RE = re.compile(r"<citation\s+id=\"([^\"]+)\"\s*/?>", re.IGNORECASE)
_LIST_BULLET_RE = re.compile(r"^\s*[-*•]\s*")


def strip_citations(text: str) -> tuple[str, list[Citation]]:
    """Pull `<citation>` markers out of model text and inject `[N]` refs.

    Two recognized forms, in order:
      * `<citation id="X">claim</citation>` — wrapped, claim is the inner text.
      * `<citation id="X">` with no closing tag — treated as a footnote
        marker; the claim is the preceding sentence in the text up to the
        tag (back to the last `.`/`!`/`?` or newline).

    In both cases the marker is replaced in the displayed text by a `[N]`
    reference number that matches the position of the citation in the
    returned list. Repeat citations of the same `id` reuse the same number;
    each of their claim phrasings is appended to that entry's ``claims``
    list so the source listing can show every place the source was used.
    """
    citations: list[Citation] = []
    id_to_index: dict[str, int] = {}

    def _ref(citation_id: str, claim: str) -> str:
        existing = id_to_index.get(citation_id)
        if existing is not None:
            entry = citations[existing - 1]
            if claim and claim not in entry.claims:
                entry.claims.append(claim)
            return f"[{existing}]"
        idx = len(citations) + 1
        id_to_index[citation_id] = idx
        citations.append(Citation(id=citation_id, claims=[claim] if claim else []))
        return f"[{idx}]"

    def _wrapped(m: re.Match) -> str:
        ref = _ref(m.group(1), m.group(2).strip())
        return f"{m.group(2)} {ref}"

    text = _CITATION_RE.sub(_wrapped, text)

    # Iteratively handle the bare form. We rebuild the text each iteration
    # so the recovered claim reflects the already-cleaned prose around it.
    while True:
        m = _CITATION_BARE_RE.search(text)
        if m is None:
            break
        prefix = text[: m.start()]
        boundary = max(
            prefix.rfind("."),
            prefix.rfind("!"),
            prefix.rfind("?"),
            prefix.rfind("\n"),
        )
        claim = prefix[boundary + 1 :] if boundary >= 0 else prefix
        claim = _LIST_BULLET_RE.sub("", claim).strip()
        ref = _ref(m.group(1), claim[:300])
        leading = "" if m.start() > 0 and text[m.start() - 1].isspace() else " "
        text = text[: m.start()] + f"{leading}{ref}" + text[m.end() :]

    # Trim the space the bare tag often leaves orphaned before punctuation.
    text = re.sub(r" +([.,;:!?])", r"\1", text)
    return text, citations


def extract_kb_records(
    tool_calls: list[ToolCallTrace],
    *,
    kb_prefix: str = "kb__",
) -> dict[str, dict]:
    """Pull `{id, title, source, section_path, ...}` records out of any
    kb tool result strings. The kb tool returns one or more JSON objects
    concatenated (not a JSON array); we walk the text with a JSONDecoder
    and collect every dict that has an ``id`` field. Best-effort: malformed
    fragments are skipped silently.

    ``kb_prefix`` selects which tool names count as kb-style. Deployments
    using a different MCP server name pass it explicitly instead of
    monkeypatching.
    """
    records: dict[str, dict] = {}
    decoder = json.JSONDecoder()
    for tc in tool_calls:
        if not tc.name.startswith(kb_prefix) or not tc.result:
            continue
        text = tc.result.lstrip()
        while text:
            try:
                obj, end = decoder.raw_decode(text)
            except json.JSONDecodeError:
                break
            if isinstance(obj, dict) and isinstance(obj.get("id"), str):
                records[obj["id"]] = obj
            text = text[end:].lstrip()
    return records
