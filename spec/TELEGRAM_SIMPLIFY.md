# Telegram channel simplification

`benchclaw/channels/telegrm.py` is the largest channel by far. The
outbound-send pipeline in particular has grown three branches —
plain text, text-with-mermaid, and media-with-caption — that each
re-implement the same epilogue (record the citation map, emit the
discoverability hint, deal with HTML escaping). This spec proposes
collapsing those branches into a single "send-segment" pipeline so
new content types (audio, files with captions, mid-message system
banners) light up without bespoke call sites.

The previous citation carve-out (spec/CITATIONS.md) created
`benchclaw/citations/` and moved parsing/rendering out of the
channel. That helped, but it preserved a design choice that's worth
reopening: the channel keeps a per-message `CitationStore` cache of
parsed citations + tool calls + extracted kb_records, indexed by
Telegram `message_id`. Most of that cache is derivable from data we
already keep in the session — the store can shrink to a tiny
`message_id → turn_id` map. Finding 1 below covers it.

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

### 1. `CitationStore` caches data we already have — replace with a `message_id → turn_id` index

**What's complex.** `CitationStore` holds, per outbound message:
parsed `Citation` objects, the full `tool_calls` list, the
`kb_records` extracted from those tool_calls, a `created_at`
timestamp, and an `expired` tombstone flag. It runs a TTL sweep and
a hard-cap eviction on every `record()` call. All of that exists so
that on a ❤ or 🔥 reaction we can recover what the assistant turn
"contained".

**Why it exists.** When the user reacts to Telegram message 12345,
the channel needs to know which assistant turn produced it. That's
the only Telegram-specific piece — but the original design pre-
computed and cached every downstream artifact (parsed citations,
kb_records) at send time, then policed the cache with TTL and a
hard cap to keep memory bounded.

**What's actually load-bearing.** Just the mapping. The session
JSONL already stores every assistant turn with its raw
`<citation>` markup intact and its `tool_calls` attached. Given a
`turn_id`, we can re-derive citations and kb_records lazily:

```
on send:    store[message_id] = turn_id
on react:   turn = session.get_turn(store[message_id])
            citations = strip_citations(turn.content)
            kb_records = extract_kb_records(turn.tool_calls)
            render_list(citations, kb_records, fmt=TELEGRAM_HTML)
```

**Simpler shape.** A `dict[int, TurnId]` on the channel (or a slim
typed wrapper if you want `clear()` semantics for `/clear` and
`/forgetme`). Each entry is ~24 bytes; even a year of busy chats
fits in a few MB. The TTL sweep, the tombstone state, and the hard
cap all disappear — the data lives forever in the session JSONL,
which is the existing source of truth for conversation history.
The "expired vs. unknown" UX distinction collapses too: if the
turn isn't in the session, we genuinely have no record of it
(restart with `drop_pending_updates`, or message from a deleted
session).

**Knock-on cleanup.** `benchclaw/citations/` shrinks to just
`parsing.py` + `render.py`; `store.py` and `CitationEntry`
disappear. The Telegram channel stops constructing a
`CitationStore` per chat. The "(react ❤ to any reply for sources)"
discoverability hint logic (gated by `seen_first_citation`) stays
on `_UserState` — it's per-user UX state, not citation content.

**Cost & risk.** Medium. Need a session-side accessor to fetch a
turn by some stable id (today the session is keyed by JSONL line
number / event index — fine as a `TurnId`). Need to ensure
`/clear` and `/forgetme` still drop the channel-side mapping,
which they already do today via `cite_store.clear()`. The TTL
behavior change is user-visible: today, sources for a reply older
than 24 h say "expired"; under the new design, they re-render from
the session as long as the session still has the turn. That's
strictly better — no reason to artificially expire data we still
have.

**Sequencing.** This finding subsumes the citation carve-out's
`store.py` half but leaves `parsing.py` and `render.py` in place,
so it's a net simplification of `benchclaw/citations/` rather than
a revert. Worth doing before finding 2 (the unified send pipeline)
because it changes the shape of `_record_reply` — the dispatcher's
epilogue becomes `for mid in sent_ids: turn_index[mid] = turn_id`,
which is one line instead of a `cite_store.record(...)` call with
several keyword args.

### 2. Three send paths sharing the same epilogue → one pipeline

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

### 3. The ad-hoc `("text", str) | ("mmd", RenderedDiagram)` tuple list

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

### 4. "Couldn't render this diagram" appears twice as a string template

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

### 5. `_send_html` and `_safe_send_text` — two near-identical helpers

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

### 6. Reaction handlers duplicate the no-record/expired/empty branching

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

### 7. Cite-store lookup happens before the `_app` check in `_reaction_sources`

**What's complex.** `_reaction_sources` reads `st.cite_store.lookup`
*then* checks `if not self._app: return`. If the bot lost its
connection mid-reaction we still mutate state for nothing. Tiny but
illustrative — the helper-versus-handler ordering was an accident.

**Simpler shape.** Move the `_app` guard into the helper that
actually sends, so reaction handlers can assume the bot is up. Or
just delete the explicit check — `_safe_send_text` already noops
when `self._app is None`.

