# TODO

## Lecture deployment (in-class use, AI-in-business RAG)

### RAG integration (AI-in-business knowledge)
- Implement the `search` / `fetch_doc` tool contract from lecture-knowledge `spec/ROUGH.md §2` (citation IDs + chunk metadata returned to the model).
- Hybrid retriever: BM25 + dense, hybrid-scored. Decide whether the index ships in-repo, behind an HTTP service, or via MCP.
- Citation emission: model wraps source-bearing claims in `<citation id="chunk_42">…short claim…</citation>` tags. Channel-agnostic; the Telegram channel strips the tags from the displayed text and keeps a per-message map from claim → chunk_id for the reaction-driven sources flow. System prompt must include a worked example so small Gemma emits the tag reliably. Per-user `cite` toggle controls whether the model emits citations at all.
- Persist `last_retrieval` (chunk IDs) on session state and keep a per-`message_id` map of `{citations, raw_chunks, tool_calls}` with a 24h TTL so users can react to a past message and get the sources or tool-call trace back.
- Companion lookup tools (sit alongside the curated corpus, not inside the retrieval ranking):
  - `wiki_lookup(query)` against Wikipedia — `https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=<q>&format=json` for search, `https://en.wikipedia.org/api/rest_v1/page/summary/<title>` for ~200-word lead extract + canonical URL + thumbnail, `/api/rest_v1/page/html/<title>` or `/page/wikitext/<title>` for the full body. Free, no auth, just a UA header. Sturdy fallback for "who is X / what is Y" queries the corpus doesn't cover; citations are clean (CC BY-SA + canonical URL).
  - `brave_search(query)` for the open-web fallback (recent news, niche docs not in the corpus). Results aren't curated — model must surface a "open-web result, may be unreliable" disclaimer when citing these.
  - Both tools live next to `search` / `fetch_doc`, distinct names so the model can pick deliberately. Document the precedence in the lecture system prompt: corpus first, Wikipedia for definitions/biographies, Brave only when the other two miss.

### Citation system — how it works (reference)
End-to-end map of the citation/reaction plumbing, captured here so future
agents don't have to grep the code. The channel side is fully wired and
tested via the dispatch path; nothing actually emits citations yet
because the RAG `search` tool isn't wired.

1. **Emission (model side, NOT WIRED).** Once `search` returns chunks with
   IDs, the model wraps source-bearing claims in
   `<citation id="chunk_42">short claim text</citation>`. The system
   prompt will need a worked example so a small model emits the tag
   reliably. Today no `<citation>` tags appear in any reply.
2. **Stripping (channel, wired).** `cit.strip_citations` runs on every
   outbound message. Regex `<citation\s+id=\"([^\"]+)\">(.*?)</citation>`
   matches each tag. Returns `(cleaned_text, citations)` where the text
   has tags removed (only the inner claim survives for the user to read)
   and citations is `[{"id": "chunk_42", "claim": "..."}]` in document
   order.
3. **Per-message storage (wired).** Citations + tool calls are stashed
   under the Telegram `message_id` of the first sent chunk (long replies
   split into multiple messages but the map keys only on the first).
   TTL 24h; past TTL the entry is tombstoned instead of deleted so
   reactions can distinguish "expired" from "untracked." Hard cap of
   1000 entries per user — oldest evicted first (tombstones go before
   live entries because they're older). `/clear` and `/forgetme` wipe
   the whole map.
4. **Reaction-driven retrieval (wired).** `SOURCES_REACTION` (❤️) and
   `TRACE_REACTION` (🔥) dispatch via the reactions handler with three
   branches per reaction: no record / expired / live entry — empty or
   populated.
5. **Discoverability hint.** First cited reply per session appends
   `(react ❤️ to any reply for sources)`; tracked via
   `seen_first_citation` so it never repeats within a session. Never
   fires today because no reply carries citations.
6. **Privacy boundary.** The per-message map lives on the per-chat user
   state, so one user can never react to another user's bot replies
   (different chat_id → different state object → no entry).

Feel free to clobber workspace/ if you need to.
