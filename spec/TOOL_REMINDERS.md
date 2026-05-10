# Tool Reminders

## Status quo

When a tool returns, its result becomes a `ToolEvent` in the session and is rendered to the
LLM on the next turn. There is no mechanism for attaching an out-of-band nudge to a specific
tool's result — anything we want the model to "remember" right after a particular tool runs
must be either baked into the tool's own output (requires modifying the tool) or carried by
the system prompt (always-on, no per-tool targeting, easy for the model to ignore in the
moment).

This is a problem in practice. Observed example (2026-05-10 trace):

```
user: Can you post a cute dog?
tool: cute-db__search_cute({"query":"dog"})  → returns path
asst: "Here is a cute dog picture for you, Gaurav! 🐾"   ← never called send_media
```

The model treats the search result as if showing it to the user, but the user can never see
tool output. A nudge fired exactly when the search returns ("results aren't visible to the
user; call `send_media` to actually deliver") would close this gap. Hardcoding the nudge in
the search tool is impossible because cute-db is an external MCP server we don't own.

Relevant code paths today:

- `benchclaw/agent/tools/base.py` lines 31-34 — `ToolConfig` (only `enabled: bool`).
- `benchclaw/agent/loop_state.py` lines 72-98 — `ToolCallTracker.handle_result`, the single
  funnel where every tool result becomes a session event. Already appends a `SystemEvent`
  after the `ToolEvent` for the background-completion case (lines 90-98) — exactly the shape
  we want.
- `benchclaw/session.py` lines 262-279 — `SystemEvent` (persistent ConversationEvent,
  rendered as `{"role": "user", "content": "<system_event>...</system_event>"}` on every
  subsequent turn). All SystemEvents today are persistent; there is no scoping mechanism.
- `benchclaw/agent/tools/mcp_manager.py` lines 18-38 — `MCPServerConfig` (per-server, not
  per-tool; tool names are namespaced as `<server>__<tool>`).
- `benchclaw/config.py` lines 59-114 — `Config` root and `ToolsConfig` (built-in tool configs
  filtered by `TOOL_CONFIG_TYPES`).

Precedent in the sibling repo:

- `teachclaw/agent/response.py` lines 39-45 — hardcoded `CITATION_REMINDER` constant.
- `teachclaw/agent/loop.py` line 199 — appended as a persistent `SystemEvent` whenever a
  `kb__*` tool completes. Trigger is by tool-name prefix, not config-driven.

teachclaw confirms the pattern works in production. This spec lifts it from hardcoded to
config-driven and is intended to be ported back to teachclaw once stable.

## Goals

1. After a configured tool returns, append a configurable reminder string to session history
   so the model sees it on its next LLM turn.
2. Each reminder is independently scoped as either **persistent** (visible in every
   subsequent render until compaction, matching teachclaw) or **ephemeral** (visible until
   the next user message, then hidden).
3. Configuration is keyed by tool name (matching the name the LLM sees, including MCP
   namespacing like `cute-db__search_cute`) and lives in the YAML config file — no code
   changes to register a new reminder.
4. Works uniformly for built-in tools and MCP tools without per-tool-class plumbing.
5. Forward-portable: the config section, schema change, and dispatch logic are mechanically
   liftable into teachclaw with minimal adaptation.

## Non-goals

- **Render-time-only injection (never recorded).** The ephemeral mechanism here writes the
  event to `session.jsonl` and uses a render-time visibility rule. A truly unrecorded
  injection would break replay fidelity; rejected.
- **Conditional reminders** (fire only when result matches a pattern). Out of scope; every
  configured reminder fires on every successful return.
- **Argument-templated reminders** (e.g. "you searched for `{query}` — now call send_media").
  Out of scope; reminder strings are static.
- **Reminders on tool errors.** Out of scope for v1; the failure path already emits its own
  diagnostics.
- **Configurable scope rules beyond persistent/ephemeral** (e.g. "drop after N turns",
  "drop after next assistant message"). Out of scope; if a third scope appears in practice,
  the bool can grow into an enum then.

## Design

### Layer 1: Config schema

Add a new top-level config section keyed by tool name. Each entry may be either a bare
string (defaults to persistent) or a dict with explicit `text` and optional `ephemeral`:

```yaml
tool_reminders:
  # Bare string → persistent (matches teachclaw's CITATION_REMINDER pattern).
  search_media: |
    To send a result to the user, call send_media. Mentioning a path in
    prose does not deliver it.

  # Dict form → explicit fields.
  cute-db__search_cute:
    text: |
      Search results above are not visible to the user. To deliver media,
      call send_media with the returned path.
    ephemeral: true
```

A separate top-level section, not nested under `tools:`, because:

- `tools:` is filtered against `TOOL_CONFIG_TYPES` (config.py:174) and only accepts
  built-in-tool configs. MCP tool names would be silently dropped.
- Reminders span built-ins and MCP tools uniformly; a flat `tool_name -> entry` map is the
  natural shape.
- Keeps the existing per-tool-class config classes (`ExecToolConfig`, `WebSearchConfig`)
  unchanged — no field bloat on configs that don't use it.

Schema:

```python
class ToolReminder(BaseModel):
    text: str
    ephemeral: bool = False

    @field_validator("text")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("tool_reminders text must be non-empty")
        return v


class Config(BaseSettings):
    ...
    tool_reminders: dict[str, ToolReminder] = Field(default_factory=dict)

    @field_validator("tool_reminders", mode="before")
    @classmethod
    def _coerce_reminder_strings(cls, v: dict[str, Any]) -> dict[str, Any]:
        # Accept bare strings as shorthand for {"text": "...", "ephemeral": False}.
        return {
            k: ({"text": entry} if isinstance(entry, str) else entry)
            for k, entry in (v or {}).items()
        }
```

Validation: `text` must be non-empty after stripping. No check that the key matches a
registered tool — MCP tools are discovered at runtime, so config validation can't see them.
Unknown keys are logged at startup but not fatal (a reminder for a tool that isn't loaded
is a no-op).

**Decision: do NOT extend `ToolConfig`.**
The earlier sketch added `post_result_reminder: str | None` to `ToolConfig`. Rejected
because:
- MCP tools don't have per-tool ToolConfig objects — `MCPServerConfig` is per-server, and
  individual MCP tool wrappers are constructed at runtime from server discovery. There's
  nowhere to attach the field.
- A flat `tool_reminders` map handles both built-ins and MCP with one mechanism.
- Built-in tool configs stay focused on tool-specific settings (timeouts, allowed dirs, etc.)
  rather than agent-loop-level concerns.

### Layer 2: SystemEvent gains an `ephemeral` flag

`SystemEvent` (session.py:262-279) gets one new field:

```python
@dataclass
class SystemEvent(BaseEvent):
    KIND: ClassVar[Literal["system"]] = "system"

    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    ephemeral: bool = False  # NEW
```

`to_record` writes `ephemeral` only when `True` (keep persistent records minimal).
`event_from_record` reads it with default `False`.

The flag is on `SystemEvent` rather than a new event kind because:
- Storage shape stays identical; one new optional field, no migration.
- Existing call sites (interrupt notifications, background-tool nudges) keep working
  unchanged — they construct `SystemEvent(content=...)` with `ephemeral=False`.
- The renderer rule is a position-aware predicate over events, naturally expressed as a
  property of the event itself.

### Layer 3: Renderer rule — ephemeral hidden after next UserEvent

`Session.render_llm_messages` walks events in order and skips any `SystemEvent` with
`ephemeral=True` if a `UserEvent` appears at any later index in the event list.

Concretely, before the render walk, compute a single boolean per ephemeral event:

```python
# In Session.render_llm_messages (or its event-iteration helper):
last_user_idx = max(
    (i for i, ev in enumerate(self.events) if isinstance(ev, UserEvent)),
    default=-1,
)
# An ephemeral SystemEvent at index i is visible iff i > last_user_idx.
```

One pass to find `last_user_idx`, one pass to render — both O(n). Trivial overhead.

Visibility semantics:
- Ephemeral SystemEvent appended after a tool result → visible during the model's response
  to that tool result, including any follow-up tool calls in the same beat.
- User sends next message → ephemeral becomes invisible to the renderer on every subsequent
  render. Event remains in `session.jsonl` for replay/debug fidelity.
- Multiple ephemerals in the same beat → all visible. (See Tradeoffs for the duplicate
  case.)

**Decision: scope by next UserEvent, not next AssistantEvent.**
A per-LLM-turn rule (drop after next AssistantEvent) is more aggressive but loses the
reminder before the model has a chance to act on it across multi-step responses, and gives
the reminder no second chance if the model ignores it on first attempt. The cute-db trace
shows exactly the second-attempt case — the model needed re-prompting before acting. Scope
by user message, which matches the conversational beat the reminder is targeting.

### Layer 4: Dispatch — append SystemEvent after ToolEvent

`ToolCallTracker` already owns the result-handling funnel. Inject the lookup there.

`ToolCallTracker.__init__` gains a `reminders: dict[str, ToolReminder]` parameter, threaded
through from `AgentLoop` construction (which already sees the loaded `Config`).

`handle_result` (loop_state.py:72-98) becomes:

```python
def handle_result(self, event: ToolResultEvent, session: Session) -> bool:
    self._tasks.pop(event.tool_call_id, None)
    session.append(
        ToolEvent(
            content=event.result,
            tool_call_id=event.tool_call_id,
            tool_name=event.tool_name,
        )
    )

    reminder = self._reminders.get(event.tool_name)
    if reminder:
        session.append(SystemEvent(content=reminder.text, ephemeral=reminder.ephemeral))

    if event.tool_call_id in self._in_flight:
        del self._in_flight[event.tool_call_id]
        return not self._in_flight

    session.append(
        SystemEvent(
            content=(
                f"Background tool '{event.tool_name}' completed. ..."
            )
        )
    )
    return True
```

Reminder ordering: the reminder `SystemEvent` is appended **immediately after** the
`ToolEvent` and **before** any background-completion nudge. This gives the model the most
direct possible adjacency between the result and the nudge in the next-turn render.

**Decision: persistent SystemEvent for storage, ephemeral via render-time visibility.**
Alternatives considered:
- *Two separate event kinds*: more invasive schema change, two parallel code paths in the
  renderer.
- *Render-time-only injection (never recorded)*: breaks replay fidelity — `session.jsonl`
  would no longer reproduce what the LLM saw at any historical render.
- *Embed reminder into `ToolEvent.content`*: misleading (the reminder didn't come from the
  tool), pollutes traces/replays, and would require per-tool special-cased rendering.

### Layer 5: Wiring

`AgentLoop` (or wherever `ToolCallTracker` is constructed today) reads
`config.tool_reminders` and passes it into the tracker. One-shot at construction; reminders
are not hot-reloadable.

At startup, log the active reminder set at INFO level so users can confirm which tools have
reminders attached:

```
loaded 2 tool reminders: cute-db__search_cute, search_media
```

If `tool_reminders` references a tool name that doesn't appear in the registered tool set
after MCP discovery, log a WARNING but do not fail startup. (MCP servers can come and go;
a stale entry shouldn't block boot.)

## Tradeoffs

**Accumulation (persistent reminders).** Every fire of a persistent-reminded tool adds one
`SystemEvent` to history that is rendered on every subsequent turn until compaction. For a
tool called once or twice per session this is fine. For a tool called dozens of times, the
same reminder text accumulates N copies in the prompt. Use `ephemeral: true` for tools that
fire frequently or whose reminder is "respond to this tool result like X" rather than
"this rule applies as long as the result is in scope."

**Stacking (ephemeral reminders).** If the same ephemeral-reminded tool fires twice in one
conversational beat (no UserEvent between fires), both ephemerals are visible — the prompt
contains two copies of the same reminder. Mitigations possible (render-time
dedup-by-content within the active-ephemeral set) but not implemented in v1; revisit if
observed.

**Cache impact.** Persistent reminders extend the cached prefix; identical strings across
fires keep prefix-cache hit rates high. Ephemeral reminders are *not* part of the stable
prefix once the user replies — the prompt prefix shortens between renders, which is a
cache miss on the next turn after a user message that follows an ephemeral. Acceptable: the
user message itself already invalidates the tail-cache.

**Renderer becomes position-aware.** Today each event renders in isolation. The ephemeral
visibility predicate requires a single forward scan to find the last-UserEvent index. O(n),
trivial in practice; minor coupling between rendering and event-list shape.

**Storage growth from hidden ephemerals.** Ephemeral SystemEvents stay in `session.jsonl`
forever even after they're hidden from rendering. Negligible disk; matters only for raw
log inspection. Compaction can drop hidden ephemerals entirely (they served their purpose).

**Trace honesty.** Reminders are clearly distinct from tool results (separate event,
`SystemEvent` kind, wrapped in `<system_event>` tags). Replay fidelity is preserved: walking
the event list with the same render rule reproduces what the LLM saw at any point.

## Config additions

```yaml
# Top-level, sibling to tools/, channels/, mcp_servers/.
tool_reminders:
  # Bare string → persistent. Use for rules that apply as long as the tool's
  # result is in scope (e.g. "always cite when referencing this result").
  search_media: |
    To send a result to the user, call send_media. Mentioning a path in
    prose does not deliver it.

  # Dict form with ephemeral: true → visible until the next user message.
  # Use for "respond to this result like X" nudges that should not accumulate.
  cute-db__search_cute:
    text: |
      Search results above are not visible to the user. To deliver media,
      call send_media with the returned path.
    ephemeral: true
```

No migration needed — absent or empty `tool_reminders` is a no-op. The `ephemeral` field on
`SystemEvent` defaults to `False`, so existing session logs load unchanged.

## Implementation order

1. **Add `ephemeral: bool = False` to `SystemEvent`** (session.py). Update `to_record` to
   write the flag only when `True`; update `event_from_record` to read it with default
   `False`.
2. **Update `Session.render_llm_messages`** to compute `last_user_idx` and skip ephemeral
   `SystemEvent`s at indices `<= last_user_idx`.
3. **Add `ToolReminder` model and `Config.tool_reminders`** (config.py), including the
   `mode="before"` validator that coerces bare strings to `{"text": ...}`.
4. **Thread `reminders` into `ToolCallTracker`** — add parameter to `__init__`, default
   empty dict.
5. **Append SystemEvent in `handle_result`** — lookup by `event.tool_name`, append with
   `ephemeral=reminder.ephemeral`.
6. **Wire from `AgentLoop`** — pass `config.tool_reminders` when constructing the tracker.
7. **Startup log** — info-level summary of loaded reminders (count, names, persistent vs.
   ephemeral); warning for unknown tool names after registry build.
8. **Add the cute-db reminder to the live config** as ephemeral — verify the original
   failure trace no longer reproduces.
9. **Tests**:
   - Unit test on `ToolCallTracker.handle_result` confirming SystemEvent is appended iff
     the tool name is in the reminder map and with the configured `ephemeral` flag.
   - Unit test on `Session.render_llm_messages` confirming ephemeral SystemEvents are
     hidden once a UserEvent follows them.
   - Round-trip test: `to_record` → `event_from_record` preserves the `ephemeral` flag.

## Port to teachclaw

After this lands and stabilizes:

1. Copy the `SystemEvent.ephemeral` field and the renderer rule into teachclaw's session
   module.
2. Copy `ToolReminder` and `Config.tool_reminders` into teachclaw's config root.
3. Copy the `ToolCallTracker` change (teachclaw's tracker has the same shape per the prior
   investigation).
4. Delete the hardcoded `CITATION_REMINDER` in `teachclaw/agent/response.py:39-45` and the
   `kb__*` prefix dispatch in `teachclaw/agent/loop.py:199`.
5. Move the citation-reminder string into teachclaw's `config.yaml` under `tool_reminders`,
   keyed by each `kb__*` tool name. Leave it as a bare string (persistent) — citations
   should keep applying as long as the KB result is in scope.

## Scope boundaries

**In scope:**
- Config-driven, per-tool-name reminder strings.
- Per-reminder `ephemeral: bool` flag, scoping the reminder to the current beat (until next
  user message).
- `SystemEvent.ephemeral` field plus renderer visibility rule.
- Works for built-in and MCP tools uniformly.
- Startup logging of loaded reminders and unknown-tool warnings.

**Out of scope:**
- Render-time-only injection (events not recorded to `session.jsonl`).
- Prefix or pattern matching of tool names (each tool name listed explicitly).
- Conditional reminders based on tool arguments or result contents.
- Argument templating in reminder strings.
- Reminders on tool errors.
- Hot-reload of reminder config.
- Configurable ephemeral scope rules beyond next-UserEvent.

## Open questions

1. **Prefix matching for MCP server-wide reminders?** Several MCP tools from one server
   often want the same nudge (e.g. all `kb__*` tools in teachclaw share the citation rule).
   Should `tool_reminders` accept a `cute-db__*` glob? Argues for: less repetition. Argues
   against: implicit matching is harder to debug, and the explicit form is fine if the tool
   set is small. Defer; revisit after the teachclaw port shows whether the repetition is
   actually painful.

2. **Reminder on terminal_when_lone tools?** `Tool.terminal_when_lone` (base.py:62) skips
   the follow-up LLM call when the tool is the sole call in a turn. An ephemeral reminder
   appended after such a tool is in an awkward spot: the next render only happens after
   the next UserEvent, at which point the ephemeral is hidden. So ephemeral reminders on
   terminal_when_lone tools are effectively no-ops — emit a startup warning when this
   combination is configured. Persistent reminders work fine (they're visible on the next
   render regardless of when it happens).

3. **Stacking dedup?** If the same tool fires twice in one beat, two identical ephemerals
   are both visible. Worth dedup'ing the active-ephemeral set by content at render time?
   Defer; hasn't been observed in practice.

4. **Compaction handling.** Should compaction drop hidden ephemeral SystemEvents entirely
   (they served their purpose) or hand them to the summarizer like everything else?
   Probably drop — they have no semantic value once hidden — but coordinate with whoever
   owns the compaction work (see `spec/COMPACTION.md`).
