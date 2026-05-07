# Switchable lesson modes — the workspace *is* the lesson

Today the assistant is hardwired around one course (AI in Business,
Singapore). Course-specific content lives in five places: welcome
strings in `commands.py`, personalities in `teachclaw/data/`,
`AGENTS.md` + `skills/` in `workspace/`, and the kb MCP server in
`config.yaml`. Swapping lessons means hand-editing each.

The fix is to stop treating "workspace" and "lesson" as separate
concepts. A lesson is just a workspace. To switch, point the
existing `agents.master.workspace` config field at a different
directory. Everything course-specific moves into the workspace; the
package ships zero course-flavoured defaults.

## Where the lesson currently leaks in

1. **Welcome + onboarding strings** —
   `teachclaw/channels/telegrm/commands.py:47-101` (pre/post-auth
   welcomes, group welcomes, example-prompt buttons) and
   `:222-234` (help text). Hard-codes the course name and example
   demos.
2. **Personality overlays** — `teachclaw/data/personalities.yaml`,
   loaded by `teachclaw/personalities.py:22` with an optional
   `workspace/personalities.yaml` override.
3. **System prompt** — `workspace/AGENTS.md` (and the seed at
   `workspace_default/AGENTS.md`). Hard-codes the course frame and
   citation protocol.
4. **Skills** — `workspace/skills/` (and seed `workspace_default/
   skills/`). Per-lesson but already in the right place.
5. **MCP servers** — the `kb` server in `config.yaml`. Per-
   lesson knowledge base, not per-deployment.

(1) and (2) are the only ones that don't already live in the
workspace; the rest are workspace-resident already, just lacking a
convention that says "workspace == lesson".

## The shape

A lesson is a directory laid out like a workspace. The repo ships
one or more under `lessons/`; the operator points
`agents.master.workspace` at the one they want active.

```
lessons/
  ai_in_business/
    AGENTS.md            # system-prompt skill/style file (read by the agent each turn)
    personalities.yaml   # persona overlays                    [content; replace]
    onboarding.yaml      # welcome strings, prompts, help text [content; replace]
    infra.yaml           # MCP servers, media shared_roots     [config overlay; keyed/dict-merge]
    skills/              # readable by the agent (lives in read_roots)
    common/              # readable by the agent
  software_architecture/
    AGENTS.md
    personalities.yaml
    onboarding.yaml
    skills/
```

The split is intentional. **Personalities and onboarding are content
the lesson owns wholesale** — they have no global counterpart, so
the lesson's file is loaded as-is. **Infra is a config overlay**
that merges into the global `config.yaml` (MCP servers and
shared_roots already exist there), so it needs explicit per-key
merge policies.

At runtime the workspace also grows the existing runtime tree
(`storage/`, `sessions/`, `media/`, `cron/`, `HEARTBEAT.md`); these
are `.gitignore`d so a lesson directory commits cleanly without
dragging per-user state with it.

```
config.yaml:
  agents:
    master:
      workspace: ./lessons/ai_in_business
```

Switching: change the config line, restart. Two lessons running on
the same machine = two processes pointing at two workspaces.

### `onboarding.yaml`

The strings currently in `commands.py:47-101,224-234` move here:

