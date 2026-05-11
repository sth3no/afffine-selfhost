# Content Templates — Per-Type Prompt & Render Pipeline

**Status:** Draft — design approved 2026-05-11, awaiting plan.
**Scope:** `ingest/` (Python/FastAPI). No `mcp-ext` or AFFiNE container
changes. Renderer extension stays inside ingest's orchestrator.

---

## Goal

Today the ingest service runs one fixed prompt against every captured URL —
the same `SummaryResult { title, summary_md }` shape regardless of whether
the source is a recipe, a podcast, a tutorial, a documentary, or a paper.
The output is always a 3–6 bullet summary card.

Different content types deserve different output structures:

- A **recipe** wants ingredients + numbered steps + time estimate, not bullets.
- A **tutorial** wants prereqs + numbered steps with timestamps + a code
  snippet, not a vibe summary.
- A **documentary / explainer** wants the *claim*, the *data*, and the
  *result* — connecting evidence to conclusion, not just restating.
- A **podcast** wants the topic arc, the strongest takeaways, and any
  diagrams flashed on screen.
- A **YouTube video with a clickbait title** wants the resolution
  ("they did *what?*") as the very first thing on the page.

This spec introduces **content templates** keyed by `(platform_id, topic)`,
each containing a system prompt that shapes the LLM call for captures of
that kind. Templates are stored in Postgres and editable via new API
endpoints. Unknown (platform, topic) pairs trigger **LLM-synthesized
templates** — Claude designs the template from a sample capture and saves
it for reuse.

The renderer is also extended to speak AFFiNE's full block vocabulary
(code, mermaid diagrams, embedded HTML frames, callouts, image refs to
Phase 13 keyframes, cross-doc links) so templates can request rich
structured output, not just markdown prose.

---

## Out of scope (v2 / future)

- Template-aware keyframe re-ranking inside Phase 13's `video_analysis`
  module. The current keyframe extraction stays unchanged; templates
  pick *which* of the pre-extracted keyframes to surface. v2 will let the
  template's purpose re-rank the vision pass itself.
- A/B testing two templates against the same capture.
- Per-template version history. The capture row's `template_prompt_used`
  snapshot is enough audit trail for v1.
- UI for editing templates. `curl` against the API is enough to iterate;
  a future tab in the browser extension's options page is straightforward
  once endpoints exist.
- Auto-archiving low-usage templates.
- Pre-warming synthesis (generating templates for every (platform, topic)
  pair on deploy). Synthesis happens on first encounter, lazily.

---

## Data model

### New table: `content_templates`

```sql
CREATE TABLE content_templates (
    id              TEXT PRIMARY KEY,            -- ULID
    platform_id     TEXT NOT NULL,               -- 'youtube', 'instagram', '*'
    topic           TEXT NOT NULL,               -- 'Tutorials', 'Recipes', '*'
    name            TEXT NOT NULL,               -- human label
    system_prompt   TEXT NOT NULL,               -- sent to Haiku per capture
    status          TEXT NOT NULL,               -- 'auto'|'edited'|'archived'
    generator_meta  JSONB,                       -- synthesis reasoning (nullable)
    created_by      TEXT NOT NULL,               -- 'synth'|'user'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Only one active template per (platform, topic) scope.
-- Archived rows excluded so the user can soft-delete + create a replacement.
CREATE UNIQUE INDEX content_templates_active_scope
    ON content_templates (platform_id, topic)
    WHERE status != 'archived';

-- Fallback chain queries hit (platform, topic) → (*, topic) → (platform, *) → (*, *).
CREATE INDEX content_templates_scope_lookup
    ON content_templates (platform_id, topic)
    WHERE status != 'archived';
```

**Scope semantics.** `platform_id` matches a `Platform.id` from
[topics.yaml](../../ingest/topics.yaml) or the literal `'*'` wildcard.
`topic` matches a topic folder name (case-sensitive) or `'*'`. The lookup
order is **most specific first**:

