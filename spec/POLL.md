# Polls — fixed-option questions for the class

A teacher-driven extension to TEACHERPOWERS.md. Adds two admin
commands (`/poll`, `/quiz`) that fan out a multiple-choice question
to every authenticated student and aggregate the answers back to
the instructor.

## Why native Telegram polls

Telegram's `bot.send_poll(...)` ships a real poll widget: students
tap an option, Telegram counts votes, and quiz mode supports a
designated correct answer plus an explanation that pops up after a
student answers. We don't have to invent UX or build our own
aggregation; both come for free.

The alternative — inline-keyboard message with one button per
option — is what `/personality` and the `/start` example buttons
already use. It's plug-and-play with the existing bus shape, but
the rendered UX isn't poll-shaped, and we'd write the tallying
ourselves. Not worth it when native polls exist.

## Trigger surface

Same admin gating as the rest of TEACHERPOWERS — DM only,
`is_admin(user_id)` required. Two commands, both targeting all
authed students by default (with the same `--to` / `--exclude-groups`
flags from `/announce` / `/inject` / `/ask`):

```
/poll "Which framework fits decision X best?" | 2x2 | value chain | five forces | SWOT

/quiz "Highest value capture in the AI stack?" | data | training | inference | application
   correct: 4
   explanation: Application typically captures the most value because…
```

Pipe-delimited options after the question. For longer questions or
options the teacher can compose a multi-line message in their DM
and reply-quote with `/poll` (mirroring `/announce` reply-quoting).

`/quiz` is `/poll` with `type="quiz"` plus `correct_option_id` and
`explanation` — Telegram surfaces the correct/incorrect feedback to
each student as they answer.

## Bus integration

Three small additions, all isolated to the Telegram channel + bus:

1. **Outbound poll event.** `OutboundMessage` doesn't fit a poll —
   the channel needs to call `send_poll`, not `send_message`. Add
   `OutboundPoll` (a sibling of `OutboundMessage`) with the
   question, options, type (regular | quiz), correct index, and
   explanation. `MessageBus` already routes `OutboundEvent` per
   channel, so this slots in next to `OutboundMessage` and
   `TypingEvent`.
2. **Inbound poll-answer event.** Telegram delivers answers via
   `PollAnswerHandler`. The handler wraps each into a
   `PollAnswerEvent(poll_id, addr, sender_id, option_ids)` and
   publishes it to a teacher-side aggregation queue (not into the
   per-student session — student answers are the teacher's
   business, not the agent's).
3. **Aggregator.** A small in-memory map keyed by `poll_id`,
   populated as `PollAnswerEvent`s arrive. After a timeout
   (default 5 minutes, configurable per-poll via `--timeout 2m`)
   the aggregator posts a results summary to the originating
   teacher's DM:
   ```
   Poll: Which framework fits decision X best?
   • 2x2             ████████ 12 (40%)
   • value chain     █████ 8 (27%)
   • five forces     ███ 5 (17%)
   • SWOT            ███ 5 (17%)
   29 of 47 students answered.
   ```
   `/pollresults <id>` lets the teacher pull current results
   on-demand without waiting for the timeout.

## Targeting + delivery

Same recipient resolution as `/announce`:
`auth.authenticated_addresses(workspace, channel)`. One
`OutboundPoll` per address. Group chats deliver one poll to the
group room (anyone in the room can answer); DMs deliver one
per-user.

Polls are not stored in the session. The agent neither sees the
question nor the student's answer — this is teacher-↔-students,
not student-↔-agent. If the teacher wants the agent to *know*
the class voted, they can follow up with `/inject` ("Most of the
class chose 2x2; consider that when answering follow-ups about
decision frameworks") — exactly the kind of thing `/inject` is for.

## Failure modes

- **Student has muted the bot or left.** `send_poll` raises;
  logged, doesn't break the fan-out.
- **Telegram poll size limits.** Max 10 options, ≤100 chars each,
  ≤300-char question. Validate at command-parse time and reject
  with a clear message rather than partial-send.
- **Anonymous vs. attributed.** Default `is_anonymous=False` so the
  teacher can see who answered what (the whole point of using this
  in class). Make the option explicit in the command (`--anon`)
  for cases where anonymity is desired.
- **Closing the poll.** Telegram polls can be stopped with
  `bot.stop_poll`. Aggregator's timeout fires `stop_poll` for each
  message before posting results, so the displayed counts match
  the summary.

## Out of scope for v1

- **Open-ended polls** (free-text answers). That's `/ask`.
- **Cross-poll dashboards** ("how did the class do over the last
  five quizzes?"). Add when there's demand; the data is already
  per-poll on disk if we persist `PollAnswerEvent`s.
- **Per-student remediation** ("send the explanation only to
  students who got it wrong"). Reachable later by combining the
  aggregator's per-student answer record with `/announce --to`,
  but not part of the v1 surface.
