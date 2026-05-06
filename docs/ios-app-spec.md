# Affine Capture — iOS app spec (handoff)

**Status:** Draft v1 · **Date:** 2026-05-06
**Companion service:** [`afffine-selfhost`](https://github.com/sth3no/afffine-selfhost)
ingest service (`/capture` endpoint, see `docs/specs/2026-05-06-ingest-service-design.md`)
**This repo:** *to be created separately on the Mac, e.g. `affine-capture-ios`*

This document is **self-contained**. Copy it into the iOS repo's `README.md`
or `docs/spec.md` when you start work; you should not need any other context
from the selfhost repo to begin coding.

---

## 1. What this app does

A native iOS companion to a self-hosted AFFiNE ingest service. Users share
URLs (or text) from anywhere on iOS via the share sheet → the app pings the
ingest service → AFFiNE document appears in their workspace, automatically
filed under `Sources/`.

The ingest service does **all** the heavy lifting (extraction, transcription,
classification, folder management). The iOS app is intentionally thin: a
share extension that fires HTTP requests, a small main app to inspect
history, retry failures, and manage settings.

---

## 2. Targets

Two targets, one repo, one app group for shared Keychain + UserDefaults.

| Target | Bundle id pattern | Min iOS | Purpose |
|---|---|---|---|
| Main app | `bio.stehlik.affine-capture` | 17.0 | Settings, history, detail |
| Share Extension | `bio.stehlik.affine-capture.share` | 17.0 | Receives URL/text from any app's share sheet |

App group: `group.bio.stehlik.affine-capture`. Both targets enable this
capability; used to share the API base URL and Keychain references.

Tech stack:
- SwiftUI (no Storyboards, no UIKit unless required by `UIActivityIndicatorView` etc.)
- `URLSession` async/await — no Alamofire / no third-party HTTP libs.
- Keychain via Apple's `Security` framework (`kSecClassGenericPassword`,
  `kSecAttrAccessGroup` set to the app group).
- No external dependencies. SPM only if you really need it.

---

## 3. Configuration

Stored in Keychain (shared via app group), set in the main app's Settings:

| Key | Type | Notes |
|---|---|---|
| `server_url` | String | e.g. `https://ingest.example.com` (no trailing slash) |
| `api_token` | String | The `INGEST_API_TOKEN` from the server's stack env |

UserDefaults (shared via app group, for non-secret state):
- `last_synced_at` — timestamp of last `/captures` fetch
- `affine_workspace_url` — derived from server_url; deep links open here

The first time the share extension is invoked without configured Keychain
values, it shows a "Open the Affine Capture app to set up" sheet and exits.

---

## 4. API contract reference

Base URL: `${server_url}` from Keychain. All requests carry:
```
Authorization: Bearer ${api_token}
Content-Type: application/json
User-Agent: AffineCapture/1.0 (iOS; build N)
```

### `POST /capture` — share extension uses this
Request:
```json
{
  "url": "https://...",
  "source_app": "Instagram",
  "shared_title": "...",
  "shared_text": "..."
}
```
Response 202:
```json
{
  "capture_id": "01J9X4M5...",
  "doc_id": "...",
  "web_url": "https://affine.example.com/workspace/.../...",
  "status": "queued",
  "platform": "instagram",
  "initial_path": "Sources/Socials/Instagram"
}
```

### `GET /captures?limit=50&status=&platform=` — history view
```json
{
  "items": [
    { "capture_id": "...", "url": "...", "platform": "instagram",
      "status": "done", "doc_id": "...", "web_url": "...",
      "topic_path": "Sources/Socials/Instagram/Recipes",
      "created_at": "...", "completed_at": "..." }
  ],
  "next_cursor": null
}
```

### `GET /captures/{capture_id}` — detail view
Same shape as a single item plus `error`, `retry_count`, `classifier_reasoning`.

### `POST /captures/{capture_id}/retry` — pull-to-action
Empty body, returns 202 with the capture row.