1. `(platform_id, topic)` — fully specific (e.g. `(youtube, Tutorials)`)
2. `(*, topic)` — topic everywhere (e.g. `(*, Recipes)`)
3. `(platform_id, *)` — platform default (e.g. `(youtube, *)`)
4. `(*, *)` — global default; seeded on migration with today's prompt
5. If still no match (only possible if `(*, *)` was deleted) → **synthesize**

**`status` values.**
- `auto` — created by the synthesizer. Distinguishes machine-generated
  drafts from human-tuned ones at a glance.
- `edited` — user has called `PUT /templates/{id}` and changed the prompt.
- `archived` — soft-deleted. Excluded from lookup but preserved for audit.

**`generator_meta`** (synthesis output only, `NULL` for user-created):

```json
{
  "biggest_value": "Step-by-step procedure the user can replicate.",
  "user_intent": "Bookmark for later execution; needs prereqs + steps.",
  "best_roi_format": "Numbered list with timestamps, code snippets inline.",
  "available_blocks_used": ["paragraph", "list", "code", "image"],
  "sample_capture_id": "01J...",
  "synthesizer_model": "claude-sonnet-4-6",
  "synthesized_at": "2026-05-11T14:32:00Z"
}
```

### Migration: `captures` table additions

```sql
ALTER TABLE captures
    ADD COLUMN template_id          TEXT REFERENCES content_templates(id),
    ADD COLUMN template_prompt_used TEXT,  -- system_prompt snapshot at runtime
    ADD COLUMN template_output_raw  TEXT;  -- the body_md returned (for re-render)
```

`template_prompt_used` is the snapshot at the moment the LLM call ran. If
you later edit the template, existing captures still know what they were
actually generated against. `template_output_raw` is the markdown the
LLM returned — kept so `/captures/{id}/rerender` can replay against the
same extracted content without re-fetching the source.

### Seed migration: default `(*, *)` template

The migration that creates `content_templates` also inserts one row:

```sql
INSERT INTO content_templates (id, platform_id, topic, name, system_prompt, status, created_by)
VALUES ('<ulid>', '*', '*', 'Default summarizer',
        '<the current SYSTEM_PROMPT from summarizer.py, verbatim>',
        'auto', 'synth');
```

Behavior on deploy: every existing capture (and every new one) lands on
this default template until the user creates a more specific one or
synthesis runs. Identical to today's output, just with the template
mechanics underneath.

---

## LLM call & output schema

### Replaces the current summarizer

Today: [summarizer.py:89-146](../../ingest/src/pipeline/summarizer.py)
runs a fixed Haiku call returning `SummaryResult { title, summary_md }`.

New module: `ingest/src/pipeline/templated_render.py` — a single Haiku
call per capture, parameterized by the resolved template. Returns:

```python
class TemplatedOutput(BaseModel):
    title: str          # 1-10 words, same rules as today
    lede: str | None    # See "Lede / clickbait resolver" below
    summary_md: str     # 3-6 bullet card preview, kept for search/iOS/card
    body_md: str        # Template-specific structured markdown
```

All four fields are required-by-schema; `lede` is `Optional[str]` so
templates can leave it null when the source title isn't a teaser.

The **per-template `system_prompt`** controls `body_md` shape. The other
three are template-agnostic — every doc has a card preview and a
consistent answer-the-title block when applicable.

### User-message context (every templated call)

The same per-capture user message is sent regardless of template — the
template's system prompt decides which inputs to use:

```
Captured content:
- Original title: {extracted.title or '(none)'}
- Author/channel: {extracted.author or '(unknown)'}
- Media kind: {extracted.media_kind.value}
- Published: {extracted.published_at or '(unknown)'}

Source description (from publisher — may contain sources, chapter markers,
sponsor links, related content; extract valuable references, strip noise):
{extracted.extra.description or '(none)'}

Vision-grounded summary (if Phase 13 video analysis ran):
{extracted.extra.video_summary or '(none)'}

Available keyframes (reference by index, e.g. ![caption](kf:2)):
[0] t=0:42 — {caption}
[1] t=2:34 — {caption}
... (up to settings.max_keyframes_in_doc)

Body excerpt (transcript or article, truncated to {summarizer_max_body_chars}):
{extracted.body_md[:N]}
```

