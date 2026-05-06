# Telegram bot service (lecture deployment)

Canonical design for the Telegram-facing surface of the in-class
assistant. Auth is specified separately in `spec/AUTH.md`. Compaction
and context-budget behaviour live in `spec/COMPACTION.md`. Everything
else about the user-facing experience is here.

## Goal and scope

A Telegram bot that students interact with during a lecture about AI in
business. The bot has access to a curated RAG corpus, two web-fallback
tools (Wikipedia, Brave), and a small set of personalities. The prof
runs it, controls the auth code, and gets a few admin levers.

This spec covers only the channel surface: command set, reply
rendering, reactions, image input, diagrams, rate limits, indicators,
and observability. Tooling, retrieval, storage, and compaction are out
of scope and live in their own specs and TODO sections.

## Slash commands

Registered via `setMyCommands` at startup. Default scope for students,
admin scope (per-user) for the prof.

| Command         | Scope   | Description (≤120 chars)                          |
|-----------------|---------|---------------------------------------------------|
| `/start`        | default | Greeting + 3 example questions                    |
| `/help`         | default | What I can do, what data I have, how to cite      |
| `/auth`         | default | Authenticate with the code from the slide         |
| `/personality`  | default | Switch reply persona (CFO, VC, analyst, professor)|
| `/sources`      | default | List available corpora and roughly how big each is|
| `/scope`        | default | Restrict retrieval to one corpus, or `all` to clear|
| `/cite`         | default | Toggle inline citations (on/off)                  |
| `/clear`        | default | Clear conversation history for this user          |
| `/forgetme`     | default | Delete the user's storage directory and re-auth   |
| `/setsecret`    | admin   | Rotate the shared auth secret (see `AUTH.md`)     |
| `/whoauthed`    | admin   | List currently authenticated user IDs             |
| `/stats`        | admin   | Active users, query count, retrieval latency      |

Telegram does not support per-arg autocomplete, so commands that take a
choice (`/personality`, `/scope`) reply with an inline keyboard of
options rather than expecting a typed arg. Typed args still work as a
shortcut for power users.

The auth check sits in front of every command except `/start`, `/help`,
`/auth`. See `spec/AUTH.md`.

## User identity

The user identifier is Telegram `user.id` (stable per account,
independent of `@username`). The lecture is DM-only; group-chat shape
is a separate decision and not in scope.

## State

### On disk (per-user)

Lives under `storage/telegram/<user_id>/`:

- `auth.json` — see `AUTH.md`.
- `profile.md` — durable facts the bot may elicit and store (industry,
  role, depth preference). A short summary is injected into the system
  prompt each turn.
- `media/` — images the user has sent, plus any files the bot writes
  for them.

`/forgetme` deletes this directory recursively. `/clear` does not touch
files on disk.

### In-memory (per-user, session-scoped)

- `personality` — current persona name.
- `scope` — current retrieval scope (corpus name, or `null` for all).
- `cite` — inline-citation toggle.
- `history` — conversation events. No separate cap on the bot side; the
  unified compaction trigger in `spec/COMPACTION.md` handles overflow.
- `last_retrieval` — chunk IDs from the most recent `search` call.
- `message_id → {citations, raw_chunks, tool_calls}` map with a 24h
  TTL, so users can react to past replies and get the underlying
  sources or tool-call trace back. See reaction handler below.
- `seen_first_citation` — whether the discoverability hint for the ❤️
  reaction has been shown to this user this session.

In-memory by default. Redis only if we need to survive bot restarts
mid-class, which is probably overkill.

## Reply rendering

Each model reply produces one Telegram message:

1. Answer body. Citation tags `<citation id="…">…</citation>` (see
   `spec/COMPACTION.md` and the RAG section of `TODO.md` for the
   emission convention) are stripped from the displayed text; the
   `chunk_id` values are stored in the per-`message_id` map for
   reaction lookup.
2. An inline-keyboard row of up to 3 model-suggested follow-ups,
   generated in a separate cheap call.
3. On the **first** cited reply of a session, append a one-line hint:
   `(react ❤️ to any reply for sources)`. Tracked per-user; never
   repeated within a session.

There is no inline "Show sources" button. That flow is reaction-driven.

