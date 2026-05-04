"""Tests for the channel-agnostic citations package."""

from __future__ import annotations

import time

import pytest

from benchclaw.bus import ToolCallTrace
from benchclaw.citations import (
    Citation,
    CitationStore,
    RenderFormat,
    extract_kb_records,
    render_list,
    strip_citations,
)

# ---- strip_citations -------------------------------------------------------


def test_strip_wrapped_form_injects_ref():
    text, cites = strip_citations('Foo <citation id="a">claimed thing</citation> bar.')
    assert "claimed thing [1]" in text
    assert "<citation" not in text
    assert cites == [Citation(id="a", claims=["claimed thing"])]


def test_strip_bare_form_uses_preceding_sentence_as_claim():
    text, cites = strip_citations('The sky is blue <citation id="x">. The grass is green.')
    assert "[1]" in text
    assert cites[0].id == "x"
    assert cites[0].claims == ["The sky is blue"]


def test_strip_dedup_repeats_share_index_and_collect_claims():
    text, cites = strip_citations(
        '<citation id="a">first</citation> and '
        '<citation id="a">second</citation> and '
        '<citation id="a">first</citation>.'
    )
    assert text.count("[1]") == 3
    assert "[2]" not in text
    assert len(cites) == 1
    # Same phrasing dedupes; distinct phrasings accumulate in order.
    assert cites[0].claims == ["first", "second"]


def test_strip_multiple_distinct_ids():
    text, cites = strip_citations(
        '<citation id="a">one</citation>. <citation id="b">two</citation>.'
    )
    assert "[1]" in text and "[2]" in text
    assert [c.id for c in cites] == ["a", "b"]


def test_strip_handles_self_closing_bare_form():
    text, cites = strip_citations('A statement.<citation id="src"/>')
    assert "[1]" in text
    assert cites[0].id == "src"


# ---- extract_kb_records ----------------------------------------------------


def test_extract_kb_records_concatenated_json():
    tc = ToolCallTrace(
        id="t1",
        name="kb__search",
        arguments={},
        result='{"id": "a", "title": "A"}{"id": "b", "title": "B"}',
    )
    out = extract_kb_records([tc])
    assert set(out) == {"a", "b"}
    assert out["a"]["title"] == "A"


def test_extract_kb_records_skips_non_kb_tools():
    tc = ToolCallTrace(id="t1", name="other_tool", arguments={}, result='{"id": "a"}')
    assert extract_kb_records([tc]) == {}


def test_extract_kb_records_custom_prefix():
    tc = ToolCallTrace(id="t1", name="rag_search", arguments={}, result='{"id": "a"}')
    assert extract_kb_records([tc], kb_prefix="rag_") == {"a": {"id": "a"}}


def test_extract_kb_records_skips_objects_without_string_id():
    tc = ToolCallTrace(
        id="t1",
        name="kb__search",
        arguments={},
        result='{"id": 7}{"id": "b"}{"no_id": true}',
    )
    out = extract_kb_records([tc])
    assert set(out) == {"b"}


# ---- CitationStore ---------------------------------------------------------


def test_store_record_and_lookup_round_trip():
    store = CitationStore[int]()
    cites = [Citation(id="a", claims=["x"])]
    store.record(1, citations=cites, tool_calls=[])
    entry = store.lookup(1)
    assert entry is not None
    assert entry.citations == cites
    assert entry.expired is False


def test_store_lookup_unknown_returns_none():
    store = CitationStore[int]()
    assert store.lookup(99) is None


def test_store_ttl_tombstones_aged_entries_in_place():
    store = CitationStore[int](ttl_seconds=1)
    store.record(1, citations=[Citation(id="a")], tool_calls=[])
    # Force the first entry's created_at into the past.
    store._entries[1].created_at = time.time() - 60
    store.record(2, citations=[Citation(id="b")], tool_calls=[])

    entry1 = store.lookup(1)
    assert entry1 is not None
    assert entry1.expired is True
    assert entry1.citations == []
    # Lookup of the live entry still returns a live (non-expired) record.
    entry2 = store.lookup(2)
    assert entry2 is not None
    assert entry2.expired is False


def test_store_hard_cap_evicts_oldest_first():
    store = CitationStore[int](hard_cap=3)
    for i in range(5):
        store.record(i, citations=[], tool_calls=[])
        # Ensure distinct created_at across entries even on fast clocks.
        store._entries[i].created_at = float(i)
    assert store.lookup(0) is None
    assert store.lookup(1) is None
    assert store.lookup(2) is not None
    assert store.lookup(4) is not None


def test_store_clear():
    store = CitationStore[int]()
    store.record(1, citations=[], tool_calls=[])
    store.clear()
    assert store.lookup(1) is None


def test_store_extracts_kb_records_on_record():
    store = CitationStore[int]()
    tc = ToolCallTrace(
        id="t1",
        name="kb__search",
        arguments={},
        result='{"id": "a", "title": "A", "source": "https://x"}',
    )
    store.record(1, citations=[Citation(id="a")], tool_calls=[tc])
    entry = store.lookup(1)
    assert entry is not None
    assert entry.kb_records["a"]["title"] == "A"


# ---- render_list -----------------------------------------------------------


@pytest.fixture
def sample_records():
    return {
        "a": {
            "id": "a",
            "title": "Cool Paper",
            "section_path": "Ch 1 > Intro",
            "source": "https://example.com/a",
        }
    }


def test_render_telegram_html_with_record(sample_records):
    out = render_list(
        [Citation(id="a", claims=["claim one"])],
        sample_records,
        fmt=RenderFormat.TELEGRAM_HTML,
    )
    assert '[1] <a href="https://example.com/a">Cool Paper — Ch 1 &gt; Intro</a>' in out
    assert "    claim one" in out


def test_render_markdown_with_record(sample_records):
    out = render_list(
        [Citation(id="a", claims=["claim one"])],
        sample_records,
        fmt=RenderFormat.MARKDOWN,
    )
    assert "[1] [Cool Paper — Ch 1 > Intro](https://example.com/a)" in out
    assert "  claim one" in out


def test_render_plain_with_record(sample_records):
    out = render_list(
        [Citation(id="a", claims=["claim one"])],
        sample_records,
        fmt=RenderFormat.PLAIN,
    )
    assert "[1] Cool Paper — Ch 1 > Intro (https://example.com/a)" in out
    assert "  claim one" in out


def test_render_falls_back_to_bare_id_when_no_record():
    out = render_list(
        [Citation(id="missing", claims=[])],
        {},
        fmt=RenderFormat.PLAIN,
    )
    assert out == "[1] missing"


def test_render_telegram_html_falls_back_to_code_tag():
    out = render_list(
        [Citation(id="missing", claims=[])],
        {},
        fmt=RenderFormat.TELEGRAM_HTML,
    )
    assert out == "[1] <code>missing</code>"


def test_render_multi_claim_uses_bullets(sample_records):
    out = render_list(
        [Citation(id="a", claims=["one", "two"])],
        sample_records,
        fmt=RenderFormat.PLAIN,
    )
    assert "  • one" in out
    assert "  • two" in out


def test_render_telegram_html_escapes_claim_text(sample_records):
    out = render_list(
        [Citation(id="a", claims=["a < b & c"])],
        sample_records,
        fmt=RenderFormat.TELEGRAM_HTML,
    )
    assert "a &lt; b &amp; c" in out