The description block is a v1-explicit signal: extractors like
[ytdlp_ext](../../ingest/src/pipeline/extractors/ytdlp_ext.py) already
populate `extracted.extra["description"]` with the publisher's video
description, which routinely contains sources cited, chapter markers,
related-content links, and author notes that don't appear in the
transcript. The template prompt is told to mine it for valuable
references (sources, citations, related docs) and surface them in
`body_md` — typically as a `## Sources` section or via inline
`[[doc title]]` cross-links when the linked URL matches an existing
workspace doc.

### Lede / clickbait resolver

If the source title is a question, mystery, exaggeration, or clickbait
teaser ("THEY DID IT", "This Changes Everything", "The Truth About X"),
the template MUST populate `lede` with one direct sentence answering
who/what/why. Otherwise `lede = null`.

Renderer puts the lede as the very first content block right under the
URL embed, rendered as `affine:callout` for visual prominence:

```
[embed-youtube]
[callout: <lede>]            ← only when lede != null
## Summary
- bullet
- bullet
<body_md as block tree>
Source: <url>
```

This is a baseline rule baked into both the meta-prompt (synthesizer
always emits it in generated prompts) and the seed `(*, *)` template.

### Synthesizer meta-prompt

Runs only when the fallback chain finds nothing — typically on the first
capture of a brand-new (platform, topic) pair. Uses Sonnet 4.6 (not
Haiku) because it runs once per type and quality matters; cost is
amortized across all future captures in that bucket.

System prompt (verbatim):

```
You are designing a content template for a personal knowledge-base
ingestion pipeline. Each captured URL of a given (platform, topic) kind
will be summarized into an AFFiNE document. Your job: design the system
prompt that will run for every future capture matching this scope.

You will be given:
- The platform (e.g., youtube, instagram, arxiv)
- The topic (e.g., Tutorials, Recipes, Documentary)
- One sample capture's extracted content (title, author, description,
  transcript/body, vision summary if present, keyframes available)

Ask yourself, in this order:
1. What is the biggest value in this kind of content for the user?
2. What does the user actually want when they save one of these — what
   are they going to look at again in 6 months?
3. What's the best ROI in text form — what should `body_md` look like
   to maximize signal per scroll?
4. Which of the available AFFiNE block flavours best express that?

Available block flavours the generated prompt can request (via markdown):
- Headings h1-h6: `# heading`, `## heading`
- Paragraphs: plain text
- Bulleted/numbered/todo lists: `- item`, `1. item`, `[ ] item`
- Code blocks with language: ```python ... ```  (any language)
- Mermaid diagrams: ```mermaid\nflowchart ... ```  (renders as diagram)
- Embedded HTML "frames" (SVG charts, styled cards):
  ```embed-html\n<svg ...> ... ```
- Image refs to available keyframes: `![caption](kf:<index>)`
- Cross-doc references: `[[Doc Title]]` (resolves to embed-linked-doc)
- Callouts (highlighted blocks): `> [!callout] text`
- URL embeds: paste `[](url)`; renderer picks youtube/github/figma/loom
  or falls back to bookmark
- Dividers: `---`

Rules the generated prompt MUST always include:
- Title rule: 1-10 words, English default, Czech/Slovak preserved.
- Lede rule: if source title is a question/mystery/clickbait, populate
  `lede` with one direct answering sentence; else null.
- Summary rule: 3-6 bullets, one short line each, no intro/outro.
- Description rule: mine `extracted.extra.description` for sources,
  citations, related links, chapter markers. Surface them in `body_md`
  (typically `## Sources` section). Strip sponsor/social noise.
- Body rule: tailored to this content type (your design).
- Language rule: English by default; Czech/Slovak preserved if source is.

