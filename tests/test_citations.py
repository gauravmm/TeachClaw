"""Tests for the channel-agnostic citations package."""

from __future__ import annotations

import pytest

from teachclaw.bus import ToolCallTrace
from teachclaw.citations import (
    Citation,
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
