# Group chats — shared session, per-group derived auth

Today teachclaw treats every Telegram chat as 1:1: every `chat_id` is
assumed to belong to a single user, `auth.is_authenticated` is a
per-`MessageAddress` boolean, the rate-limit window in
`channels/telegrm/state.UserState` is keyed by `chat_id`, and the
attention filter has a `summon_group` policy that is half-implemented
(`teachclaw/channels/attention.py`) but not exercised end-to-end.

This spec defines first-class group support for the Telegram channel.
SMTP/email and Claude Code stay 1:1 and are out of scope.

## Session and identity model

**One shared session per group**, keyed by the existing
`MessageAddress("telegram", group_chat_id)`. All members append to the
same conversation history, share one `personalities` overlay, and one
storage root. `sender_id` already carries the per-user identifier
(`"<user_id>|<username>"`) — that's the only per-user signal that
flows through the bus.

The agent loop and storage layer need no structural changes: a group
chat is just an address whose `metadata["is_group"]` is True (already
populated in `channels/telegrm/inbound.py:122`).

### Sender attribution in the transcript

The LLM must always know who is speaking in a group. Today
`AgentLoop._collapse_user_messages` only prefixes `[sender_id]` when a
batch contains more than one message; in DMs that's fine, in groups it
hides identity for solo turns.

Change: when the address is a group, prefix every `UserEvent` rendered
into LLM messages with a human label — `@username` if available, else
the user's `first_name`, else the bare numeric id. Do this at render
time (in `Session._render_history` or wherever `UserEvent` becomes a
chat dict), not at collapse time, so DMs stay unprefixed and we don't
have to retroactively rewrite history if a chat is migrated.

The label format is `[Alice]` (matching the existing collapse format)
on its own line before the user content. Media blocks come first per
the existing tail-injection rules.

## Auth — admin-gated, slide-code shared

The slide code (`storage/_admin/secret.json`, see `spec/AUTH.md`)
already auto-revokes everything when rotated: the per-address marker
in `auth.json` stores the literal code that was used at /auth time,
and `is_authenticated` checks `marker == current_secret`. We reuse
that mechanism unchanged for groups; the only new gate is *which*
groups are eligible to authenticate at all.

### Eligibility

A group is eligible for authentication iff at least one of its
current Telegram chat admins has a user_id listed in
`channels.telegram.admin_user_ids` (existing config field, already
populated). This means:

- A bot operator (someone in `admin_user_ids`) must be a member of
  the group, with admin status, before /auth can succeed there.
  Random users can't add the bot to a private group and unlock it —
  they'd need an operator in the room first.
- Removing the operator's admin status (or removing them from the
  group) makes the room ineligible going forward; the *existing*
  marker still validates until the slide code rotates, but we
  re-check eligibility at /auth time only. Re-checking on every
  message is too chatty (one `getChatAdministrators` call per turn)
  and would prevent authed groups from working when the operator is
  briefly off-list.

### `/auth <code>` in groups

When a member runs `/auth <code>` in a group:

1. Fetch `getChatAdministrators(chat_id)`. If no admin's `user.id`
   appears in `admin_user_ids`, reply `This group isn't authorized
   for the assistant.` and return. (Logged at info.)
2. Otherwise treat the request exactly like a DM /auth: validate
   `code == read_secret(workspace).code`, write the marker for the
   group's `MessageAddress`, reply `✅ group authenticated`. Failure
   path uses the same rate-limited generic message as DMs.

Rotating the slide code invalidates every group's marker on the
next inbound message, same as DMs — no group-specific revocation
machinery needed.

The slide code does appear in the group transcript when typed —
acceptable for our classroom use case (the code is already on a
slide visible to the room). The bot deletes its own auth-failure
replies after 30 s; success replies stay.

### Pre-auth gate behavior in groups

When a group is unauthenticated, the channel:

- Lets `/auth` and `/start` through (existing `allow_unauth=True`).
- Drops every other inbound message silently — no
  "please authenticate" reply, since the bot would be spamming a
  room of N people for one unauth user. Logged at debug only.

The reduced `/start` welcome (see *Commands* below) tells the room
how to authenticate.

## Commands in groups

| Command | DM behavior | Group behavior |
|---|---|---|
| `/start` | full two-stage flow (see `STARTFLOW.md`) | reduced welcome (below); no example keyboard |
| `/auth <code>` | per-user auth, unchanged | derives group stamp (above) |
| `/personality [name]` | per-user overlay | **admin-only**, sets shared overlay |
| `/clear` | per-user session reset | **admin-only**, resets shared session |
| `/forgetme` | per-user wipe (current) | **admin-only**, wipes shared group storage and stamp |
| `/help` | unchanged | unchanged but examples reference `@bot` mentions |