Return JSON:
{
  "name": str,                   // human label, e.g. "YouTube Tutorial v1"
  "system_prompt": str,          // the prompt sent to Haiku per capture
  "biggest_value": str,
  "user_intent": str,
  "best_roi_format": str,
  "available_blocks_used": [str]
}
```

User message: the sample capture's content (same shape as the per-capture
user message above).

Output: `name`, `system_prompt` saved on the new `content_templates`
row; the four reflective fields saved as `generator_meta`. The
synthesized template is then **immediately used for the triggering
capture** — no review gate.

### Cost shape

- Known (platform, topic) pair: 1 Haiku 4.5 call per capture (same as today).
- New (platform, topic) pair: 1 extra Sonnet 4.6 call to synthesize +
  1 Haiku call to render. Amortized across every future capture in that
  bucket.
- Re-render endpoint: 1 Haiku call per replayed capture. No extraction
  cost.

---

## Orchestrator integration

The orchestrator's state machine ([orchestrator.py:34](../../ingest/src/pipeline/orchestrator.py))
gets one new step between classify and file:

```
extract → classify → resolve-or-synthesize template → templated render → file
```

Pseudocode (replaces the title/summary block at
[orchestrator.py:78-92](../../ingest/src/pipeline/orchestrator.py)):

```python
# After classification result is known:
template = await templates_repo.resolve(platform_id=platform.id, topic=result.topic)
if template is None:
    template = await synthesize_template(
        platform_id=platform.id,
        topic=result.topic,
        sample_extracted=extracted,
    )
    # synthesize_template inserts with ON CONFLICT DO NOTHING; on conflict
    # it re-reads and uses the winner's row.

rendered = await templated_render(
    template=template,
    extracted=extracted,
    keyframes=(extracted.extra or {}).get("keyframes") or [],
)

await repo.save_template_run(
    capture_id=row.id,
    template_id=template.id,
    prompt_used=template.system_prompt,
    output_raw=rendered.body_md,
)

await filer._mcp.set_doc_title(row.doc_id, rendered.title)
# (existing file → folder logic stays unchanged)

await _replace_doc_body(
    filer=filer,
    doc_id=row.doc_id,
    rendered=rendered,
    keyframes=keyframes,
    url=row.url,
)
```

### Block layout (replaces `_build_body_blocks`)

```
[embed url]                                    ← unchanged URL embed
[callout: lede]                                ← if rendered.lede is not None
## Summary
- bullet                                       ← from rendered.summary_md
- ...
<rendered.body_md → parsed by markdown_render> ← all the rich block types
Source: <url>                                  ← unchanged
```

No more hardcoded `## Description` section (the template incorporates it
into `body_md`); no more hardcoded `## Keyframes` section (the template
embeds keyframes inline where they're relevant via `kf:<n>` refs, and
keyframes the template doesn't reference don't render).

### Concurrency

Two captures of a brand-new `(youtube, AI)` could race into synthesis
simultaneously. Resolved with the partial UNIQUE index on
`(platform_id, topic) WHERE status != 'archived'` + Postgres
`INSERT ... ON CONFLICT DO NOTHING`. The loser re-reads and uses the
winner's row. The duplicate synthesis call is wasted (cost: one Sonnet
call) — acceptable for what is by definition a rare event.

---

## Rich block renderer

Today's [`_markdown_to_blocks`](../../ingest/src/pipeline/orchestrator.py:378)
handles paragraphs, headings, bullets. The template-driven design needs
to also speak fenced code, mermaid, embed-html, callouts, keyframe refs,
and cross-doc refs. The hand-rolled regex parser becomes a liability
at this many flavours.

New module: `ingest/src/pipeline/markdown_render.py`. Uses `markdown-it-py`
(already a transitive dep via `markitdown`) for the parse, plus a small
AST → block-spec mapper.

### Markdown syntax → block flavour mapping