### `DELETE /captures/{capture_id}` — swipe-to-delete
Returns 200 `{ "ok": true }`. AFFiNE doc is soft-trashed on the server.

### `GET /health` — Settings shows green dot if reachable
```json
{ "ok": true, "queue_depth": 2, "worker_alive": true, "version": "0.1.0" }
```

### Error envelope (any 4xx/5xx)
```json
{ "error": { "code": "INVALID_TOKEN", "message": "Token rejected." } }
```
Codes the iOS app needs to handle:
- `INVALID_TOKEN` (401) → bounce to Settings, highlight token field
- `RATE_LIMITED` (429) → back off, show toast
- `INTERNAL` (500/503) → generic "Server error, retry" + offer retry button

---

## 5. Screens

### 5.1 Settings (root view if not configured)
Form with:
- Server URL (URL keyboard, autocorrect off)
- API Token (secure entry, with paste button)
- "Test connection" button → `GET /health` → green/red dot + version string
- "Save" → write to Keychain, navigate to History.

Empty state shown if either field blank.

### 5.2 History (root view if configured)
- List of recent captures from `GET /captures?limit=50`.
- Pull-to-refresh.
- Each row:
  - Platform icon (use SF Symbols: `play.rectangle` for Youtube, `camera`
    for Instagram, etc.; map in code, fallback to `link`)
  - Title (from `shared_title` or fallback to truncated URL)
  - `topic_path` shown smaller, secondary color
  - Status badge: `queued`/`extracting`/`classifying`/`filing` → spinner;
    `done` → green check; `failed` → red x with retry shortcut
  - Timestamp (relative: "2 min ago")
- Swipe actions: **Retry** (any non-`done`), **Delete** (any).
- Tap row → Detail.

### 5.3 Detail
- Large title (from `shared_title`)
- URL (tappable — opens `web_url` in Safari, NOT the original URL)
- Status timeline (queued → extracting → classifying → filing → done)
- `classifier_reasoning` if present
- `topic_path` rendered as breadcrumb
- Buttons: Open in AFFiNE (Safari to `web_url`), Retry, Delete
- Error block with message if `failed`

### 5.4 Share Extension UI
Tiny, single screen:
1. Loading spinner (200ms minimum so the user sees feedback).
2. After `POST /capture`:
   - Success: Native confirmation sheet "Saved to AFFiNE" with two buttons:
     "Open" (deep links to `web_url` in Safari) and "Done".
   - Failure: "Couldn't save. Open Affine Capture to retry." button "Open".
3. Auto-dismisses after 3 s on success if the user doesn't tap.

The share extension does **not** show history; that's the main app's job.

---

## 6. Share extension flow

```swift
// Pseudocode
override func viewDidLoad() {
    super.viewDidLoad()
    Task { await handleShare() }
}

func handleShare() async {
    guard let server = Keychain.shared.serverURL,
          let token = Keychain.shared.apiToken else {
        showSetupRequiredSheet(); return
    }

    // Extract URL or text from extensionContext.inputItems
    // (NSItemProvider hasItemConformingToTypeIdentifier kUTTypeURL / kUTTypePlainText)
    let payload = try await extractPayload()  // { url?, sharedText?, sharedTitle?, sourceApp? }

    do {
        let response = try await IngestClient(server: server, token: token).capture(payload)
        showSuccess(webURL: response.webURL)
    } catch IngestError.invalidToken {
        showError("Token rejected. Open the app and re-enter your token.")
    } catch {
        showError("Couldn't save. Try again from the app.")
    }
}
```

Source app detection: read `extensionContext.inputItems[0].userInfo` and
`NSExtensionContext`'s `subjectName` if available; fall back to the bundle
identifier of the host app inferred from `Bundle.main.bundleIdentifier`
(works only in some cases). When you can't detect, leave `source_app` nil.

Hard timeout: 20 s. The server returns 202 within ~500 ms even for huge
content, so anything past 5 s is a network problem — show error.

---

## 7. IngestClient (network layer)

