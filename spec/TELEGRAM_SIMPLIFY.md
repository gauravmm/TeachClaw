# Telegram channel simplification

`benchclaw/channels/telegrm.py` is the largest channel by far. The
outbound-send pipeline in particular has grown three branches —
plain text, text-with-mermaid, and media-with-caption — that each
re-implement the same epilogue (record the citation map, emit the
discoverability hint, deal with HTML escaping). This spec proposes
collapsing those branches into a single "send-segment" pipeline so
new content types (audio, files with captions, mid-message system
banners) light up without bespoke call sites.

Citations and the `cite_store` are out of scope below — they were
already extracted to `benchclaw/citations/` in a prior pass. The
remaining mess is in the channel.

## The current outbound flow

```
send(msg)
├── strip_citations(msg.content)       # always
├── if msg.media:
│       _send_media_message(...)        # caption + 1 file, no mermaid
│       └── cite_store.record(sent.message_id, ...)
└── else:
        _send_text_with_mermaid(...)
        ├── extract_blocks(text)
        ├── build segments: [("text", str) | ("mmd", RenderedDiagram)]
        ├── for each segment:
        │     "text" → split_long → _send_html (markdown→HTML, fallback to plain)
        │     "mmd"  → _send_diagram (PNG or fallback to source-as-html)
        └── cite_store.record(first_msg_id, ...)
            + first-citation discoverability hint
```

Each leaf branch independently:

- captures a `message_id` (the "first" sent goes in the cite store),
- swallows exceptions so a bad chunk doesn't tank the whole reply,
- decides whether `parse_mode="HTML"` is safe for its payload.

## Findings, ordered by impact

### 1. Three send paths sharing the same epilogue → one pipeline

**What's complex.** `send()` has a media-vs-text branch at the top.
Each branch reaches into `cite_store.record(...)` itself, with
slightly different policies: media records `sent.message_id` (the
single message), text-with-mermaid records `first_msg_id` (the first
sent of N). The discoverability hint check
(`if citations and not st.seen_first_citation`) lives only in the
text-with-mermaid path, so users whose first cited reply happens to
be a media reply never see the hint.

**Why it exists.** Mermaid was added on top of an already-working
plain-text path; media support was bolted on later. Each addition
took the shape of its own helper.

**Simpler shape.** Build a typed list of `OutboundSegment`s up
front, then loop one renderer over it:

```python
@dataclass
class TextSegment:
    body: str  # markdown; convert at send time

@dataclass
class DiagramSegment:
    rendered: RenderedDiagram  # already includes failure fallback source

@dataclass
class MediaSegment:
    path: Path
    mime: str
    caption: str | None  # markdown; convert at send time

OutboundSegment = TextSegment | DiagramSegment | MediaSegment
```

`send()` becomes:

```python
async def send(self, msg: OutboundMessage) -> None:
    chat_id = self._chat_id_or_warn(msg.address)
    if chat_id is None:
        return
    text, citations = cit.strip_citations(msg.content)
    tool_calls = list((msg.metadata or {}).get("tool_calls") or [])

    segments = await self._plan_segments(msg, text)
    sent_ids = [mid async for mid in self._dispatch(chat_id, segments)]
    if sent_ids:
        self._record_reply(chat_id, sent_ids, citations, tool_calls)
```

`_plan_segments` is the only function that knows about mermaid/media
splitting; `_dispatch` is the only function that knows about the
Telegram bot API; `_record_reply` is the only function that knows
about the cite store and the discoverability hint. Each concern has
one home.

**Cost & risk.** Mechanical refactor, ~200 lines moved. Behavior
preserved by recording `sent_ids[0]` in the cite store (matches
today). One real behavior change worth keeping: index every sent
`message_id` into the cite store, not just the first, so a reaction
on any segment of the reply (text + diagram + diagram, or media +
caption-as-reply) finds the same entry. That's a one-line change in
`CitationStore.record` taking a list of keys.

### 2. The ad-hoc `("text", str) | ("mmd", RenderedDiagram)` tuple list

**What's complex.** `_send_text_with_mermaid` builds
`segments: list[tuple[str, Any]]` where the first element is a
discriminator string and the second is `Any`. The dispatcher loop
inside the same function does `if kind == "text": ...` to recover
the type. The `Any` payload also defeats Pylance.

**Why it exists.** The logic was a single function; tuples were the
quickest way to thread two shapes through one loop without naming
them.

**Simpler shape.** Use the dataclasses from finding 1. The same
list-of-segments structure, but each item carries its own type and
the dispatcher matches on `isinstance`.

**Cost & risk.** Trivial; subsumed by finding 1.

### 3. "Couldn't render this diagram" appears twice as a string template