| Markdown | AFFiNE flavour | Notes |
|---|---|---|
| `# H1` … `###### H6` | `affine:paragraph` style=h1..h6 | unchanged |
| Plain paragraph | `affine:paragraph` style=text | unchanged |
| `> quote` | `affine:paragraph` style=quote | unchanged |
| `- item` | `affine:list` style=bulleted | unchanged |
| `1. item` | `affine:list` style=numbered | new — needed for recipe steps |
| `[ ] item` / `[x] item` | `affine:list` style=todo | new — checklist support |
| <code>```lang ... ```</code> | `affine:code` language=lang | new — generic code blocks |
| <code>```mermaid ... ```</code> | `affine:code` language=mermaid | AFFiNE renders as diagram |
| <code>```embed-html ... ```</code> | `affine:embed-html` html=body | sentinel lang — body becomes the html prop |
| `---` | `affine:divider` | new |
| `> [!callout] text` | `affine:callout` text=text | new — GH-flavoured callout |
| `[[Doc Title]]` | `affine:embed-linked-doc` | resolved via `find_doc_by_title` MCP at render time; falls back to inline text if not found |
| `![alt](kf:<n>)` | `affine:image` sourceId=<keyframe[n].blob_source_id> | new — keyframe refs |
| `![alt](url)` external | inline note text (skip) | external image embeds out of scope v1 |
| `[label](url)` inline | inline-op with `link` attribute | preserved from today |

### Keyframe resolution

When `body_md` contains `![caption](kf:2)`, the renderer:
1. Looks up `extracted.extra["keyframes"][2]` → gets `blob_source_id`.
2. Emits `{type: "image", sourceId: <id>, caption: "<caption text>"}`.
3. If index is out of range, drops the ref silently (logged at warn).

The `kf:` sentinel scheme keeps the template prompt simple — the LLM
doesn't need to know blob IDs, just keyframe indices listed in its
user-message context.

### Cross-doc resolution

`[[Doc Title]]` in `body_md` calls `find_doc_by_title` via the existing
MCP client at render time. If exactly one match → `embed-linked-doc` block
with that docId. If zero/multiple → emits inline text `[[Doc Title]]` as
plain paragraph text and logs at warn (no link). Avoids ambiguous link
targets.

### URL embeds in body

`[](https://github.com/foo/bar)` (empty label) → renderer picks the right
embed flavour based on host, same logic as today's `_url_embed_block`. If
the URL has a label `[label](url)`, it stays inline rich text (link)
rather than promoting to an embed.

### Phase 13 keyframes as inputs

Today keyframes auto-render in a `## Keyframes` section
([orchestrator.py:273-294](../../ingest/src/pipeline/orchestrator.py)).
Under the new design, keyframes are **passive inputs** the template
references via `![caption](kf:<n>)`. Keyframes the template doesn't
reference don't render.

The hard cap from [config.py:42](../../ingest/src/config.py)
(`max_keyframes_in_doc: int = 6`) still applies — even if the template
references more, the renderer drops anything past the cap.

The full keyframe list (up to `max_frames_per_video=12`) is still passed
into the template's user message so the LLM can pick the best subset.

---

## API surface

All endpoints gated by the existing `INGEST_API_TOKEN` bearer auth.

### Template CRUD

| Method | Path | Body / Query | Purpose |
|---|---|---|---|
| `GET` | `/templates` | `?platform=&topic=&status=` | List, filtered, with `usage_count` joined from `captures.template_id`. |
| `GET` | `/templates/{id}` | — | Full row including `system_prompt`, `generator_meta`. |
| `POST` | `/templates` | `{platform_id, topic, name, system_prompt}` | Manually create. `status='edited'`, `created_by='user'`. Returns the row. 409 on UNIQUE violation. |
| `PUT` | `/templates/{id}` | `{name?, system_prompt?, platform_id?, topic?}` | Edit. If `system_prompt` changes, flip `status` from `auto` → `edited`. 409 if scope change collides. |
| `DELETE` | `/templates/{id}` | — | Soft-archive (`status='archived'`). Existing captures keep their snapshot. |

### Template ops

