# TeachClaw — codebase simplification review

## P0 — these aren't simplifications, they're crashes

These have to be fixed before any of the rest matters. AST-parsing the tree
found two files with **Python-2-only `except` syntax**, which raises
`SyntaxError` at import time. They sit on the bot's import path, so the
process can't actually start as committed:

1. `teachclaw/auth.py:69` — `except OSError, ValueError, KeyError:`
2. `teachclaw/auth.py:95` — `except OSError, ValueError:`
3. `teachclaw/media.py:277` — `except FileNotFoundError, ValueError:`

Fix is one keystroke per site (parens). `auth.py` is imported by every
Telegram code path; `media.py` is imported in `__main__.py`. The fact that
this is sitting on `master` strongly suggests the test suite isn't actually
exercising channel boot — worth confirming `pytest` collects/runs
`test_telegrm_commands.py` and `test_media_paths.py`.

---

## High-impact structural simplifications

### 1. CLAUDE.md describes an architecture that no longer exists

This is the highest-leverage cleanup because every future contributor (and
every sub-agent) is reading a document that drifts further from the code
with each commit.

CLAUDE.md claims:
- `agent/skills.py`, `agent/subagent.py`, `tools/memory.py`,
  `tools/message.py`, `tools/kill.py` exist — **none do**
  (`find teachclaw -name 'subagent*' -o -name 'skills.py' -o -name 'memory.py' -o -name 'kill.py'`
  returns nothing).
- `register_tool()`, `register_channel()`, `register_tool_config()` are the
  registration mechanism — **none exist**. `BUILTIN_TOOLS` is a static
  tuple in `agent/tools/builtins.py`; `Config.channels` is a hardcoded
  `ChannelConfigs` pydantic model.
- `ToolContext` carries `is_subagent`, `subagent_manager`, `background_tasks`
  for "the master loop only / subagents".
- `master_only = True` lets tools opt out of subagent registries.

What's actually in the repo: one master loop, one channel (Telegram), one
tool registry, no subagents. Either resurrect the subagent system or
**strike it from CLAUDE.md and the code**. Recommendation: strike — the
spec list in `spec/` doesn't even reference subagents, and TODO.md has
been moving away from that frame.

Concrete code that goes when subagents go:

- `ToolContext.is_subagent`, `ToolContext.subagent_manager` —
  `teachclaw/agent/tools/base.py:32-33`. Never read anywhere.
- `Tool.master_only` ClassVar mention in CLAUDE.md — there's no such
  attribute on `Tool` (`teachclaw/agent/tools/base.py`); the doc is just
  wrong.
- The "None for subagents/tests" / "background/subagents" comments on
  `ToolContext.bus` and `ToolContext.address` (`base.py:31, 33`).

**Migration cost:** ~5 lines of code change + a CLAUDE.md rewrite. The
CLAUDE.md rewrite is the work — it should reflect the simpler reality
(one event-driven loop per address, static tool/channel manifests, no
subagents).

### 2. `BUILTIN_CHANNEL_CONFIGS` is a registry with no consumer

`teachclaw/channels/builtins.py` exports a tuple of `(name, ConfigClass)`.
`teachclaw/channels/__init__.py` re-exports it. **Nothing reads it.**
Channel discovery happens in `teachclaw/config.py:118-127` via a
hand-written `ChannelConfigs` model with one
`telegram: TelegramConfig | None` field, and `ChannelManager.__init__`
iterates `config.channels` directly.

There are exactly two coherent end states:

- **Delete the gadget**: drop `channels/builtins.py` and the
  `BUILTIN_CHANNEL_CONFIGS` re-export. `ChannelConfigs` becomes the single
  source of truth. This is the right call until a second channel actually
  lands.
- **Use the registry**: build `ChannelConfigs` dynamically from
  `BUILTIN_CHANNEL_CONFIGS` (mirroring `ToolsConfig`). Only worth it if a
  second channel is imminent.

**Recommendation:** delete. Same applies to the doc claim that "channel
files self-register via `register_channel()`" — there's no such function.

### 3. `AttentionEvent` is a phantom in the outbound type union

`teachclaw/bus.py:122-133` defines `AttentionEvent` and includes it in
`OutboundEvent = OutboundMessage | TypingEvent | AttentionEvent`. **No
code publishes or consumes it.** `ChannelManager._dispatch_channel` only
branches on `TypingEvent` vs default
(`teachclaw/channels/manager.py:65-79`).

Drop `AttentionEvent` entirely and tighten the union to
`OutboundMessage | TypingEvent`. The dispatcher loop becomes a clean
two-arm match.

### 4. The bus has two consume paths; only one is used

`MessageBus.consume_inbound(address=...)` (single event) at `bus.py:202-204`
is referenced **only in the docstring above it**. Every real call site
uses `consume_inbound_batch`. Delete `consume_inbound` and the docstring
example.