**What's complex.** Both `_send_text_with_mermaid` (for diagrams 3+
that we elected to drop) and `_send_diagram` (for failed render)
build the same fallback markdown:

```python
"\n_couldn't render this diagram, source below_\n```\n" + source + "\n```\n"
```

**Why it exists.** Two different code paths each discovered the
same UX pattern.

**Simpler shape.** Push the fallback into `mermaid_renderer` itself.
`RenderedDiagram` already carries `source`; add a `fallback_markdown`
property (or a module-level helper `format_failure(source: str) ->
str`) so both call sites use one definition. Better: never special-
case extras at all. Promote each extra mermaid block to its own
`DiagramSegment` and let `_dispatch` handle it the same way as the
first two — the renderer already returns `status="fail"` once you
exceed `_MAX_DIAGRAMS` if we move the cap into `render` instead of
the channel. Then the channel doesn't know about `_MAX_DIAGRAMS` at
all.

**Cost & risk.** Small; mostly moves a constant. The "first two
diagrams render, the rest go as raw source" rule becomes "the
renderer caps render attempts; failure means we post the raw source,
same as any other failure".

### 4. `_send_html` and `_safe_send_text` — two near-identical helpers

**What's complex.** Both send a text message to a chat. `_send_html`
runs `_markdown_to_telegram_html` first, falls back to plain text on
parse error, returns `message_id`. `_safe_send_text` takes pre-
rendered text + optional `parse_mode`, has its own try/except,
returns `None`. Reaction handlers and rate-limit warnings use the
second; the outbound pipeline uses the first.

**Why it exists.** `_safe_send_text` was the original generic helper
("send something, don't crash if Telegram is flaky"). `_send_html`
was added when Telegram's HTML mode was introduced for outbound
replies.

**Simpler shape.** One helper, one shape:

```python
async def _post(
    self,
    chat_id: int,
    body: str,
    *,
    markdown: bool = True,
    reply_to_message_id: int | None = None,
) -> int | None:
    """Send a Telegram text message. Returns the new message_id, or None on
    failure. When markdown=True, body is converted to Telegram HTML and
    parse_mode='HTML' is set; on parse error, falls back to the raw body
    with no markup. When markdown=False, body is sent verbatim — set
    parse_mode externally via a separate optional kwarg if needed."""
```

The citation listing goes through this with `markdown=False` plus an
explicit `parse_mode="HTML"` (since `cit.render_list` already emits
Telegram HTML). The "(react ❤ to any reply for sources)" hint goes
through it with `markdown=False`. The outbound text pipeline goes
through it with `markdown=True`.

**Cost & risk.** Small. The visible behavior change is that
exception logging becomes uniform (one `logger.warning` regardless
of caller).

### 5. Reaction handlers duplicate the no-record/expired/empty branching

**What's complex.** `_reaction_sources` and `_reaction_trace` each
have the same three-step early-exit ladder:

```
entry = st.cite_store.lookup(message_id)
if entry is None: send "no record"; return
if entry.expired: send "expired"; return
if not entry.<thing>: send "no <thing>"; return
... render ...
```

Three copies of the lookup-and-explain dance, with messages varying
only in noun ("sources" vs "tool trace") and predicate
(`entry.citations` vs `entry.tool_calls`).

**Why it exists.** The two reactions were added independently; the
shared shape only emerged after both existed.

**Simpler shape.** A small helper that resolves the entry into a
typed verdict:

```python
class CitationEntryStatus(StrEnum):
    UNKNOWN = "unknown"      # entry is None
    EXPIRED = "expired"      # tombstoned past TTL
    PRESENT = "present"      # callable .citations / .tool_calls

def classify(entry: CitationEntry | None) -> CitationEntryStatus: ...
```

Each handler then has a tight body:

```python
match classify(entry):
    case CitationEntryStatus.UNKNOWN: msg = "I don't have a record..."
    case CitationEntryStatus.EXPIRED: msg = "<thing> have expired..."
    case CitationEntryStatus.PRESENT: msg = render(entry)
```

The "empty" case (`entry.present but no citations`) collapses into
`PRESENT` with `render` returning a "didn't cite any sources" line —
or stays as a fourth match arm if you prefer; either way the
duplication is gone.

**Cost & risk.** Small. Worth doing only if a third reaction lands;
otherwise the duplication is annoying but cheap.

### 6. Cite-store lookup happens before the `_app` check in `_reaction_sources`

**What's complex.** `_reaction_sources` reads `st.cite_store.lookup`
*then* checks `if not self._app: return`. If the bot lost its
connection mid-reaction we still mutate state for nothing. Tiny but
illustrative — the helper-versus-handler ordering was an accident.

