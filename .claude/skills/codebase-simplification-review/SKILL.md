---
name: codebase-simplification-review
description: Review an entire repository for opportunities to simplify implementation details, APIs, abstractions, and obsolete code. Use when the user asks for simplification ideas, architectural cleanup, API cleanup, removal of unnecessary gadgets, or a broad "make the codebase simpler" audit.
---

# Codebase Simplification Review

Use this skill when the user wants a whole-codebase pass aimed at reducing complexity rather than adding features.

If the software is prerelease or being prepared for a major revision, do not preserve backwards compatibility by default. Unless the user explicitly asks otherwise, prefer the simpler end state even when it requires breaking old formats, deleting compatibility shims, or collapsing legacy pathways.

The optimization target is clean, maintainable code. Programmer time is expensive, so optimize for simplicity, interpretability, and elegance over cleverness, configurability, or preserving unnecessary historical structure.

Where possible, prefer structured objects with type annotations, validation, and parsers over raw dicts. This keeps the meaning of data separate from its serialized or presentation form.

Prefer transform-before-erasure design: do meaningful transformations while data is still in its richest typed form, then render it into serialized or provider-specific shapes at one clear boundary.

Prefer preserving meaningful type distinctions. Maintain type safety when different types encode real invariants or valid-state constraints, and only collapse them when the differences are purely decorative.

## Output

Default to a review mindset:

- Findings first, ordered by impact
- Focus on simplification opportunities, not style nits
- For each finding, include:
  - what is unnecessarily complex
  - why it exists today
  - the simpler shape to move toward
  - likely migration cost and risk
  - concrete file references

If the user asks for implementation after the review, turn the highest-value items into an execution plan or patch set.

## Workflow

1. Map the codebase shape first.
   - Use `rg --files` and a few targeted file reads to understand top-level modules, entrypoints, config, and major subsystems.
   - Look for repeated patterns, parallel pathways, and places where state or responsibilities are split awkwardly.

2. Sample representative hotspots instead of reading everything linearly.
   - Prefer entrypoints, registries, builders, context assembly, tool layers, API boundaries, state machines, serializers, and high-churn tests.
   - Follow duplication trails with `rg`, especially repeated phrases, repeated schemas, repeated conversion logic, and repeated lifecycle code.

3. Evaluate simplification at two scales.
   - Local: smaller refactors inside functions, classes, and files.
   - Structural: fewer concepts, fewer pathways, fewer special cases, smaller API surface.

4. Produce a prioritized simplification report.
   - Separate quick wins from architectural simplifications.
   - Call out deletions explicitly when code appears to be a gadget, compatibility shim, or dead extension point.

## What To Look For

### Small-Scale Simplifications

- Prefer simpler code inside functions:
  - fewer branches
  - fewer temporary variables
  - less state mutation
  - earlier returns instead of nested conditionals
  - direct data flow instead of hand-rolled plumbing
  - omit single-use variables where that makes the code easier to read

- Eliminate small stub functions when they only rename another call, forward one line, or hide no meaningful policy.

- Extract functionality into objects when it improves encapsulation:
  - state and behavior currently passed around together
  - repeated bundles of parameters
  - lifecycle logic spread across multiple call sites
  - invariants enforced informally rather than structurally

- Collapse duplicate parsing, normalization, validation, or formatting code into one path.

- Replace boolean or sentinel-heavy control flow with clearer objects, enums, or distinct code paths when that reduces branching.

- Remove one-off helpers that exist only to preserve an old layering choice.

- Prefer `assert` for internal invariants and impossible states when failure indicates a programmer error, instead of open-coded `if`/`then` guard branches.

- Prefer exception-driven error handling over returning ad hoc error objects or error strings through normal result paths when the call site is already operating in a failure-capable control flow.

- Prefer structured return types when the result shape is likely to grow:
  - objects or dataclasses over tuples with positional meaning
  - typed records over loosely shaped dicts
  - explicit result containers over stringly encoded multi-purpose payloads

- Prefer structured domain objects over loose dicts when data crosses boundaries or accumulates behavior:
  - parsers and validators at the edge
  - typed meaning in the middle
  - encoding/serialization only at the edge

