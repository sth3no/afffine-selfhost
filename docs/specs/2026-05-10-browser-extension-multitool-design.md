# AFFiNE Capture — browser extension (multitool) design

**Status:** Draft v1 · **Date:** 2026-05-10
**Companion service:** `affine_ingest` (`/capture` + `/youtube/cookies`)
**Companion app:** [iOS app spec](../ios-app-spec.md) — this extension is its desktop sibling
**Repo:** in-tree at `portainer-stack/browser-extension/` (existing — being expanded, not replaced)

This document is **self-contained**. It supersedes the v0.1 cookie-only design ([phase-12 plan](../plans/2026-05-08-phase-12-yt-cookie-extension.md)) by absorbing it as one subsystem of a larger multitool. The cookie-sync behavior shipped in v0.1 is preserved verbatim.

---

## 1. Goal

Ship a single browser extension — **AFFiNE Capture** — that:

1. **Sends any web content** (page URL, link, selected text, image) to the user's self-hosted `affine_ingest` service via `POST /capture`, surfacing results, retries, and deletes.
2. **Continues syncing YouTube cookies** to the same ingest service (today's behavior — no functional change, just relocated into a `cookies/` module).

The extension is the desktop sibling of the iOS Capture app described in [ios-app-spec.md](../ios-app-spec.md). It uses the same ingest API contract, the same `INGEST_API_TOKEN`, and the same UX vocabulary (Settings · History · Detail · Share-style confirmation). What the iOS Share Extension is to the iOS main app, the toolbar **popup** is to the **options page** here.

---

## 2. Identity

- **Name:** AFFiNE Capture (was: "Affine YT Cookie Sync")
- **Manifest version:** `0.2.0`
- **Folder:** `portainer-stack/browser-extension/` (unchanged path; existing files refactored in place)
- **Distribution:** Unpacked install (Chrome/Edge/Brave/Arc developer mode; Firefox `about:debugging`). Not store-listed in v1 — single-user tool, same as v0.1.
- **Browsers:** Chrome 88+, Edge 88+, Brave/Arc, Firefox 109+ (MV3 cross-compat).

---

## 3. Architecture

Single MV3 extension with two cooperating subsystems sharing config + transport.

### 3.1 File layout

```
browser-extension/
├── manifest.json                 # MV3, permissions: cookies, storage, alarms,
│                                 #                    contextMenus, scripting, activeTab
├── background.js                 # Top-level: routes events to cookies/* and capture/*
├── lib/
│   ├── api.js                    # Shared HTTP client; auth, error mapping, retries
│   ├── storage.js                # chrome.storage.local helpers (config + caches)
│   ├── badge.js                  # Toolbar badge state (cookies-stale OR capture-failed)
│   └── design-tokens.css         # AFFiNE design system tokens as CSS custom properties
├── cookies/                      # Today's behavior, relocated. Behavior unchanged.
│   ├── sync.js                   # syncCookies() — full flow
│   └── netscape.js               # cookiesToNetscape() — format helper
├── capture/
│   ├── client.js                 # POST /capture, GET /captures, GET /captures/{id},
│   │                             # POST /captures/{id}/retry, DELETE /captures/{id}
│   ├── context-menu.js           # Page / Link / Selection / Image right-click items
│   └── payload.js                # Tab + selection -> CaptureRequest builder
├── content/
│   ├── _shared/
│   │   ├── pill.js               # <af-pill> Web Component (Shadow DOM)
│   │   └── pill.css              # Imports design-tokens via @import in Shadow DOM
│   ├── youtube.js                # Anchors: ytd-watch-metadata #actions
│   ├── instagram.js              # Anchors: per-post article on hover
│   ├── twitter.js                # Anchors: per-tweet action bar on hover
│   ├── tiktok.js                 # Anchors: per-FYP-card on hover
│   └── reddit.js                 # Anchors: per-post card on hover
├── popup/
│   ├── popup.html                # Share-sheet style (~360 px)
│   ├── popup.js
│   └── popup.css                 # Uses design tokens
├── options/
│   ├── options.html              # Tabs: Settings · History · Cookies (~960 px wide)
│   ├── options.js
│   ├── options.css
│   └── components/
│       ├── af-button.js          # <af-button variant="primary|secondary|ghost">
│       ├── af-input.js           # <af-input type="text|password"> w/ paste button
│       ├── af-status-badge.js    # <af-status-badge status="queued|done|failed|...">
│       └── af-card.js            # <af-card> wrapper
└── icons/                        # Rebrand placeholders (16/32/48/128)
```

### 3.2 Module responsibilities

- `background.js` is the only service worker. It registers listeners for `chrome.runtime.onInstalled`, `chrome.cookies.onChanged`, `chrome.alarms.onAlarm`, `chrome.runtime.onMessage`, `chrome.contextMenus.onClicked`. It dispatches into `cookies/sync.js` or `capture/client.js` accordingly. No UI, no DOM.
- `lib/api.js` is the only place that hits the ingest server. Reads `ingestUrl` + `ingestToken` from storage; attaches `Authorization: Bearer ${token}`; maps HTTP status codes to a typed error object `{kind: 'invalid_token' | 'rate_limited' | 'server' | 'network', message, retryAfter?}`.
- `cookies/sync.js` is **byte-for-byte the v0.1 logic**, moved out of `background.js`. The `syncCookies()`, `collectYouTubeCookies()`, `fetchServerStatus()`, `verdictFromStatus()` and `applyBadge()` flow stays the same. The only change: it imports `lib/api.js` for the actual `fetch` (so cookie sync and capture share the same auth/error path).
- `capture/client.js` exposes `captureUrl(req)`, `listCaptures(opts)`, `getCapture(id)`, `retryCapture(id)`, `deleteCapture(id)`. Returns plain JS objects matching the server's Pydantic models.
- `content/_shared/pill.js` defines `<af-pill>` — a Custom Element with Shadow DOM that renders the AFFiNE-styled "Save to AFFiNE" pill. The Shadow DOM means injected pills cannot be styled by host-page CSS (YT/IG/X aggressively overwrite global styles).
- Each `content/<site>.js` script: (a) finds anchor element(s) via site-specific selectors, (b) inserts an `<af-pill>` into them, (c) sets up a `MutationObserver` for SPA route changes / infinite scroll, (d) on click, computes the canonical URL for that item and posts a message to the background script, which calls `capture/client.js`.

### 3.3 Permissions

```jsonc
{
  "permissions": [
    "cookies",         // existing — for cookies subsystem
    "storage",         // existing
    "alarms",          // existing
    "contextMenus",    // NEW — context menu items
    "scripting",       // NEW — programmatic content-script injection (per-site)
    "activeTab",       // NEW — popup capture of the current tab
    "notifications"    // NEW — toast feedback for context-menu / pill triggers (no popup is open in those flows)
  ],
  "host_permissions": [
    "*://*.youtube.com/*",     // existing — cookies + YT pill
    "*://*.instagram.com/*",   // NEW
    "*://*.x.com/*",           // NEW
    "*://*.twitter.com/*",     // NEW (legacy)
    "*://*.tiktok.com/*",      // NEW
    "*://*.reddit.com/*",      // NEW
    "https://*/*",             // existing — required so any ingest URL works
    "http://*/*"               // existing — localhost dev
  ],
  "optional_host_permissions": [
    "*://*.google.com/*",          // existing — extended-scope cookies
    "*://accounts.google.com/*"    // existing
  ]
}
```

`https://*/*` and `http://*/*` are kept (today's value) so the user's ingest URL — wherever they host it — works without further prompts. Not used for content-script injection.

---

## 4. UX surfaces

Mirroring the iOS spec's split: **popup = Share Extension equivalent**, **options page = Main App equivalent**.

### 4.1 Toolbar popup (~360 px wide)

Used for: capturing from any tab (supported or not).

```
┌────────────────────────────────────────┐
│ [favicon]  YouTube · Article title…    │  ← header, monoline truncate
│                                        │
│ ┌────────────────────────────────────┐ │
│ │      Save to AFFiNE                │ │  ← primary button (af-blue, full width)
│ └────────────────────────────────────┘ │
│                                        │
│ ⏳ Capturing…  /  ✓ Saved · Open ↗     │  ← status row, swaps in place
│                                        │
│ ───────────────────────────────────── │
│ Cookies: synced 14m ago         ●     │  ← footer, dim
│ Open AFFiNE Capture →                  │  ← deep links to options.html
└────────────────────────────────────────┘
```

- States: idle → capturing → saved (with `web_url` link) | error (red, with retry button).
- "Open" link goes to `web_url` from the response in a new tab.
- Footer cookie status uses today's verdict (`fresh` / `stale` / `missing` / `unknown`) — same dot color logic.
- Auto-closes 2s after `saved` (preserves today's behavior for the cookie sync popup).

### 4.2 Options page (~960 px wide, three tabs)

Standalone tab; reachable from popup footer, toolbar icon's right-click menu, or `chrome.runtime.openOptionsPage()`.

#### Tab: Settings

Fields, in `<af-card>` with `--af-space-4` padding:
- **Server URL** (`<af-input type="text">`, URL keyboard hint, autocorrect off)
- **Bearer token** (`<af-input type="password">`, with paste button)
- **Test connection** (`<af-button variant="secondary">`) → `GET /health` → green/red dot + version string
- **Save** (`<af-button variant="primary">`)

Empty state shown if either field blank.

#### Tab: History

- **Filter pills** (top): All · Done · Failed · In progress
- **List** of recent captures, paginated (50 per page), pulled from `GET /captures?limit=50&status=`:
  - Platform icon (linear thin-stroke SVG; mapping: youtube → play.rectangle, instagram → camera, x → x.logo, tiktok → music.note, reddit → r.logo, article → link)
  - Title (`shared_title` or fallback truncated URL)
  - `topic_path` underneath in `--af-text-body` (smaller)
  - `<af-status-badge status>` (queued/extracting/classifying/filing → spinner-blue; done → green check; failed → red x)
  - Relative time ("2 min ago")
  - On hover: Retry (only if not `done`) + Delete icon buttons appear right-aligned
- **Empty state**: card with "No captures yet — try sharing something from a supported site or right-clicking on this page"

Click row → Detail view (slides in from right; same tab; Back button).

#### Tab: Detail (sub-view of History)

- **Title** large (H2 from the design system: 36px Semibold, but capped at the card width)
- **`web_url`** small, tappable (opens in new tab; not the original URL — the AFFiNE doc URL)
- **Status timeline** (horizontal): queued → extracting → classifying → filing → done. Failed shows a red dot with the error message below.
- **`classifier_reasoning`** in a soft-blue (`--af-bg-soft`) callout card if present
- **`topic_path`** rendered as breadcrumb (`Sources / Socials / Instagram / Recipes`)
- **Action row**: `<af-button variant="primary">Open in AFFiNE</af-button>` · Retry (if not `done`) · Delete

Error block (red-tinted card) with `error` field if `failed`.

#### Tab: Cookies

Today's options-page content, restyled with the design system. No behavior changes:
- Last sync (browser-side) + server verdict (server-side)
- Sync now button
- Extended-scope checkbox for `accounts.google.com`

### 4.3 Context menu

`background.js` registers four `chrome.contextMenus.create` entries on install:

| Context | Title | Payload built |
|---|---|---|
| `page` | "Save page to AFFiNE" | `{url: tab.url, source_app: hostname, shared_title: tab.title}` |
| `link` | "Save link to AFFiNE" | `{url: linkUrl, source_app: hostname, shared_title: linkText}` |
| `selection` | "Save selection to AFFiNE" | `{url: tab.url, source_app: hostname, shared_text: selectionText}` |
| `image` | "Save image to AFFiNE" | `{url: srcUrl, source_app: hostname, shared_title: tab.title}` |

Result feedback for context-menu triggers: a transient toast via `chrome.notifications.create` ("Saved · Open in AFFiNE" / "Failed: …"). No popup is opened (the user wasn't in the popup).

### 4.4 In-page pill (content scripts)

Five sites, one shared `<af-pill>` Web Component, different anchor strategies:

| Site | Anchor selector (heuristic, observed via MutationObserver) | URL captured |
|---|---|---|
| YouTube | `ytd-watch-metadata #actions` (next to native Save/Share row) | `https://www.youtube.com/watch?v=<id>` (canonicalized) |
| Instagram | `article[role=presentation]` per post (on hover) | `https://www.instagram.com/p/<shortcode>/` or `/reel/<shortcode>/` |
| Twitter/X | `article[data-testid=tweet]` (on hover) | `https://x.com/<user>/status/<id>` |
| TikTok | `div[data-e2e=recommend-list-item-container]` (on hover) | `https://www.tiktok.com/@<user>/video/<id>` |
| Reddit | `shreddit-post` or `[data-testid=post-container]` (on hover) | `https://www.reddit.com/r/<sub>/comments/<id>/<slug>/` |

Each `<af-pill>`:
- Renders an `<af-button variant="primary" size="sm">` clone with the AFFiNE wordmark+icon
- On click: stops event propagation (so it doesn't trigger the host site's own row click), sends `{type:'capture', payload}` to background
- After response, animates to a checkmark for 1.5s then back

Selectors will rot. Each script logs a single `[AFFiNE Capture] anchor not found on <site>` warning if its observer never finds a match within 10s — visible via `chrome://extensions` "Inspect service worker" → console. Documented in README so the user knows where to look.

---

## 5. Design system port

`lib/design-tokens.css` is a single file imported by every UI surface (popup, options, Shadow DOM of every Web Component).

```css
:root {
  /* Color */
  --af-blue:     #2B85FF;
  --af-navy:     #001A3F;
  --af-success:  #4CAF50;
  --af-error:    #FF4D4F;
  --af-bg-soft:  #F5F9FF;
  --af-surface:  #FFFFFF;
  --af-gray-50:  #F8F9FA;
  --af-border:   #E5E7EB;
  --af-text-body: #4B5563;

  /* Radii */
  --af-radius-button: 8px;
  --af-radius-card:   12px;
  --af-radius-pill:   999px;

  /* Spacing — 8px base */
  --af-space-1: 4px;
  --af-space-2: 8px;
  --af-space-3: 16px;
  --af-space-4: 24px;
  --af-space-5: 32px;
  --af-space-6: 64px;

  /* Typography */
  --af-font: 'Inter', -apple-system, system-ui, 'Segoe UI', sans-serif;
  --af-font-mono: 'Roboto Mono', 'JetBrains Mono', ui-monospace, monospace;

  /* Type scale */
  --af-h1: 700 48px/1.1 var(--af-font);
  --af-h2: 600 36px/1.2 var(--af-font);
  --af-h3: 600 24px/1.3 var(--af-font);
  --af-body-l: 400 18px/1.5 var(--af-font);
  --af-body:   400 16px/1.5 var(--af-font);
  --af-small:  500 14px/1.4 var(--af-font);

  /* Elevation */
  --af-shadow-card: 0 1px 2px rgba(0,26,63,.06), 0 4px 12px rgba(0,26,63,.04);
}
```

- Web Components import these tokens inside their Shadow DOM via `:host` and `@import url('chrome-extension://__MSG_@@extension_id__/lib/design-tokens.css')` — or, cleaner, by `adoptedStyleSheets` on each component.
- **Inter font**: not bundled in v1 — system stack (`-apple-system, system-ui, 'Segoe UI'`) covers Mac/Win/Linux acceptably and avoids licensing/loading complexity. Bundling Inter as woff2 (~80KB total for 4 weights) is a v0.3 polish.
- **Iconography**: inline SVG, 2px stroke, currentColor. A small `lib/icons.js` exports SVG strings for `play-rectangle`, `camera`, `link`, `arrow-up-right`, `check`, `x-circle`, `arrow-clockwise` (retry), `trash`.
- **No light/dark toggle in v1** — light theme only (matching the AFFiNE design system MD). Dark mode is a v0.3 add.

---

## 6. Data flow

### 6.1 Capture flow (end-to-end)

```
[trigger]                                                [server]
popup button     ─┐
context menu     ─┤  → background.js     →  capture/client.js  → fetch POST /capture →  ingest 202
content pill     ─┤      buildPayload                               (lib/api.js)
                  │      dispatch
                  ▼
            chrome.storage.local
            recentCaptures cache  ◄────── response cached here
                  │
                  ▼
        ┌─────────────────────┐
        │   trigger surface   │   popup → status row updates
        │   renders feedback  │   context → chrome.notifications toast
        │                     │   pill → checkmark animation
        └─────────────────────┘
```

The options page's History tab seeds from `recentCaptures` cache on open (instant render), then does a fresh `GET /captures?limit=50` and replaces. On failure, keeps cache + a banner "Couldn't refresh" — same pattern as the iOS spec.

### 6.2 Cookie flow (unchanged from v0.1)

- `chrome.runtime.onInstalled` → `syncCookies()` (first sync) + create daily alarm
- `chrome.cookies.onChanged` for `*.youtube.com` → debounced (30s) alarm → `syncCookies()`
- Daily alarm → `syncCookies()`
- Manual: cookies-tab "Sync now" button → `syncCookies()`
- Server `GET /youtube/cookies/status` checked after each upload; verdict drives badge.

### 6.3 Shared state in `chrome.storage.local`

| Key | Type | Purpose |
|---|---|---|
| `ingestUrl` | string | Existing. Server base URL. |
| `ingestToken` | string | Existing. Bearer token. |
| `extendedScope` | bool | Existing. Cookies opt-in. |
| `lastSync` | object | Existing. Cookie sync result + verdict. |
| `lastResult` | object | NEW. Last capture result `{ok, capture_id?, web_url?, error?}`. |
| `recentCaptures` | array | NEW. ≤50 most recent capture rows for instant History render. |

`recentCaptures` size cap: hard-truncate to 50 entries, ~10 KB. Source of truth is the server.

---

## 7. Error handling

`lib/api.js` returns a typed error object `{kind, message, retryAfter?}`:

| HTTP / cause | `kind` | UI behavior |
|---|---|---|
| 401 + `INVALID_TOKEN` | `invalid_token` | Red toolbar badge ("!"), popup shows "Token rejected — open Settings", deep-link to options Settings tab with the token field outlined `--af-error`. |
| 429 + `RATE_LIMITED` | `rate_limited` | Toast "Rate limited, retrying in Ns". One auto-retry after 5s for popup-triggered captures only; context menu / pill triggers show toast and stop. |
| 5xx | `server` | Toast "Server error", manual Retry button. |
| Network failure (DNS, no internet, CORS preflight, timeout > 10s) | `network` | Toast "Couldn't reach ingest". |

Every error is also persisted to `lastResult` so the popup can render the latest state when reopened (matches today's `lastSync` pattern).

---

## 8. Acceptance criteria (v0.2 ship gate)

Manual smoke tests — extensions resist meaningful unit testing past payload builders.

### Configuration
- [ ] Settings tab → entering URL+token + clicking Test connection returns green dot within 2s on a healthy ingest.
- [ ] Settings tab → invalid token returns red dot + INVALID_TOKEN message.

### Popup capture
- [ ] On any tab, popup → "Save to AFFiNE" → status flips to "Saved · Open ↗" within 2s.
- [ ] Clicking "Open ↗" loads the AFFiNE doc URL in a new tab.
- [ ] Auto-close after 2s on success.

### Context menu
- [ ] Right-click page → "Save page to AFFiNE" → notification toast within 2s.
- [ ] Right-click link → captures link URL, not page URL.
- [ ] Right-click selection → captures `shared_text` (verify in History detail view).
- [ ] Right-click image → captures image src URL.

### Content-script pills
- [ ] YouTube watch page → pill renders in actions row, captures `youtube.com/watch?v=` URL (no playlist params).
- [ ] Instagram feed → hovering a post shows pill, click captures `/p/<id>/` or `/reel/<id>/`.
- [ ] X feed → hovering a tweet shows pill, captures `/<user>/status/<id>`.
- [ ] TikTok FYP → hovering a card shows pill, captures `@<user>/video/<id>`.
- [ ] Reddit → hovering a post shows pill, captures the comments-permalink.
- [ ] Anchor not found within 10s on any site → single warning logged, no crash, no double-pills.

### History + Detail
- [ ] History tab lists last 50 captures with platform icons + status badges.
- [ ] Filter pills (All / Done / Failed / In progress) filter the list correctly.
- [ ] Hover row → Retry + Delete icon buttons appear; Retry re-fires `/captures/{id}/retry` and updates status; Delete soft-trashes via DELETE.
- [ ] Click row → Detail view shows status timeline + classifier_reasoning + topic_path breadcrumb.

### Cookie subsystem (no regression)
- [ ] Cookies tab shows last sync + server verdict — same as v0.1.
- [ ] YT cookie change still triggers a debounced sync.
- [ ] Daily alarm still fires.
- [ ] Cookie badge `!` still appears on stale/missing.

### Error states
- [ ] Token revoked mid-session → next capture surfaces red badge + "Token rejected" + deep-link to Settings.
- [ ] Ingest unreachable → toast "Couldn't reach ingest", `lastResult` updated.

### Design system fidelity
- [ ] All AFFiNE colors used exactly as specified — no off-by-one hex.
- [ ] All UI uses Inter (or system fallback), not the browser default.
- [ ] Buttons all 8px radius, cards all 12px radius.
- [ ] Spacing follows the 8px scale (4/8/16/24/32).
- [ ] Pills injected into host sites are not visually altered by host CSS (Shadow DOM check).

---

## 9. Out of scope (v0.2)

- Inter font bundling (v0.3 polish).
- Dark mode (v0.3 polish).
- Extension-store distribution (v0.3+: Chrome Web Store + Firefox AMO require store listings + signing).
- Per-account / multi-server config (matches iOS scope).
- Saving DOM-side extras as `shared_text` from supported sites (e.g., scraping the IG caption). Server's existing extractors handle this.
- Push notifications when a capture finishes (server doesn't emit them yet — same constraint as iOS).
- Folder picker UI (server auto-files; users edit in AFFiNE).
- File / image-blob upload (only image URLs in v0.2).
- Keyboard shortcut (`commands` API) — stretch goal, easy to add post-v0.2.

---

## 10. Open questions / decisions deferred

- Whether to ship Inter as woff2. Defer to v0.3 polish pass — system stack acceptable for MVP.
- Whether the badge state should distinguish `cookies-stale` vs `capture-failed`. v0.2 uses one shared "!" — the popup explains which when opened.
- Per-site URL canonicalization edge cases (YouTube Shorts, IG threads/stories, X quote-tweets vs replies). Document each as a footnote in `payload.js`; server's idempotency hash absorbs minor variations.
- Whether to detect mobile-emulation mode and fall back to popup-only (mobile Chrome doesn't support extensions on Android except via Kiwi). Defer.
