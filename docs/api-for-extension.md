# Ingest API — reference for the browser extension

This document is the contract between the **AFFiNE Capture** browser
extension and the **ingest service** running in your Portainer stack.
It supersedes the original `/capture`-only contract from v0.1: after
Phase 14 the service exposes a full content-template management API
plus capture replay.

The extension already implements:
- `POST /capture`
- `GET /captures` + `GET /captures/{id}`
- `POST /captures/{id}/retry`
- `DELETE /captures/{id}` (soft-delete)
- `GET /health`
- `POST /youtube/cookies` + `GET /youtube/cookies/status`

This doc adds the **new endpoints** introduced in Phase 14 plus a
**proposed Templates options-tab** sketch for the next extension iteration.

---

## 1. Base URL & auth

All requests target the ingest service at the URL the user configured in
**Options → Settings → Ingest base URL** (e.g. `https://ingest.example.com:3200`).

Every request carries the bearer token from **Options → Settings →
Bearer token** (the `INGEST_API_TOKEN` from the stack env):

```
Authorization: Bearer <INGEST_API_TOKEN>
Content-Type: application/json
```

The token is stored in `chrome.storage.local` and surfaced via
[`lib/api.js`](../browser-extension/lib/api.js)'s `apiCall()` helper —
new code should reuse that helper instead of constructing requests
directly.

---

## 2. Existing capture endpoints (unchanged from v0.1 / v0.3)

These are already wired up in `lib/api.js`. Listed for completeness.

| Method | Path | What it does |
|---|---|---|
| `POST` | `/capture` | Submit a URL or shared_text for ingestion. Returns 202 with `capture_id` + `doc_id` + `web_url`. |
| `GET` | `/captures?limit=&status=&platform=&before=` | Paginated list, newest first. `before` is a cursor (ISO timestamp). |
| `GET` | `/captures/{capture_id}` | Single capture with status, error, retry_count, classifier_reasoning. |
| `POST` | `/captures/{capture_id}/retry` | Reset a failed row → re-enter the worker queue. |
| `DELETE` | `/captures/{capture_id}` | Soft-delete (status='deleted'; the AFFiNE doc itself survives). |
| `GET` | `/health` | Liveness + version. |
| `POST` | `/youtube/cookies` | YT cookie sync (NDJSON). Extension-only. |
| `GET` | `/youtube/cookies/status` | Cookie freshness check. |

---

## 3. **NEW** — Templates: per-(platform, topic) AI prompts

**Concept.** Every captured doc is rendered by an LLM prompt called a
"content template". Templates are keyed by `(platform_id, topic)` — e.g.
`(youtube, Tutorials)` or `(*, Recipes)` — with `*` as wildcard. When
a capture's classification lands in a `(platform, topic)` pair that has
no template, the service **synthesizes** one via Sonnet 4.6, saves it,
then uses it. The user can edit any template's `system_prompt` to
customize how content of that kind is rendered.

Use cases the extension should surface:
- **Browse all templates** the user has accumulated (auto-generated + manually-edited)
- **Edit a template's `system_prompt`** to tweak the rendering instructions
  (e.g. "for YouTube Tutorials, always include per-step time estimates")
- **Trigger synthesis** for a (platform, topic) pair that doesn't have one yet
- **Re-render a capture** after editing the relevant template, to see the new output

### 3.1 List templates

```http
GET /templates?platform=youtube&topic=&status_filter=
```

Query params (all optional):
- `platform` — filter by `platform_id` (e.g. `youtube`, `instagram`, `*`)
- `topic` — filter by topic name (e.g. `Tutorials`)
- `status_filter` — `auto` (synth defaults), `edited` (user-tuned), or `archived`

**Response:** `200 OK` — JSON array of `ContentTemplateView`:

```json
[
  {
    "id": "01J7AB...",
    "platform_id": "youtube",
    "topic": "Tutorials",
    "name": "YouTube Tutorial v1",
    "system_prompt": "You are a tutorial summarizer. For each captured...",
    "status": "edited",
    "generator_meta": {
      "biggest_value": "Step-by-step procedure the user can replicate.",
      "user_intent": "Bookmark for later execution; needs prereqs + steps.",
      "best_roi_format": "Numbered list with timestamps, code snippets inline.",
      "available_blocks_used": ["paragraph", "list", "code"],
      "synthesizer_model": "claude-sonnet-4-6",
      "synthesized_at": "2026-05-11T14:32:00Z"
    },
    "created_by": "user",
    "created_at": "2026-05-11T14:32:00Z",
    "updated_at": "2026-05-11T15:00:00Z",
    "usage_count": 14
  },
  {
    "id": "01J5XYZ_SEED_DEFAULT",
    "platform_id": "*",
    "topic": "*",
    "name": "Default summarizer",
    "system_prompt": "You are the content summarizer for...",
    "status": "auto",
    "generator_meta": null,
    "created_by": "synth",
    "created_at": "2026-05-11T00:00:00Z",
    "updated_at": "2026-05-11T19:00:00Z",
    "usage_count": 47
  }
]
```

