"""Tests for the web_search and web_fetch tools.

Both tools instantiate ``httpx.AsyncClient`` themselves, so we patch
the class to return a client backed by ``httpx.MockTransport``. The
tools' execute() methods are async; pytest-asyncio is in auto mode so
``@pytest.mark.asyncio`` is unnecessary on test functions, but we use
real coroutines via ``await``.
"""

from __future__ import annotations

from typing import Callable

import httpx
import pytest

from teachclaw.agent.tools.base import ToolContext
from teachclaw.agent.tools.web import (
    WebFetchTool,
    WebSearchConfig,
    WebSearchTool,
    _normalize,
    _strip_tags,
    _validate_url,
)


def _patch_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Force every ``httpx.AsyncClient(...)`` constructor to use a MockTransport."""
    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        # Drop kwargs MockTransport doesn't honour; they're harmless to AsyncClient.
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_validate_url_accepts_https():
    assert _validate_url("https://example.com/path") == (True, "")


def test_validate_url_rejects_non_http_scheme():
    ok, err = _validate_url("ftp://example.com")
    assert ok is False
    assert "http" in err


def test_validate_url_rejects_missing_domain():
    ok, err = _validate_url("https://")
    assert ok is False
    assert err == "Missing domain"


def test_strip_tags_removes_script_and_style():
    src = "<script>alert(1)</script>hello<style>p{}</style> world"
    assert _strip_tags(src) == "hello world"


def test_strip_tags_decodes_entities():
    assert _strip_tags("a &amp; b &lt;c&gt;") == "a & b <c>"


def test_normalize_collapses_whitespace_and_blank_runs():
    assert _normalize("a   b\n\n\n\nc") == "a b\n\nc"


# ---------------------------------------------------------------------------
# WebSearchTool
# ---------------------------------------------------------------------------


async def test_web_search_requires_api_key():
    tool = WebSearchTool(config=WebSearchConfig(api_key=""))
    ctx = ToolContext(workspace=None)
    with pytest.raises(RuntimeError, match="api_key"):
        await tool.execute(ctx, query="anything")


async def test_web_search_formats_results(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "T1", "url": "https://a", "description": "D1"},
                        {"title": "T2", "url": "https://b"},  # no description
                    ]
                }
            },
        )

    _patch_httpx_client(monkeypatch, handler)
    tool = WebSearchTool(config=WebSearchConfig(api_key="brave-key", max_results=5))
    out = await tool.execute(ToolContext(workspace=None), query="puppies", count=2)

    assert "Results for: puppies" in out
    assert "1. T1" in out and "https://a" in out and "D1" in out
    assert "2. T2" in out and "https://b" in out
    # Parameters and auth header sent correctly.
    assert "q=puppies" in captured["url"]
    assert "count=2" in captured["url"]
    assert captured["headers"].get("x-subscription-token") == "brave-key"


async def test_web_search_clamps_count_to_one_to_ten(monkeypatch: pytest.MonkeyPatch):
    """count is clamped to [1, 10] before the API call. count=999 → 10;
    count=0 falls back to max_results."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, json={"web": {"results": []}})

    _patch_httpx_client(monkeypatch, handler)
    tool = WebSearchTool(config=WebSearchConfig(api_key="k", max_results=3))

    await tool.execute(ToolContext(workspace=None), query="x", count=999)
    assert "count=10" in captured[-1]


async def test_web_search_handles_empty_results(monkeypatch: pytest.MonkeyPatch):
    _patch_httpx_client(monkeypatch, lambda _r: httpx.Response(200, json={"web": {"results": []}}))
    tool = WebSearchTool(config=WebSearchConfig(api_key="k"))
    out = await tool.execute(ToolContext(workspace=None), query="q")
    assert out == "No results for: q"


async def test_web_search_wraps_http_error(monkeypatch: pytest.MonkeyPatch):
    _patch_httpx_client(monkeypatch, lambda _r: httpx.Response(500))
    tool = WebSearchTool(config=WebSearchConfig(api_key="k"))
    with pytest.raises(RuntimeError):
        await tool.execute(ToolContext(workspace=None), query="q")


