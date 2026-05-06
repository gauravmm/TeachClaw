# Citations — channel-agnostic package

Today the entire citation system — parsing, dedup, kb-record extraction,
TTL-tombstoned per-message store, and HTML rendering — lives inside
`teachclaw/channels/telegrm.py`. Claude Code and SMTP-email
channels see `<citation id="…">…</citation>` markup pass straight through
to the user as raw text. This spec carves the channel-agnostic logic out
into a `teachclaw/citations/` package so any channel can light up the
same surface with a few lines of glue.

## Current behavior

Defined in `telegrm.py`:

- `_strip_citations(text)` — regex parser that handles two forms
  (`<citation id="X">claim</citation>` wrapped, and bare
  `<citation id="X">` footnote markers), injects `[N]` reference numbers
  into the displayed text, dedupes by id, and accumulates each distinct
  claim phrasing into `Citation.claims`.
- `_extract_kb_records(tool_calls)` — walks `ToolCallTrace.result`
  strings from any tool whose name starts with `kb__`, parses
  concatenated JSON objects with `json.JSONDecoder`, and returns
  `{id: record}` for entries with a string `id` field.
- `_MessageMapEntry` — `{citations, tool_calls, kb_records, created_at,
  expired}` — the per-message blob the reaction handler reads.
- `_record_message_map` — TTL-tombstone-with-hard-cap policy. Past TTL
  entries get their content cleared and `expired=True`; entries beyond
  the hard cap are evicted oldest-first.
- `_render_citation_list(citations, kb_records)` — Telegram-HTML output
  with `<a href="source">title — section_path</a>` headers and
  one-or-many claim sub-bullets per id.
- Reaction-handler branching (`_reaction_sources`, `_reaction_trace`):
  `entry is None` → "no record"; `entry.expired` → "expired";
  empty content → "didn't cite any sources"; live content → render.

Channel-specific concerns also tangled in:

- Reaction emoji set (`SOURCES_REACTION`, `TRACE_REACTION`) and
  `_normalize_emoji` for the U+FE0F variation-selector quirk.
- Telegram update routing (`_dispatch_reaction`, `_on_reaction`,
  `setMessageReaction` ack).
- The `parse_mode="HTML"` markup choice and Telegram-HTML escaping.
- The "(react ❤ to any reply for sources)" discoverability hint and
  the per-user `seen_first_citation` flag.

## Problems

1. **Two channels leak markup.** Claude Code and email both
   send messages with raw `<citation>` text visible to the user. There's
   no way for those channels to enable the citation surface without
   copy-pasting ~250 lines from `telegrm.py`.
2. **Hard to test in isolation.** The pure-string transforms
   (`_strip_citations`, `_extract_kb_records`) are private to a 1300-line
   Telegram module that imports `telegram`, `httpx`, etc. Pytest needs
   the whole import graph to exercise a regex.
3. **Renderer assumes Telegram HTML.** Other surfaces want different
   dialects: SMTP wants Markdown or HTML (different escaping), Claude
   Code wants Markdown, a future console channel wants plain text.
4. **Coupling to per-channel state.** `_record_message_map` reads
   `_UserState.message_map` — a Telegram-only data class. The TTL +
   eviction policy is good but tied to Telegram's `_UserState` shape.

## Proposed package

```
teachclaw/citations/
    __init__.py        public exports
    parsing.py         strip_citations, extract_kb_records, regexes
    store.py           CitationStore — keyed TTL + tombstone + hard cap
    render.py          render_list(..., fmt=…) — PLAIN | MARKDOWN | TELEGRAM_HTML
```

### Public types

```python
@dataclass
class Citation:
    id: str
    claims: list[str]   # distinct phrasings, in first-appearance order

@dataclass
class CitationEntry:
    citations: list[Citation]
    tool_calls: list[ToolCallTrace]
    kb_records: dict[str, dict]
    created_at: float
    expired: bool = False

class RenderFormat(StrEnum):
    PLAIN = "plain"
    MARKDOWN = "markdown"
    TELEGRAM_HTML = "telegram_html"
```

### Parsing

```python
def strip_citations(text: str) -> tuple[str, list[Citation]]: ...
def extract_kb_records(
    tool_calls: list[ToolCallTrace],
    *,
    kb_prefix: str = "kb__",
) -> dict[str, dict]: ...
```

`strip_citations` keeps the current two-form parser (wrapped + bare)
plus `[N]` injection plus claim dedup-with-first-appearance ordering.
`extract_kb_records` takes the kb prefix as a kwarg so a deployment
with a different MCP server name doesn't have to monkeypatch.

### Store

```python
class CitationStore[KeyT]:
    """Per-conversation message → CitationEntry map with TTL/tombstone
    eviction. KeyT is whatever the channel uses (Telegram message_id is
    int; an SMTP channel might use Message-ID strings)."""

    def __init__(self, *, ttl_seconds: int = 24 * 3600,
                 hard_cap: int = 1000): ...

    def record(self, key: KeyT, *, citations: list[Citation],
               tool_calls: list[ToolCallTrace]) -> None:
        """Compute kb_records from tool_calls, store the entry, run the
        TTL tombstone sweep, and enforce the hard cap."""

    def lookup(self, key: KeyT) -> CitationEntry | None: ...
    def clear(self) -> None: ...
```

