# Compaction

## Current behavior

Compaction is **reactive and lossy**. After each LLM response, `_maybe_compact_session` checks
`response.usage["total_tokens"]` against 80% of `context_window` (22k default). If exceeded,
`session.compact()` appends a `SummaryEvent` containing the last 20 log entries and sets
`compacted_through` so only the summary + last `memory_window` (50) events are rendered.

Problems:

1. **Reactive only** — the oversized request is already sent before compaction is detected.
   The LLM is charged for tokens it couldn't use effectively.
2. **Crude trigger** — 80% of a static `context_window` doesn't account for variable system
   prompt size, tool definitions, or images.
3. **Single-shot** — compaction runs once. If the summary + recent events still overflow,
   there's no second pass.
4. **Log-store dependency** — the summary is built from `log.jsonl`, which is an unrelated
   append-only interaction log. It contains no conversation semantics.
5. **No structure** — the summary is an opaque text blob. The LLM has no way to ask for
   details about specific turns that were dropped.

## Proposed mechanisms

### 1. LLM-generated summarization

Use a separate (cheaper/smaller) LLM call to summarize the dropped history before discarding it.

```
old events ──► summarize(old events) ──► SummaryEvent
                ▲
                │ second LLM call
```

**Pros:**
- Semantically rich summary that preserves key facts, decisions, and open tasks.
- The summary can be structured (e.g. JSON with fields for "pending tasks", "decisions made",
  "user preferences discovered").
- The summarization model can be different from the main model (cheaper, smaller context).

**Cons:**
- Extra LLM call adds latency (200-800ms) and cost on every compaction.
- Summarization quality varies; important details can be silently dropped.
- Needs a prompt for the summarizer that captures what the main agent needs to remember.
- If compaction is frequent, the summarizer itself may overflow.

### 2. Rolling window with pinned context

Instead of summarizing, maintain a fixed set of "pinned" events that always survive compaction,
plus a rolling tail of recent events.

```
[pinned events] + [recent events: last N]
```

Pinning rules:
- User explicitly pins (e.g. "remember this").
- Agent pins when it detects a commitment ("I'll do X tomorrow").
- High-token events (images, long tool results) are candidates for pinning or eviction.

**Pros:**
- No extra LLM call. Deterministic, fast, free.
- User has explicit control over what survives.
- Pinned events are exact (no lossy summarization).

**Cons:**
- Pinned events consume budget permanently; too many pins → no room for recent context.
- No semantic compression — a 2000-token tool result stays 2000 tokens even if the useful
  information is "the build passed".
- Pin management becomes a UX problem (how does the user unpin?).
- Doesn't handle the common case where the important information is distributed across
  many small turns.

### 3. Hierarchical summarization (summarize-on-compact, retain tiers)

On compaction, don't throw away everything. Instead, tier the history:

```
Tier 0: pinned events (always kept verbatim)
Tier 1: summary of older window (LLM-generated)
Tier 2: recent N events (verbatim)
```

On each compaction, the previous Tier 1 summary is folded into the new summary along with
the events being dropped, so context degrades gracefully rather than vanishing.

```
before:  [Tier0] + [Tier1 old_summary] + [last 50 events]
compact: [Tier0] + [Tier1 new_summary(old_summary + dropped)] + [last 50 events]
```

**Pros:**
- Best of (1) and (2): semantic summaries + pinned verbatim context.
- Graceful degradation — information is compressed, not deleted.
- Summarization cost is bounded because it only runs at compaction boundaries.

**Cons:**
- Most complex to implement. Needs tier management, fold-in logic, and careful prompt design.
- Summary-of-summary can drift or hallucinate over multiple compactions.
- More state to persist and render correctly.
- Debugging is harder: which turn ended up in the summary vs. verbatim?

### 4. Pre-call proactive compaction

Estimate token count *before* sending to the LLM and compact preemptively if needed.

```
estimate(messages) > budget * 0.7  →  compact  →  re-estimate  →  send
```

Estimation methods:
- **Character heuristic** (already removed): `len(json) // 4`. Fast but ±30% inaccurate.
- **tiktoken / model tokenizer**: exact for the specific model, but adds a dependency and
  ~5ms per call.
- **Provider-side**: some providers return `prompt_tokens` in streaming mode before the full
  response. Not universally available.

**Pros:**
- Prevents wasted tokens on oversized requests.
- Composable with any of the above compaction strategies.
- Cheap if using the character heuristic.

**Cons:**
- Heuristic estimates can be wrong, causing either premature compaction (wasted context)
  or late compaction (wasted tokens).
- Adds a render-estimate-compact-render cycle before every call.
- The correct budget is `context_window - max_tokens` (output reserve), not `context_window`.

### 5. Sliding context with tool-result truncation

Instead of dropping whole events, selectively truncate large inline content:

- Tool results over N tokens get truncated to a summary + "call read_file to see full output".
- Image references stay as stubs (`[image: path]`) rather than base64 inline.
- Assistant reasoning content is trimmed aggressively (already done: `_MAX_REASONING_CHARS = 500`).

**Pros:**
- Preserves conversational structure (every turn is still visible).
- Targeted: only the expensive parts are cut, not entire turns.
- No LLM call needed for the truncation itself.

