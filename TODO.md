# TODO

## Lecture deployment (in-class use, AI-in-business RAG)

### Logging removal
- Remove the voluntary `LogTool` and `LogStore` (`benchclaw/agent/tools/memory.py`).
- Drop the `("log", LogTool)` entry from `benchclaw/agent/tools/builtins.py` and any `ctx.log_store` plumbing.
- Strip log-related guidance from `workspace/AGENTS.md` (the "Use the log tool…" / "Do not log routine compliance…" lines) and any references in `system_prompt.j2` / config.
- Delete `workspace/logs/` handling and the `log.jsonl` summary path used by the current reactive compaction.

### Compaction rebuild
Canonical design in **`spec/COMPACTION.md`** (LLM-generated summarization, restart with `system_prompt + summary`, single proactive prompt-size trigger calibrated to ~18k for the 24k lecture window, stale-chunk elision with system-prompt hint, persisted summary).

- Implement per the spec. Wire config knobs: `compaction.threshold`, `compaction.summarize_model`, `compaction.elide_chunks_after_turn`, summarization-prompt template path.
- Validate with the dummy-LLM harness (multi-turn sessions, summary handoff, post-compaction continuity).
- Ensure the persisted summary is included in transcript dumps (see Telegram observability).

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

### Storage layout (per-conversation sandbox)
Replaces the `memory/` folder convention entirely. Memory is just files in the conversation's storage directory.

- Per-conversation root at `storage/<channel>/<id>/` (nested, not flat — channels like WhatsApp/email have IDs containing `@` `+` `.`). Media subdirectory at `storage/<channel>/<id>/media/`. For Telegram lecture use, `<id>` = `user.id` (stable per account, DM-only assumption); group-chat shape is a separate decision.
- Skills stay at `workspace/skills/`, read-only for everyone, listed with descriptions in the system prompt as today.
- `common/` is **read-only** by default — prof prepares shared resources, students read them.
- Each user gets `common/scratch/<user_id>.md` as a known-writable file inside common: "read everyone's, write your own" with no overlap and no last-write-wins corruption.
- Tool-level path enforcement (not prompt-level): `read_file` / `write_file` take a per-call allow-list (conversation root + skills read + common read + own scratch write) and reject absolute paths plus `..` traversal. Prompt instructions are a hint, not a sandbox.
- Top-level listing of `storage/<channel>/<id>/` is injected as a **synthetic tail turn** (e.g. a fake user/system message right before the latest user message), *not* in the system prompt. Reasons: the system-prompt prefix (tools, persona, skills index) stays cache-stable across writes; the tail turn is uncached anyway because the new user message lives there. Listing format: deterministic sort, names + sizes, no timestamps that bucket-shift mid-session.
- Per-user profile lives at `storage/telegram/<user_id>/profile.md` (durable facts the bot may elicit and store: industry, role, depth preference). A short summary is injected into the end of the system prompt each turn, and is not persisted in the session.
- `/forgetme` = delete `storage/<channel>/<user_id>/` recursively (clears profile, auth, and media). `/reset` only clears in-memory session state (history, last_retrieval, personality); does not touch files.
- `storage/_admin/` (used by auth) is **out of scope** for all user-facing tools — the bot service reads it directly with hard-coded paths. Path enforcement must reject any tool-driven attempt to read or write under it.
- Drop the `memory_files` Jinja branch and the `memory_dir` field on the context builder.

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