While here: the docstring at `bus.py:147-167` advertises
`publish_inbound(addr, msg)` *and* `publish_inbound(addr, msg1, msg2, …)`
*and* `publish_inbound(addr, tool_result)`. That's just one function with
`*events`. The example list is doing more harm than good — it suggests
there are several flavors when there's one.

### 5. `ToolContext.allowed_dir` is genuinely dead

`teachclaw/agent/tools/base.py:36` keeps `allowed_dir: Path | None = None`
with a comment "legacy single-dir restriction (no sandbox case)".
`_resolve_path` in `filesystem.py:73-75` checks it. **Nothing in the repo
sets it** (`grep -rn "allowed_dir" teachclaw tests` shows only the field,
the doc, and the consumer — no producer).

The agent loop always builds the `call_ctx` with a `storage_root`
(`loop.py:243-258`), so the legacy branch in `_resolve_path`
(`filesystem.py:71-77`) is unreachable in production. Delete:

- `ToolContext.allowed_dir`
- The `if ctx.storage_root is not None:` / else split in `_resolve_path` —
  collapse to the sandbox path
- The "Legacy mode" paragraph in the docstring

That cuts `_resolve_path` roughly in half and removes a "two ways to do
the same thing" smell directly off the hottest tool path.

### 6. `Session._render_history` is a private method called publicly

`teachclaw/agent/compactor.py:135-138` does
`session._render_history(events_to_summarize, options=...)`. The leading
underscore is then a lie — it's part of the public contract. Rename to
`render_history` or push the call site through a public method on
`Session` that takes the doomed event list. Trivial fix; the bigger win
is that someone refactoring `Session` won't assume `_render_history` is
safe to remove.

### 7. `terminal_when_lone` lookup goes through `type(tool)`

`teachclaw/agent/tools/registry.py:96-100`:

```python
def is_terminal_when_lone(self, name: str) -> bool:
    tool = self._tools.get(name)
    return bool(tool and type(tool).terminal_when_lone)
```

The `type(tool).` indirection is to read the ClassVar without picking up
an instance attribute, but in practice nothing sets it on the instance.
`bool(tool and tool.terminal_when_lone)` reads the same. Tiny, but the
`type()` call is the kind of thing that makes readers wonder whether
there's a subtle reason.

---

## Medium-impact: fewer concepts, fewer special cases

### 8. `PromptBuilder` and `ContextBuilder` are two layers for one job

- `agent/prompt.py` (173 lines) renders the per-turn message list and
  inserts the synthetic `<current_time>/<storage_listing>/<persona>`
  block.
- `agent/context/builder.py` (126 lines) builds the system prompt from a
  Jinja template + `AGENTS.md` + skills enumeration.

There's only one call site for `build_system_prompt` (`prompt.py:144-156`)
and only one call site for `PromptBuilder.build`. Splitting prompt
assembly across two modules + one Jinja template adds a layer without
paying. Either:

- Inline `build_system_prompt` into `PromptBuilder` (move the template
  loader + skill enumeration onto `PromptBuilder`), making
  `agent/prompt.py` the single owner of "what the model sees this turn."
  Drop the `agent/context/` package; templates can live at
  `agent/templates/` next to the only consumer.
- Or push the synthetic-context insertion into `build_system_prompt` and
  dissolve `PromptBuilder` (less clean — it loses the cache-prefix
  boundary distinction).

Recommendation: option 1. It also collapses `agent/__init__.py` and
`agent/context/__init__.py` re-exports.

### 9. `_resolve_logical` returns a discriminated tuple — make it a real type

`teachclaw/media.py:362-393` — `_resolve_logical(path)` returns
`("sandbox", filename)` or `("shared", alias, sub_parts)`. Every caller
pattern-matches with `kind, *rest = ...` and conditionals. This is
exactly the place to use a `match`-friendly dataclass union:

```python
@dataclass(frozen=True)
class SandboxPath:    filename: str
@dataclass(frozen=True)
class SharedPath:     alias: str; sub_parts: tuple[str, ...]
LogicalMediaPath = SandboxPath | SharedPath
```

Then `resolve_file`, `set_caption`, `_normalize_relpath` each become a
2-arm `match` instead of
`kind, *rest = ...; if kind == "sandbox": (filename,) = rest; ...`.

### 10. `CITATION_TOOL_PREFIXES` is a tuple of one

`agent/response.py:34` —
`CITATION_TOOL_PREFIXES: tuple[str, ...] = ("kb__",)`. Used at
`loop.py:200` as
`any(result.tool_name.startswith(p) for p in CITATION_TOOL_PREFIXES)`.
Until there's a second prefix, a single `str` constant +
`result.tool_name.startswith(CITATION_TOOL_PREFIX)` reads better.
Tuple-of-one is a speculative-future smell.

