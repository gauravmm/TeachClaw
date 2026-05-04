# /start flow — first-touch onboarding for the class assistant

Today `/start` shoves the auth ask into the welcome message and lists
three example questions in plain text. Most students don't know what
the bot is good at, can't tell that it draws diagrams, can't tell that
it cites sources, and don't know there are personalities they can
switch between. This spec proposes a richer first-touch flow that
demonstrates the surface before asking for the auth code.

## Current behavior

In `benchclaw/channels/telegrm.py` (`_cmd_start`):

```
Welcome to the AI-in-Business class assistant.

Try one of these to get started:
• What is a value chain, with an example from healthcare?
• Map AI use cases to a 2x2 of effort vs. business impact.
• Compare build vs. buy for a recommendation engine.

Authenticate first: send /auth <code> using the code on the slide.
```

`_gate(allow_unauth=True)` lets the message through. The user sees the
prompt, then has to type `/auth XYZ` themselves before any of the
example questions actually do anything.

## Problems

1. **Auth is the first verb.** The welcome paragraph buries
   "authenticate" at the bottom but it's the only command that does
   anything before the user is gated. The examples are a tease, not a
   path.
2. **No demo of what the bot can do.** The bot renders Mermaid
   diagrams (see `_send_text_with_mermaid`) and surfaces source
   citations on a ❤ reaction (see `_reaction_sources` and the
   `benchclaw/citations` package). Both are invisible from the welcome
   screen until the user happens to ask the right question.
3. **Personalities are undiscoverable.** `/personality` exists in
   the command menu, but the welcome doesn't mention it; users who
   never read the menu never learn there's a CFO or McKinsey-analyst
   voice.
4. **Re-running `/start` after auth is a dead end.** Users who type
   `/start` again to remember what they can do see the auth ask again.

## Proposed flow

Two stages, branching on whether the user is already authenticated.

### Stage 1 — pre-auth welcome (chat is fresh, /start was sent)

A single message that tells the user (a) what the bot is, (b) what
makes it interesting, (c) how to authenticate. Auth is the ask, but
the demo is what motivates the ask.

```
Welcome — I'm the AI-in-Business class assistant.

I can:
• Answer questions about the lecture material with citations you
  can audit (react ❤ to any reply to see the sources).
• Draw diagrams when they help — value chains, 2x2s, flowcharts.
• Adopt different personas (Skeptical CFO, VC Partner, McKinsey
  Analyst, Professor) — try /personality.

To start, send /auth <code> using the code on the slide.
```

No example prompts here — they're for after auth, where they actually
work. This message stays short on purpose: the only action available
is `/auth`, so anything beyond explaining-what-this-is is noise.

### Stage 2 — post-auth welcome (user is authenticated when /start fires)

The prompt-bait stage. Now the example questions do something, so we
list ones that exercise the full surface — citations, diagrams, and
the persona overlay — in one shot.

```
You're in. Three things to try:

• "Can you explain the value chain of AI direct-to-consumer
  marketing?"
   → walks the value chain end-to-end and draws the Mermaid
     diagram; cites the lecture chunks it pulls from.

• "Map AI use cases for a regional bank to a 2x2 of effort vs.
  impact."
   → renders the 2x2 as a Mermaid diagram, with citations on the
     classification calls.

• "Compare build vs. buy for a recommendation engine, as a
  skeptical CFO."
   → demonstrates the persona overlay. Try /personality to make
     it stick across the whole session.

React ❤ to any reply to see the source citations; react 🔥 to see
which tools I called for that reply.
```

The first example is the user's nominated demo question; it's the
strongest because it triggers all three surfaces (text + diagram +
citation) in one turn. The other two cover the remaining axes.

### Branching logic

`_cmd_start` checks auth:

```python
async def _cmd_start(self, update, _ctx):
    if not await self._gate(update, allow_unauth=True):
        return
    msg = update.effective_message
    if not msg:
        return
    await self._refresh_command_menu()
    addr = self._addr(update.effective_chat.id)
    if auth_module.is_authenticated(self.workspace, addr):
        await msg.reply_text(_POST_AUTH_WELCOME)
    else:
        await msg.reply_text(_PRE_AUTH_WELCOME)
```

`/auth` should also surface the post-auth welcome on success, so a
user who comes in cold (no `/start`) and just sends the code lands
on the same demo prompts:

```python
# in _cmd_auth, after the success branch:
await msg.reply_text("Authenticated.\n\n" + _POST_AUTH_WELCOME)
```

Drop the standalone "Authenticated. Ask me anything." line; the
post-auth welcome is the better cue.

## Why these examples?

The user nominated "Can you explain the value chain of AI direct-to-
consumer marketing?" because it's a real lecture question that
naturally produces all three surfaces:

- **Text answer** — the model walks each stage of the value chain.
- **Mermaid diagram** — value chains render cleanly as left-to-right
  flowcharts; the McKinsey-analyst persona overlay even nudges the
  model toward emitting one (see `personalities.py`).
- **Citations** — the lecture corpus has DTC marketing content, so
  the model has chunks to cite.

The 2x2 example is paired because it's the *other* canonical lecture
diagram type, and the build-vs-buy example is paired with a persona
hint (`as a skeptical CFO`) so the user notices personas exist before
hunting for `/personality` in the menu.

## Open questions

TODO: I want an Inline keyboard for the examples, including an option to dismiss.

- **Inline keyboard for the examples?** Telegram supports inline
  keyboards with callback buttons (we already use them for
  `/personality`). Tapping a button could populate the chat input
  with the example prompt, removing the copy-paste friction. Trade-
  off: copy-paste is universal; callback-data prompts only work on
  the official Telegram clients. I'd start with plain text and add
  buttons if onboarding completion is low.
- **Should `/help` mirror the post-auth welcome?** Today `/help` is
  a one-liner about which commands exist. Once the post-auth welcome
  carries the example prompts, `/help` could either (a) point at
  `/start` ("send /start for examples"), (b) duplicate the example
  block, or (c) stay terse and command-focused. (a) keeps a single
  source of truth. TODO: (a) is fine, you can also mention /clear and /forgetme
- **Discoverability hint after first citation.** The
  `seen_first_citation` flag in `_UserState` already sends a one-shot
  "(react ❤ to any reply for sources)" the first time a reply
  contains a citation. With the post-auth welcome already mentioning
  the ❤ reaction, this nudge is now redundant on the first reply but
  still useful if the welcome was skipped (cold `/auth`). Keep it
  for now; revisit if it's noisy. TODO: Keep the nudge.
- **Personality preview in the welcome?** A nicer demo would show
  the *same* prompt answered in two voices. Out of scope for the
  welcome message, but a `/demo` command that takes a fixed prompt
  through two personas would be a fun follow-up. TODO: drop this.
