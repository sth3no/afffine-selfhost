# Second Brain README

> Live structure reference for both humans and AI agents. This file is auto-updated by the weekly folder-organizer routine — it always reflects the current shape of the workspace, not an aspirational plan.

This workspace is a living second brain. It is used as a personal knowledge base, life operating system, source library, project space, and AI-accessible memory structure.

The goal is not to perfectly classify everything immediately. The goal is to place information into the best current location, keep the structure clean, and evolve folders only when real semantic pressure appears.

This file lives in two places:

- **AFFiNE workspace** — title `Second Brain README`. Source of truth for both humans (open it to navigate) and AI agents (read it via `find_doc_by_title`).
- **Git repo** — `workspace-readme.md` in `afffine-selfhost`. Tracked across versions; the organizer routine commits structural changes here too.

---

# Core principle

Every document should answer one primary question:

> What role does this information play?

Use the folder structure based on **role**, not just topic. The same topic can exist in multiple places if the role is different.

### Example — nutrition

- `Knowledge/Basics/Biology` — how nutrition works biologically
- `Systems/Protocols` — my nutrition protocol
- `Life/Health` — my actual health, nutrition logs, measurements, problems
- `Sources/Articles` — external article about nutrition
- `Data` — how to query nutrition-related data

> ⚠️ Do not merge these roles. The role is the discriminator, not the topic.

---

# Top-level structure

8 top-level folders. Each has a clear role, a small set of subfolders, and a recommended write style. Don't add new top-level folders unless 2+ existing docs clearly don't fit anywhere.

## Operations

Administrative and maintenance reality. Things that must be managed, renewed, tracked, organized, paid, configured, or maintained.

**Examples:** accounts, official documents, admin notes, maintenance tasks, planning, practical task lists.

**Subfolders:** Maintanace, Accounts, Documents, Tasks, Planing, Admin

**Write style:** practical, checklist-oriented, action-focused; clear next steps; dates and statuses when useful.

```md
# Title

## Status
Active / Waiting / Done / Archived

## Context

## Tasks
- [ ] ...

## Notes

## Related
```

## Life

Real-world living. Personal experience, body, home, money, relationships — the lived layer, not the theoretical or aspirational one.

**Examples:** health logs, training notes, sleep, mood, measurements; household, repairs, shopping lists; personal finance state — budgets, accounts overview, decisions.

**Subfolders:** Home, Finance, Health

**Write style:** first-person, concrete, observational; log entries with dates; current state, not theory.

```md
# Title

## Current state

## Recent entries
- 2026-04-28 — ...
- 2026-04-21 — ...

## Open questions

## Related protocols / sources
```

## Systems

How I do things. Reusable methods: workflows, automations, agents, protocols. The instruction-set layer of life and work.

**Examples:** Claude/Codex agent prompts, MCP setups, scheduled routines; n8n / Zapier flows, deploy pipelines, backup procedures; personal protocols (nutrition, training, sleep, focus, money, decisions).

**Subfolders:** Workflows, Automations, Agents, Protocols

**Write style:** imperative, reusable, version-aware; describe inputs, steps, outputs; note when last verified.

```md
# Title

## Purpose
What this system achieves, in one line.

## Inputs
- ...

## Steps
1. ...
2. ...

## Outputs
- ...

## Failure modes
- ...

## Last verified
YYYY-MM-DD
```

## Creation

Active making. Things being built, designed, written, explored. Anything I'm currently producing or iterating on.

**Examples:** active projects, prototypes, drafts, experiments; concept sketches, raw ideas waiting to be developed; content (blog posts, talks, papers, threads).

**Subfolders:** Concepts, Content, Projects, Experiments, Ideas

**Write style:** exploratory, sketching, mid-flight; always include a status line; archive when dormant > 3 months.

```md
# Title

## Status
Idea / Active / Paused / Shipped / Archived

## Goal
One sentence. What done looks like.

## Approach

## Open questions

## Decisions log
- 2026-04-28 — ...

## Related
```

## Knowledge

Universal, reference-grade understanding. Not personal experience, not active work — the timeless layer. What I want to know about the world.

**Examples:** concept summaries, mental models, principles; field overviews and how-X-works explanations; frameworks for thinking about a topic.

**Subfolders — three layers:**

- **Basics** — foundational fields: Biology, Chemistry, Physics, Computation, Systems, Language, Information, Logic, Mathematics
- **Domains** — applied human knowledge: History, Art, Practical skills, Communication, Economics, Technology, Society, Mind, Culture
- **Meta** — knowledge about knowledge: Structure, Epistemology, Ontology, Models, Axioms, Principles

**Write style:** third-person, encyclopedic, durable; cite sources; distinguish facts from interpretations; prefer atomic notes (one concept per doc) over mega-pages.