### 11. `Tool.background()` and `tool._task` plumbing carries weight that doesn't pay

`agent/tools/base.py:118-120` — every Tool can have a `background()`
coroutine started by `ToolRegistry.__aenter__` and a
`_task: Task | None = None` attribute. In the current codebase, exactly
one tool overrides `background`: `CronTool`. Compare cron-specific
machinery (`agent/tools/cron/tool.py:108-134`) to a hypothetical refactor
where the cron loop is just a separate task started by `AgentLoop.run()`
— `Tool.background()` becomes unused.

This is a "less general mechanism" simplification: today there's a
13-line lifecycle (cancel + 5s wait + suppress) on every Tool and the
only client is one tool. Pulling cron's loop out into `AgentLoop`
(alongside the new-address dispatcher) deletes:

- `Tool.background` + `Tool._task`
- The `if type(tool).background is not Tool.background:` branch in
  `ToolRegistry.__aenter__`
- The cancel-and-wait-on-tool-tasks block in `__aexit__`

Cost: cron then needs an explicit task in `AgentLoop`, but it's already
constructed there in spirit. Net deletion.

### 12. `OutboundMessage.metadata['tool_calls']` is a typed payload smuggled as `dict[str, Any]`

`agent/response.py:121-127` and `agent/response.py:155-160` both build
`metadata={"tool_calls": list(state.tool_call_trace)}`. Telegram's
`outbound.send` reaches into `msg.metadata.get("tool_calls")`
(`channels/telegrm/outbound.py:55`), then casts it to
`list[ToolCallTrace]`.

This is "transform before erasure" backwards: a typed
`list[ToolCallTrace]` is being stuffed into an untyped dict at the bus
boundary just so it passes through transparently. Either:

- Add `tool_calls: list[ToolCallTrace] = field(default_factory=list)`
  directly on `OutboundMessage` (the right move — it's a stable concept
  now used by both reactions and the trace view).
- Or add a typed channel-specific subtype:
  `OutboundReply(OutboundMessage)`.

Option 1. It cleans up the runtime cast in `outbound.send` and gives
Telegram's reactions handler a type-safe entry point.

### 13. `Session.events`'s typed render path leaks ad-hoc post-processing

`session.py` — `_render_event_message` calls `to_llm_message`, then
immediately calls `_truncate_inline_images` to walk the rendered dict
and shorten image URLs. The typed event already has the media list in
structured form; we render it to a dict, then walk the dict to truncate.
That's "transform after erasure."

A cleaner shape: pass `max_inline_image_url_chars` into `to_llm_message`
(or compute the truncation at the point where the URL is first rendered,
in `_render_user_content`). Then `_truncate_inline_images` and its
`dict`-walking helper go away.

### 14. `Session.to_record` / `event_from_record` is a hand-rolled serializer

`session.py:288-525` — every event class has `_record_base()` +
`to_record()` + dispatch in `event_from_record()`. The pattern is
consistent enough that pydantic discriminated unions would do this for
free, with type validation. This is more invasive (probably ~80 lines
down to ~30, plus a one-time JSONL migration consideration), but if you
take it the round-trip becomes provably-correct rather than
convention-correct.

A lower-risk version: keep the hand-rolled writer but introduce a single
`_optional_fields` helper to remove the repeated
`for key, value in optional_fields.items(): if value is not None:`
pattern in three event classes.

---

## Lower-impact local cleanups

### 15. `teachclaw/utils.py` mixes too many concerns

One module is currently:

- `JsonlIO` namespace (used 0 times — `grep` only finds the definition)
- `truncate_string` (used 0 times)
- `parse_duration` / `format_duration` (kept) + `DurationField` annotated
  type (kept)
- `now_aware`, `local_timezone`, `ensure_aware`, `_parse_timestamp`,
  `parse_optional_timestamp`, `TimestampSerializer`,
  `OptionalTimestampSerializer` (kept)
- `parse_optional_message_address` / `_encode_message_address` /
  `MessageAddressField` (kept)

Delete `JsonlIO` (no callers — channels read JSONL inline) and
`truncate_string` (Telegram inlines its own truncation).

### 16. Three near-duplicate "directory listing" functions

- `storage.listing_for_user` (`storage.py:491-522`) — per-user storage
  listing.
- `MediaRepository.shared_root_listing` (`media.py:90-117`) — shared
  roots listing.
- They duplicate the file-size + child-count rendering logic and the
  empty-directory format.

Extract a `_listing_for_dir(root: Path, header: str) -> str` helper in
`storage.py` (or a new `listings.py`) and have both call it. ~30 lines
deleted.

### 17. Telegram command boilerplate

`channels/telegrm/commands.py:148-180` (and the next ~10 handlers) all
start with:

```python
if not await gate(channel, update, ...): return
msg = update.effective_message
chat = update.effective_chat
if not (msg and chat): return
```