**Cost & risk.** Trivial.

### 8. `_record_message_map` policy now lives entirely on `CitationStore`

**What's complex.** Nothing — this finding is already addressed by
the citations carve-out. Calling out so future readers don't try to
re-extract it.

### 9. `_user_state` constructs a `CitationStore` per chat with a
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

### 10. `_dispatch_reaction` normalizes both sides on every call

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

### 11. `_send_media_message` does Path resolution inline

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

### 12. Promote `telegrm.py` to a `telegrm/` package

**What's complex.** The single file is ~1100 lines covering: config,
per-user state, slash-command handlers (12 of them), markdown→HTML
conversion, the outbound send pipeline, mermaid + media dispatch,
the typing-indicator background loop, reaction routing, and the
auth gate. Every concern is a section delimited by a comment
banner; nothing imports anything from another section because they
all share `self`. Reading the file means scrolling past 800 lines of
unrelated concerns to find the one you care about.

**Why it exists.** The channel started small ("just hook up
python-telegram-bot to the bus") and accreted. Each new concern
took the path of least resistance: another method on
`TelegramChannel`, another module-level helper, another comment
banner.

**Simpler shape.** Break it up by concern, with the bot class as a
thin orchestrator that delegates to focused modules:

```
benchclaw/channels/telegrm/
    __init__.py        re-export TelegramChannel + TelegramConfig
    channel.py         TelegramChannel class — lifecycle + handler wiring
    config.py          TelegramConfig (today's class)
    state.py           _UserState; per-user cite turn-index dict
    commands.py        the 12 slash-command handlers + menu publishing
    auth_gate.py       _gate, the auth-rate-limit wiring
    outbound.py        send() + segment planner + dispatcher
                       (this is where finding 2 lands)
    markdown_html.py   _markdown_to_telegram_html + _split_long
    reactions.py       _on_reaction + _dispatch_reaction
                       + _reaction_sources + _reaction_trace
    typing_loop.py     start/stop typing background task
```

The class stays in one place but each method body is one or two
lines that delegate to a module-level function taking the bits of
state it needs (chat_id, `self._app.bot`, `_UserState`). Small
helpers stop being methods on a 1100-line class and start being
top-level functions in a 100-line module — easier to test
(no `TelegramChannel` instance required), easier to read (one
concern per file), easier to grep.

**Why this is worth doing now.** Several of the other findings here
naturally split along these boundaries:

- Finding 1 (`message_id → turn_id` index) is one struct in
  `state.py` instead of a `cite_store` field on `_UserState`.
- Finding 2 (unified send pipeline) is the entirety of
  `outbound.py`.
- Findings 5 and 7 (`_send_html`/`_safe_send_text` merge, `_app`
  ordering) are within `outbound.py` and `reactions.py`
  respectively.
- Finding 6 (reaction handler dedup) is local to `reactions.py`,
  where the duplication becomes obvious because both functions are
  in the same 80-line file.
- Finding 11 (`_send_media_message` resolution helper) is
  `outbound._resolve_outbound_media`.

Each per-file refactor stays under 300 lines and the diff for
"add a new slash command" or "add a new reaction" stops being a
1100-line file edit.

**Cost & risk.** Medium-large because it touches every section,
but mechanical — the existing comment-banner sections are already
the boundaries. The bigger risk is doing it on its own and getting
stalled: the package split is most valuable as the *substrate* for
findings 1, 2, 5, 6, 11, not as a pre-refactor that just moves code
around. Sequencing matters (see below).

**Cost & risk caveat.** External imports today are
`from benchclaw.channels.telegrm import TelegramChannel,
TelegramConfig` (e.g. tests/test_media_tools.py:13,
benchclaw/channels/builtins.py:6). Re-exporting from
`benchclaw/channels/telegrm/__init__.py` keeps those working,
which is the point of `__init__` — no caller needs to know we
split the file.

## Suggested order

Findings 1 and 2 are interlocking and worth doing as one patch (the
new send pipeline's `_record_reply` is much simpler when the cite
store is already a plain dict):

1. Replace `CitationStore` with a `dict[int, TurnId]` on the
   channel and a session-side `get_turn(turn_id)` accessor; render
   on demand in the reaction handlers (finding 1).
2. Extract `OutboundSegment` types and rebuild `send()` around
   `_plan_segments` + `_dispatch` + `_record_reply` (finding 2).
3. Move the mermaid-failure fallback string into the renderer
   (finding 4).

Then a second patch promotes `telegrm.py` to a package (finding 12),
landing the file split on the now-simpler bodies. Doing the package
split first means re-doing each module twice; doing the in-file
simplifications first means the package split mostly just moves
already-clean blocks into named files.

That leaves the small follow-ups: findings 5, 7, 9, 10 are tiny and
can sweep into a single commit alongside the package split. Finding
6 is worth the helper if a third reaction is on the roadmap;
otherwise it can wait. Finding 11 is independent and can land any
time. Finding 3 (the typed segment list) and finding 8 (dead
`_record_message_map` reference) are subsumed by findings 1 and 2.

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
