# teachclaw — Claude Code Guide

teachclaw is an ultra-lightweight personal AI assistant. It connects one or
more chat channels (currently Telegram) to an LLM agent via an async message
bus.

## Package Layout

```
teachclaw/
  agent/
    loop.py             AgentLoop — event-driven, one asyncio task per address
    loop_state.py       AddressState, ToolCallTracker, TurnOutcome
    response.py         ResponseHandler — tool dispatch, citation validation, outbound
    prompt.py           PromptBuilder — per-turn message list + cache prefix boundary
    compactor.py        Summarisation when prompt nears context budget
    cache_monitor.py    Per-address watchdog for prompt-cache prefix stability
    dump.py             Optional pretty-printed prompt dump for debugging
    templates/          Jinja templates (system_prompt.j2 etc.)
    tools/
      base.py           Tool, ToolContext
      registry.py       ToolRegistry — lifecycle + execution
      builtins.py       BUILTIN_TOOLS tuple (the canonical tool manifest)
      filesystem.py     read_file, write_file, edit_file, glob, grep
      shell.py          exec
      web.py            web_search, web_fetch
      media.py          read_media, send_media, annotate_media
      mcp_manager.py    MCP server connections (stdio/http)
      cron/             cron tool + schedule types + persistent store
  bus.py                MessageBus — per-address inbound queues, per-channel outbound queues
  channels/
    base.py             ChannelConfig, BaseChannel
    manager.py          ChannelManager — owns channel tasks + outbound dispatchers
    attention.py        InboundAttentionFilter (group-chat summon policy)
    telegrm/            Telegram channel (split across handler files)
  citations/            <citation> parsing, validation, render dialects
  providers/
    base.py             LLMProvider, LLMResponse, ToolCallRequest
    litellm_provider.py LiteLLM (OpenRouter / vLLM)
    scripted.py         ScriptedProvider — replays a YAML/JSON fixture for tests
  rendering/mermaid.py  mmdc shellout + caching
  config.py             Config (pydantic_settings), ConfigManager (YAML load/save)
  lessons.py            Lesson-pack loader + boot-time validator
  storage.py            Per-conversation sandbox path layout
  personalities.py      Persona overlays
  auth.py               Shared-secret auth + per-user marker
  media.py              MediaRepository (per-conversation + shared roots)
  session.py            SessionManager + JSONL persistence
  utils.py              Time, duration, MessageAddress (de)serialisers
  __main__.py           Entry point

config.yaml             Runtime config
lessons/<name>/         Lesson packs (the active lesson IS the workspace)
  AGENTS.md             system-prompt skill/style file (read each turn)
  personalities.yaml    persona overlays
  onboarding.yaml       welcome strings, example prompts, help text
  infra.yaml            optional: MCP servers + media.shared_roots overlay
  skills/               agent-readable skill packs
  common/               agent-readable shared resources
  storage/, sessions/, media/, cron/  runtime (gitignored)
```

The active lesson is selected by `agents.master.workspace` in `config.yaml`.
Switching lessons = change that path, restart. See `spec/SWITCHMODE.md`. The
lesson is validated in full at boot (`lessons.validate_workspace`); a
misconfigured pack fails startup with an aggregated `LessonValidationError`.

Lesson source files (`AGENTS.md`, `personalities.yaml`, `onboarding.yaml`,
`infra.yaml`) are unreachable to the agent's filesystem tools — they sit
outside `read_roots` and are also listed in `ToolContext.forbidden_files` as
defence-in-depth.

## Message Bus (`bus.py`)

```
bus.inbound:  dict[MessageAddress, Queue[AddressEvent]]
bus.outbound: dict[str (channel), Queue[OutboundEvent]]
```

`AddressEvent = InboundMessage | ToolResultEvent | SystemMessageEvent | SessionControlEvent`
flows through the per-address inbound queues.

`OutboundEvent = OutboundMessage | TypingEvent` flows through the per-channel
outbound queues.

Surface:

- `publish_inbound(addr, *events)` — channel/tool/cron → bus; creates the
  per-address queue on first use and notifies new-address subscribers.
