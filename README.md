# benchclaw

![benchclaw](benchclaw.png)

An ultra-lightweight personal AI assistant. Connects one or more chat channels (Telegram, Email) to an LLM agent via a fully async message bus — the agent can receive and process new user messages while tools are still running in the background.

Focused on:

- Consistent architecture with less dead code
- Asynchronous tool use — the LLM can receive messages while tools are running
- Per-address message queues dispatched directly (no contested shared queue)
- CRON-based scheduled tasks (replaces HEARTBEAT)

## Running

```bash
uv run benchclaw
```

Config file: `config/config.yaml` (created automatically on first run with defaults).

## Adding a Tool

1. Create `benchclaw/agent/tools/<name>.py`
2. Define a class inheriting `Tool`, implementing `name`, `description`, `parameters`, and `execute(ctx, **kwargs)`
3. Call `register_tool("name", ToolClass)` at module level
4. Import the module in `benchclaw/agent/tools/__init__.py`

If the tool has config, call `register_tool_config("name", ConfigClass)` — the config becomes a field on `ToolsConfig` automatically.

Set `master_only = True` to exclude the tool from subagent registries.

`ToolContext` provides: `workspace`, `bus`, `address`, `background_tasks` (keyed by `tool_call_id`), and `is_subagent`.

## Adding a Channel

1. Create `benchclaw/channels/<name>.py`
2. Define `<Name>Config(ChannelConfig)` with a `make_channel(bus)` method
3. Define `<Name>Channel(BaseChannel)` with `background()` and `send(msg)`
   - `background()` runs forever; do cleanup in a `finally` block on `CancelledError`
   - `send()` delivers outbound messages
4. Call `register_channel("name", ConfigClass)` at module level

Channel config is picked up automatically — presence in `config/config.yaml` is sufficient to enable the channel, no `enabled` flag needed.

## Anti-Thrashing Mechanisms

Three mechanisms in `agent/loop.py` prevent LLM thrashing, particularly with thinking models (Qwen, etc.):

### SystemEvent buffering

`SystemEvent`s (cron jobs, heartbeats) that arrive while tool calls are in-flight are buffered in `pending_system_events` and only injected into the conversation after all pending tool results land. Without this, a system message would appear after an assistant tool-call message with no tool result yet, creating invalid conversation state that causes the model to fill the gap with a spurious extra tool call.

### `<plan>` tags

The model can include a `<plan>` block anywhere in its response to leave itself a concise note for the next turn:

```
Bitcoin is $69,051 now. I'll check back in 5 minutes.
<plan>Fetched BTC=$69051. Cron set for 5min. On fire: web_search BTC price, compare with $69051, report delta.</plan>
```

The tag is stripped from user-visible output and injected as a system message at the start of the next LLM call, then cleared. This replaces the model having to re-derive its plan from verbose `reasoning_content` each turn, which tends to restart circular deliberation.

### reasoning_content truncation

Thinking models attach `reasoning_content` to assistant messages. `_strip_old_reasoning` removes it from all but the most recent assistant message (required by some model APIs). The kept blob is also truncated to `_MAX_REASONING_CHARS` (500) to prevent a single verbose deliberation from ballooning every subsequent call's context.