```yaml
pre_auth_welcome: |
  Welcome — I'm the AI-in-Business class assistant.
  …
  • Adopt different personas ({persona_pitch}) — try /personality.
  To start, send /auth <code> using the code on the slide.

group_welcome_pre_auth: |
  …
group_welcome_authed: |
  …

post_auth_welcome: |
  You're in. Three things to try (tap a button to run one):
  …
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
  `teachclaw/channels/telegrm/state.py`. Lesson author doesn't need
  to know the current emoji.
- `{persona_pitch}` — comma-joined `label`s of all non-`default`
  personas in this workspace's `personalities.yaml`. Keeps the
  welcome honest if a lesson drops or adds a persona.

`example_prompts` caps at ~4 (Telegram inline-keyboard rows are
ugly past that). The Dismiss button is added by channel code, not
the lesson.

### `personalities.yaml`

Same shape as today's
`teachclaw/data/personalities.yaml`. Move into the workspace root
and delete the packaged copy. `personalities.py:_PACKAGED` becomes
`workspace / "personalities.yaml"`; the existing
"workspace-override" branch collapses since the workspace file *is*
the canonical one.

If the file is missing the loader should fail loudly at startup —
not silently fall back to a default that masks a misconfigured
lesson.

### `AGENTS.md`

Already lives in `workspace/`. No move needed; today's
`workspace_default/AGENTS.md` becomes `lessons/ai_in_business/AGENTS.md`
and `workspace_default/` goes away.

### `skills/`, `common/`

Same — already workspace-rooted; just move from `workspace_default/`
into `lessons/<name>/`.

### `infra.yaml` (optional)

Anything that's both lesson-specific *and* already exists as a key
on the global `config.yaml` lives here. Today that's two things:
MCP servers (the kb corpus) and `media.shared_roots` (alias-keyed
paths to curated image collections used by `send_media`).

```yaml
mcp_servers:
  - name: kb
    transport: stdio
    command: sh
    args: [...]

media:
  shared_roots:
    cuteness: /home/.../cute-db/cuteness
    memes: /home/.../memes
```

The file is merged into the loaded `Config` at startup. Two merge
policies cover both keys:

| key | policy | rationale |
| --- | --- | --- |
| `mcp_servers` | **keyed-merge by `name`** | lesson can override one global server (the kb), or add new ones, without losing unrelated globals |
| `media.shared_roots` | **dict-merge** | lesson can override one alias's path, or add new aliases, without losing global ones |

In both cases lesson entries win on collision. Lessons that need
neither omit the file.

Adding a third overridable key in the future means picking one of
these two policies and adding a row to the table — no new policy
machinery. Anything else (scalar replacement, nested deep-merge) is
explicitly out of scope; if a future need wants something different
that's a sign the global config and the lesson have grown a real
schema mismatch and deserve a more deliberate redesign.

Top-level keys not in the schema are rejected at load time so a
typo (`mcp_server:` vs. `mcp_servers:`) fails loudly rather than
silently dropping the kb override.

## Securing lesson source from the agent

Lesson source files (`AGENTS.md`, `personalities.yaml`,
`onboarding.yaml`, `infra.yaml`) sit in the workspace root. Students
must not be able to coax the agent into reading them — that exposes
the prompt frame, the persona overlays, and any inline guidance
intended only for the model.

The good news: **the existing sandbox already blocks this.** In
`teachclaw/agent/tools/filesystem.py:_resolve_path`:

- The agent loop sets
  `read_roots=(workspace/skills, workspace/common)`
  (`agent/loop.py:268-270`). Workspace root itself is *not* in
  `read_roots`.
- Relative paths whose first segment isn't `skills/` or `common/`
  resolve under `storage_root`, never the workspace root
  (`filesystem.py:52-56`).
- Absolute paths are rejected outright.
- Path-escape attempts (`skills/../personalities.yaml`,
  `../AGENTS.md`) are caught by the post-resolve `_is_within` check
  against `(storage_root, *read_roots)` — `workspace/personalities.yaml`
  is in none of those roots.

So `AGENTS.md`, `personalities.yaml`, `onboarding.yaml`, and
`infra.yaml` at the workspace root are unreachable to `read_file`,
`grep`, and `glob` already.

Two reinforcements worth adding:

1. **A named deny-list on `ToolContext`.** Even though the root-
   based fence already handles it, an explicit allow- and deny-
   list signals intent and survives a future careless edit that
   adds workspace root to `read_roots`. Concretely, add:

   ```python
   class ToolContext:
       ...
       forbidden_files: tuple[Path, ...] = ()  # always reject these targets
   ```

   Populate at agent-loop startup with the resolved paths of the
   four lesson source files in `workspace_path`. `_resolve_path`
   checks against `forbidden_files` before returning. Two lines of
   defence.

2. **A test.** A regression test that for each of the four lesson
   source filenames, every plausible attack path
   (`AGENTS.md`, `./AGENTS.md`, `skills/../AGENTS.md`,
   `common/../AGENTS.md`, absolute path) raises `PermissionError`.
   Cheap to write, catches future "I added a debug root" mistakes.

What we **don't** need to fence: `skills/<*>/SKILL.md` files. Those
are deliberately readable — they're how the lesson hands the model
its tools-of-the-trade. Lesson authors must keep secrets out of
`skills/` and `common/`; a one-line note in the lesson layout
section above documents this.

## Per-lesson storage isolation — automatic

`storage_root = workspace/storage/<channel>/<chat_id>` already lives
inside the workspace (`teachclaw/storage.py:24`). Pointing the
workspace at a different lesson directory automatically gives each
lesson its own `storage/` tree — separate auth markers, profiles,
sessions, personalities. No per-lesson namespacing logic is needed
because the workspace path is the namespace.

The auth secret at `workspace/storage/_admin/secret.json` follows
the same rule: each lesson has its own, rotated independently.

## Loading + integration

A small new module — `teachclaw/lessons.py` — owns the loaders for
the workspace-resident config files:

```python
@dataclass(frozen=True)
class Onboarding:
    pre_auth_welcome: str
    group_welcome_pre_auth: str
    group_welcome_authed: str
    post_auth_welcome: str
    example_prompts: tuple[ExamplePrompt, ...]
    help_text: str

