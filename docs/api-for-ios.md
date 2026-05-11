# Ingest API — reference for the iOS app

This document is the contract between the **AFFiNE Capture** iOS app
(main app + share extension) and the **ingest service** running in your
Portainer stack. It supersedes section 4 of [`ios-app-spec.md`](ios-app-spec.md):
after Phase 14 the service exposes a full content-template management
API plus capture replay.

The current spec covers the v0.1 endpoints. This doc adds the **new
endpoints** introduced in Phase 14 and proposes a **Templates view** for
the iOS main app.

---

## 1. Base URL & auth (unchanged from spec §3)

Base URL: `${server_url}` from the Keychain (configured in **Settings**).
Token: `${api_token}` from the Keychain (same `INGEST_API_TOKEN` value
your stack env carries).

Every request:

```
Authorization: Bearer ${api_token}
Content-Type: application/json
User-Agent: AffineCapture/1.0 (iOS; build N)
```

Implementation: extend the `APIClient` actor in your iOS repo with the
new endpoints below. Reuse the existing `URLSession` instance and the
JSON encoder/decoder configured with ISO-8601 dates.

---

## 2. Existing endpoints (covered in `ios-app-spec.md` §4)

Already part of the iOS spec. Listed for completeness — no changes:

- `POST /capture` — share extension entry point
- `GET /captures` — history view
- `GET /captures/{capture_id}` — detail view
- `POST /captures/{capture_id}/retry` — pull-to-action
- `DELETE /captures/{capture_id}` — swipe-to-delete
- `GET /health` — Settings connectivity check

---

## 3. **NEW** — Templates: per-(platform, topic) AI prompts

**Concept.** Every captured doc is rendered by an LLM prompt called a
"content template". Templates are keyed by `(platform_id, topic)` — e.g.
`(youtube, Tutorials)` or `(*, Recipes)` — with `*` as wildcard.

