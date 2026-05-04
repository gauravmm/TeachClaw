# TODO

## Lecture deployment (in-class use, AI-in-business RAG)

### Logging removal — **DONE (awaiting test)**
- [x] Remove the voluntary `LogTool` and `LogStore` (`benchclaw/agent/tools/memory.py` deleted).
- [x] Drop the `("log", LogTool)` entry from `benchclaw/agent/tools/builtins.py` and `ctx.log_store` plumbing in `agent/tools/base.py` and `agent/loop.py`.
- [x] Strip log-related guidance from `workspace/AGENTS.md` and `workspace_default/AGENTS.md`. (`system_prompt.j2` had no log references.)
- [x] Drop the `log.jsonl` summary path from session compaction (the old `Session.compact(log_store)` is replaced by a `compact_with_summary(summary)` method; the call site in `_maybe_compact_session` is stubbed pending the compaction rebuild). No `workspace/logs/` directory existed in this repo.

### Compaction rebuild — **DONE (awaiting test)**
Canonical design in **`spec/COMPACTION.md`**.

- [x] LLM-generated summarization in `AgentLoop._summarize_conversation`; the summarizer is called with `tools=None` so it cannot take actions.
- [x] `Session.compact_with_summary(summary, *, keep_from_index)` replaces history with a `SummaryEvent`, optionally keeping events from the most recent UserEvent onward verbatim (so the user's current question and any pending media stay attached).
- [x] Single proactive trigger in `_maybe_compact_proactive`: estimate `len(json.dumps(messages)) // 4`, fire above `threshold * (context_window − max_tokens)`. Defaults: `context_window=24000`, `max_tokens=2048`, `threshold=0.82` ⇒ trigger at ~18k.
- [x] Stale-chunk elision in `Session._render_history`: ToolEvents whose `tool_name` matches `compaction.elide_tool_names` and which sit before the most recent UserEvent get their content replaced with a stub. The underlying event is not mutated; only the rendered view changes.
- [x] System-prompt hint (`chunk_elision_active` Jinja flag) tells the model that prior retrieval results may be elided and to call the retrieval tool again rather than quote remembered text.
- [x] Config knobs added to `CompactionConfig`: `threshold`, `summarize_model` (null ⇒ same as agent model), `elide_chunks_after_turn`, `elide_tool_names`.
- [x] Summary is persisted as a `SummaryEvent` on the session, so it survives reload and shows up in any transcript dump.
- Tests in `tests/test_agent_loop.py`: proactive compaction calls the summarizer + main provider in order, `tools=None` for the summarizer, latest user message stays verbatim post-compaction; under-threshold runs do not compact; elision replaces old retrieval ToolEvents with a stub while keeping the most recent verbatim.
- [ ] Validate with the dummy-LLM harness once that lands (multi-turn sessions, summary handoff, post-compaction continuity).
- [ ] Ensure the persisted summary is included in transcript dumps (Telegram observability work).

### Dummy-LLM harness (testing prerequisite) — **DONE (awaiting test)**
- [x] `benchclaw/providers/scripted.py`: `ScriptedProvider` replays a YAML/JSON
      fixture's `responses:` list in order; past the end the last response
      repeats. Each entry can carry `content`, `tool_calls`, `usage_total`,
      `finish_reason`, and `balloon` (in chars).
- [x] Wired in `__main__.py`: `provider.name = scripted` + `provider.api_base
      = <fixture_path>` selects the harness without touching prod config.
- [x] Sample fixture at `config/fixtures/scripted_demo.yaml`.
- [x] Tests in `tests/test_scripted_provider.py` cover: in-order replay, tail
      repeat, balloon inflation, fixture-required validation.
- [ ] Drive multi-turn sessions end-to-end through the fake provider once the
      Telegram surface is exercised manually (left as a follow-up).

### RAG integration (AI-in-business knowledge)
- Implement the `search` / `fetch_doc` tool contract from lecture-knowledge `spec/ROUGH.md §2` (citation IDs + chunk metadata returned to the model).
- Hybrid retriever: BM25 + dense, hybrid-scored. Decide whether the index ships in-repo, behind an HTTP service, or via MCP.
- Citation emission: model wraps source-bearing claims in `<citation id="chunk_42">…short claim…</citation>` tags. Channel-agnostic; the Telegram channel strips the tags from the displayed text and keeps a per-message map from claim → chunk_id for the reaction-driven sources flow (see `spec/TELEGRAM.md`). System prompt must include a worked example so small Gemma emits the tag reliably. Per-user `cite` toggle controls whether the model emits citations at all.
- Persist `last_retrieval` (chunk IDs) on session state and keep a per-`message_id` map of `{citations, raw_chunks, tool_calls}` with a 24h TTL so users can react to a past message and get the sources or tool-call trace back.
- Admin `/reload_corpus` to re-index from disk without restarting the bot.
- Companion lookup tools (sit alongside the curated corpus, not inside the retrieval ranking):
  - `wiki_lookup(query)` against Wikipedia — `https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=<q>&format=json` for search, `https://en.wikipedia.org/api/rest_v1/page/summary/<title>` for ~200-word lead extract + canonical URL + thumbnail, `/api/rest_v1/page/html/<title>` or `/page/wikitext/<title>` for the full body. Free, no auth, just a UA header. Sturdy fallback for "who is X / what is Y" queries the corpus doesn't cover; citations are clean (CC BY-SA + canonical URL).
  - `brave_search(query)` for the open-web fallback (recent news, niche docs not in the corpus). Results aren't curated — model must surface a "open-web result, may be unreliable" disclaimer when citing these.
  - Both tools live next to `search` / `fetch_doc`, distinct names so the model can pick deliberately. Document the precedence in the lecture system prompt: corpus first, Wikipedia for definitions/biographies, Brave only when the other two miss.

### Storage layout (per-conversation sandbox) — **DONE (awaiting test)**
Replaces the `memory/` folder convention entirely. Memory is just files in the conversation's storage directory.

- [x] Per-conversation root at `storage/<channel>/<chat_id>/` (nested), media subdirectory pre-created. For Telegram DM, `chat_id == user.id`. Group-chat shape still a separate decision (not in scope for the lecture).
- [x] Skills stay at `workspace/skills/`, read-only — addressable from inside the sandbox via the `skills/` prefix.
- [x] `common/` is read-only by default. Addressable from inside the sandbox via the `common/` prefix.
- [x] Each user gets `common/scratch/<chat_id>/` as a known-writable directory. **Spec deviation:** a per-user dir rather than a single `<user_id>.md` file — uniform dir-vs-file enforcement is simpler and the file convention can sit on top.
- [x] Tool-level path enforcement in `benchclaw/agent/tools/filesystem.py:_resolve_path`. Sandbox mode (engaged when `ToolContext.storage_root` is set) rejects absolute paths and any post-resolve target outside `storage_root + read_roots` (or `+ write_roots` for writes). The `skills/` and `common/` path prefixes resolve under the workspace so the model doesn't have to count `..` segments.
- [x] Top-level storage listing injected as a synthetic tail turn (`<storage_listing>...</storage_listing>` user message) right before the latest user message in `AgentLoop._inject_storage_listing`. System-prompt prefix stays cache-stable. Listing format is deterministic (alpha sort, file sizes, dir item counts, no timestamps).
- [x] Per-user profile at `storage/<channel>/<chat_id>/profile.md`; current contents injected into the system prompt under "What you know about this user" via a new `profile_text` template variable. Read fresh each turn; not persisted in the session.
- [x] `storage/_admin/` is out of scope for all user-facing tools — the only way the model can address it would be via a literal "_admin" path under storage_root, which is outside the sandbox roots and rejected. The auth bot service will read it directly with hard-coded paths when that lands.
- [x] Dropped `memory_files` from `system_prompt.j2` and `memory_dir` from `ContextBuilder`.
- [x] `AgentLoop._address_loop` calls `storage_layout.ensure_user_dirs(workspace, addr)` on entry; constructs the `call_ctx` with sandbox fields populated.
- Tests in `tests/test_filesystem_tools.py` cover: absolute paths rejected; `..` traversal outside storage rejected; `_admin/` access rejected; storage-root relative paths allowed; `skills/` and `common/` prefixes resolve under the workspace; common/ writes rejected; own-scratch writes allowed; cross-user scratch writes rejected.
- [ ] `/forgetme` and `/reset` semantics land with the Telegram bot service work — the sandbox here is what makes them safe. Documented in `spec/TELEGRAM.md` and `spec/AUTH.md`.

### Lecture customization — **DONE (awaiting test)**
- [x] `workspace_default/AGENTS.md` and `workspace/AGENTS.md` rewritten for the
      classroom persona (no OcelliBot, no Notion, no memory-folder mentions).
      Per-conversation storage instructions in their place.
- [x] System prompt template gets a `personality_overlay` block; profile and
      personality are read fresh each turn in `AgentLoop._build_prompt_and_messages`.
- [x] `benchclaw/personalities.py`: `default`, `skeptical_cfo`, `vc_partner`,
      `mck_analyst`, `professor`. Selection persists at
      `storage/<channel>/<chat_id>/personality.txt`; `/reset` clears it.
- [x] Mermaid encouraged in the new AGENTS.md with a worked example;
      renderer lives at `benchclaw/rendering/mermaid.py` and the Telegram
      channel post-processes blocks via `mmdc`.

### Telegram bot service & lecture surface — **DONE (awaiting test)**
Canonical design in **`spec/TELEGRAM.md`**.

- [x] Slash commands wired: `/start`, `/help`, `/auth`, `/personality`,
      `/cite`, `/reset`, `/forgetme`, `/sources`, `/scope` (last two stub
      until RAG lands), and admin `/setsecret`, `/whoauthed`,
      `/reload_corpus`, `/stats`. `setMyCommands` runs at startup with
      admin-scope overrides for the prof.
- [x] Auth gate sits in front of every command except `/start`, `/help`,
      `/auth`, plus the non-command message handler. Wrong codes are rate
      limited (5 fails / 10 min, then 1 h lockout).
- [x] DM-only enforcement: group chats get a one-line "DM-only" reply.
- [x] Reaction dispatcher reads `message_reaction` updates and dispatches
      via a generic emoji table. 👀 surfaces citations from the
      per-`message_id` map (24 h TTL); 🔍 dumps the tool-call trace.
      Citation tags are stripped from the displayed text in
      `_strip_citations` and stored alongside tool calls in the map.
- [x] Mermaid post-processor: `benchclaw.rendering.mermaid` extracts
      fenced `mermaid` blocks (max 2 per reply), shells out to `mmdc`,
      caches by `sha256(source+theme)`. The Telegram channel splits the
      reply at fence boundaries and posts photos in order; failures fall
      back to the raw source. Heads-up: `mmdc` must be installed
      (`npm install -g @mermaid-js/mermaid-cli`).
- [x] Rate limits: 30 msg / 10 min soft cap (replies "take a breath"),
      one-in-flight-per-user with the second message answered as "still
      thinking…" until the typing indicator drops.
- [x] Tool-call trace plumbed through `OutboundMessage.metadata.tool_calls`
      from the agent loop; the channel records it on the per-`message_id`
      map so the 🔍 reaction can replay it.
- [x] `SessionControlEvent("reset"|"forget")` lets the channel ask the
      agent loop to clear the in-memory session and (for `forget`) delete
      the user's storage directory.
- [ ] Image-input limits (1 per turn, downscale to 1024 px) — NOT YET
      enforced in the channel; current code accepts 1 photo per message
      but doesn't downscale.
- [ ] Inline-keyboard "follow-ups" row — NOT YET emitted; the cheap
      separate call for suggestions is deferred until the model surface
      is settled.
- [ ] Per-message log / observability dashboard — NOT YET wired beyond
      logger lines.

### Auth — **DONE (awaiting test)**
- [x] `benchclaw/auth.py`: secret read/write at
      `storage/_admin/secret.json`, marker read/write at
      `storage/<channel>/<user>/auth.json` (stores the **matched code**,
      not a version number — see `spec/AUTH.md`).
- [x] Code generation in the lecture alphabet
      `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`, length 6 by default.
- [x] `AuthRateLimiter` keeps in-memory per-user failure counters
      (5 / 10 min → 1 h lockout).
- [x] Telegram channel `_gate` middleware lets `/start`, `/help`, `/auth`,
      `/forgetme` through unauthenticated; everything else replies with
      the one-line auth nudge. Admin commands additionally check the
      caller's `user.id` against `TelegramConfig.admin_user_ids`.
- [x] Tests in `tests/test_auth.py` cover round-trip, marker drift after
      rotation, `authenticated_addresses` filtering, the rate limiter's
      lockout-after-threshold behaviour.

### Persona switch coherence — **DONE (awaiting test)**
- [x] Persona overlay moved out of `system_prompt.j2` and into the
      synthetic tail message in `AgentLoop._inject_tail` as a
      `<persona>` block alongside `<current_time>` and
      `<storage_listing>`. Persona switches no longer bust the
      cacheable system-prompt prefix.
- [x] Telegram `_announce_persona_switch` publishes a
      `SystemMessageEvent` whenever `personalities.write_personality`
      is called (both the slash-command form and the inline-keyboard
      callback). The agent loop appends it to the session, so the
      switch shows up in transcript dumps and gives the model an
      explicit boundary to anchor on.

### Prompt-cache busting check — **DONE (awaiting test)**
- [x] In-process monitor in `benchclaw/agent/cache_monitor.py`. Keyed
      by `MessageAddress`, called from `AgentLoop._build_prompt_and_messages`
      after `_inject_tail`. Stores the last system message (raw, for
      diffs) and a tuple of message hashes for the stable prefix
      (everything before the synthetic injection). On divergence, logs
      a warning naming the offset and a ~120-char context window
      (system) or the message index, role, and content preview
      (history). Repeated identical fingerprints are de-duplicated per
      address.
- [x] Forgotten on `/reset` and `/forgetme` so a wiped session starts
      with a fresh snapshot.
- Open question: does vLLM expose per-request prefix-cache hit/miss
  stats? If so, extend `LLMResponse.usage` to carry them and log the
  hit-rate per turn alongside the in-process monitor. Skipped for now
  — vLLM's `/metrics` endpoint exposes aggregate prefix-cache stats
  but not per-request fields. Revisit if that changes.

# Progress

  TODO status:
  - ✅ Logging removal — DONE (awaiting test)
  - ✅ Compaction rebuild — DONE (awaiting test)
  - ✅ Storage layout — DONE (awaiting test)
  - ✅ Dummy-LLM harness — DONE (awaiting test)
  - ✅ Lecture customization — DONE (awaiting test)
  - ⬜ RAG integration — DEFERRED
  - ✅ Telegram bot service — DONE (awaiting test); image downscale + inline follow-ups deferred
  - ✅ Auth — DONE (awaiting test)

Feel free to clobber workspace/ if you need to.
