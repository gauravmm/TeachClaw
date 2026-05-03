# TODO

## Lecture deployment (in-class use, AI-in-business RAG)

### Logging removal
- Remove the voluntary `LogTool` and `LogStore` (`benchclaw/agent/tools/memory.py`).
- Drop the `("log", LogTool)` entry from `benchclaw/agent/tools/builtins.py` and any `ctx.log_store` plumbing.
- Strip log-related guidance from `workspace/AGENTS.md` (the "Use the log tool…" / "Do not log routine compliance…" lines) and any references in `system_prompt.j2` / config.
- Delete `workspace/logs/` handling and the `log.jsonl` summary path used by the current reactive compaction.

### Compaction rebuild (per spec/COMPACTION.md decision)
- Implement LLM-generated summarization: on trigger, summarize the prior conversation and restart the context with `system_prompt + summary` only. No tiered history, no log-store summary.
- Single proactive trigger keyed to total prompt size: fire at ~70% of `(context_window − max_tokens)`. Anticipated model is `google/gemma-4-e4b-it`, which supports up to 128k natively, so a 16k working window is well inside its quality envelope; the trigger exists for budget control, not to dodge attention degradation. With 16k window and a ~2k output reserve, the trigger fires at ~9.8k of input. No separate per-turn or per-history caps — one trigger handles both.
- Stale-chunk elision: in turns *after* the retrieval call, replace verbatim chunk bodies in the tool-result event with a stub like `[chunks elided — ids: a, b, c]`. Citation IDs stay visible so prior `[1] [2]` references still resolve, and `fetch_doc` can re-hydrate on demand. This runs before summarization is triggered, so the summarizer sees a leaner history.
  - System-prompt hint required: when elision is enabled, `system_prompt.j2` (or the lecture persona) must explicitly tell the model that prior chunk bodies are stubs and to call `fetch_doc(id)` rather than quote remembered text. Without this, the model will occasionally hallucinate quotes from chunks it can no longer see.
- Add config knobs: `compaction.threshold` (fraction; default 0.7), `compaction.summarize_model` (default to main model), `compaction.elide_chunks_after_turn` (bool; default true), summarization prompt template.
- Persist the summary on the session so reloads and debugging can inspect it.

### Dummy-LLM harness (testing prerequisite)
- Add a deterministic fake provider behind the existing provider interface (`benchclaw/providers/`) that:
  - Replays scripted responses / tool calls from a YAML or JSON fixture.
  - Reports configurable `usage.total_tokens` so compaction triggers can be exercised.
  - Supports a "balloon" mode that emits large outputs to force overflow.
- Wire it into config so a lecture/test profile selects it without touching prod config.
- Add tests that drive multi-turn sessions through the fake provider to validate: tool dispatch, compaction firing, summary handoff, post-compaction continuity.

### RAG integration (AI-in-business knowledge)
- Implement the `search` / `fetch_doc` tool contract from lecture-knowledge `spec/ROUGH.md §2` (citation IDs + chunk metadata returned to the model).
- Hybrid retriever: BM25 + dense, hybrid-scored. Decide whether the index ships in-repo, behind an HTTP service, or via MCP.
- Citation rendering: footnote-style `[1] [2]` inline plus a "Sources" section; respect a per-user `cite` toggle.
- Persist `last_retrieval` (chunk IDs) on session state so a "Show sources" follow-up can dump the raw chunks.
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
- `/forgetme` = delete `storage/<channel>/<user_id>/` recursively. `/reset` only clears in-memory session state (history, last_retrieval, personality if we want); does not touch files. Document this split so it isn't ambiguous later.
- Drop the `memory_files` Jinja branch and the `memory_dir` field on the context builder.