| Method | Path | Body | Purpose |
|---|---|---|---|
| `GET` | `/templates/resolve` | `?platform=&topic=` | Returns the template that *would* be picked for a (p, t) pair via the fallback chain. Debugging tool — no LLM call. 404 if not even `(*, *)` exists. |
| `POST` | `/templates/synthesize` | `{platform_id, topic, sample_capture_id?}` | Manually trigger synthesis. If `sample_capture_id` omitted, picks the most recent capture for that (p, t). Returns the new template. 409 if active template already exists at that scope — caller must DELETE first. |
| `POST` | `/captures/{id}/rerender` | — | Re-runs the current resolved template against this capture's stored extracted content. Replaces blocks in the AFFiNE doc + updates `template_id`, `template_prompt_used`, `template_output_raw`. Returns the updated capture row. Errors if capture has no stored extraction (pre-template captures get a fresh extract first, gated behind a `?reextract=true` query flag). |

### OpenAPI integration

FastAPI auto-generates docs at `/docs`. New endpoints get Pydantic
request/response models in `src/models.py` (`ContentTemplate`,
`CreateTemplateRequest`, `UpdateTemplateRequest`,
`SynthesizeRequest`, `RerenderResponse`).

---

## Module structure

```
ingest/src/pipeline/
├── orchestrator.py           ← modified: insert template step, drop hardcoded body blocks
├── summarizer.py             ← deleted (replaced by templated_render.py)
├── templates.py              ← NEW: ContentTemplate model + repo + fallback resolver
├── template_synth.py         ← NEW: Sonnet meta-prompt call → ContentTemplate row
├── templated_render.py       ← NEW: Haiku call w/ template.system_prompt → TemplatedOutput
└── markdown_render.py        ← NEW: markdown_it_py → AFFiNE block specs
```

`templates.py` exposes:
- `ContentTemplate(BaseModel)` — the row shape.
- `TemplatesRepository(asyncpg pool)` with: `resolve(platform_id, topic)`,
  `get(id)`, `list(filters)`, `create(...)`, `update(id, ...)`,
  `archive(id)`, `count_usage(id)`.

`template_synth.py` exposes:
- `synthesize_template(*, platform_id, topic, sample_extracted) -> ContentTemplate`
  — Sonnet 4.6 call; inserts with `ON CONFLICT DO NOTHING`; re-reads on
  conflict.

`templated_render.py` exposes:
- `TemplatedOutput` Pydantic model.
- `render(*, template, extracted, keyframes) -> TemplatedOutput` — Haiku
  call via `messages.parse(output_format=TemplatedOutput)`.

`markdown_render.py` exposes:
- `markdown_to_blocks(md, *, keyframes, mcp_client) -> list[dict]` —
  async because `[[Doc Title]]` resolution hits MCP.

---

## Migrations

Two migration files (project uses sequential SQL files in
[ingest/migrations/](../../ingest/migrations/)):

- `0002_content_templates.sql`
  - `CREATE TABLE content_templates (...)`
  - `CREATE UNIQUE INDEX content_templates_active_scope ...`
  - `CREATE INDEX content_templates_scope_lookup ...`
  - `ALTER TABLE captures ADD COLUMN template_id ...`
  - `ALTER TABLE captures ADD COLUMN template_prompt_used ...`
  - `ALTER TABLE captures ADD COLUMN template_output_raw ...`
  - `INSERT INTO content_templates (id, platform_id, topic, name, system_prompt, status, created_by) VALUES ('<seed-ulid>', '*', '*', 'Default summarizer', '<verbatim current summarizer.SYSTEM_PROMPT>', 'auto', 'synth');`

Idempotent (`IF NOT EXISTS` / `ON CONFLICT DO NOTHING`) like
[0001_init.sql](../../ingest/migrations/0001_init.sql).

---

## Testing strategy

The project has heavy `pytest` coverage in
[ingest/tests/](../../ingest/tests/). New tests:

- `test_templates_repo.py` — CRUD, fallback chain ordering, UNIQUE
  enforcement, archived rows excluded from resolve.
- `test_template_synth.py` — mock Anthropic client; verify
  meta-prompt structure, ON CONFLICT path, generator_meta capture.
- `test_templated_render.py` — mock Anthropic client; verify the
  user-message includes description + keyframes + video_summary;
  TemplatedOutput parsed correctly; lede null vs populated.