Long replies (>~3500 chars, well under Telegram's 4096 limit) are split
on paragraph boundaries.

## Reaction handler

Enable `message_reaction` in `setWebhook` `allowed_updates`. Dispatch
on emoji via a generic table, not a hardcoded eyes-only branch, so
future affordances drop in without restructuring:

| Emoji | Action                                                   |
|-------|----------------------------------------------------------|
| ❤️    | Reply with the source chunks for that message, using the per-`message_id` map. Acknowledge by reacting back with ❤️ via `setMessageReaction`. |
| 🔥    | Reply with the tool-call trace for that message — structured list of `name(args) → short result summary`, no LLM narration. Same 24h TTL. Pedagogical for an AI-in-business class; cheap because no extra model call. |
| 👍/👎 | Future: feedback signal for transcript review. Stub now, no behaviour. |
| ❓    | Future: "explain more" — re-prompt with the prior reply as context. |
| 🔁    | Future: regenerate with a different sample.              |

If the per-`message_id` map has expired (24h TTL elapsed, bot restart),
the bot replies with `Sources for that reply have expired — ask again
and I'll re-cite.`

## Image input

Photo + optional caption is routed to the multimodal call. The image is
**not** embedded into the retrieval index; the model may emit a
follow-up `search` call instead. Limits:

- 1 image per turn.
- Downscale to 1024px on the long edge before sending to the model.
- Stored under `storage/telegram/<user_id>/media/`.

## Mermaid diagrams

The model is encouraged (in the lecture system prompt) to draw diagrams
when they help — value chains, 2x2s, sequence diagrams. Mermaid is the
wire format because it is compact, deterministic, and easy for a small
model to emit correctly.

### Renderer module

Implementation lives in `teachclaw/rendering/mermaid.py` as a
channel-agnostic pure function: `(mermaid_source, theme) → PNG bytes`
or cached path. No Telegram knowledge in the renderer itself; the
Telegram channel calls it and handles delivery. A future skill/tool
wrapper or a second channel can reuse the same module without a
rewrite.

### Detection and rendering

- Scan the model's reply for fenced code blocks with the `mermaid`
  info-string.
- Render via `mmdc` (`@mermaid-js/mermaid-cli`) in a long-lived warm
  worker, to avoid headless-Chrome cold start per request. Pinned
  version, fixed CSS theme, transparent background.
- 5s timeout per diagram. On timeout or syntax error, fall back to
  posting the raw Mermaid source in a code block with a one-line
  apology: `couldn't render this diagram, source below`.
- Max output: 2048×2048 px, downscale on the long edge if larger.
- Cap: at most 2 diagrams per reply. Extras get the fallback treatment.
- Cache keyed by `sha256(mermaid_source + theme_id)`. Near-zero cost
  on re-asks of the same question.

### Telegram delivery

The rendered PNG is sent as a photo, **replacing** the fenced block in
the text. The remaining prose is sent as the photo's caption if it
fits in 1024 chars, otherwise as a separate text message that
references the image. Order is preserved by sending image and text in
the order they appear in the original reply.

Hosted services like `mermaid.ink` and `kroki.io` are rejected:
network dependency mid-reply, third-party data leak, and rate limits
that bite during a live demo.

## Rate limits

- One in-flight request per user. A second message while the first is
  still being processed gets queued or rejected with `still thinking…`.
- Soft cap: 30 messages per user per 10 minutes. On exceeding, reply
  `take a breath` and ignore further messages for the rest of the
  window.
- Hard cap on tool calls per turn: 3. Bounds latency and cost.

## Typing indicator

Implemented in `teachclaw/channels/telegrm.py:_typing_loop`. The agent
emits `TypingEvent`s; the channel sends `send_chat_action` with the
`typing` action every 4 seconds, capped at 8 invocations (~32s) per
event so a hung agent cannot hammer Telegram.

If lecture demos consistently exceed 32s per turn (small GPU + heavy
retrieval + long output), options are: bump the cap, or have the agent
re-emit a `TypingEvent` mid-turn after a long retrieval to restart the
loop. Tune from real timings rather than guessing.

## Observability

- Per-message log: hashed `user_id`, latency breakdown
  (retrieval / model / format), tokens in/out, tool calls, retrieved
  chunk IDs.
- `/stats` for the prof during class: active users, query count,
  retrieval latency.
- Dump full transcripts at end of session for post-lecture review.
