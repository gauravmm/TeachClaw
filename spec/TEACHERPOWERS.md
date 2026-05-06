# Teacher powers — pushing into student sessions from the lectern

The bot today is fully reactive: every conversation is driven by the
student typing first. The instructor can rotate the auth code
(`/setsecret`) and snoop the user list (`/whoauthed`), but cannot
*push* anything into a student session — no announcements, no nudges,
no in-class polls. This spec adds three teacher-driven primitives that
ride on the bus and admin gating already in place.

## What we want

Three operations, in increasing intrusiveness:

1. **Broadcast text/media** — "Slide deck is at <url>". Direct chat
   message to every authenticated student. The agent isn't involved.
2. **Inject instruction** — "From here on, only answer in the context
   of Module 4." Adds a system-prompt directive to every active
   session so the model adopts it on the next student turn.
3. **Class question** — "Pick a 2x2 framework that fits decision
   X. Reply when you've thought about it." Runs as if each student
   had typed the prompt: the agent answers it once, in each session.

The three sit on a spectrum: (1) is purely outbound, (2) is silent
state-mutation that surfaces on the next reply, (3) actively spends
LLM tokens for every authed user. The trigger surface is the same;
the side effects differ.

## What's already in place

The plumbing exists; the only thing missing is a teacher-facing API
that uses it.

- **Per-channel admin gating** — `TelegramChannel.is_admin(user_id)`
  (`channels/telegrm/channel.py:100-101`) and
  `admin_user_ids` in `TelegramConfig`. Every operation in this spec
  is admin-only, gated the same way `/setsecret` already is
  (`commands.py:515-536`).
- **Recipient enumeration** —
  `auth.authenticated_addresses(workspace, channel)`
  (`auth.py:116-131`) walks `storage/<channel>/*` and returns the
  list of users whose marker matches the current secret. Exactly the
  cohort we want to broadcast to.
- **Outbound delivery** — `MessageBus.publish_outbound` and
  `OutboundMessage` (`bus.py:62-69`, `233-245`) already carry text,
  media, and metadata to any address.