- `test_markdown_render.py` — round-trip every block flavour:
  fenced code → `affine:code`, mermaid → `affine:code` lang=mermaid,
  embed-html → `affine:embed-html`, `kf:<n>` → image with right
  sourceId, `[[Doc]]` → `find_doc_by_title` resolution +
  fallback, numbered list, todo list, callout, divider.
- `test_template_api.py` — endpoint contracts: GET/POST/PUT/DELETE,
  filters, 409 on collision, /resolve fallback, /synthesize,
  /captures/{id}/rerender.
- `test_orchestrator.py` — extend existing tests: synthesis triggered
  on unknown (platform, topic); template_id + snapshot persisted;
  block layout includes lede callout when present; keyframes
  inline-referenced not auto-dumped.

Delete:
- `test_summarizer.py` — replaced by `test_templated_render.py`
  (covers same surface + more). Deletion happens in the same PR that
  removes `summarizer.py`.

Pre-deletion: the seed-migration SQL embeds the current
`summarizer.SYSTEM_PROMPT` as a literal TEXT value, so the prompt is
preserved as data before the module is removed. The plan should land
the migration in the same commit that deletes `summarizer.py`.

---

## Error handling

- **Synthesis fails** (timeout, Sonnet error): orchestrator falls back to
  the `(*, *)` default template for this capture. Captures still complete.
  No retry of synthesis — next capture in the same scope tries again.
- **Templated render fails** (Haiku error): mark capture as failed; the
  worker's existing retry backoff applies. After 3 retries, capture stays
  failed.
- **Output parse fails** (rare with structured outputs but possible if
  schema-enforced parse returns `None`): same as render fail.
- **Markdown render fails** on a block (malformed mermaid, missing
  keyframe index): log at warn, render the offending block as plain text,
  continue with the rest. Don't fail the capture.
- **MCP timeout during `[[Doc Title]]` resolution**: render as inline
  plain text `[[Doc Title]]`, log at warn. Don't block on it.

---

## Migration & rollout

1. Ship migration 0002 with the seed `(*, *)` row containing today's
   verbatim summarizer prompt.
2. Behavior identical to today on first deploy — every capture lands on
   the seed template.
3. Users create scoped templates manually via API as they spot patterns.
   Or wait — a new (platform, topic) pair (e.g. someone captures the
   first arxiv/Theory paper) triggers synthesis.
4. No backfill of existing captures. They keep their pre-template output;
   `template_id` is NULL for them. The `/captures/{id}/rerender` endpoint
   can be called manually to bring an old capture under template
   management (with `?reextract=true` if its raw body is gone).
5. Once template usage is healthy (say 80% of new captures use a
   non-default template), revisit auto-archiving and version history.

---

## Open questions

None for v1. The design is intentionally additive to today's pipeline:
- Drops one module (`summarizer.py`), replaced by four small focused
  ones (`templates.py`, `template_synth.py`, `templated_render.py`,
  `markdown_render.py`).
- Existing classifier, filer, extractor, worker, API auth, retry logic
  all unchanged.
- The renderer extension is the largest single piece of new code but it's
  contained to one module with thorough unit coverage.

### Seed template protection

The `(*, *)` row is load-bearing — without it, the fallback chain
forces synthesis on every capture. Protections:
- `DELETE /templates/{id}` rejects with 409 if the target is the only
  `(*, *)` row. Caller must POST a replacement first.
- Migration 0002 is idempotent (insert only if no `(*, *)` exists),
  so re-running migrations restores the seed if it's missing.
- If `(*, *)` is somehow gone AND synthesis fails for a capture, the
  capture marks failed with a specific error code rather than looping.

---

## Related

- [2026-05-06-ingest-service-design.md](2026-05-06-ingest-service-design.md) — original pipeline spec
- [2026-05-08-phase-13-video-frame-analysis.md](2026-05-08-phase-13-video-frame-analysis.md) — keyframe extraction (now consumed as template input)
- [topics.yaml](../../ingest/topics.yaml) — platform routing & topic hints (template scope vocabulary)