**Simpler shape.** Move the `_app` guard into the helper that
actually sends, so reaction handlers can assume the bot is up. Or
just delete the explicit check — `_safe_send_text` already noops
when `self._app is None`.

**Cost & risk.** Trivial.

### 7. `_record_message_map` policy now lives entirely on `CitationStore`

**What's complex.** Nothing — this finding is already addressed by
the citations carve-out. Calling out so future readers don't try to
re-extract it.

### 8. `_user_state` constructs a `CitationStore` per chat with a
default factory that's never used

**What's complex.** `_UserState.cite_store` has a `default_factory=
cit.CitationStore[int]` so the dataclass is constructible without
extra args, but `_user_state` always passes in an explicit store
configured with the channel's TTL. The factory exists only to keep
the dataclass valid.

**Why it exists.** Workaround for "the channel knows the TTL, the
dataclass doesn't, but both want a default-constructible
`_UserState`".

**Simpler shape.** Drop the default. Make `cite_store` a required
positional and let `_user_state` be the one constructor that knows
how to build the right `CitationStore`. Or move `cite_store` off
`_UserState` entirely into a `dict[int, CitationStore[int]]` on the
channel keyed by `chat_id` — same per-chat ownership, fewer fields
on the dataclass.

**Cost & risk.** Trivial.

### 9. `_dispatch_reaction` normalizes both sides on every call

**What's complex.**

```python
normalized = _normalize_emoji(emoji)
if normalized == _normalize_emoji(SOURCES_REACTION): ...
elif normalized == _normalize_emoji(TRACE_REACTION): ...
```

`SOURCES_REACTION` and `TRACE_REACTION` are module constants — they
don't change at runtime. Normalize them once at module load and
compare against the cache.

**Simpler shape.**

```python
_SOURCES = _normalize_emoji(SOURCES_REACTION)
_TRACE = _normalize_emoji(TRACE_REACTION)
...
match _normalize_emoji(emoji):
    case _SOURCES: ...
    case _TRACE: ...
```

(`match` won't actually bind module-level constants without the
`Foo.bar` form — easier to use `if`/`elif`. The point is to
normalize the constants once.)

**Cost & risk.** Trivial; pure perf/clarity.

### 10. `_send_media_message` does Path resolution inline

**What's complex.** ~15 lines of "if media_repo and not absolute,
ask the repo; otherwise treat as a filesystem path; otherwise call
filetype.guess_mime; otherwise raise" path resolution mixed with
"now send it via send_photo / send_video / send_audio / send_document
based on `mime.split("/", 1)[0]`".

**Simpler shape.** Pull resolution into a single helper:

```python
def _resolve_outbound_media(
    self, address: MessageAddress, ref: str
) -> tuple[Path, str]:
    """Return (absolute_path, mime). Raises FileNotFoundError if the
    referenced path is missing. Uses the media repo when configured;
    otherwise probes the filesystem with filetype."""
```

…and split the per-mime dispatch into a tiny dict:

```python
_BOT_SENDERS = {
    "image": ("send_photo", "photo"),
    "video": ("send_video", "video"),
    "audio": ("send_audio", "audio"),
}
method, kw = _BOT_SENDERS.get(mime.split("/", 1)[0], ("send_document", "document"))
sent = await getattr(self._app.bot, method)(**{kw: fh, **send_kwargs})
```

Probably overkill if we don't gain a fifth case soon, but the
resolution helper is worth pulling out either way — it's the only
piece that's reused (`_on_message` does the inverse for inbound
media).

**Cost & risk.** Small.

## Suggested order

The first three findings are interlocking and worth doing as one
patch:

1. Extract `OutboundSegment` types into `telegrm.py` (or a sibling
   module if it grows).
2. Replace `send`/`_send_text_with_mermaid`/`_send_media_message`
   with `_plan_segments` + `_dispatch` + `_record_reply`.
3. Move the mermaid-failure fallback string into the renderer.

That's the patch that genuinely simplifies the channel; the rest are
follow-ups. Findings 4, 6, 8, 9 are tiny cleanups you can sweep up in
a single follow-up commit. Finding 5 is worth the helper if a third
reaction is on the roadmap; otherwise it can wait until then. Finding
10 is independent and can land any time.

## What to leave alone

- `_markdown_to_telegram_html` — single-purpose, well-named, not
  duplicated. Don't touch.
- The `_normalize_emoji` U+FE0F handling — covered by finding 9 but
  the function itself is exactly the right shape.
- `_typing_loop` and the rate-limit window — no duplication, no
  abstraction tax.
- `seen_first_citation` flag and the discoverability hint — keep as
  Telegram-shaped UX; the spec/CITATIONS open question already
  noted "promote to the package only if a second channel wants the
  same UX".