- `consume_inbound_batch(address=addr)` — agent loop blocks for at least one
  event then drains the queue into a sorted `InboundMessageBatch`.
- `subscribe_new_addresses()` — returns a `Queue[MessageAddress]` that
  receives each new address as it first appears; used by `AgentLoop.run()` to
  spawn per-address tasks.
- `publish_outbound(event)` / `consume_outbound(channel=name)` — channel
  dispatcher reads its own queue.

## Agent Loop (`agent/loop.py`)

Fully event-driven. `run()` subscribes to new addresses and spawns one
`_address_loop` task per `MessageAddress`. Each address loop:

1. Drains `bus.consume_inbound_batch(address=addr)` into an
   `InboundMessageBatch` (tool results, system events, user messages, control
   events).
2. Applies the batch to the session: appends events, flushes pending system
   messages when tools settle, fires `/clear` and `/forgetme` control events.
3. Calls the LLM if anything in the batch warrants it (a user message, a
   completed tool batch with no in-flight tools, a buffered system event)
   and respects `max_tool_iterations`.
4. Hands the response to `ResponseHandler.apply` which dispatches background
   tool calls, validates citations, and publishes the user-visible reply.
5. `_run_tool_and_post` catches `CancelledError` and posts `"Cancelled."` as
   the result so the conversation stays consistent.

`ToolCallTracker` (in `loop_state.py`) owns the live `asyncio.Task` handles
keyed by `tool_call_id` and the in-flight name set.

## Tools

**Conventions:**

- Each tool lives in `teachclaw/agent/tools/<name>.py`.
- Define a class inheriting `Tool`, implement `name`, `description`,
  `Params` (pydantic model), `execute(ctx, **kwargs)`, and a classmethod
  `build(config, ctx)`.
- Add an entry to `BUILTIN_TOOLS` in `agent/tools/builtins.py`.
- If the tool has config, add a field of the config type to `ToolsConfig` in
  `config.py`. Tools without config use the base `ToolConfig` default.
- `terminal_when_lone = True` (ClassVar) marks a tool whose lone use counts
  as the model's user-facing reply for that turn (e.g. `send_media`); the
  loop won't nudge the model for a follow-up text response.

**`ToolContext` fields:**

- `workspace: Path`
- `bus: MessageBus | None` — None in tests.
- `address: MessageAddress | None` — current session.
- `media_repo: MediaRepository | None`
- `storage_root, read_roots, write_roots` — sandbox boundaries.
- `forbidden_files: tuple[Path, ...]` — defence-in-depth deny-list.
- `file_snapshots: dict[Path, FileSnapshot]` — read-before-write snapshots
  enforced by the filesystem tools.

## Channels

Each platform lives in `teachclaw/channels/<name>(.py | /)`:

- Define `<Name>Config(ChannelConfig)` with a `make_channel(bus, ...)`
  factory. `is_configured()` returns False to skip startup when required
  settings are missing (e.g. no token).
- Add the config field to `ChannelConfigs` in `config.py`.
- Define `<Name>Channel(BaseChannel)` with `background()` and `send(msg)`.
  `background()` runs forever; do cleanup in a `finally` block on
  `CancelledError`. `send()` is called by the dispatcher to deliver
  outbound messages.
- `ChannelManager` owns all channel tasks and outbound dispatcher tasks;
  cancels them on shutdown.

## Config (`config.py`)

- `Config` is a `pydantic_settings.BaseSettings` (env prefix `BENCHCLAW_`,
  delimiter `__`).
- `ChannelConfigs` declares each channel statically (currently just
  `telegram: TelegramConfig | None`).
- `ToolsConfig` declares config fields for tools that have config (cron,
  exec, web_search). Tools without config use the base `ToolConfig`.
- Loaded from `config.yaml`; written on first run with defaults.

## Running Locally

```bash
uv run teachclaw          # start all configured channels + agent
```

Config file: `config.yaml` (created automatically on first run with defaults).

## Cautions

- When inspecting `debug_dump/*.txt`, only read the final few lines or the
  selected region. Do not read the whole file because it burns context
  quickly.
- If the system prompt is needed, read
  `teachclaw/agent/templates/system_prompt.j2`.