**Cons:**
- Tool result truncation can break multi-step tool chains (agent calls tool A, then needs
  tool A's full output for tool B).
- Doesn't help when the overflow is caused by many small messages, not a few large ones.
- The agent needs re-prompting or re-calling tools to recover truncated data, wasting tokens.

## Recommendation

**Phase 1** (immediate, no new dependencies):
- Implement **(4) pre-call estimation** using the character heuristic, re-added cleanly.
- Fix the budget to `context_window - max_tokens` to reserve output space.
- Combine with **(5) tool-result truncation** for results over a configurable threshold.

**Phase 2** (when compaction becomes a real pain point):
- Implement **(1) LLM-generated summarization** as the compaction strategy, replacing the
  current log-store approach.
- Use the same model with a dedicated summarization prompt, or a cheaper model.
- Store the summary as structured JSON (not free text) so future compactions can fold in.

**Phase 3** (if sessions grow very long-lived):
- Add **(2) pinned events** on top of (1), giving users explicit control.
- The pinned set is a session-level concept, not per-compaction.

## Config additions

```yaml
agents:
  master:
    context_window: 22000
    max_tokens: 8192
    memory_window: 50
    compaction:
      threshold: 0.7          # fraction of (context_window - max_tokens)
      max_tool_result_tokens: 2000  # truncate tool results above this
      summarize: false        # Phase 2: use LLM summarization
      summarize_model: null   # Phase 2: model for summarization (default: same model)
```

# DECISION

The earlier sections are background. This section is the canonical
design.

## Strategy

LLM-generated summarization. On trigger, a separate model call
condenses the prior conversation into a single summary, and the
context window is **restarted** with `system_prompt + summary`. The
old events are not retained as fallback context. There is no tiered
history, no log-store summary, no `SummaryEvent` appended to the
existing event list.

The summarizer model defaults to the main agent model. Configurable
to a cheaper model later if cost/latency becomes a concern.

The summarization prompt is templated and lives alongside the system
prompt template; it should ask the summarizer to preserve facts,
decisions, open tasks, user preferences, and any commitments the agent
has made — and to drop conversational filler.

## Trigger

A single proactive trigger, keyed to total prompt size, fires
**before** the next request is sent. No reactive trigger; no separate
per-turn or per-history caps.

```
fire when:  estimate(prompt) > threshold * (context_window − max_tokens)
default:    threshold = 0.82  (operating point for the lecture profile)
```

For the anticipated lecture model `google/gemma-4-e4b-it` running with
a 24k working window and a ~2k output reserve, the trigger fires at
~18k of input. Gemma 4 e4b's native max is 128k, so 24k is well
inside its quality envelope; the trigger exists for budget control
rather than to dodge attention degradation.

The threshold sits well above the conservative 0.7 default that would
make sense without chunk elision. Because elision (below) replaces
verbatim chunk bodies in turns *after* a retrieval, the cross-turn
prompt footprint is dominated by the system prompt + recent history +
elision stubs, not by accumulated chunk text. The transient retrieval
response of the current turn is allowed to consume the difference
between the trigger (18k) and the absolute input budget (22k), giving
the model a 4k slack window for the live tool-result event without
tripping compaction. Without elision we would want the lower 0.7
default; with it, 0.82 is the right operating point.

Token estimation can use a cheap heuristic (`len(json) // 4`) for a
first pass and tighten to a tokenizer-backed estimate later. The 4k
slack between the trigger and the absolute input budget covers both
the heuristic's ~30% inaccuracy and the in-flight retrieval response.

## Stale-chunk elision

Before summarization is even considered, retrieved chunk bodies in
tool-result events are elided from turns *after* the retrieval call.
The verbatim text is replaced with a short stub:

```
[chunks elided — ids: chunk_42, chunk_57, chunk_88]
```

Citation IDs stay visible so prior `[1] [2]` references still resolve,
and the agent can call `fetch_doc(id)` to re-hydrate the chunk on
demand. This is not a substitute for summarization — it is a cheap
prefix step that keeps the summarizer's input lean when it does fire,
and meaningfully extends the number of turns we get before
summarization becomes necessary.

When elision is enabled (the default), the lecture system prompt or
persona must include a hint along the lines of *"Prior retrieved
chunks have been elided to save space; if you need to quote one, call
`fetch_doc(id)` rather than relying on memory."* Without this, small
models will occasionally hallucinate quotes from chunks they can no
longer see.

## Persistence

The generated summary is persisted on the session so that:

- A bot restart mid-class does not lose conversational context.
- Post-class debugging can inspect what the summarizer chose to keep
  versus drop.
- Transcript dumps include the summary alongside the verbatim turns
  that survived.

## Config

```yaml
agents:
  master:
    context_window: 24000
    max_tokens: 2048
    compaction:
      threshold: 0.82             # fraction of (context_window - max_tokens); higher than the no-elision default of 0.7
      summarize_model: null       # null → same model as the agent
      summarize_prompt: ...       # template path or inline
      elide_chunks_after_turn: true
```

`memory_window` and `max_tool_result_tokens` from the earlier
brainstorm are dropped — the new design does not need a sliding
event window or a per-tool-result truncation knob (chunk elision
covers the only case that mattered).

## Out of scope

- Pinned events (Phase 3 in the brainstorm above). Not needed for
  one-hour lecture sessions.
- Tiered / hierarchical summarization. The restart model is simpler
  and good enough for the session length we expect.
- Summarizer-of-summarizer chaining. If a single session ever
  generates enough material to summarize twice, that is the signal
  to revisit, not to pre-engineer for.