### Lecture customization
- Adapt `workspace/AGENTS.md` style for classroom persona (replace OcelliBot framing, drop personal-assistant specifics like Notion clearinghouse, image annotation, memory-folder conventions that don't apply). Replace memory-folder guidance with the per-conversation storage model from the Storage layout section.
- Trim the `memory_files` listing from `system_prompt.j2` — the per-conversation listing now lives in a tail turn, not the system prompt. Keep `bootstrap_files`; that's how AGENTS.md gets loaded into the system prompt.
- Pick a per-class workspace layout so each session starts clean. This should be put in `workspace_default/`
- Personalities: implement `/personality` swapping the system prompt only (retrieval/tools unchanged). Initial set: `default`, `skeptical_cfo`, `vc_partner`, `mck_analyst`, `professor`. Per-user, persists for the session, cleared on `/reset`.
- Encourage Mermaid diagrams in the lecture system prompt (value chains, 2x2s, sequence diagrams) — render path lives in the Telegram bot section.

### Telegram bot service (lecture)
- Slash commands via `setMyCommands` at startup, default scope for students, admin scope for the prof: `/start`, `/help`, `/personality`, `/sources`, `/scope`, `/cite`, `/reset`, `/forgetme`, admin `/reload_corpus`, `/stats`.
- Per-user profile keyed by Telegram `user.id` (stable per account). Bot may ask durable questions (industry, role, depth preference) and store answers; a short profile summary is injected into the system prompt each turn. `/reset` clears conversation history only; `/forgetme` deletes the profile file as well.
- Inline keyboards for choice-style commands (`/personality`, `/scope`) since Telegram has no arg autocomplete; typed args still accepted as a power-user shortcut.
- Reply rendering: one message per turn with answer body, optional citations + Sources, and an inline-keyboard row of up to 3 model-suggested follow-ups plus a "Show sources" button. Split on paragraph boundaries above ~3500 chars.
- Image input: accept photo + caption, route to multimodal call. Image is *not* indexed; model may emit a follow-up `search` call. Downscale to 1024px long edge, 1 image/turn.
- Mermaid rendering pipeline (post-processor in the Telegram channel for the lecture; renderer itself lives in a standalone module so a future skill/tool wrapper or a second channel can reuse it without a rewrite):
  - House the renderer in something like `benchclaw/rendering/mermaid.py` — pure function from Mermaid source + theme to PNG bytes (or cached path), no Telegram knowledge.
  - Detect fenced ```mermaid blocks in model output.
  - Render via `mmdc` (`@mermaid-js/mermaid-cli`) in a long-lived warm worker (avoid headless-Chrome cold start per request). Pinned version, fixed CSS theme, transparent background.
  - 5s timeout per diagram; on timeout/syntax error fall back to posting raw Mermaid in a code block with a one-line apology.
  - Max 2048×2048 px (downscale long edge); cap 2 diagrams per reply, extras get fallback.
  - Cache keyed by `sha256(mermaid_source + theme_id)`.
  - Telegram delivery: send PNG as photo *replacing* the fenced block; remaining prose goes in the photo caption if ≤1024 chars else as a separate text message; preserve original order.
- Rate limits: 1 in-flight request per user (queue or "still thinking…"); soft cap 30 msgs / user / 10 min then a "take a breath" reply; hard cap 3 tool calls per turn.
- Per-user state: `personality`, `scope`, `cite`, `history`, `last_retrieval`. In-memory by default; Redis only if we need restart survival (probably overkill for one lecture).
- No separate history cap on the bot side — the unified compaction trigger (see Compaction rebuild) handles overflow. Drop the handoff's "8 turns or 6k tokens" sketch; turn-count caps fire inconsistently when tool calls inflate message count, and a second cap competes with the orchestrator's trigger.

### Observability (lecture)
- Per-message log: hashed `user_id`, latency breakdown (retrieval / model / format), tokens in/out, tool calls, retrieved chunk IDs.
- `/stats` for the prof during class (active users, query count, retrieval latency).
- Dump full transcripts at end of session for post-lecture review.

## Other / pre-existing
- Telegram — write a spec for updates
  - secret session-auth (users must enter a code to authenticate a session; rotating the code revokes access)
    - rate limits for failed tries.
  - Check if support for "…" (typing indicator) works well for long times.
  - Check if reading emoji responses to messages or writing emoji responses on user messages is possible.
  - Check if we need any new features for the handoff doc.