- **Instruction injection** — `SystemMessageEvent`
  (`bus.py:81-86`) is exactly the right primitive. It's already
  used for persona switches (`commands.py:178-186`), and the agent
  loop's batch processor (`agent/loop.py:208-214`) appends it to
  the session and triggers an LLM turn unless tools are in flight
  (in which case it's buffered and flushed once tools settle).
- **Synthetic user prompts** —
  `_handle_example_callback` (`commands.py:385-421`) already submits
  a prompt as if the user typed it, by calling
  `channel._handle_message(...)`. Class-question delivery is the
  same path, fanned out to N addresses.

In short: this spec adds a thin teacher-facing layer over primitives
that already exist. No new event types on the bus, no new channel
abstractions.

## Trigger surface — admin DM commands

The instructor talks to the bot in their own DM (where they're
already an admin per `admin_user_ids`) using new slash commands.
Sticking to slash commands keeps the trigger inside the channel that
already does auth and command dispatch — no new transport, no extra
auth story.

```
/announce <text>                    — broadcast the text to all authed students
/announce                           — (replied to a photo/document) broadcast that media
/inject <text>                      — push a system-prompt directive into every active session
/ask <text>                         — fan out the prompt as if every student had typed it
/cohort                             — list authed students with their last-active timestamp
```

A long-message authoring problem is solved by reply-quoting: an
admin can compose the announcement as a normal message in their DM,
then reply to it with `/announce` (no body) to broadcast the quoted
text. The body of the replied-to message is what gets sent. Same
trick for `/inject` and `/ask`.

For media, the admin sends a photo or document with the caption
`/announce`; the photo/document is broadcast with the caption text
(minus the leading `/announce`) as the message body. This matches
how teachers naturally send pictures of the whiteboard.

### Targeting

By default, every operation targets *all* addresses currently
authenticated against the live secret. That's what the room of
students plus the bot has agreed to: if you authed, you opted into
class messages.

Three escape hatches, from least to most surgical:

- `--exclude-groups` flag — only DMs, not group chats. Useful when
  there's a TA channel or course-staff group room that shouldn't see
  a class announcement.
- `--to <chat_id>` — single address, for spot-checking.
- `--cohort <name>` — a saved list of `chat_id`s, written by
  `/cohort save <name>` (out of scope for v1; the file format is
  trivial enough to add later).

V1 ships only the default ("all authed") and `--to` for testing.
The flag syntax is simple to parse and easy to add a flag to later
without breaking callers.

## Operation 1 — `/announce <text>` (and reply-to-media)

Pure outbound. The agent loop is not touched.

```python
async def cmd_announce(channel, update, _ctx):
    if not channel.is_admin(update.effective_user.id):
        await msg.reply_text("Admin command.")
        return
    body, media_paths = _resolve_announcement_body(update)  # text or quoted msg
    targets = auth.authenticated_addresses(channel.workspace, channel.name)
    sent, failed = 0, 0
    for chat_id in targets:
        addr = MessageAddress(channel.name, chat_id)
        try:
            await channel.bus.publish_outbound(
                OutboundMessage(
                    address=addr,
                    content=body,
                    media=media_paths,
                    metadata={"announcement": True, "from_admin": user.id},
                )
            )
            sent += 1
        except Exception as e:
            logger.warning(f"announce to {addr} failed: {e}")
            failed += 1
    await msg.reply_text(f"Announced to {sent} student(s); {failed} failure(s).")
```

Two design choices worth flagging:

- **The model doesn't see the announcement.** A pure
  `OutboundMessage` bypasses the session — the student gets a
  message, but the next time they ask a follow-up the agent has no
  record that "the prof said X". For most announcements ("class
  starts at 14:30", "slides at <url>") that's fine; for content
  announcements ("we just covered build-vs-buy") it's not. The
  follow-up question can be answered correctly by also publishing a
  `SystemMessageEvent` with the announcement body — see
  `--remember` flag below.
- **Format**: a small visual marker so students can tell a teacher
  push from a normal bot reply. Telegram channels can render the
  outbound with a fixed prefix (e.g. `📣 *From the instructor:*\n\n`)
  driven by the `metadata['announcement']` flag, applied by
  `outbound.send` (`channels/telegrm/outbound.py`).

`--remember` flag: when present, additionally publish a
`SystemMessageEvent` per address with content like
*"The instructor announced to the class: <body>. Treat it as
context for follow-up questions."* That gives the agent the same
context the student now has. Default off — most announcements are
operational, not pedagogical.

## Operation 2 — `/inject <text>`

Mutates session state, no immediate user-visible reply.

```python
async def cmd_inject(channel, update, _ctx):
    if not channel.is_admin(...):
        return
    body = _resolve_body(update)
    targets = auth.authenticated_addresses(...)
    for chat_id in targets:
        addr = MessageAddress(channel.name, chat_id)
        await channel.bus.publish_inbound(
            addr,
            SystemMessageEvent(content=f"INSTRUCTOR DIRECTIVE: {body}"),
        )
    await msg.reply_text(f"Injected directive into {len(targets)} session(s).")
```

The agent loop already handles the rest: `SystemMessageEvent` lands
in the per-address queue, gets appended as a `SystemEvent`, and
triggers an LLM turn (`agent/loop.py:208-214`). For users who are
mid-tool-call when the injection arrives, it buffers and flushes
once tools settle (same code path) — so we don't race the agent.

**The "triggers an LLM turn" property is intentional** for some
directives ("answer in Module 4 context only" — best confirmed
back so the student knows to ignore stale reasoning) and unwanted
for others (a silent reframing the student shouldn't see called
out). Two ways to draw the line:

1. **Quiet flag** — `--quiet` suppresses the LLM turn. Implementation:
   the directive is queued but the loop skips the LLM call if the
   only batched event was a quiet system message. Requires a tiny
   addition to `SystemMessageEvent` (`quiet: bool = False`) and a
   one-line guard in `_apply_inbound_batch`.
2. **Always reply, but tell the model to be terse** — phrase
   injected directives as "Acknowledge this directive in one short
   sentence: …". No code change. Less reliable.

Recommendation: do (1). It's three lines and the discipline of an
explicit flag is worth it; "I want this without surfacing it" is a
common ask for class management.

## Operation 3 — `/ask <text>`

The class poll. Each authed student's session gets the prompt as if
*they* had typed it; the agent answers per-student, with each
student's persona, profile, and history.

```python
async def cmd_ask(channel, update, _ctx):
    if not channel.is_admin(...):
        return
    body = _resolve_body(update)
    targets = auth.authenticated_addresses(...)
    for chat_id in targets:
        await channel._handle_message(
            sender_id=f"instructor:{user.id}",
            chat_id=chat_id,
            content=body,
            metadata={"source": "instructor_ask", "from_admin": user.id},
        )
    await msg.reply_text(f"Posed the question to {len(targets)} student(s).")
```

`channel._handle_message` is the same entry point used by the
`/start` example buttons (`commands.py:410-421`). It already wraps
the content into an `InboundMessage` with the right address and
publishes it; no new session-bookkeeping work is needed.

A few side-notes:

- **Cost.** This fires N agent turns. For a 60-student class a
  single `/ask` could cost real money. The teacher should see a
  confirmation before fan-out: "Send this to 47 students? Reply
  /confirm within 30s." Implementation: stash the pending ask in
  `_users[admin_id]` state; require a follow-up `/confirm`. Skip
  for v1 if the teacher is the only admin — the slash command is
  already deliberate.
- **Provenance.** Pass `metadata={"source": "instructor_ask"}` so
  the agent loop can prepend a marker to the user-message text in
  the session ("[instructor question] …"), which keeps the model
  honest about who asked. The agent loop already passes `metadata`
  through (`InboundMessage.metadata`); we'd add a small
  `_collapse_user_messages` branch to prefix instructor-asks.
- **Replying back.** Each student replies in their own DM as today.
  We could mirror replies to a teacher's "results" channel (a
  group chat the instructor watches), but that's a v2; v1 just
  spends the tokens and lets the prof spot-check via `/whoauthed`
  + DM peek.

## Optional Operation 4 — `/cohort`

Read-only listing for sanity-check before pushing:

```
/cohort
→ 47 authenticated students:
   • telegram:11122233 — last active 2m ago
   • telegram:44455566 — last active 14m ago
   …
```

"Last active" comes from the latest event in
`workspace/sessions/<addr>/*.jsonl`. Not load-bearing — but useful
before firing `/ask`.

## Surface summary

What this spec adds:

- **Five new admin slash commands** in
  `teachclaw/channels/telegrm/commands.py`: `cmd_announce`,
  `cmd_inject`, `cmd_ask`, `cmd_cohort`, plus a small
  `_resolve_body` / `_resolve_announcement_body` helper.
- **Two lines on `SystemMessageEvent`** in `bus.py` — add a
  `quiet: bool = False` field plus a one-line guard in
  `agent/loop.py:_apply_inbound_batch` to skip the LLM turn when
  every batched system event in this turn is quiet (preserve the
  trigger when at least one isn't).
- **A small `outbound.send` branch** that prepends
  `📣 *From the instructor:*\n\n` when
  `metadata.get("announcement")` is set.
- **Three new entries in `ADMIN_COMMANDS`** in
  `channels/telegrm/config.py` so the admin Telegram menu shows
  them.

What this spec does *not* change:

- The bus event types (other than the `quiet` field).
- The agent loop's per-address concurrency model.
- Auth, storage layout, or per-user state.
- Any other channel. The same primitives port to email/Slack later
  by reading from a shared "teacher API" module that owns the
  recipient list and message construction; v1 keeps the logic in
  the Telegram command file because Telegram is the only channel
  with a real teacher right now.

## Failure modes worth thinking about now

- **A student has /clear-ed mid-class.** Their session is empty.
  `/inject` lands cleanly (creates a new session); `/ask` lands
  cleanly (creates a new session whose first event is the
  instructor's question, which is fine). No special handling.
- **A student has /forgetme'd.** Their auth marker is gone. They're
  not in `authenticated_addresses` anymore, so they're skipped. No
  surprise.
- **The secret was rotated mid-broadcast.** `authenticated_addresses`
  is computed once at the top of the command. We commit to the
  cohort as it was at the moment the teacher hit enter; rotation
  during the loop doesn't undo a partial broadcast. That matches
  what the teacher expects.
- **Tools are in flight for some addresses.** Already handled:
  `SystemMessageEvent` buffers; `InboundMessage` interrupts via the
  existing tracker.handle_interrupt path.
- **A student is in a group chat.** Group `chat_id`s show up in
  `authenticated_addresses` once an admin authed the room. A
  broadcast to a group will be visible to everyone in it, which is
  usually what the teacher wants ("class announcement"). Document
  this in the `/announce` confirmation banner so the teacher
  doesn't accidentally announce a private message into a TA's
  staff group.

## Out of scope for v1

- **Saved cohorts.** Tag-based subsets ("just the EMBA cohort") —
  add when needed; the file format is one line per `chat_id`.
- **Scheduled pushes.** "Send this announcement at 14:30" — easy
  to layer on later via the existing `cron` tool, but conflates
  scheduling with teacher-API.
- **Reply aggregation.** Pulling all student replies to an `/ask`
  into a results panel for the teacher.
- **A web operator console.** Useful eventually, especially for
  multi-step authoring (preview, edit, broadcast). Out of scope
  while we have one teacher and a Telegram DM.
