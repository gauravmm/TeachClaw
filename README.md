# <img src="teachclaw.png" height="36" alt="teachclaw" /> TeachClaw

A teaching assistant your students can chat with on Telegram. It draws
on your slide deck, the readings you assign, the question bank you've
written, and (soon) whatever's on screen at that moment — and cites
the source for every claim it makes.

Think of it as the patient TA who does the office hours you can't:
each student gets their own private thread, asks the dumb question
they were too embarrassed to raise in class, and gets back an answer
grounded in *your* materials, not a guess from the open internet.

## What it's good for

- **Pre-class warm-up.** Students arrive having already poked at the
  reading; the bot answers definitions, framework questions, and
  "wait, what does that acronym mean" so class time is for harder
  stuff.
- **Live Q&A backchannel.** During the lecture, students who don't
  want to raise their hand drop questions into the bot. The bot can
  see your slides and answers in context; you see the volume and
  themes during a break.
- **After-class deepening.** "Walk me through the two-by-two on
  slide 14 again" or "give me the McKinsey-flavored read of the
  case." The bot can switch personas (Skeptical CFO, VC Partner,
  McKinsey Analyst, Professor) so the same material can be probed
  from different angles.
- **Defensible answers.** Every substantive claim links to the
  source — your slide, the assigned PDF, the YouTube talk at the
  exact timestamp. If the bot can't find a source, students learn to
  push back.

## Cost

Designed to run on inexpensive open-weight models — Google's
[Gemma 4 26B A4B](https://openrouter.ai/google/gemma-4-26b-a4b-it)
<sup>$0.06/$0.33</sup>, [Gemma 4 31B](https://openrouter.ai/google/gemma-4-26b-a4b-it)
<sup>$0.13/$0.38</sup>, or [Qwen 3.5 Flash](https://openrouter.ai/qwen/qwen3.5-flash-02-23)
<sup>$0.07/$0.26</sup>. Forty students should cost under $2/hr at API
rates. Aggressive prompt caching keeps it there.

Sample knowledgebases are on Github:
[AI in Business](https://github.com/gauravmm/knowledgebase-businessai),
[Agentic AI](https://github.com/gauravmm/knowledgebase-agenticai).

This is a branch of [BenchClaw](https://github.com/gauravmm/benchclaw/),
which is itself a fork of [nanobot](https://github.com/HKUDS/nanobot),
though very little of the original code remains.

## Running

```bash
uv run teachclaw
```

Config file: `config/config.yaml`, created automatically on first run.
By default it's tuned for `google/gemma-4-e4b-it`; small models can
have quirks.

## Connecting a knowledgebase

The course library lives in a separate repo and is exposed to the bot
as a [Model Context Protocol](https://modelcontextprotocol.io) (MCP)
server. The bot can call into it the same way it calls any other
tool, but the corpus is curated by *you* — slides, readings, videos,
question bank — and the bot is restricted to it. No open-web browsing
unless you wire that in deliberately.

Wire it up by pointing `mcp_servers` at the knowledgebase's launch
command:

```yaml
mcp_servers:
  - name: kb
    transport: stdio
    command: sh
    args:
      - "-c"
      - "cd /path/to/lecture-knowledge; uv run knowledge-mcp"
```

Or, if you'd rather run the knowledgebase as a long-lived service:

```yaml
mcp_servers:
  - name: kb
    transport: http
    url: http://127.0.0.1:8765/mcp
```

The reference knowledgebase
([lecture-knowledge](https://github.com/gauravmm/knowledgebase-agenticai))
includes:

- The lecturer's slide decks (workshop backbone).
- Macro AI context — AI Index, Epoch notable models, OWID AI page —
  for benchmarks and capability claims.
- Vendor pricing for model comparisons.
- AI regulation for governance and policy questions.
- Selected consulting reports, essays, and canonical YouTube talks.
- Memes for the light-touch teaching layer.

To swap in your own corpus, fork the knowledgebase repo, replace the
fetch/process manifests with your sources, and re-run the build. The
bot doesn't care what's in there as long as the MCP surface stays the
same.

## Citations

Every substantive answer links back to the source it came from. The
bot emits `<citation id="…">` markers around any claim it sourced
from the knowledgebase; the channel layer rewrites those into
clickable references the student can tap.

A citation looks like:

- `[NVDA 10-K FY26 / Item 7]` — the MD&A section of that filing
- `[McKinsey State of AI 2025, p.14]` — page 14 of the PDF
- `[Karpathy "Intro to LLMs", 12:45]` — the video at 12:45
- `[Sequoia: AI Ascent 2026 Keynote]` — the linked talk

Tapping the reference opens the original document at the right page,
section, or timestamp. Sourcing is enforced server-side: if the bot
makes a strong claim without a citation, the channel pushes back and
asks it to cite or retract. Students who want to dig in can react to
any reply with ❤ to see the full source list, or with 🔍 to see the
tool-call trace that produced the answer.

## Pending features

- **Screen scraping.** Capturing the lecturer's screen (the current
  slide, a live spreadsheet, a code editor) so the bot can answer
  "what's on screen right now?" questions in real time.
- **Interactive Q&A.** Bot-driven mini-quizzes — the bot picks a
  concept from the question bank, asks the student, and walks them
  through their answer with hints and follow-ups instead of just
  grading.
