# TeachClaw — codebase simplification review

## Medium-impact: fewer concepts, fewer special cases

### 10. `CITATION_TOOL_PREFIXES` is a tuple of one

`agent/response.py:34` —
`CITATION_TOOL_PREFIXES: tuple[str, ...] = ("kb__",)`. Used at
`loop.py:200` as
`any(result.tool_name.startswith(p) for p in CITATION_TOOL_PREFIXES)`.
Until there's a second prefix, a single `str` constant +
`result.tool_name.startswith(CITATION_TOOL_PREFIX)` reads better.
Tuple-of-one is a speculative-future smell.

TODO: Leave this as-is.

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

TODO: Dedup never fires (from logs). Safe to remove.

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

TODO: Do the latter, if not removed by #20

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

TODO: Clean up TODO, removing everything DONE.

### 25. `spec/` has 11 files totalling ~3,100 lines

Several (`SWITCHMODE.md` 412 lines, `TELEGRAM_SIMPLIFY.md` 548 lines)
describe simplifications that may already be implemented. A pass to
retire completed specs would compress the design surface significantly.
(`SWITCHMODE.md` reads as fully implemented based on `lessons.py`.)


---

## Suggested order of operations

Lower-impact cleanups (#15–#25) as opportunity allows.