`status` semantics:
- `auto` — synthesized by Sonnet, never touched by user
- `edited` — user has called `PUT /templates/{id}` at least once
- `archived` — soft-deleted; excluded from the resolver chain

`usage_count` is a join against `captures.template_id` — how many captures
have run through this template. Useful for sorting "most-used" first in
the UI.

### 3.2 Get a single template

```http
GET /templates/{template_id}
```

Returns one `ContentTemplateView` (same shape as list elements) or `404`.

### 3.3 Resolve which template would run for a (platform, topic) pair

```http
GET /templates/resolve?platform=youtube&topic=Tutorials
```

Walks the fallback chain `(p, t) → (*, t) → (p, *) → (*, *)` and returns
the first match. Useful as a debugging tool — "if I capture a YouTube
Tutorial right now, which template runs?"

Returns one `ContentTemplateView` or `404` (only when even `(*, *)` is
missing, which means the seed got deleted).

### 3.4 Create a template manually

```http
POST /templates
Content-Type: application/json

{
  "platform_id": "youtube",
  "topic": "Recipes",
  "name": "YouTube Recipe v1",
  "system_prompt": "You are summarizing a recipe video..."
}
```

`status` is set to `'edited'` and `created_by` to `'user'`.

**Responses:**
- `201` — the new template
- `409` — an active template already exists at this exact `(platform_id, topic)` scope. Use `PUT` to edit it or `DELETE` to archive first.
- `422` — validation error (e.g. empty `system_prompt`)

### 3.5 Edit a template

```http
PUT /templates/{template_id}
Content-Type: application/json

{
  "name": "YouTube Recipe v2 (with per-step timings)",
  "system_prompt": "... updated prompt ..."
}
```

Body fields are all optional but **at least one must be present** (`422`
on empty body). When `system_prompt` changes, the template's `status`
auto-promotes from `'auto'` → `'edited'` (signal to the UI that the user
has tuned it).

**Responses:**
- `200` — updated template
- `404` — id not found
- `409` — scope change (`platform_id` / `topic`) collides with another active template
- `422` — empty body / validation failure

### 3.6 Archive (soft-delete) a template

```http
DELETE /templates/{template_id}
```

Sets `status='archived'`. Excluded from the resolver chain. Existing
captures' `template_id` references remain.

**Responses:**
- `200` — the archived template
- `404` — id not found
- `409` — refuses to delete the only active `(*, *)` seed (the resolver
  needs a fallback; create a replacement first)

### 3.7 Manually trigger synthesis

```http
POST /templates/synthesize
Content-Type: application/json

{
  "platform_id": "youtube",
  "topic": "Documentary",
  "sample_capture_id": "01J7..."   // optional
}
```

If `sample_capture_id` is omitted, the server picks the most recent
capture matching `(platform_id, topic)` to feed Sonnet. Costs one Sonnet
4.6 call.

**Responses:**
- `201` — the new template
- `409` — an exact-scope active template already exists (delete first)
- `400` — no sample capture available for this scope

### 3.8 Re-render a capture

```http
POST /captures/{capture_id}/rerender?reextract=false
```

Re-runs the **currently-resolved** template for this capture's (platform,
topic) against its stored `extracted_snapshot`. Replaces the doc body in
AFFiNE.

⚠️ **v1 caveat — append-only.** The new blocks are appended; the previous
render's blocks remain in the doc. The user has to manually clean up old
content in AFFiNE. v2 will diff/replace.

⚠️ **v1 caveat — no concurrency lock.** Two simultaneous rerenders of the
same capture will both succeed and produce duplicate content.