This pattern repeats ~9 times. Extract a small `@require_msg_chat`
decorator (or a context-manager-ish helper that yields
`(msg, chat, addr)` after gate-checking). Cuts ~20 lines and makes the
actual command bodies legible.

### 18. `ScriptedProvider`'s `ScriptedResponse.from_dict` and `to_response`

Two methods that do JSON↔dataclass conversion when pydantic with a
discriminator would handle both. Lower priority — this is test infra
and the explicit form is debuggable.

### 19. `_FALLBACK_DEFAULT` in `personalities.py` is a special case for missing files

Boot-time `validate_workspace` already requires `personalities.yaml` to
exist. The fallback-`Personality("default", …)` only fires when a test
creates a bare workspace. Either:

- Push the test fixtures to provide a real `personalities.yaml` (drop
  the fallback)
- Or accept the fallback but delete the comment justifying it ("Tests
  sometimes spin up a bare workspace…") — the code is self-explanatory.

Same shape with `_FALLBACK_DEFAULT`'s use in `_load`: the cache lookup +
`or by_name["default"]` fallback in `read_personality` are doing
belt-and-suspenders work. After validate_workspace, `by_name["default"]`
is always present, and the `name not in by_name` branch can just
`KeyError` — that's a programmer error.

### 20. `_typing_active` per-chat dedupe duplicates work the bus already does

`channels/base.py:71-83`'s `_handle_typing` dedupes typing events per
chat_id. But `agent/loop.py:264` already publishes a
`TypingEvent(addr, is_typing=False)` only when the address is idle, and
`is_typing=True` only on the user-message branch. So the dedupe is
catching bursts that the agent loop has *already* shaped to be
edge-triggered. Verify with logs whether the dedupe ever actually fires;
if not, delete the per-chat dict and the `_handle_typing` indirection.

### 21. `AddressState.pending_system_events` is a `list[str]` next to a typed event source

`agent/loop_state.py:37` —
`pending_system_events: list[str] = field(default_factory=list)`. But
the source is `SystemMessageEvent(content=str)`. Storing the typed event
would let downstream code distinguish where each system message came
from (cron, citation retry, …) for logging without breaking anything
else. Tiny.

### 22. `_handle_typing` lives on `BaseChannel` but is private + "not really overridable"

`channels/manager.py:71` calls `await channel._handle_typing(msg)`
(private method). Either make it public (it's part of the dispatcher
contract) or move the dedupe into `ChannelManager._dispatch_channel`
and have `BaseChannel` only expose `notify_typing`.

### 23. `ConfigManager` writes-on-exit is a surprising side effect

`teachclaw/config.py:182-186` — `__exit__` writes a default config file
iff the file didn't exist on `__enter__`. Reasonable behavior, but the
as-`ContextManager` API hides it. A dedicated
`Config.bootstrap_default(path)` called once from `__main__.py` makes
the side effect visible at the call site and lets `ConfigManager`
shrink to a load.

### 24. `TODO.md` is 243 lines of partly-completed history

Lots of items marked "**DONE (awaiting test)**" from months ago. This
is fine to keep around but would do better as a `CHANGELOG` for done
items + a short live `TODO`. Not a code change; affects new-contributor
onboarding.

### 25. `spec/` has 11 files totalling ~3,100 lines

Several (`SWITCHMODE.md` 412 lines, `TELEGRAM_SIMPLIFY.md` 548 lines)
describe simplifications that may already be implemented. A pass to
retire completed specs would compress the design surface significantly.
(`SWITCHMODE.md` reads as fully implemented based on `lessons.py`.)

---

## Suggested order of operations

If you want to act on this:

1. **First** — fix the three `except` syntax errors. Otherwise none of
   this ships.
2. **Then** — rewrite `CLAUDE.md` to match reality and delete the
   subagent vestiges (#1, #5, the dead `is_subagent`/`subagent_manager`
   fields). This is the single biggest readability win and unblocks
   future contributors / agents from chasing ghosts.
3. **Then** — the strict deletions: `BUILTIN_CHANNEL_CONFIGS`,
   `AttentionEvent`, `consume_inbound` singular, `allowed_dir`,
   `Tool.background` lifecycle (#2, #3, #4, #5, #11). All independent,
   all small, all pure deletes.
4. **Then** — the structural moves: merge
   `PromptBuilder`+`ContextBuilder` (#8), promote `tool_calls` onto
   `OutboundMessage` (#12), discriminated-union for media paths (#9).
   These are real refactors but each is local.
5. **Last** — the type-safety upgrades on session serialization (#14)
   and inline-image truncation (#13). Higher value but more invasive.

Items 1–3 are 1–2 hours of work and remove ~150 LOC of dead/misleading
code. Items 4–5 are 4–6 hours and improve the type story considerably
without changing observable behavior.