def test_web_search_picks_up_env_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BRAVE_API_KEY", "from-env")
    tool = WebSearchTool(config=WebSearchConfig(api_key=""))
    assert tool.api_key == "from-env"


# ---------------------------------------------------------------------------
# WebFetchTool
# ---------------------------------------------------------------------------


async def test_web_fetch_rejects_invalid_url():
    tool = WebFetchTool()
    with pytest.raises(ValueError, match="URL validation failed"):
        await tool.execute(ToolContext(workspace=None), url="not-a-url")


async def test_web_fetch_returns_json_for_application_json(monkeypatch: pytest.MonkeyPatch):
    _patch_httpx_client(
        monkeypatch,
        lambda _r: httpx.Response(
            200,
            json={"foo": "bar"},
            headers={"content-type": "application/json"},
        ),
    )
    tool = WebFetchTool()
    out = await tool.execute(ToolContext(workspace=None), url="https://api.example.com/x")
    import json as _json

    parsed = _json.loads(out)
    assert parsed["extractor"] == "json"
    assert parsed["status"] == 200
    assert '"foo": "bar"' in parsed["text"]
    assert parsed["truncated"] is False


async def test_web_fetch_extracts_html_with_readability(monkeypatch: pytest.MonkeyPatch):
    body = (
        "<!doctype html><html><head><title>My Title</title></head>"
        "<body><article><h1>Headline</h1><p>Paragraph one.</p>"
        "<p>Paragraph two.</p></article></body></html>"
    )
    _patch_httpx_client(
        monkeypatch,
        lambda _r: httpx.Response(200, text=body, headers={"content-type": "text/html"}),
    )
    tool = WebFetchTool()
    out = await tool.execute(
        ToolContext(workspace=None),
        url="https://example.com/article",
        extractMode="text",
    )
    import json as _json

    parsed = _json.loads(out)
    assert parsed["extractor"] == "readability"
    # Readability keeps article text; HTML tags should be gone in text mode.
    assert "Paragraph one." in parsed["text"]
    assert "<p>" not in parsed["text"]


async def test_web_fetch_handles_raw_text_when_content_type_unknown(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_httpx_client(
        monkeypatch,
        lambda _r: httpx.Response(
            200, text="just plain text", headers={"content-type": "text/plain"}
        ),
    )
    tool = WebFetchTool()
    out = await tool.execute(ToolContext(workspace=None), url="https://example.com/note.txt")
    import json as _json

    parsed = _json.loads(out)
    assert parsed["extractor"] == "raw"
    assert parsed["text"] == "just plain text"


async def test_web_fetch_truncates_at_max_chars(monkeypatch: pytest.MonkeyPatch):
    big = "x" * 5000
    _patch_httpx_client(
        monkeypatch,
        lambda _r: httpx.Response(200, text=big, headers={"content-type": "text/plain"}),
    )
    tool = WebFetchTool()
    out = await tool.execute(
        ToolContext(workspace=None),
        url="https://example.com/big",
        maxChars=100,
    )
    import json as _json

    parsed = _json.loads(out)
    assert parsed["truncated"] is True
    assert parsed["length"] == 100
    assert len(parsed["text"]) == 100


async def test_web_fetch_wraps_http_error(monkeypatch: pytest.MonkeyPatch):
    _patch_httpx_client(monkeypatch, lambda _r: httpx.Response(404))
    tool = WebFetchTool()
    with pytest.raises(RuntimeError):
        await tool.execute(ToolContext(workspace=None), url="https://example.com/missing")


def test_web_fetch_to_markdown_renders_links_headings_lists():
    tool = WebFetchTool()
    src = '<h2>Title</h2><p>visit <a href="https://x">site</a></p><ul><li>one</li><li>two</li></ul>'
    out = tool._to_markdown(src)
    assert "## Title" in out
    assert "[site](https://x)" in out
    assert "- one" in out and "- two" in out