def load_onboarding(workspace: Path) -> Onboarding: ...
def load_infra_overlay(workspace: Path) -> InfraOverlay: ...   # mcp_servers + media.shared_roots
def lesson_forbidden_files(workspace: Path) -> tuple[Path, ...]: ...

def merge_infra_into_config(base: Config, overlay: InfraOverlay) -> Config:
    """Apply infra.yaml on top of the loaded global config.

    - mcp_servers: keyed-merge by `name` (overlay wins per name)
    - media.shared_roots: dict-merge (overlay wins per alias)
    """
```

Wiring (small, mechanical):

- **Onboarding strings** — `commands.py` reads `Onboarding` from
  the channel (channels already get the workspace; pass the
  `Onboarding` along the same path) and renders the welcomes with
  the placeholder substitution. The existing constants
  `_PRE_AUTH_WELCOME` / `_GROUP_WELCOME_*` / `_POST_AUTH_WELCOME` /
  `EXAMPLE_PROMPTS` / `_EXAMPLE_BUTTON_LABELS` go away.
- **Personalities** — `personalities.py:_PACKAGED` becomes
  `workspace / "personalities.yaml"`. The "packaged plus override"
  fallback collapses to "load the workspace file or fail".
- **AGENTS.md / skills** — no code change. They already live where
  the agent reads them.
- **Infra overlay** — `ConfigManager` calls
  `merge_infra_into_config(config, load_infra_overlay(workspace))`
  after the global config has loaded. Today this composes the kb
  MCP server and the curated-media `shared_roots`; future overrides
  with the same merge shape (keyed-merge by name, or alias-dict
  merge) slot in via the table in the `infra.yaml` section.
- **Forbidden files** — `AgentLoop.__init__` resolves
  `lesson_forbidden_files(workspace_path)` once and threads it
  into every `ToolContext` it builds.

Nothing in the agent loop, bus, sessions, or auth changes shape.

## Migration — minimum viable cut

One PR:

1. Rename `workspace_default/` → `lessons/ai_in_business/`.
2. Move `teachclaw/data/personalities.yaml` →
   `lessons/ai_in_business/personalities.yaml`. Delete
   `teachclaw/data/`.
3. Author `lessons/ai_in_business/onboarding.yaml` from the
   constants in `commands.py:47-101,224-234`.
4. Add `teachclaw/lessons.py` (~120 LOC); wire the four call sites
   above.
5. Update `config.yaml` to set
   `agents.master.workspace: ./lessons/ai_in_business`.
6. Update `.gitignore` to ignore the runtime children
   (`lessons/*/storage/`, `lessons/*/sessions/`,
   `lessons/*/media/`, `lessons/*/cron/`,
   `lessons/*/HEARTBEAT.md`) so a lesson directory commits clean.
7. Add `forbidden_files` enforcement + the regression test.
8. Update `CLAUDE.md` "Package Layout" to mention `lessons/` and
   the workspace-is-the-lesson convention.

After this, swapping lessons is: copy
`lessons/ai_in_business/` → `lessons/<new_name>/`, edit the
workspace files inside it (`AGENTS.md`, `personalities.yaml`,
`onboarding.yaml`, optionally `infra.yaml`), set
`workspace: ./lessons/<new_name>` in `config.yaml`, restart.

## Tradeoffs to keep in mind

- **Source and runtime state share a directory.** The workspace
  contains both the lesson source (committed) and runtime data
  (gitignored). It's the same trade Rails / Django / many web
  frameworks make — code and `tmp/` next to each other. A clear
  `.gitignore` is the only discipline needed.
- **Hand-edits during class are mixed in with lesson source.** If
  a TA tweaks `workspace/AGENTS.md` mid-session, the change sits
  alongside lesson source. The operator decides whether to commit
  or revert. The previous "lesson dir + workspace dir" proposal
  surfaced this distinction in the directory layout; this one
  surfaces it only via git status. Acceptable cost for the
  conceptual saving.
- **No bundled fallback for missing files.** A workspace without a
  `personalities.yaml` or `onboarding.yaml` won't boot. That's
  intentional — silent fallback masks a half-configured lesson.

## Resolved

- **One lesson per process.** No multi-tenancy. The active workspace
  is process-wide. Two lessons on one machine = two processes with
  two `config.yaml`s pointing at two `lessons/<name>/` directories.
  Sessions, auth, kb, and storage all assume a fixed workspace for
  the process lifetime; lifting that assumption is out of scope and
  will not be designed for now.

- **Pack validation is mandatory and complete at boot.**
  `teachclaw/lessons.py` fully parses every lesson file before
  `AgentLoop.run` is called and refuses to start the process if
  anything is missing or malformed. Same posture as a bad
  `config.yaml`. Specifically, on boot the loader:
  1. Asserts the workspace directory exists and contains
     `AGENTS.md`, `personalities.yaml`, `onboarding.yaml`, and
     `skills/`. `infra.yaml` is optional; if present it must parse.
  2. Parses `personalities.yaml` and verifies (a) each entry has
     `name`, `label`, `description`, `overlay`, all non-empty
     strings (overlay may be empty only for the `default` entry),
     (b) `name` values are unique, (c) a `default` entry exists.
  3. Parses `onboarding.yaml` and verifies all six required keys
     are present and non-empty (`pre_auth_welcome`,
     `group_welcome_pre_auth`, `group_welcome_authed`,
     `post_auth_welcome`, `example_prompts`, `help_text`); checks
     that every `{placeholder}` in the welcome strings is one of
     the three the renderer knows (`{sources_reaction}`,
     `{trace_reaction}`, `{persona_pitch}`); checks that
     `example_prompts` is 1–4 entries each with non-empty `label`
     and `prompt`.
  4. Parses `infra.yaml` (if present) and rejects unknown
     top-level keys. For `mcp_servers`, validates each entry has
     a `name` and a transport-appropriate command set. For
     `media.shared_roots`, validates the alias rules already
     enforced by `Config.media.shared_roots` (`config.py:92-105`)
     — non-empty alias, no slashes, not the reserved `media`
     name, target path exists.
  5. Resolves `forbidden_files` from the four lesson source
     filenames in the workspace and threads the tuple into
     `ToolContext`.

  Failures raise a single aggregated `LessonValidationError` listing
  every problem found, not just the first. Boot logs the error and
  exits non-zero. The validator is exercised by tests that ship a
  series of intentionally-broken workspace fixtures.

- **Admin `/lessons` listing.** Dropped. Process-wide single-lesson
  selection means there's nothing to list — the active lesson is
  the workspace path in `config.yaml`. Bring it back if and when
  multi-tenancy lands.
