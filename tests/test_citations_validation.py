from __future__ import annotations

from teachclaw.citations.validation import (
    kb_records_from_events,
    unverified_postscript,
    validate_citations,
)
from teachclaw.session import AssistantEvent, ToolEvent, UserEvent


def _kb_events(*ids: str) -> list[ToolEvent]:
    return [
        ToolEvent(
            tool_call_id="tc-0",
            tool_name="kb__search",
            content="\n".join(f'{{"id": "{cid}"}}' for cid in ids),
        )
    ]


def test_passes_when_all_ids_valid() -> None:
    events = _kb_events("a", "b")
    content = 'See <citation id="a">one</citation> and <citation id="b">two</citation>.'
    bad_ids, bad_refs, records = validate_citations(content, events)
    assert bad_ids == []
    assert bad_refs == []
    assert set(records) == {"a", "b"}


def test_flags_unknown_ids_with_indexed_refs() -> None:
    events = _kb_events("a", "b")
    # First valid, second invalid → bad_ref points at [2].
    content = 'See <citation id="a">good</citation> and <citation id="ghost">made up</citation>.'
    bad_ids, bad_refs, records = validate_citations(content, events)
    assert bad_ids == ["ghost"]
    assert bad_refs == [2]
    assert "ghost" not in records


def test_no_kb_calls_marks_everything_bad() -> None:
    content = 'Citing <citation id="x">x</citation>.'
    bad_ids, bad_refs, records = validate_citations(content, [])
    assert bad_ids == ["x"]
    assert bad_refs == [1]
    assert records == {}


def test_accepts_ids_from_earlier_turn() -> None:
    # kb__search ran in turn 1; turn 2 follow-up cites the prior id.
    # Validator must walk the full session, not the current-turn trace.
    events: list = [
        UserEvent(content="turn 1"),
        ToolEvent(tool_call_id="tc1", tool_name="kb__search", content='{"id": "page-028"}'),
        AssistantEvent(content="answered turn 1"),
        UserEvent(content="turn 2 follow-up"),
    ]
    bad_ids, _, _ = validate_citations('<citation id="page-028">prior</citation>.', events)
    assert bad_ids == []


def test_kb_records_from_events_returns_full_records() -> None:
    events = _kb_events("a", "b")
    records = kb_records_from_events(events)
    assert records == {"a": {"id": "a"}, "b": {"id": "b"}}


def test_unverified_postscript_singular_and_plural() -> None:
    assert unverified_postscript("Hello.", []) == "Hello."
    one = unverified_postscript("Hello.", [3])
    assert one.endswith("_Citation [3] is not automatically verifiable. Check claims carefully._")
    many = unverified_postscript("Hello.", [2, 5])
    assert many.endswith(
        "_Citations [2], [5] are not automatically verifiable. Check claims carefully._"
    )
