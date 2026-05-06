# /start flow — first-touch onboarding for the class assistant

Today `/start` shoves the auth ask into the welcome message and lists
three example questions in plain text. Most students don't know what
the bot is good at, can't tell that it draws diagrams, can't tell that
it cites sources, and don't know there are personalities they can
switch between. This spec proposes a richer first-touch flow that
demonstrates the surface before asking for the auth code.

## Current behavior

In `teachclaw/channels/telegrm/commands.py` (`cmd_start`):

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
   `teachclaw/citations` package). Both are invisible from the welcome
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
the persona overlay — in one shot. Each example is a tap-to-run inline
keyboard button; a fourth button dismisses the welcome.

Message body:

```
You're in. Three things to try (tap a button to run one):

• Value chain demo — walks the value chain of AI direct-to-
  consumer marketing end-to-end, draws the Mermaid diagram, and
  cites the lecture chunks it pulls from.
• 2x2 framework demo — renders a 2x2 of AI use cases for a
  regional bank as a Mermaid diagram, with citations on the
  classification calls.
• Build-vs-buy as Skeptical CFO — demonstrates the persona overlay
  on a recommendation-engine question. Try /personality to make a
  persona stick across the whole session.

React ❤ to any reply to see the source citations; react 🔥 to see
which tools I called for that reply.
```

Inline keyboard, one button per row:

```
[ Value chain demo →     ]
[ 2x2 framework demo →   ]
[ Build vs. buy (CFO) →  ]
[ Dismiss                ]
```

Tap behaviour:

- **Example button** (callback ``e:N``) — answer the callback to
  clear the loading spinner, edit the welcome message to drop the
  keyboard (so the row can't be tapped a second time), then run the
  prompt through ``_handle_message`` exactly as if the user had
  typed it. The agent loop produces the reply.
- **Dismiss button** (callback ``d:``) — delete the welcome message
  outright.

The first example is the user's nominated demo question; it's the
strongest because it triggers all three surfaces (text + diagram +
citation) in one turn. The other two cover the remaining axes.

### Branching logic

`cmd_start` checks auth:

```python
async def cmd_start(channel, update, _ctx):
    if not await gate(channel, update, allow_unauth=True):
        return
    msg = update.effective_message
    if not msg:
        return
    await refresh_command_menu(channel)
    addr = channel.addr(update.effective_chat.id)
    if auth_module.is_authenticated(channel.workspace, addr):
        await msg.reply_text(_POST_AUTH_WELCOME, reply_markup=_post_auth_keyboard())
    else:
        await msg.reply_text(_PRE_AUTH_WELCOME)
```

`/auth` should also surface the post-auth welcome on success, so a
user who comes in cold (no `/start`) and just sends the code lands
on the same demo prompts:

```python
# in cmd_auth, after the success branch:
await msg.reply_text(
    "Authenticated.\n\n" + _POST_AUTH_WELCOME,
    reply_markup=_post_auth_keyboard(),
)
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

## Resolved during review

- **Inline keyboard for the examples** — yes, with a Dismiss row.
  Telegram callback buttons can't pre-fill the chat input, so tapping
  an example *runs* the prompt rather than copy-pasting it: the bot
  edits the welcome to drop the keyboard, then submits the prompt via
  ``_handle_message``. Folded into Stage 2 above.
- **`/help` after this lands** — point at `/start` for the example
  list (single source of truth), and mention `/clear` and `/forgetme`
  alongside the existing command summary. Folded into the
  implementation below.
- **Discoverability hint after first citation** — keep the existing
  ``seen_first_citation`` one-shot. Even with the post-auth welcome
  mentioning the ❤ reaction, the nudge still covers the cold-`/auth`
  path where the welcome was skipped.
- **Personality preview / `/demo` command** — dropped.
