"""Citation-id validation against a session's kb history.

Used by the agent loop to push back when the model cites an id that
was never returned by any kb tool. Lives next to ``parsing.py`` and
``render.py`` so all citation logic stays in one package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from teachclaw.bus import ToolCallTrace
from teachclaw.citations.parsing import extract_kb_records, strip_citations

if TYPE_CHECKING:
    from teachclaw.session import ConversationEvent


def kb_records_from_events(events: list["ConversationEvent"]) -> dict[str, dict]:
    """Collect kb records from every kb-tool ToolEvent in the session.

    Validates against the *full* session history rather than the
    current-turn trace because the model can legitimately remember an
    id from an earlier turn's ``kb__search``. Render-time chunk elision
    replaces the rendered tool content with a stub but leaves the
    original event in the session, so this walk still finds the ids.
    Compaction-with-summary is the one exception: events older than the
    latest user message are replaced with a SummaryEvent, so any kb
    ids cited from that pre-summary history will read as unverifiable.
    """
    # Local import to avoid a session→citations cycle at module load.
    from teachclaw.session import ToolEvent

    trace = [
        ToolCallTrace(
            id=ev.tool_call_id,
            name=ev.tool_name,
            arguments={},
            result=ev.content if isinstance(ev.content, str) else None,
        )
        for ev in events
        if isinstance(ev, ToolEvent)
    ]
    return extract_kb_records(trace)


def validate_citations(
    content: str, events: list["ConversationEvent"]
) -> tuple[list[str], list[int], dict[str, dict]]:
    """Find ``<citation>`` ids in ``content`` that aren't in any kb
    result observed in the session.

    Returns ``(bad_ids, bad_refs, kb_records)``:
    * ``bad_ids`` — citation ids not present in the session's kb history.
    * ``bad_refs`` — 1-indexed positions matching ``[N]`` markers as the
      user will see them after :func:`strip_citations` renders.
    * ``kb_records`` — the full id→record dict so the caller can build
      a "valid ids" message without re-walking the events.

    Both lists preserve first-appearance order and contain no duplicates
    (``strip_citations`` already dedupes by id).
    """
    _, citations = strip_citations(content)
    if not citations:
        return [], [], {}
    records = kb_records_from_events(events)
    valid = set(records.keys())
    bad_ids: list[str] = []
    bad_refs: list[int] = []
    for idx, c in enumerate(citations, start=1):
        if c.id not in valid:
            bad_ids.append(c.id)
            bad_refs.append(idx)
    return bad_ids, bad_refs, records


def unverified_postscript(content: str, bad_refs: list[int]) -> str:
    """Append an italicised ``Citation [N] is not automatically
    verifiable. Check claims carefully.`` postscript.

    Returns ``content`` unchanged if ``bad_refs`` is empty.
    """
    if not bad_refs:
        return content
    if len(bad_refs) == 1:
        head = f"Citation [{bad_refs[0]}] is"
    else:
        head = f"Citations {', '.join(f'[{n}]' for n in bad_refs)} are"
    return content.rstrip() + f"\n\n_{head} not automatically verifiable. Check claims carefully._"