**Responses:**
- `200` — the updated `CaptureDetail`
- `404` — capture not found
- `400` — no `extracted_snapshot` (pre-Phase-14 captures don't have one)
- `501` — `reextract=true` not implemented yet

---

## 4. Proposed Templates UI for the options page

The extension's options page already has tabs: **Settings**, **History**,
**Cookies** (see [options/options.html](../browser-extension/options/options.html)).
Add a fourth tab: **Templates**.

### Tab structure

```
Templates
├── Filter bar
│   ├── platform_id <select> (populated from /templates platform_ids)
│   ├── topic <input text>
│   └── status <select> (all / auto / edited / archived)
├── Template list (cards)
│   └── <af-template-row> per template
│       ├── name + scope badge (e.g. "youtube · Tutorials" or "(*, *) — global default")
│       ├── status pill (auto / edited / archived)
│       ├── usage count
│       └── click → opens detail view
└── Detail view (slide-over or modal)
    ├── Metadata header (id, scope, status, created_by, updated_at)
    ├── system_prompt <textarea> (monospace, ~30 rows)
    ├── name <input>
    ├── generator_meta block (read-only, when status='auto')
    │   └── biggest_value / user_intent / best_roi_format / available_blocks_used / synthesized_at
    ├── Actions:
    │   ├── [Save] → PUT /templates/{id}
    │   ├── [Archive] → DELETE /templates/{id}
    │   ├── [Re-synthesize from a sample capture] → POST /templates/synthesize
    │   └── [Apply to existing capture…] → opens a capture picker, then POST /captures/{id}/rerender
    └── Help footer linking to AFFiNE markdown reference
```

### Components to add

Following the existing [options/components/](../browser-extension/options/components/) pattern:

- `af-template-row.js` — list item, mirrors `af-history-row.js`
- `af-template-editor.js` — detail/edit form, monospace textarea
- `af-prompt-textarea.js` — wraps `af-input.js` with monospace + 30-row default + monospace font from design-tokens.css
- (Reuse existing: `af-card.js`, `af-button.js`, `af-status-badge.js`, `af-breadcrumb.js`)

### Routing

Extend [options/options.js](../browser-extension/options/options.js)'s tab router with `?tab=templates&template=<id>` deep-link state so users can bookmark / share specific template editors.

### `lib/api.js` additions

```js
// browser-extension/lib/api.js — add after the existing capture helpers:

export async function listTemplates({ platform, topic, statusFilter } = {}) {
  const params = new URLSearchParams();
  if (platform) params.set("platform", platform);
  if (topic) params.set("topic", topic);
  if (statusFilter) params.set("status_filter", statusFilter);
  return apiCall(`/templates?${params}`);
}

export async function getTemplate(id) {
  return apiCall(`/templates/${id}`);
}

export async function resolveTemplate({ platform, topic }) {
  const params = new URLSearchParams({ platform, topic });
  return apiCall(`/templates/resolve?${params}`);
}

export async function createTemplate(body) {
  return apiCall("/templates", { method: "POST", body });
}

export async function updateTemplate(id, patch) {
  return apiCall(`/templates/${id}`, { method: "PUT", body: patch });
}

export async function archiveTemplate(id) {
  return apiCall(`/templates/${id}`, { method: "DELETE" });
}

export async function synthesizeTemplate({ platformId, topic, sampleCaptureId } = {}) {
  return apiCall("/templates/synthesize", {
    method: "POST",
    body: { platform_id: platformId, topic, sample_capture_id: sampleCaptureId },
  });
}

export async function rerenderCapture(id, { reextract = false } = {}) {
  const q = reextract ? "?reextract=true" : "";
  return apiCall(`/captures/${id}/rerender${q}`, { method: "POST" });
}
```

### UX patterns worth getting right

- **Empty state.** When the user has no templates beyond the `(*, *)` seed, show "Templates appear here automatically when you capture content of a new (platform, topic) kind. Try saving a recipe video to seed your first Recipe template."

- **Diff before save.** When editing `system_prompt`, show a small "changes will affect future captures only; click [Apply to existing capture…] to backfill" hint.

- **Validation hints.** The renderer is markdown-it-py with AFFiNE-specific extensions — the `system_prompt` should encourage outputs that use supported syntax. Show a collapsible "AFFiNE markdown reference" panel next to the textarea (link to [this section in the seed prompt](#)) listing supported flavours: headings, lists incl. todo, fenced code (any lang + mermaid + embed-html), callouts, kf:N keyframe refs, [[Doc Title]] cross-refs.

- **Audit clarity.** When status='auto', show the `generator_meta` block as read-only context — the user can see what Sonnet was thinking when it designed the template, and use that as a starting point for their edits.

- **Confirm archive.** Archiving the `(*, *)` seed is forbidden by the server (409). Other archives should be confirmable with "Are you sure? Existing captures keep their `template_id` reference but no future captures will use this template."

---

## 5. Error handling conventions

The service returns JSON error envelopes (see [`error_envelope.py`](../ingest/src/error_envelope.py)):

```json
{ "detail": "Human-readable message", "code": "OPTIONAL_MACHINE_CODE" }
```

Common HTTP statuses to handle:
- `401` — bad / missing bearer token → prompt the user to re-check Settings
- `404` — capture or template not found → refresh the list
- `409` — scope conflict (POST /templates) or seed protection (DELETE /templates/seed) → show the `detail` string
- `422` — Pydantic validation error → highlight the offending field
- `500` — server crash → "ingest service hit a bug, check `docker logs affine_ingest`"
- `503` — pool not initialized → service is starting up; retry in a few seconds

---

## 6. Versioning

The service is currently API v1; there is no `/v1` path prefix because
the project is single-tenant self-hosted. Breaking changes are
communicated through the migration files in `ingest/migrations/` —
review them before bumping `AFFINE_REVISION` in the stack.

The extension should display the server's version (from `GET /health`)
in the options page footer and warn on a mismatch with the extension's
expected major version.
