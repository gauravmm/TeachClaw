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

### Dummy-LLM harness (testing prerequisite)
- Add a deterministic fake provider behind the existing provider interface (`benchclaw/providers/`) that:
  - Replays scripted responses / tool calls from a YAML or JSON fixture.
  - Reports configurable `usage.total_tokens` so compaction triggers can be exercised.
  - Supports a "balloon" mode that emits large outputs to force overflow.
- Wire it into config so a lecture/test profile selects it without touching prod config.
- Add tests that drive multi-turn sessions through the fake provider to validate: tool dispatch, compaction firing, summary handoff, post-compaction continuity, typing-indicator behaviour during long agent turns.

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

### Lecture customization
- Adapt `workspace/AGENTS.md` style for classroom persona (replace OcelliBot framing, drop personal-assistant specifics like Notion clearinghouse, image annotation, memory-folder conventions that don't apply). Replace memory-folder guidance with the per-conversation storage model from the Storage layout section.
- Trim the `memory_files` listing from `system_prompt.j2` — the per-conversation listing now lives in a tail turn, not the system prompt. Keep `bootstrap_files`; that's how AGENTS.md gets loaded into the system prompt.
- Pick a per-class workspace layout so each session starts clean. This should be put in `workspace_default/`.
- Personalities: implement `/personality` swapping the system prompt only (retrieval/tools unchanged). Initial set: `default`, `skeptical_cfo`, `vc_partner`, `mck_analyst`, `professor`. Per-user, persists for the session, cleared on `/reset`.
- Encourage Mermaid diagrams in the lecture system prompt (value chains, 2x2s, sequence diagrams) — render path is specified in `spec/TELEGRAM.md`.

### Telegram bot service & lecture surface
Canonical design in **`spec/TELEGRAM.md`** (slash commands, reply rendering with `<citation>` stripping, reaction dispatcher with 👀 sources / 🔍 tool-call trace, image input, Mermaid post-processor, rate limits, typing-indicator behaviour, observability).

- Implement per the spec.
- Agent loop must capture tool calls into the per-`message_id` map so the 🔍 reaction can dump them.
- Mermaid renderer module: `benchclaw/rendering/mermaid.py`, channel-agnostic pure function `(source, theme) → PNG`.

### Auth
Canonical design in **`spec/AUTH.md`** (shared-secret session gate, per-user `auth.json` stores the matched code rather than a version number, rate-limited `/auth`, admin `/setsecret`/`/whoauthed`, integration with `/forgetme` and `/reset`).