- Preserve type safety when it carries real meaning:
  - keep separate types when they prevent invalid states or encode distinct invariants
  - prefer discriminated unions or specific classes over omnibus records when fields are not actually valid for every case
  - only merge types when the distinctions are cosmetic, redundant, or otherwise purely decorative

- Prefer "transform before erasure":
  - do cleanup, filtering, truncation, and policy decisions before flattening typed state into transport or debug payloads
  - keep one canonical structured representation for as long as possible
  - derive multiple output views from that representation instead of layering ad hoc post-processing on already-rendered data

### Larger Simplifications

- Simplify API design:
  - reduce argument count
  - reduce optional knobs
  - reduce overlapping entrypoints
  - reduce separate "current chat" vs "explicit destination" pathways when one model can cover both cleanly

- Generalize repeated special cases into a single mechanism when the abstraction is real.
  - Good example shape: moving several ad hoc inline behaviors into one inner-tag dispatch path.

- Look for gadgets:
  - code paths that support a feature or future that is no longer intended
  - compatibility layers nobody relies on
  - extension points with no concrete users
  - caches, planners, wrappers, or managers that add indirection without paying rent

- Consolidate parallel representations:
  - the same concept stored in multiple shapes
  - dual APIs for the same action
  - separate metadata and runtime paths that could be unified

- Narrow ownership boundaries:
  - too many layers touching the same state
  - builder plus registry plus loop plus helper all partially own one behavior

- Remove speculative abstractions:
  - plugin systems, strategy hooks, config knobs, generic wrappers, or adapters created for flexibility that the product does not actually need

- Prefer deletion over frameworking:
  - if two code paths exist but one is clearly preferred, ask whether the other should be removed rather than abstracted

- Prefer standard library or direct data structures over custom mini-frameworks when custom code adds maintenance without leverage.

- When the product is prerelease or headed for a major revision, explicitly look for places where backwards compatibility is the main reason complexity still exists.

## Review Questions

Ask these while scanning:

- Is this abstraction carrying real policy, or only indirection?
- Are there two ways to do the same thing?
- Can this special case be folded into the default path?
- Would deleting this code make the system easier to explain?
- Is this state owned in one place?
- Does this helper preserve an old design that the current product no longer needs?
- Is this API optimized for a hypothetical future instead of present usage?

## Heuristics

- Favor the design that is easiest to explain to a new maintainer.
- Favor fewer concepts over more generic concepts.
- Favor explicit dominant paths over symmetric but rarely used alternatives.
- Favor moving complexity to one well-named boundary rather than spreading it across helpers.
- Favor preserving structure until the boundary: transform typed data first, then serialize or adapt it.
- Favor maintaining type safety unless the type distinctions are purely decorative.
- Favor removing code before abstracting code.
- Favor `assert` for invariants, exceptions for failures, and structured return types for evolving interfaces.
- When backwards compatibility is not required, favor the clean break over layered migration code.

## Suggested Prompt

Use or adapt this when the user wants a broad simplification audit:

> Review the entire codebase and identify opportunities to simplify it.
> Focus on both local simplifications and structural simplifications.
>
> Local examples:
>
> - simpler code inside functions
> - removing stub functions that add no policy
> - extracting cohesive state and behavior into better-encapsulated objects
>
> Structural examples:
>
> - simplifying API surfaces
> - folding special cases into more general mechanisms
> - finding gadgets, obsolete extension points, or code for futures we no longer intend
>
> Add any other high-value simplification opportunities you see, especially around duplicated pathways, speculative abstractions, split ownership, compatibility layers, and dead code.
> Prefer recommendations that move the code toward asserts for invariants, exceptions for failures, and structured return types where interfaces are likely to grow.
> If the project is prerelease or preparing for a major revision, ignore backwards compatibility unless explicitly told otherwise, and recommend radical simplifications where warranted.
>
> Search the whole repository, prioritize findings by impact, and for each one explain:
>
> - what is too complex
> - the simpler shape
> - why it is safe or worthwhile
> - file references
> - migration risk
