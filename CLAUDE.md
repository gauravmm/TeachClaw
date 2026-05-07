# teachclaw — Claude Code Guide

teachclaw is an ultra-lightweight personal AI assistant. It connects one or more chat channels (Telegram, Email, …) to an LLM agent via an async message bus.

## Package Layout

```
teachclaw/
  agent/
    loop.py          Event-driven AgentLoop (one asyncio task per address)
    context/         ContextBuilder: assembles system prompt + message history
    skills.py        Skill prompt loader
    subagent.py      SubagentManager (not yet wired up)
    tools/
      base.py        Tool, ToolContext, register_tool()
      registry.py    ToolRegistry: lifecycle + execution
      filesystem.py  read_file, write_file, edit_file, list_dir, glob, grep
      shell.py       exec
      web.py         web_fetch
      memory.py      memory read/write
      message.py     send_message (routes OutboundMessage via bus)
      kill.py        kill (cancel a background tool task by tool_call_id)
      cron/          cron tool + type helpers
  bus.py             MessageBus: per-address inbound queues, per-channel outbound queues
  channels/
    base.py          ChannelConfig, BaseChannel, register_channel()
    manager.py       ChannelManager: owns channel tasks + outbound dispatchers
    telegrm.py       Telegram
  config.py          Config (pydantic_settings), ConfigManager (YAML load/save)
  lessons.py         Lesson-pack loader + validator (workspace IS the lesson)
  providers/         LLM provider registry; litellm_provider.py wraps LiteLLM
  session.py         SessionManager: per-address JSONL conversation history
  __main__.py        Entry point

config.yaml          Runtime config
lessons/             Lesson packs (the active lesson is the workspace)
  <name>/
    AGENTS.md          system-prompt skill/style file (read each turn)
    personalities.yaml persona overlays
    onboarding.yaml    welcome strings, example prompts, help text
    infra.yaml         optional: MCP servers + media.shared_roots overlay
    skills/            agent-readable skill packs
    common/            agent-readable shared resources
    storage/, sessions/, media/, cron/, HEARTBEAT.md  runtime (gitignored)
```

The active lesson is selected by `agents.master.workspace` in
`config.yaml`. Switching lessons = change that path, restart. See
`spec/SWITCHMODE.md`. The lesson is validated in full at boot
(`lessons.validate_workspace`); a misconfigured pack fails startup
with an aggregated `LessonValidationError`.

Lesson source files (`AGENTS.md`, `personalities.yaml`,
`onboarding.yaml`, `infra.yaml`) are unreachable to the agent's
filesystem tools — they sit outside `read_roots` and are also
listed in `ToolContext.forbidden_files` as defence-in-depth.

## Message Bus (`bus.py`)

```
bus.inbound:  dict[MessageAddress, Queue[InboundMessage | ToolResultEvent]]
bus.outbound: dict[str (channel), Queue[OutboundMessage]]
```

- `publish_inbound(msg)` — channel → bus; creates the per-address queue on first use and notifies `subscribe_new_addresses()` subscribers
- `publish_tool_result(addr, event)` — background tool task → bus; posts to existing per-address queue
- `consume_inbound(address=addr)` — agent loop reads from per-address queue
- `subscribe_new_addresses()` — returns a `Queue[MessageAddress]` that receives each new address as it first appears; used by `AgentLoop.run()` to spawn per-address tasks
- `consume_outbound(channel=name)` — channel dispatcher reads its own queue

`ToolResultEvent(tool_call_id, tool_name, result)` and `AddressEvent = InboundMessage | ToolResultEvent` are defined in `bus.py`.

## Agent Loop (`agent/loop.py`)

Fully event-driven. `run()` subscribes to new addresses and spawns one `_address_loop` task per `MessageAddress`. Each address loop:

1. Reads `AddressEvent` directly from `bus.consume_inbound(address=addr)`
2. On `InboundMessage`: if tools are in-flight, adds synthetic tool results + a system message listing them, then processes the user message normally
3. On `ToolResultEvent`: records the result; calls LLM once all in-flight tools for the current batch are done; if the iteration was already closed by an earlier user message, delivers the result as a background notification instead
4. Calls LLM after each event (respecting `max_tool_iterations`)
5. On LLM tool calls: stores `asyncio.Task` handles in `background_tasks` (keyed by `tool_call_id`), tracks names in `in_flight`, dispatches `_run_tool_and_post` tasks
6. `_run_tool_and_post` catches `CancelledError` and posts `"Cancelled."` as the result so the conversation stays consistent

`ToolContext.background_tasks` (`dict[str, Task]`) is set per-address and gives tools (like `kill`) access to the task handles.

## Tools

**Conventions:**

- Each tool lives in `teachclaw/agent/tools/<name>.py`
- Define a class inheriting `Tool`, implement `name`, `description`, `parameters`, `execute(ctx, **kwargs)`
- Call `register_tool("name", ToolClass)` at module level
- Call `register_tool_config("name", ConfigClass)` if the tool has config; the config becomes a field on `ToolsConfig`
- Import the module in `teachclaw/agent/tools/__init__.py`
- `master_only = True` excludes the tool from subagent registries

**`ToolContext` fields:**

- `workspace: Path`
- `bus: MessageBus | None`
- `address: MessageAddress | None` — current session
- `background_tasks: dict[str, Task] | None` — master loop only; keyed by `tool_call_id`
- `is_subagent: bool`
- `subagent_manager` — not yet wired

## Channels

Each platform lives in `teachclaw/channels/<name>.py`:

- Define `<Name>Config(ChannelConfig)` with `make_channel(bus)` — no `enabled` field; presence in config is sufficient
- Define `<Name>Channel(BaseChannel)` with `background()` and `send(msg)`
- `background()` runs forever; do cleanup in a `finally` block on `CancelledError`
- `send()` is called by the dispatcher to deliver outbound messages
- Call `register_channel("name", ConfigClass)` at module level
- `ChannelManager` owns all channel tasks and outbound dispatcher tasks; cancels them on shutdown

## Config (`config.py`)

- `Config` is a `pydantic_settings.BaseSettings` (env prefix `BENCHCLAW_`, delimiter `__`)
- `ToolsConfig` and `ChannelConfigs` are built dynamically from the tool/channel registries
- Loaded from `config.yaml`; written on first run with defaults
- Channel configs live in their channel files, not in `config.py`

## Running Locally

```bash
uv run teachclaw          # start all configured channels + agent
```

Config file: `config.yaml` (created automatically on first run with defaults).

## Cautions

- When inspecting `debug_dump/*.txt`, only read the final few lines or the selected region. Do not read the whole file because it burns context quickly.
- If the system prompt is needed, read `teachclaw/agent/context/templates/system_prompt.j2`.
