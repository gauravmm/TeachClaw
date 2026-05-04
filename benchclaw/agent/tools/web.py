"""Web tools: web_search and web_fetch."""

import html
import json
import os
import re
from typing import Any, ClassVar, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field
from readability import Document

from benchclaw.agent.tools.base import Tool, ToolContext


class WebSearchConfig(BaseModel):
    """Web search tool configuration."""

    api_key: str = ""
    max_results: int = 5


# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5  # Limit redirects to prevent DoS attacks


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL: must be http(s) with valid domain."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)


class WebSearchTool(Tool):
    """Search the web using Brave Search API."""

    class Params(BaseModel):
        query: str = Field(description="Search query")
        count: int | None = Field(default=None, ge=1, le=10, description="Results (1-10)")

    Params: ClassVar[type[BaseModel]] = Params

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str | None:
        return (
            "Query the Brave Search API and return up to 10 results, each with a title, URL, and description snippet. "
            "Example: `{'query': 'python asyncio best practices 2025', 'count': 5}`."
        )

    @classmethod
    def build(cls, config: "WebSearchConfig | None", ctx: ToolContext) -> "WebSearchTool":
        return cls(config=config or WebSearchConfig())

    def __init__(self, config: WebSearchConfig):
        self.api_key = config.api_key or os.environ.get("BRAVE_API_KEY", "")
        self.max_results = config.max_results

    async def execute(
        self, ctx: ToolContext, query: str, count: int | None = None, **kwargs: Any
    ) -> str:
        if not self.api_key:
            raise RuntimeError("tools.web_search.api_key not configured")

        try:
            n = min(max(count or self.max_results, 1), 10)
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": n},
                    headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
                    timeout=10.0,
                )
                r.raise_for_status()

            results = r.json().get("web", {}).get("results", [])
            if not results:
                return f"No results for: {query}"

            lines = [f"Results for: {query}\n"]
            for i, item in enumerate(results[:n], 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('url', '')}")
                if desc := item.get("description"):
                    lines.append(f"   {desc}")
            return "\n".join(lines)
        except Exception as e:
            raise RuntimeError(str(e)) from e


class WebFetchTool(Tool):
    """Fetch and extract content from a URL using Readability."""

    class Params(BaseModel):
        url: str = Field(description="URL to fetch")
        extractMode: Literal["markdown", "text"] = Field(default="markdown")
        maxChars: int | None = Field(default=None, ge=100)

    Params: ClassVar[type[BaseModel]] = Params

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str | None:
        return (
            "Fetch a URL and extract its main readable content using Readability, returning JSON with the extracted text in markdown or plain-text format. "
            "Handles HTML pages, JSON APIs, and raw text; content is truncated at `maxChars` (default 50,000). "
            "Example: `{'url': 'https://example.com/article', 'extractMode': 'markdown', 'maxChars': 10000}`."
        )

    @classmethod
    def build(cls, _config: None, _ctx: ToolContext) -> "WebFetchTool":
        return cls()

    def __init__(self, max_chars: int = 50000):
        self.max_chars = max_chars

    async def execute(
        self,
        ctx: ToolContext,
        url: str,
        extractMode: str = "markdown",
        maxChars: int | None = None,
        **kwargs: Any,
    ) -> str:
        extract_mode = extractMode
        max_chars = maxChars or self.max_chars

        # Validate URL before fetching
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            raise ValueError(f"URL validation failed: {error_msg}")

        try:
            async with httpx.AsyncClient(
                follow_redirects=True, max_redirects=MAX_REDIRECTS, timeout=30.0
            ) as client:
                r = await client.get(url, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()

            ctype = r.headers.get("content-type", "")

            # JSON
            if "application/json" in ctype:
                text, extractor = json.dumps(r.json(), indent=2), "json"
            # HTML
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                doc = Document(r.text)
                content = (
                    self._to_markdown(doc.summary())
                    if extract_mode == "markdown"
                    else _strip_tags(doc.summary())
                )
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                text, extractor = r.text, "raw"

            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]

            return json.dumps(
                {
                    "url": url,
                    "finalUrl": str(r.url),
                    "status": r.status_code,
                    "extractor": extractor,
                    "truncated": truncated,
                    "length": len(text),
                    "text": text,
                }
            )
        except Exception as e:
            raise RuntimeError(str(e)) from e

    def _to_markdown(self, html: str) -> str:
        """Convert HTML to markdown."""
        # Convert links, headings, lists before stripping tags
        text = re.sub(
            r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
            lambda m: f"[{_strip_tags(m[2])}]({m[1]})",
            html,
            flags=re.I,
        )
        text = re.sub(
            r"<h([1-6])[^>]*>([\s\S]*?)</h\1>",
            lambda m: f"\n{'#' * int(m[1])} {_strip_tags(m[2])}\n",
            text,
            flags=re.I,
        )
        text = re.sub(
            r"<li[^>]*>([\s\S]*?)</li>", lambda m: f"\n- {_strip_tags(m[1])}", text, flags=re.I
        )
        text = re.sub(r"</(p|div|section|article)>", "\n\n", text, flags=re.I)
        text = re.sub(r"<(br|hr)\s*/?>", "\n", text, flags=re.I)
        return _normalize(_strip_tags(text))