```md
# Title

## TL;DR
One paragraph definition.

## Core idea

## Mechanism / how it works

## Examples

## Caveats / common confusions

## See also
- Related concepts

## Sources
```

## Data

Where data lives and how to query it. Schemas, dataset references, query patterns, data dictionaries. Not the data itself — the map to it.

**Examples:** database schemas, table descriptions, column meanings; SQL snippets / canned queries with their purpose; API endpoints reference, dataset locations.

**Subfolders:** *(currently flat — add subfolders only when 5+ docs accumulate per source)*

**Write style:** structured, table-like, examples-first; every query / endpoint paired with what it answers.

````md
# Title

## What this is
One-line description of the data source.

## Access
Connection, auth, where it lives.

## Schema
| column | type | notes |
| ------ | ---- | ----- |

## Common queries
```sql
SELECT ...
```

## Gotchas
````

## Sources

External inputs I want to remember and reference. Captured-not-yet-distilled material. Things made by other people.

**Examples:** research papers, articles, books, podcasts, talks; websites, social-media saves (Youtube, Instagram, X), conversations.

**Subfolders:** Research papers, Websites, Social media (Youtube / Instagram / X), Conversations, Docs, Podcasts, Books, Articles

**Write style:** capture format — not finalized prose; always include the original source link / citation; distill into Knowledge or Systems when the idea is digested.

```md
# Title

## Source
URL / author / date / venue

## Why I saved this

## Key claims
- ...

## Highlights / quotes

## My take

## Promotes to
- Knowledge/... (when distilled)
```

## Archive

Cold storage. Things that are no longer active but worth keeping. Move things here instead of deleting — the archive is searchable forever.

**Examples:** completed projects, abandoned experiments; old protocols I no longer follow, superseded notes; former interests / research threads I've moved on from.

**Subfolders:** *(flat — add subfolders by year only if Archive grows past ~50 docs)*

**Write style:** leave the doc as-is at archival time; add a short "Why archived" note at the top.

```md
# Title

## Why archived
One line: completed / superseded / abandoned + date.

## Original content below
...
```

---

# How to use this — humans

1. **Decide the role first.** Before you create a doc, ask: what role does this play? (memory of an experience? reusable system? external source? reference knowledge?) The role chooses the folder — not the topic.
2. **Use the format template** from the section above. Don't reinvent structure for every doc — the templates exist so you (and the AI) can scan content fast.
3. **Don't fight unfiled docs.** Daily journals, scratch notes, and very fresh material can sit unfiled at root. The weekly organizer routine will file or leave them.
4. **Promote captured material.** When a `Sources/*` doc gets digested, write the distilled version into `Knowledge/*` and link back to the source. The source stays.
5. **Archive instead of delete.** Past projects and obsolete protocols go to Archive, not trash.

---

# How to use this — AI agents

> 🤖 If you are an AI agent with `affine-mcp-ext` access: **read this document FIRST** before creating, moving, or filing anything. It is the single source of truth for folder semantics.

1. Always start by calling `list_folder_tree` and `find_doc_by_title` with title `"Second Brain README"`. Read this doc before deciding any structural change.
2. Match new docs to a *role* here, not a topic. If a doc could plausibly live in two folders, prefer the role described above (Sources for captured, Knowledge for distilled, Systems for reusable, Life for personal-state, Operations for admin).
3. **Honour the format templates** when creating new docs in a folder. Use the markdown template shown for that section.
4. **Never delete docs.** `delete_doc` is forbidden for the organizer routine. Move stale docs to Archive instead.
5. **Don't move journal/daily docs.** Titles like `2026-04-28` or `Daily ...` stay unfiled at root.
6. **Update this README** when the structure actually changes (new top-level folder, renamed subfolder, deleted folder). The weekly organizer routine is responsible for keeping this file in sync — see the prompt at `folder-organizer-prompt.md` in the repo.

---

# How structure evolves

- **Empty folders are removed.** If a subfolder accumulates zero docs after a quarter, the organizer collapses it.
- **Subfolders appear when 5+ docs cluster.** The organizer doesn't pre-create empty taxonomies.
- **Top-level folders are stable.** Adding or renaming a top-level folder is a deliberate decision, not a routine action. The 8 above are intended to last.
- The changelog is at `Vault Structure Log` — the weekly organizer appends a dated entry there each run with what changed and why.

---

## Live snapshot

**Last updated:** 2026-04-28 — initial version (8 top-level folders, 67 nodes total, 30 unfiled docs).

*Top-level:* Operations · Life · Systems · Creation · Knowledge · Data · Sources · Archive

> 📦 This file is auto-maintained by the weekly folder-organizer routine. Manual edits are welcome — the organizer treats this README as *the spec* and updates the live snapshot section to reflect reality.