Single struct, one file:
```swift
struct IngestClient {
    let server: URL
    let token: String

    func capture(_ payload: CapturePayload) async throws -> CaptureResponse
    func list(limit: Int = 50, status: String? = nil) async throws -> CapturesPage
    func get(_ id: String) async throws -> CaptureDetail
    func retry(_ id: String) async throws -> CaptureResponse
    func delete(_ id: String) async throws
    func health() async throws -> Health
}
```

Common impl:
- `URLSession.shared` (or a session with 20s timeout for share extension).
- Set `Authorization: Bearer \(token)` header.
- JSON encode/decode using `JSONEncoder`/`JSONDecoder` with
  `.iso8601` date strategy.
- Translate HTTP errors into `IngestError` enum:
  ```swift
  enum IngestError: Error {
      case invalidToken, rateLimited, server(String), network(URLError)
  }
  ```

No retry inside the client — the user retries explicitly via the UI.

---

## 8. Deep links

`web_url` from the server points at the AFFiNE web UI. Open with
`UIApplication.shared.open(url)` — let the user's default browser handle it
(or Safari if they tap a Safari deep link from share extension).

If/when AFFiNE ships an iOS app with URL scheme support, swap to that
preferentially with `canOpenURL` check.

---

## 9. Local cache

The History view is **always backed by a fresh GET** but should not feel
slow. Cache strategy:
- On view appear, immediately render last-known list from `UserDefaults`
  (small JSON blob, max ~10 KB).
- Kick off a `GET /captures` in parallel; replace cache + UI on success.
- On failure, keep showing cache + an unobtrusive banner "Couldn't refresh".

No persistent local DB. The server is source of truth.

---

## 10. App icon, naming, branding

- Working title: **Affine Capture**
- Suggested SF Symbol for app concept: `square.and.arrow.down.on.square`
- App icon: leave for design pass after MVP works. Use a placeholder for now.

---

## 11. Build, sign, distribute

- Personal Apple Developer account ($99/yr) — required for share extension
  on a real device.
- Provision both targets with the same team + matching bundle ids (see §2).
- App group `group.bio.stehlik.affine-capture` enabled on both.
- TestFlight for personal use; no App Store review unless you decide to
  publish.

For ad-hoc testing without paid account: 7-day signing limit applies; you'll
re-install weekly. Acceptable for MVP.

---

## 12. Acceptance criteria (MVP)

- [ ] Configure server URL + token in Settings → "Test connection" returns
      green within 2 s.
- [ ] Share a YouTube URL from Safari → toast "Saved to AFFiNE" within 3 s →
      tapping "Open" loads the AFFiNE doc URL in Safari, doc body fills in
      within 60 s.
- [ ] Share an Instagram reel from the Instagram app → same flow, doc lands
      under `Sources/Socials/Instagram/<topic-or-root>` within 60 s.
- [ ] History view lists last 50 captures with correct status badges; pull
      to refresh.
- [ ] Swipe-to-retry on a failed capture re-runs the pipeline; status
      transitions visible without leaving the screen (re-fetch on completion).
- [ ] Swipe-to-delete soft-trashes the AFFiNE doc and removes the row.
- [ ] Token rejected (invalid/rotated) → user is sent to Settings with a
      red message on the token field.
- [ ] Killing the share extension while the request is in flight does NOT
      double-create on retry (server-side idempotency by URL hash).

---

## 13. Out of scope (MVP)

- APNs push when capture completes (server doesn't emit them yet).
- Folder picker UI (server auto-files; users edit in AFFiNE).
- File / image attachments from share sheet (only URLs + `shared_text`).
- Quick capture in main app (typed-text "memo" mode — different domain).
- Multi-account / multi-server.
- iCloud sync, Handoff, Siri shortcuts.

---

## 14. Open questions / decisions deferred

- iOS app icon style — defer to post-MVP design pass.
- Onboarding flow (QR code from server admin to auto-fill URL+token vs
  manual paste). MVP = manual paste. Post-MVP = QR.
- Whether the "Open in AFFiNE" action should prefer a hypothetical official
  AFFiNE iOS app once one exists.
