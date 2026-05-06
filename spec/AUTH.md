# Auth (shared-secret session gate)

## Goal and threat model

Prevent random Telegram users from finding and using the lecture bot.
Allow the prof to rotate the secret to revoke the whole cohort at once
(end of class, leaked code, new cohort).

This is **enrollment auth**, not identity auth. The threat model is
"a stranger discovers the bot's username and tries to use it." A
determined attacker who shoulder-surfs the slide is in — that is the
expected behaviour.

## Mechanism

A single shared secret, displayed on a slide, that students type into
the bot once per session. The current secret is stored on disk; per-user
auth state stores the secret value the user authenticated against, not a
version number. Mismatch on the next request bounces the user back to
the auth prompt.

## Secret format

6 characters from the 32-char alphabet `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`
(no `0/O`, no `1/l/I`). Example: `K7P3WQ`.

- ~30 bits of entropy. With the rate limit below, brute force is not
  realistic in any lecture-relevant timeframe.
- Short enough to fit on a slide in 200pt type.
- Confusable characters removed so a student typing under pressure does
  not retry on a misread `0` vs `O`.

`/setsecret` with no argument generates a fresh random code in this
alphabet and prints it back to the prof. With an argument, the prof's
chosen string is used as-is.

## State

### Current secret

`storage/_admin/secret.json`:

```json
{
  "code": "K7P3WQ",
  "set_at": "2026-05-03T14:00:00Z"
}
```

`storage/_admin/` is **not** in any user's tool sandbox — neither the
model nor any user-facing tool can read or write it. Only the bot
service process and the prof (via admin commands) touch this file.

### Per-user auth marker

`storage/telegram/<user_id>/auth.json`:

```json
{
  "code": "K7P3WQ",
  "authenticated_at": "2026-05-03T14:03:11Z"
}
```

The marker stores the **actual code** the user authenticated against,
not a version number or any other indirection.

### Why the code, not a version number

The user-storage directory is writable by the model on that user's
behalf, by design (memory, scratch files, etc.). If the marker were
`{secret_version: N}`, an attacker could prompt-inject the model into
writing `auth.json` with `secret_version` set to any small integer —
the current version is guessable (start at 1 and increment) and there
is no per-write knowledge required.

Storing the code closes that hole: writing a valid marker requires
knowing the current secret, which is exactly what auth is gating. The
secret itself is not reachable from inside the user's sandbox
(`storage/_admin/` is out of scope), and other users' auth.json files
are out of scope under the same path enforcement, so cross-user copy is
blocked too.

This change matters even if you trust the model — it removes a class of
attack rather than relying on the model never being convinced to write
a 5-byte JSON file.

### Rate-limit counters

In-memory only, keyed by Telegram `user.id`:

```
{user_id: (failures_in_window: int, window_started_at: ts, locked_until: ts | None)}
```

5 failed `/auth` attempts per 10-minute window per user. On the 6th,
`/auth` is locked out for that user for 1 hour. Lockout state is in
memory; surviving a bot crash is unnecessary for a one-hour lecture.

## User flow

1. Student messages the bot for the first time (any text, e.g. `/start`).
2. Bot reads the user's `auth.json`. If absent or `code` does not match
   the current `storage/_admin/secret.json`, the user is unauthenticated.
3. Bot replies (one line): `This is the class assistant. Send /auth <code> — the code is on the slide.`
4. Student sends `/auth K7P3WQ`.
5. On match: write `auth.json` with the current code and timestamp,
   reply `Authenticated. Ask me anything.`
6. On miss: increment failure counter, reply `Wrong code. (n/5 tries in this window.)`
7. While unauthenticated, the only commands that work are `/start`,
   `/help`, `/auth`. Everything else returns the same one-line nudge.

## Rotation

Admin-only `/setsecret <new_code>` (or `/setsecret` for an auto-generated
code) writes `storage/_admin/secret.json` with the new code. No
versioning, no list of revoked codes — the comparison is simply
`user_code == current_code`.

Every previously-authenticated user gets bounced on their next message:

> `Auth expired. Send /auth <code> from the new slide.`

This is the "if the code changes they can no longer get messages"
behaviour without needing per-user revocation lists.

## Admin commands

`admin` scope is gated by Telegram `user.id` in the bot config (the
prof's account ID is hard-coded or env-configured). All admin commands
require the caller to already be authenticated via the regular flow.

- `/setsecret [code]` — rotate. With no arg, generates a random code in
  the lecture alphabet and prints it back.
- `/whoauthed` — count and list of currently authenticated user IDs.
  Useful sanity check during class.

## Composition with other commands

- `/forgetme` deletes `storage/<channel>/<user_id>/` recursively, which
  removes `auth.json`. Re-auth required after.
- `/clear` clears in-memory session state only (history, last_retrieval,
  personality). Does not touch `auth.json`. The student stays
  authenticated.
- A new `/setsecret` does not retroactively edit per-user `auth.json`
  files; users stay authenticated against the old code in their marker
  until their next message, at which point the comparison fails and
  they are bounced. This is fine — there is no security-meaningful
  window because no further actions happen between rotation and the
  user's next message.

## Implementation notes

- The auth check sits in front of every other command except `/start`,
  `/help`, `/auth`. Implement as a single decorator or middleware on
  the message handler — do not scatter checks through individual
  command handlers.
- Reading `storage/_admin/secret.json` happens in the bot service
  process, not via any tool. The path is hard-coded; do not expose it
  through `read_file` or `list_files`.
- `storage/_admin/` is created with mode 0700 if it doesn't exist on
  startup. Same for `secret.json`. Tighten if running multi-user on the
  host.
- A bot restart with no `secret.json` on disk should auto-generate.