`/forgetme` is overloaded by chat type: in a DM it keeps its current
per-user semantics; in a group the same command, run by an admin,
emits a `SessionControlEvent(action="forget")` for the group address.
The agent loop's existing `forget` handler already wipes
`storage_root(workspace, addr)` recursively — we just need the gate
to additionally clear `workspace/groups/<chat_hash8>/auth.json` so
the group falls back to unauthenticated.

### Admin gate

Three commands above require admin status. Implement once as a
helper:

```python
async def _is_group_admin(channel, chat_id, user_id) -> bool:
    member = await channel._app.bot.get_chat_member(chat_id, user_id)
    return member.status in ("creator", "administrator")
```

Anonymous-admin posts (where `update.effective_user.id ==
1087968824`) are treated as admin: if the message arrived as
"GroupAnonymousBot", we trust the group's anonymous-admin toggle and
proceed. The action is logged with that fact noted, since we can't
attribute it to a specific human.

Non-admins who run an admin-only command get a single short reply:
`Admins only.` deleted after 10 s.

### Reduced `/start` welcome (group)

```
I'm the AI-in-Business class assistant.

In this group:
• Mention me (@<bot_username>) or reply to one of my messages to talk
  to me. I ignore everything else.
• An admin must authenticate the room first: /auth <code> using the
  code on the slide.
• Admins can /personality, /clear, or /forgetme this room.
```

No example keyboard (would spam everyone), no persona pitch (admins
discover it via the command menu).

## Attention and rate limiting

The existing `summon_group` attention policy in
`teachclaw/channels/attention.py` already does the right thing:
queue group messages quietly; on `mention` or `reply` summon, replay
the contiguous recent history before the summon as context. Set the
Telegram channel's policy to `summon_group` when `is_group` is
True — we read this from `metadata["is_group"]` on the inbound
message before applying.

**Rate limiting needs a fix.** `UserState.rate_window` is keyed by
`chat_id`, so in a group all members share one window — one chatty
user starves the room. Switch group rate-limit windows to be keyed
by `(chat_id, sender_id)` while keeping DM rate-limit keyed by
`chat_id` (which is equivalent to per-user there). The "take a
breath" warning still posts in-channel but addresses the offending
user by name.

## Reactions in groups

`❤` (citations) and `🔥` (tool trace) reactions on a bot reply work
in groups, with two adjustments:

1. **Anyone in the group can react** — we don't gate reactions to
   the original asker. The first reaction wins per (message_id,
   reaction_emoji) pair; subsequent reactions are no-ops. Track the
   served set in `UserState` (or a per-message ephemeral cache),
   keyed `(chat_id, message_id, emoji)`.
2. **Bot replies in-thread** to the reacted message, same as DMs.
   The trace/citation post is therefore visible to all members,
   which we accept as the cost of public reactions.

If the bot's reply was edited (e.g., the welcome dropping its
keyboard), reactions still resolve against the original
`message_id`.

## State on disk

No new files. Group sessions, storage, and auth markers all use the
existing per-address layout under
`storage_layout.storage_root(workspace, addr)`; a group's `addr` is
just `MessageAddress("telegram", group_chat_id)`. The auth marker at
`storage/<channel>/<chat_hash8>/auth.json` works for groups as-is.

## Compaction and cost

A shared group session fills the context window faster than a 1:1
session — N speakers, one transcript. The proactive compaction in
`AgentLoop._maybe_compact_proactive` already triggers on token
estimate and is unchanged here, but the practical implication is
that group sessions will compact more often, which is fine.

## Open questions / out of scope

- **Forum-style supergroups (topics).** Telegram supergroups can
  have multiple topics within a single chat_id. We treat the whole
  group as one address for now; revisit if a class section uses
  topics.
- **Group profile.** `personalities.read_personality` and
  `storage.read_profile` will return the group's shared profile.
  We're not adding a per-user profile-overlay-inside-group concept;
  if a member wants their own profile, they DM the bot.
- **DM ↔ group bridging.** A user authed in DM does not get auto-
  authed in a group, and vice versa. Each address is its own gate.
- **`/leavegroup` / explicit removal.** Out of scope; admins remove
  the bot through Telegram's UI, the `my_chat_member` handler
  cleans up state.