The store is generic over key type so each channel can pick whatever
identifier it natively addresses messages by. Behavior is identical to
the current Telegram code: `record()` tombstones aged entries
in-place (so `lookup()` can distinguish "expired" from "never tracked"),
and the hard cap evicts oldest-first which means tombstones go before
live entries because they're older.

### Render

```python
def render_list(
    citations: list[Citation],
    kb_records: dict[str, dict],
    *,
    fmt: RenderFormat,
) -> str: ...
```

Dialect handling:

- **PLAIN**: `[1] title — section_path (URL)\n  • claim`. No markup.
  Suitable for terminal channels. Falls back to bare id when no record.
- **MARKDOWN**: `[1] [title — section_path](URL)\n  • claim`.
  Standard markdown link. Works for Claude Code and any markdown email.
- **TELEGRAM_HTML**: the current output:
  `[1] <a href="URL">title — section_path</a>\n    • claim`.
  Goes alongside `parse_mode="HTML"` and `disable_web_page_preview=True`.

All dialects share the same multi-claim sub-bullet rule: one inline
claim line when there's a single phrasing, bulleted list when there
are multiple, dropdown to bare id when no kb_record matches.

## Channel integration

After the carve-out, Telegram becomes a thin user:

```python
from teachclaw import citations as cit

class TelegramChannel(BaseChannel):
    def __init__(self, ...):
        self._cite_store = cit.CitationStore[int](
            ttl_seconds=self.config.message_map_ttl_seconds,
            hard_cap=_MESSAGE_MAP_HARD_CAP,
        )

    async def send(self, msg):
        text, cites = cit.strip_citations(msg.content)
        ...
        self._cite_store.record(first_msg_id, citations=cites,
                                tool_calls=tool_calls)

    async def _reaction_sources(self, message_id, st, chat_id):
        entry = self._cite_store.lookup(message_id)
        if entry is None: ...
        elif entry.expired: ...
        elif not entry.citations: ...
        else:
            listing = cit.render_list(
                entry.citations, entry.kb_records,
                fmt=cit.RenderFormat.TELEGRAM_HTML,
            )
            await self._safe_send_text(chat_id, listing,
                                       reply_to_message_id=message_id,
                                       parse_mode="HTML")
```

The Telegram-only concerns — emoji set + dispatch, the
`seen_first_citation` discoverability hint, the `setMessageReaction`
ack — stay in `telegrm.py`. They're genuinely Telegram-shaped.

For other channels, the wiring becomes:

- **Claude Code / console**: `strip_citations` + `CitationStore`, then
  surface a `/sources <message_id>` slash-command-equivalent that calls
  `lookup` and prints the `MARKDOWN` rendering.
- **SMTP email**: same pattern, but the "key" is the outbound
  `Message-ID` header. Trigger could be a reply with `[sources]` in the
  subject. Render with `MARKDOWN` (or HTML email if we want clickable
  links in the email body).
## Migration plan

Two commits to keep the diff reviewable:

1. **Carve-out, no behavior change.** Create `teachclaw/citations/`.
   Move `_strip_citations`, `_extract_kb_records`, `_MessageMapEntry`,
   the TTL/tombstone policy, and `_render_citation_list` (renamed). Add
   `RenderFormat` with `TELEGRAM_HTML` first, since that's the current
   behavior. Telegram channel imports the new package; the per-user
   `message_map` becomes `CitationStore[int]` on the channel instance
   (replacing the dict on `_UserState`). Tests for the parser and store
   move alongside, callable without importing `telegram-bot`.

2. **Add MARKDOWN and PLAIN dialects.** Implement the two missing
   renderers with their own focused tests. No call-site changes — these
   are inert until a channel opts in.

A third future commit lands when the second consumer (Claude Code or
email) wires up; that's also the test that the abstraction holds. If
the second wiring requires twisting the API, that feedback informs
shape changes before more channels copy the pattern.

## Open questions

- **Per-user vs. per-channel store.** Telegram's current
  `_UserState.message_map` is per-chat. The new `CitationStore` could
  be a single instance per channel (keyed by `(chat_id, message_id)`
  tuples) or one instance per chat. Per-chat is closer to the current
  shape and keeps `clear()` semantics simple for `/clear` and
  `/forgetme`. I'd default to per-chat.
- **Where does the kb prefix live?** Today it's hardcoded as `kb__` in
  both the agent loop's `_CITATION_TOOL_PREFIXES` and the channel's
  `_KB_TOOL_PREFIX`. After extraction, both still need to agree.
  Option A: keep the duplication, document the contract. Option B:
  put it in config (`mcp_servers[*].citation_prefix`) and read it from
  both sides. A is simpler and matches "one knowledge base for the
  lecture"; B is the right answer if multiple kb-style MCP servers
  show up.
- **Discoverability hint location.** The "(react ❤ to any reply for
  sources)" copy and `seen_first_citation` flag are Telegram-shaped
  but the *concept* (show the user how to access citations once per
  session) is general. Keep it in the channel for now; promote to
  the package only if a second channel wants the same UX.
