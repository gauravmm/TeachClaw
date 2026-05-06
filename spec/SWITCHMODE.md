# Switchable lesson modes — pulling the "AI-in-Business" assumptions into a pack

Today the assistant is hardwired around one course (AI in Business,
Singapore) across at least four files. Swapping to a different lesson
("Software Architecture", "Negotiation 101", "Intro to ML") means
hand-editing each of those files and remembering not to commit
half-changes. This spec extracts the lesson-specific surface into a
single drop-in **lesson pack** and adds one config field that picks
which pack is active.

## Where the lesson currently leaks in

A grep for "AI in Business", "Singapore", "lecture", "CFO" turns up
five seams. Each is independent today:

1. **Welcome + onboarding strings** — `teachclaw/channels/telegrm/commands.py:47-101`
   - `_PRE_AUTH_WELCOME` names the course ("AI-in-Business class
     assistant") and the personas it pitches (Skeptical CFO …).
   - `_GROUP_WELCOME_AUTHED` / `_GROUP_WELCOME_PRE_AUTH` repeat the
     course name.
   - `_POST_AUTH_WELCOME` describes three demos ("value chain", "2x2",
     "build vs. buy") tightly bound to the course.
   - `EXAMPLE_PROMPTS` + `_EXAMPLE_BUTTON_LABELS` — the tap-to-run
     prompts STARTFLOW.md introduced.
2. **Help text** — `teachclaw/channels/telegrm/commands.py:222-234`,
   names the lecture.
3. **Personality overlays** — `teachclaw/data/personalities.yaml`,
   loaded by `teachclaw/personalities.py` with a workspace-root
   override (`workspace/personalities.yaml`). The five personas
   (CFO, VC, McKinsey, Professor, default) are business-flavoured.
4. **System prompt / AGENTS.md** — `workspace/AGENTS.md` and
   `workspace_default/AGENTS.md`. Hard-codes "AI-in-Business
   lecture", "Singapore, lat 1.3667, lon 103.8", the kb-citation
   protocol, the Mermaid-diagram bias.
5. **Skills + KB binding** — `workspace/skills/` is per-lesson; the
   `kb` MCP server in `config/config.yaml` points at a specific
   knowledge-base service. Skills already vary per workspace; the kb
   doesn't.

`personalities.py` is the only one with a built-in override hook
(`workspace/personalities.yaml`). The other four are either source
edits or workspace-file edits with no concept of swapping.

## Proposed shape — a "lesson pack"

A lesson pack is a directory that owns *every* lesson-specific
artifact. One directory per lesson; switching lessons is one config
change.

```
lessons/
  ai_in_business/
    meta.yaml           # name, label, description
    onboarding.yaml     # welcome strings, example prompts, help text
    personalities.yaml  # same shape as today's
    AGENTS.md           # the system-prompt skill/style file
    skills/             # workspace skills for this lesson
    kb.yaml             # optional: MCP servers to merge in for this lesson
  software_architecture/
    meta.yaml
    onboarding.yaml
    personalities.yaml
    AGENTS.md
    skills/
```

Everything outside `lessons/<name>/` stays lesson-neutral: the bus,
agent loop, channel plumbing, auth, citations, command dispatch, the
post-auth keyboard wiring, etc.

### `meta.yaml`

```yaml
name: ai_in_business
label: AI in Business
description: MBA-grade class assistant for a lecture series on AI strategy.
```

`name` is the slug used by config; `label` is what shows in any UI;
`description` is one line for an admin-facing `/lessons` listing if
we ever add it.

### `onboarding.yaml`

Single source of truth for what STARTFLOW.md describes today. All
strings that mention the course move here:

```yaml
pre_auth_welcome: |
  Welcome — I'm the AI-in-Business class assistant.

  I can:
  • Answer questions about the lecture material with citations …
  • Draw diagrams when they help — value chains, 2x2s, flowcharts.
  • Adopt different personas ({persona_pitch}) — try /personality.

  To start, send /auth <code> using the code on the slide.

group_welcome_pre_auth: |
  I'm the AI-in-Business class assistant. …
group_welcome_authed: |
  I'm the AI-in-Business class assistant. …

post_auth_welcome: |
  You're in. Three things to try (tap a button to run one):

  • Value chain demo — …
  • 2x2 framework demo — …
  • Build-vs-buy as Skeptical CFO — …

  React {sources_reaction} to any reply to see the source citations;
  react {trace_reaction} to see which tools I called for that reply.

example_prompts:
  - label: Value chain demo →
    prompt: Can you explain the value chain of AI direct-to-consumer marketing?
  - label: 2x2 framework demo →
    prompt: Map AI use cases for a regional bank to a 2x2 of effort vs. impact.
  - label: Build vs. buy (CFO) →
    prompt: Compare build vs. buy for a recommendation engine, as a skeptical CFO.

help_text: |
  I'm a small assistant for the AI-in-Business lecture. …
```

A few placeholders are filled at render time so we don't duplicate
constants:

- `{sources_reaction}` / `{trace_reaction}` — from
  `teachclaw/channels/telegrm/state.py`. Avoids the lesson author
  having to know the current emoji.
- `{persona_pitch}` — the comma-joined `label`s of all
  non-`default` personas in this pack's `personalities.yaml`. Today's
  string ("Skeptical CFO, VC Partner, McKinsey Analyst, Professor")
  is brittle; deriving it from `personalities.yaml` keeps the welcome
  honest if a pack drops or adds a persona.

`example_prompts` has at most ~4 entries (Telegram inline-keyboard
rows are ugly past that). The Dismiss button is added by the channel
code, not the pack.

### `personalities.yaml`

Identical shape to today's
`teachclaw/data/personalities.yaml`. Move the file from
`teachclaw/data/` into `lessons/ai_in_business/`. The packaged copy
becomes the *bundled lesson*; lessons are not part of the Python
package's `data/`.

### `AGENTS.md`

Move `workspace_default/AGENTS.md` into the pack. The file the agent
actually reads (`workspace/AGENTS.md`, mounted into the system prompt
by `teachclaw/agent/context/builder.py:18,59-63`) becomes a *symlink
or copy* placed by the lesson loader at startup, so the prompt
template doesn't change.

### `skills/`

Same idea: move `workspace_default/skills/` into the pack. On startup
the loader copies (or symlinks) the lesson's skills into
`workspace/skills/` so `SkillsLoader` finds them at the existing path.

### `mcp.yaml` (optional)

Most lessons want a different corpus, but the seam is more general
than just the kb — it's any MCP server the lesson wants to add or
override. Today every MCP server is hardwired in
`config/config.yaml`. A lesson can ship a small file:

```yaml
mcp_servers:
  - name: kb
    transport: stdio
    command: sh
    args: [...]
```

When loading config, lesson MCP servers are merged into the global
list, with the lesson's entries overriding any global server of the
same `name`. Lessons with no special corpus omit this file.

## Config — one new field

Add to `Config` (`teachclaw/config.py`):

```yaml
lesson: ai_in_business
lessons_dir: ./lessons   # optional, defaults to ./lessons
```

`lesson` selects the active pack by `meta.yaml:name`. If unset, the
loader picks the only pack present, or errors if there are zero or
more than one. That keeps a fresh checkout zero-configuration while
still forcing an explicit choice once a second pack lands.

The lesson is read once at startup; switching lessons requires a
restart. We don't try to hot-swap mid-run — the kb MCP server, the
system prompt, and per-user auth all assume a fixed pack for the
process lifetime.

## Loading + integration

A small new module — `teachclaw/lessons.py` — owns pack discovery
and exposes the data the rest of the code needs. Sketch:

```python
@dataclass(frozen=True)
class LessonPack:
    name: str
    label: str
    description: str
    root: Path                       # lessons/<name>/
    onboarding: Onboarding           # parsed onboarding.yaml
    personalities_path: Path         # lessons/<name>/personalities.yaml
    agents_md_path: Path             # lessons/<name>/AGENTS.md
    skills_dir: Path                 # lessons/<name>/skills/
    mcp_servers: list[MCPServerConfig]  # may be empty (from mcp.yaml)

def load_lesson(lessons_dir: Path, name: str | None) -> LessonPack: ...
```

Wiring (small, mechanical):

- **Onboarding strings** — `commands.py` imports `LessonPack.onboarding`
  via the channel (channels already get the workspace; pass the lesson
  the same way) and renders strings with the placeholder substitution
  described above. The four constants
  `_PRE_AUTH_WELCOME` / `_GROUP_WELCOME_*` / `_POST_AUTH_WELCOME` /
  `EXAMPLE_PROMPTS` / `_EXAMPLE_BUTTON_LABELS` become attributes of
  `LessonPack.onboarding`. `cmd_help` reads `onboarding.help_text`.
- **Personalities** — `personalities.py:_PACKAGED` becomes
  `lesson.personalities_path`. The workspace-override behaviour
  (`workspace/personalities.yaml` overrides packaged) is preserved
  for ad-hoc edits; if the lesson's bundled file is enough, the
  override is just absent. `_load(workspace)` becomes
  `_load(lesson, workspace)` and the `_CACHE` keys on the pair.
- **AGENTS.md / skills** — at startup, the lesson loader stages the
  pack's `AGENTS.md` + `skills/` into the active workspace
  (`workspace/AGENTS.md`, `workspace/skills/<name>/`). `BOOTSTRAP_FILES`
  in `agent/context/builder.py:18` keeps reading
  `workspace/AGENTS.md` — no template change. Staging is idempotent;
  if the user has hand-edited `workspace/AGENTS.md` we don't clobber
  it (compare hashes, log a warning, leave the user's copy alone).
- **MCP servers** — `Config.mcp_servers` is merged with
  `lesson.mcp_servers` in `ConfigManager` after both have loaded;
  lesson entries win on `name` collision.

Nothing in the agent loop, bus, sessions, or auth needs to change. The
lesson is a *configuration* concern, not a runtime one.

## Per-lesson storage isolation

Per-user storage today lives at `workspace/storage/<channel>/<chat_id>/`
(see `teachclaw/storage.py` and the references in
`teachclaw/personalities.py:81`). Switching lessons mid-deployment
without isolation would let students from a previous course see
profile/personality state from another. Two options, in increasing
disruptiveness:

1. **Storage namespaced by lesson** — `workspace/storage/<lesson>/
   <channel>/<chat_id>/`. One-line change in `storage_root()`. Old
   data needs a one-shot migration script (move
   `workspace/storage/*` to `workspace/storage/ai_in_business/*`
   before the first run on the new layout).
2. **Auth marker scoped by lesson** — a user authed for lesson A
   doesn't auto-pass when the operator switches to lesson B. The
   auth secret already lives at `workspace/auth_secret.json`; either
   namespace that file by lesson or rotate the code on every lesson
   switch. The latter is simpler and forces a deliberate cut-over
   (the prof writes the new code on the slide anyway).

Recommendation: do (1). It's small, prevents cross-lesson leakage of
profile/personality, and keeps `/forgetme` semantics clean. Skip (2)
on the assumption that lesson swaps coincide with secret rotation.

## Migration — minimum viable cut

Do this in one PR, not five:

1. Create `lessons/ai_in_business/` and move existing files in:
   - `teachclaw/data/personalities.yaml` →
     `lessons/ai_in_business/personalities.yaml`
   - `workspace_default/AGENTS.md` →
     `lessons/ai_in_business/AGENTS.md`
   - `workspace_default/skills/` →
     `lessons/ai_in_business/skills/`
   - Author `lessons/ai_in_business/onboarding.yaml` from the
     constants currently in `commands.py:47-101,224-234`.
   - Author `lessons/ai_in_business/meta.yaml`.
2. Delete `workspace_default/` once the loader stages from the pack
   instead.
3. Add `teachclaw/lessons.py` (~150 LOC) and wire the four call sites
   listed above.
4. Add `lesson:` to `Config`, default-pick when there's one pack.
5. Namespace storage by lesson, with a one-shot migrate for the
   existing `workspace/storage/*` tree.
6. Update `CLAUDE.md` "Package Layout" to mention `lessons/` and
   the loader.

After this, swapping lessons is: copy
`lessons/ai_in_business/` → `lessons/<new_name>/`, edit the four
files inside it, set `lesson: <new_name>` in `config.yaml`, restart.

## Open questions

- **Multi-tenant in one process.** A single bot serving two lessons 
  on different chat IDs would need per-address lesson selection
  rather than process-wide. Out of scope for this spec — fixable
  later by lifting `lesson` from `Config` to a per-channel or
  per-address setting and letting `LessonPack` flow through
  `ToolContext` and `PromptBuilder`. The pack abstraction proposed
  here is the right primitive for that future.
- **Where does `/lessons` (admin command listing packs) live?** Not
  needed for v1; add when we have ≥3 packs and an operator gets
  confused.
- **Pack validation.** A malformed `onboarding.yaml` should fail
  loudly at startup, not at first `/start`. The loader should
  fully parse the pack on boot and refuse to come up if anything's
  missing or unparsable — same posture as a bad `config.yaml`.
- **Bundled vs. user packs.** Should `lessons/` ship inside the
  Python package (importable resources), or stay a top-level
  directory the operator authors next to `config/`? The latter is
  simpler and matches how `workspace/` works today; recommended.