When a capture's classification lands in a `(platform, topic)` pair that
has no template yet, the service **automatically synthesizes** one via
Claude Sonnet 4.6 (a meta-prompt that asks "what's the biggest value
in this kind of content? what's the best ROI format? which AFFiNE
blocks should it use?"), saves the synthesized template, then uses it
to render the capture.

The user can edit any template's `system_prompt` to customize how
content of that kind is rendered. For example: for `(youtube,
Tutorials)`, they might want to add "always include per-step time
estimates" to the prompt. Future YouTube tutorial captures will then
include those time estimates.

iOS use cases this exposes:
- **Templates tab** — browse templates accumulated over time
- **Template detail/edit screen** — edit `system_prompt`, view audit metadata
- **"Re-render"** action on the capture detail screen — re-runs the current
  template against a previously-captured doc (e.g. after editing the
  template's prompt, replay it to see new output)
- **"Why did this capture land here?"** — a *Resolve* lookup that shows
  the user which template ran for a given capture's (platform, topic)
  pair

### 3.1 List templates

```http
GET /templates?platform=youtube&topic=&status_filter=
```

Query params (all optional):
- `platform` — filter by `platform_id` (e.g. `youtube`, `instagram`, `*`)
- `topic` — filter by topic name
- `status_filter` — `auto`, `edited`, `archived`

**Response 200:** JSON array. Each element is a `ContentTemplateView`:

```swift
struct ContentTemplateView: Codable, Identifiable {
    let id: String
    let platform_id: String        // "youtube", "instagram", or "*"
    let topic: String              // "Tutorials", "Recipes", or "*"
    let name: String               // Human label, e.g. "YouTube Tutorial v1"
    let system_prompt: String      // The LLM instruction (~1-5k chars)
    let status: String             // "auto" | "edited" | "archived"
    let generator_meta: [String: AnyCodable]?  // synth reasoning, nil for user-created
    let created_by: String         // "synth" | "user"
    let created_at: Date
    let updated_at: Date
    let usage_count: Int           // joined from captures.template_id
}
```

Notes:
- `status` values:
  - `"auto"` — synthesized by Sonnet, never touched by user
  - `"edited"` — user has changed `system_prompt` at least once
  - `"archived"` — soft-deleted; excluded from the resolver chain
- `usage_count` enables "most-used first" sorting in the list view
- `generator_meta`, when present, contains `biggest_value`, `user_intent`,
  `best_roi_format`, `available_blocks_used`, `synthesized_at`,
  `synthesizer_model` — what Sonnet was "thinking" when it designed
  the template. Show these as read-only audit info next to an `auto`
  template.

### 3.2 Get a single template

```http
GET /templates/{template_id}
```

Returns one `ContentTemplateView` or 404.

### 3.3 Resolve (debug: which template runs for a scope?)

```http
GET /templates/resolve?platform=youtube&topic=Tutorials
```

Walks the fallback chain `(p, t) → (*, t) → (p, *) → (*, *)` and returns
the first match. Useful for the **capture detail screen**: surface "this
capture was rendered by *YouTube Tutorial v1*" as a tappable row linking
to the template editor.

Returns one `ContentTemplateView` or 404 (only when even `(*, *)` is
missing — which means the seed was deleted; surface this as a
configuration error).

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

Server sets `status='edited'`, `created_by='user'`.

**Responses:**
- `201` — the new template
- `409` — an active template already exists at this scope (use PUT or DELETE first)
- `422` — validation error

### 3.5 Edit a template

```http
PUT /templates/{template_id}
Content-Type: application/json

{ "system_prompt": "Updated prompt...", "name": "New name" }
```

All fields optional. **Empty body returns 422** — at least one field must
change. When `system_prompt` changes, the server auto-promotes
`status='auto'` → `'edited'`.

**Responses:**
- `200` — updated template
- `404` — id not found
- `409` — scope-change collision
- `422` — empty body / validation failure

### 3.6 Archive

```http
DELETE /templates/{template_id}
```

Soft-delete; `status='archived'`. Existing captures' `template_id` references
remain (audit).

**Responses:**
- `200` — the archived template
- `404` — id not found
- `409` — refuses to archive the only active `(*, *)` seed

### 3.7 Trigger synthesis manually

```http
POST /templates/synthesize
Content-Type: application/json

{
  "platform_id": "youtube",
  "topic": "Documentary",
  "sample_capture_id": "01J7..."   // optional
}
```

Costs one Sonnet 4.6 call. Useful as a power-user action ("regenerate
a template after editing the meta-prompt" — future feature).

**Responses:**
- `201` — the new template
- `409` — exact-scope active template already exists
- `400` — no sample capture available

### 3.8 Re-render a capture

```http
POST /captures/{capture_id}/rerender?reextract=false
```

Re-runs the currently-resolved template for this capture's (platform,
topic) against its stored `extracted_snapshot`. Replaces the doc body
in AFFiNE.

⚠️ **v1 caveat — append-only.** Blocks are appended; old render blocks
remain. Surface this clearly in the UI ("Re-render appends new content
to the AFFiNE doc; you may want to clean up the previous content
manually") or wait for the v2 replace semantics.

⚠️ **v1 caveat — no concurrency lock.** Two simultaneous rerenders of
the same capture both succeed and produce duplicate content.

**Responses:**
- `200` — the updated `CaptureDetail`
- `404` — capture not found
- `400` — no `extracted_snapshot` (pre-Phase-14 captures)
- `501` — `reextract=true` not supported yet

---

## 4. Proposed Templates view for the iOS main app

The current spec has three screens: Settings, History, Detail. Add a
fourth: **Templates**, accessible from the tab bar (or a "Templates"
button in the Settings view if you prefer to keep the tab bar lean).

### 4.1 Templates list screen

NavigationStack with a TemplatesListView:

```swift
struct TemplatesListView: View {
    @State private var templates: [ContentTemplateView] = []
    @State private var filter: TemplatesFilter = .all
    @State private var search: String = ""

    var body: some View {
        List(filteredTemplates) { template in
            NavigationLink(value: template) {
                TemplateRowView(template: template)
            }
        }
        .searchable(text: $search, prompt: "Filter by topic or platform")
        .toolbar { /* status filter picker + sort */ }
        .navigationDestination(for: ContentTemplateView.self) { t in
            TemplateEditorView(template: t)
        }
        .task { await loadTemplates() }
        .refreshable { await loadTemplates() }
    }
}

struct TemplateRowView: View {
    let template: ContentTemplateView
    var body: some View {
        HStack {
            VStack(alignment: .leading) {
                Text(template.name).font(.headline)
                Text("\(template.platform_id) · \(template.topic)")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing) {
                StatusBadge(status: template.status)
                Text("\(template.usage_count) uses")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
    }
}
```

Scope badge shape recommendation:
- Exact scope: `"youtube · Tutorials"`
- Topic wildcard: `"any · Tutorials"`
- Platform wildcard: `"youtube · any"`
- Global default: `"any · any (default)"` with a special "Default" pill

Status badge colors (use the existing `StatusBadge` from the History view):
- `auto` — gray (machine-generated, never edited)
- `edited` — accent color (user has tuned this)
- `archived` — muted, struck through

### 4.2 Template editor screen

A vertically scrolling `Form` with:

```swift
struct TemplateEditorView: View {
    @State var template: ContentTemplateView
    @State private var editedPrompt: String
    @State private var editedName: String
    @State private var isSaving = false
    @State private var showRerender = false

    var body: some View {
        Form {
            Section("Scope") {
                LabeledContent("Platform", value: template.platform_id)
                LabeledContent("Topic", value: template.topic)
                LabeledContent("Status", value: template.status)
                LabeledContent("Usage", value: "\(template.usage_count) captures")
            }
            Section("Name") {
                TextField("Template name", text: $editedName)
            }
            Section {
                TextEditor(text: $editedPrompt)
                    .font(.system(.body, design: .monospaced))
                    .frame(minHeight: 400)
            } header: {
                Text("System prompt")
            } footer: {
                AffineMarkdownReferenceLink()
            }
            if let meta = template.generator_meta {
                Section("Synthesizer audit") {
                    ForEach(Array(meta), id: \.key) { key, value in
                        LabeledContent(key.humanReadable, value: String(describing: value))
                    }
                }
            }
            Section {
                Button("Save changes") { Task { await save() } }
                    .disabled(!hasChanges || isSaving)
                Button("Apply to existing capture…") { showRerender = true }
                Button("Archive", role: .destructive) { Task { await archive() } }
            }
        }
        .navigationTitle(template.name)
        .sheet(isPresented: $showRerender) {
            CapturePickerView(scope: template) { captureId in
                await api.rerenderCapture(captureId)
            }
        }
    }
}
```

### 4.3 Capture detail integration

In the existing **capture detail** screen, surface the template that
produced this capture. Add a row at the bottom:

```swift
// In CaptureDetailView body:
if let template = resolvedTemplate {
    NavigationLink(value: template) {
        LabeledContent("Rendered by template", value: template.name)
    }
} else {
    LabeledContent("Rendered by template", value: "(none — pre-Phase 14 capture)")
}

Button("Re-render with current template") {
    Task { await api.rerenderCapture(capture.capture_id) }
}
.disabled(capture.extracted_snapshot == nil)
.help("Append-only; old content stays in the AFFiNE doc until manually removed")
```

Load `resolvedTemplate` via:

```swift
let resolved = try await api.resolveTemplate(
    platform: capture.platform,
    topic: capture.classifier_topic ?? "*"
)
```

### 4.4 APIClient additions

Extend the existing `APIClient` actor (or whatever you named it):

```swift
extension APIClient {
    func listTemplates(
        platform: String? = nil,
        topic: String? = nil,
        statusFilter: String? = nil
    ) async throws -> [ContentTemplateView] {
        var params: [URLQueryItem] = []
        if let platform { params.append(.init(name: "platform", value: platform)) }
        if let topic { params.append(.init(name: "topic", value: topic)) }
        if let statusFilter { params.append(.init(name: "status_filter", value: statusFilter)) }
        return try await request("GET", "/templates", query: params)
    }

    func getTemplate(_ id: String) async throws -> ContentTemplateView {
        try await request("GET", "/templates/\(id)")
    }

    func resolveTemplate(platform: String, topic: String) async throws -> ContentTemplateView? {
        do {
            return try await request("GET", "/templates/resolve",
                query: [.init(name: "platform", value: platform),
                        .init(name: "topic", value: topic)])
        } catch APIError.notFound { return nil }
    }

    func createTemplate(_ body: CreateTemplateRequest) async throws -> ContentTemplateView {
        try await request("POST", "/templates", body: body, expectedStatus: 201)
    }

    func updateTemplate(_ id: String, _ patch: UpdateTemplateRequest) async throws -> ContentTemplateView {
        try await request("PUT", "/templates/\(id)", body: patch)
    }

    func archiveTemplate(_ id: String) async throws -> ContentTemplateView {
        try await request("DELETE", "/templates/\(id)")
    }

    func synthesizeTemplate(
        platformId: String,
        topic: String,
        sampleCaptureId: String? = nil
    ) async throws -> ContentTemplateView {
        let body = SynthesizeRequest(
            platform_id: platformId, topic: topic, sample_capture_id: sampleCaptureId
        )
        return try await request("POST", "/templates/synthesize", body: body, expectedStatus: 201)
    }

    func rerenderCapture(_ id: String, reextract: Bool = false) async throws -> CaptureDetail {
        let q: [URLQueryItem] = reextract ? [.init(name: "reextract", value: "true")] : []
        return try await request("POST", "/captures/\(id)/rerender", query: q)
    }
}
```

### 4.5 UX recommendations

- **Empty state.** "Templates appear here automatically as you capture
  content of new kinds. Try capturing a recipe video or a tutorial to
  seed your first specialized template."

- **Save confirmation.** When the user edits a template, show a brief
  toast: "Saved. Future YouTube · Tutorials captures will use this
  prompt. Tap **Apply to existing capture…** to backfill an old one."

- **AFFiNE markdown reference.** Provide an inline link / sheet that
  documents which markdown features render correctly in AFFiNE. The
  ingest service's `(*, *)` seed prompt embeds this reference — load
  it via `GET /templates/01J5XYZ_SEED_DEFAULT` and parse the section.
  Or hard-code a Swift constant. Critical for users tuning prompts.

- **Rerender warning UX.** Before kicking off a rerender, show:
  > "Re-rendering will APPEND new blocks to the AFFiNE doc; the
  > previous render's content will remain in the doc. You may want to
  > open AFFiNE and clean up the old content first."
  > [Cancel] [Re-render anyway]

- **Synthesizer audit hint.** When viewing an `auto`-status template,
  show `generator_meta` (`biggest_value`, `user_intent`,
  `best_roi_format`) in a collapsible "Why this template?" section.
  Helps the user understand what Sonnet thought when designing it,
  and gives them a starting point for edits.

- **Confirm archive of seed.** Server returns 409 for archiving the
  only `(*, *)` row; surface this as: "The default template can't be
  archived (it's the fallback for any content type). Create a
  replacement first."

---

## 5. Error envelope (unchanged from spec §4)

Same shape as v0.1:

```json
{ "error": { "code": "INVALID_TOKEN", "message": "Token rejected." } }
```

Status codes for the new endpoints:
- `400` — bad request (e.g. no sample capture for synthesis)
- `404` — template / capture not found
- `409` — scope conflict, seed protection
- `422` — Pydantic validation error (e.g. empty PUT body)
- `501` — `reextract=true` not yet implemented

Handle uniformly via the existing `APIError` enum.

---

## 6. Backwards compatibility

iOS apps written against the original `ios-app-spec.md` continue to
work — the new endpoints are additive. The capture endpoints
(`/capture`, `/captures`, `/captures/{id}`) return the same wire shape
plus optional new fields:
- `CaptureDetail.template_id: String?` — null for pre-Phase-14 captures
- `CaptureDetail.template_prompt_used: String?` — prompt snapshot at render time

These are present in the response JSON but the Swift `Codable` struct
should declare them optional so existing decoder logic still parses
older / minimal responses.
