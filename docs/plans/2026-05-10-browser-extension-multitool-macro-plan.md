# AFFiNE Capture (browser extension) — Macro Implementation Plan

> **For agentic workers:** This is a **macro plan** — it sequences phases, each of which will receive its own detailed task-level plan via the `writing-plans` skill before execution. Use `superpowers:subagent-driven-development` to execute each phase.

**Goal:** Expand the existing `portainer-stack/browser-extension/` (today: "Affine YT Cookie Sync", v0.1.0, cookie sync only) into **AFFiNE Capture** v0.2.0 — a multitool that *also* sends any web content (page URL, link, selected text, image, per-post pill on 5 major sites) to the ingest service via `POST /capture`, with a polished AFFiNE-design-system-themed Settings / History / Detail UI that mirrors the iOS Capture app.

**Spec:** [`docs/specs/2026-05-10-browser-extension-multitool-design.md`](../specs/2026-05-10-browser-extension-multitool-design.md)

**Architecture:** Single MV3 extension. Two cooperating subsystems share the same `ingestUrl` + `ingestToken` and the same HTTP client (`lib/api.js`): the **cookies** subsystem (today's v0.1 behavior, byte-for-byte) and the new **capture** subsystem. UI surfaces (popup, options page, content-script pills) are vanilla HTML + Web Components with Shadow DOM, styled exclusively from a single `lib/design-tokens.css` that ports the AFFiNE design system to CSS custom properties. No bundler. No framework.

**Tech Stack:**
- Manifest V3 (Chrome 88+ / Firefox 109+ / Edge / Brave / Arc)
- Vanilla JavaScript (ES2022 modules, top-level `await`)
- Web Components (Custom Elements + Shadow DOM + `adoptedStyleSheets`)
- `chrome.cookies` · `chrome.alarms` · `chrome.contextMenus` · `chrome.scripting` · `chrome.notifications` · `chrome.storage.local` · `chrome.action`
- Design system: AFFiNE blue (#2B85FF), 8 px spacing scale, 8/12/999 px radii, Inter via system stack
- No external dependencies. No build step.

**Total effort estimate:** ~2–3 days of focused work for one developer. Phases 1 + 4 are scaffold (no user-visible change); Phases 2 + 3 + 5 each independently demonstrate value; Phase 6 + 7 + 8 + 9 polish to ship gate.

---

## Phase Dependency Graph

```
Phase 1 (refactor + lib/ + manifest)
     │
     ├──────────────────┬─────────────────────────────────┐
     ▼                  ▼                                 ▼
Phase 2 (capture        Phase 4 (Web Components +         (cookies/* unchanged
 client + popup          design tokens)                    behavior preserved
 capture flow)              │                              by Phase 1)
     │                      │
     │                      ├─────────────┬───────────────┐
     ▼                      ▼             ▼               ▼
Phase 3 (context        Phase 5      Phase 6        Phase 7
 menu items)            (Settings    (History +     (Cookies tab,
     │                   tab)         Detail        restyled)
     │                       │         sub-view)         │
     │                       │             │             │
     └───────────────────────┴─────────────┴─────────────┘
                             │
                             ▼
                    Phase 8 (content scripts:
                     pill + 5 site adapters)
                             │
                             ▼
                    Phase 9 (polish + acceptance pass)
```

Phase 1 unblocks everything. Phase 4 unblocks all UI tabs (5/6/7) and the in-page pill (8). Phase 2 unblocks the popup capture flow + History (which calls the same client). Phases 4–7 can be done in parallel by separate subagents once 1+2 are done. Phase 8 needs both the capture client (2) and Web Components (4). Phase 9 closes out.

---

## Phase 1 — Refactor scaffold + manifest + `lib/` shared core

**Goal:** Reorganize the existing extension files into the new layout (`lib/`, `cookies/`, capture/options/popup folders) and update `manifest.json` to v0.2.0 with the new permissions and the renamed identity. **No user-visible behavior change** — cookie sync continues to work exactly as in v0.1.

**Files:**
- Modify: `browser-extension/manifest.json` (rename to "AFFiNE Capture", bump version, add `contextMenus` / `scripting` / `activeTab` / `notifications` permissions, add IG/X/TikTok/Reddit host permissions)
- Modify: `browser-extension/background.js` (slim down to a router that imports `cookies/sync.js`; keep all v0.1 listeners delegating into the new module)
- Create: `browser-extension/cookies/sync.js` (move `syncCookies`, `collectYouTubeCookies`, `fetchServerStatus`, `verdictFromStatus`, `applyBadge` here verbatim)
- Create: `browser-extension/cookies/netscape.js` (move `cookiesToNetscape` here)
- Create: `browser-extension/lib/api.js` (shared HTTP client: `request(method, path, opts)`, `IngestError` typed errors, attaches `Authorization: Bearer ${token}`, maps 401/429/5xx/network)
- Create: `browser-extension/lib/storage.js` (`getConfig()`, `setConfig()`, `getCachedRecent()`, `setCachedRecent()`, `getLastResult()`, `setLastResult()`, `getLastSync()`, `setLastSync()`)
- Create: `browser-extension/lib/badge.js` (`applyBadge({cookies, capture})` — single source of truth for the toolbar badge)
- Create: `browser-extension/lib/design-tokens.css` (full AFFiNE design system as CSS custom properties; contents per spec §5)
- Modify: `browser-extension/popup/popup.html` (move from root; preserve current cookie status UI; will be rebuilt in Phase 2)
- Modify: `browser-extension/popup/popup.js`
- Modify: `browser-extension/options/options.html` (move from root; preserve v0.1 form; will be rebuilt in Phase 5+7)
- Modify: `browser-extension/options/options.js`
- Modify: `browser-extension/README.md` (rename, update install / settings instructions for the new identity; defer "what's new in v0.2" until Phase 9)
- Create: `browser-extension/icons/` placeholder rebrand notes (actual icon refresh deferred to Phase 9)

**Acceptance:**
- `chrome://extensions` → "Load unpacked" the folder → extension shows as "AFFiNE Capture" v0.2.0.
- Click toolbar icon → popup loads with the v0.1 cookie UI intact.
- Configure URL + token in options → click "Sync now" → server-side `docker logs affine_ingest` shows the cookie upload log line (proving the cookie subsystem still works after the file move).
- `chrome.cookies.onChanged` for `youtube.com` triggers the debounced 30s alarm and a sync (verify in service-worker console).
- Daily safety-net alarm still scheduled (`chrome://extensions` → "Inspect service worker" → `chrome.alarms.getAll(console.log)` → both `yt-cookie-daily-sync` and any debounce alarm visible).
- No console errors in either the popup or the service worker.

**Out of scope for this phase:** Capture functionality. Web Components. Design system styling beyond `:root` token declarations. Context menu. New popup/options layouts (only file moves).

---

## Phase 2 — Capture API client + popup capture flow

**Goal:** From the toolbar popup, on any tab, the user can click "Save to AFFiNE" and a row appears server-side via `POST /capture` with the tab URL + title. The popup shows ⏳ Capturing → ✓ Saved · Open ↗ and persists `lastResult` to storage.

**Files:**
- Create: `browser-extension/capture/client.js` (`captureUrl(req)`, `listCaptures({limit, status, cursor})`, `getCapture(id)`, `retryCapture(id)`, `deleteCapture(id)` — all using `lib/api.js`)
- Create: `browser-extension/capture/payload.js` (`buildPayloadFromTab(tab, {selection?, link?, srcUrl?, source = 'popup'})` returning `CaptureRequest`-shaped object)
- Create: `browser-extension/capture/__tests__/payload.test.js` (jsdom-friendly: input variations → expected `{url, source_app, shared_title, shared_text}`)
- Modify: `browser-extension/popup/popup.html` (add primary "Save to AFFiNE" button + status row above the existing cookie status footer; spec §4.1 layout)
- Modify: `browser-extension/popup/popup.js` (on click → `chrome.tabs.query({active:true, currentWindow:true})` → `buildPayloadFromTab` → `chrome.runtime.sendMessage({type:'capture', payload})` → render result; handle error states)
- Modify: `browser-extension/popup/popup.css` (use `--af-blue` primary button, `--af-radius-button: 8px`, etc.)
- Modify: `browser-extension/background.js` (add `'capture'` message handler that calls `client.captureUrl`, persists `lastResult` and updates `recentCaptures` cache, returns the response to the caller)

**Acceptance:**
- Open popup on any tab → primary "Save to AFFiNE" button visible.
- Click it → status row shows "Capturing…" with a spinner, then within 2s flips to "Saved · Open in AFFiNE" with a working `web_url` link.
- Server-side: `GET /captures?limit=1` returns the new capture with the right URL + `source_app: <hostname>`.
- Click the "Open in AFFiNE" link → opens `web_url` in a new tab.
- Popup auto-closes 2s after success (existing behavior preserved).
- `chrome.storage.local` → `lastResult` set to `{ok: true, capture_id, web_url, ...}`.
- Idempotency: clicking "Save to AFFiNE" twice on the same tab → both responses carry the same `capture_id` (server idempotency hash kicks in).
- Token rejected (rotate `INGEST_API_TOKEN` server-side) → status flips to red "Token rejected — open Settings", deep-links open the options page.
- `payload.test.js` unit tests pass: 6+ assertions covering popup tab, link, selection, image, and edge cases (no `shared_title`, very long titles).

**Out of scope:** Context menu integration. History view. Detail view. In-page pill. Visual polish beyond design tokens (placeholder Web Component styles still acceptable).

---

## Phase 3 — Context menu (page / link / selection / image)

**Goal:** Right-click anywhere → "Save … to AFFiNE" with four context-aware items. Successful capture surfaces a `chrome.notifications` toast with "Open in AFFiNE" action. Failures surface the error.

**Files:**
- Create: `browser-extension/capture/context-menu.js` (registers four menu items in `chrome.runtime.onInstalled`; `onClicked` handler dispatches via `payload.js` + `client.js`; calls `chrome.notifications.create` for feedback)
- Modify: `browser-extension/background.js` (import `capture/context-menu.js` so its `onInstalled` listener fires)
- Modify: `browser-extension/capture/payload.js` (extend `buildPayloadFromTab` to accept `info` from `chrome.contextMenus.OnClickData` — `linkUrl`, `selectionText`, `srcUrl`, `pageUrl`)
- Add to: `browser-extension/capture/__tests__/payload.test.js` (4 new assertions: page / link / selection / image inputs → correct CaptureRequest shape)
- Modify: `browser-extension/icons/` (ensure `icon-128.png` is suitable for use in the notification — Chrome requires a square icon path)

**Acceptance:**
- Right-click any page → context menu lists "Save page to AFFiNE", "Save link to AFFiNE" (when on a link), "Save selection to AFFiNE" (when text is selected), "Save image to AFFiNE" (when on an image). Items appear/disappear correctly based on the click target.
- Click "Save page" → notification toast appears within 2s with title "Saved to AFFiNE" and a button "Open" that opens `web_url`.
- Click "Save link" → captures the link's `href`, NOT the page URL. Verify in History detail.
- Click "Save selection" with text highlighted → captures `shared_text` with the highlighted text. Verify the History detail view shows the selection text.
- Click "Save image" → captures the image's `src` URL.
- On error (e.g. token revoked) → notification toast title "Couldn't save", body shows the error message.
- All 10 unit tests in `payload.test.js` pass.

**Out of scope:** Customizing the notification beyond title + body + 1 action button. Internationalization of menu strings. Right-click on video poster (Chrome treats those as images, which works automatically — verify but don't add bespoke handling).

---

## Phase 4 — Web Components for the AFFiNE design system

**Goal:** Four reusable Custom Elements — `<af-button>`, `<af-input>`, `<af-status-badge>`, `<af-card>` — each Shadow-DOM-encapsulated and pulling style exclusively from `lib/design-tokens.css` via `adoptedStyleSheets`. Renders identically inside the popup, the options page, and content-script-injected DOM (where host-page CSS would otherwise leak in).

**Files:**
- Create: `browser-extension/options/components/af-button.js` (variants: `primary` | `secondary` | `ghost` | `icon`; sizes: `sm` | `md`; states: hover scale, click darken, disabled; props via observed attributes)
- Create: `browser-extension/options/components/af-input.js` (types: `text` | `password` | `url`; optional `paste-button` attribute; emits `change` events)
- Create: `browser-extension/options/components/af-status-badge.js` (statuses match server enum: `queued` | `extracting` | `classifying` | `filing` | `done` | `failed` | `deleted`; queued/extracting/classifying/filing show spinner-blue dot; done shows green check; failed shows red x; renders SVG inline)
- Create: `browser-extension/options/components/af-card.js` (just a styled wrapper with `--af-radius-card`, `--af-space-4` padding, `--af-shadow-card`)
- Create: `browser-extension/lib/icons.js` (exports SVG strings: `play-rectangle`, `camera`, `link`, `arrow-up-right`, `check`, `x-circle`, `arrow-clockwise`, `trash`, `r-logo`, `music-note`, `x-logo`; all 2px stroke, currentColor)
- Create: `browser-extension/options/components/__tests__/components.test.js` (jsdom-friendly: each component renders, observed attribute changes propagate, custom events fire)

**Acceptance:**
- Loading any component file in a `<script type="module">` registers the custom element (`customElements.get('af-button')` returns the constructor).
- `<af-button variant="primary">Save</af-button>` renders with `background: #2B85FF`, `color: white`, `border-radius: 8px`, padding from `--af-space-2`/`--af-space-3`, Inter font.
- `<af-button variant="secondary">` renders with white bg + AFFiNE-blue border + AFFiNE-blue text.
- `<af-input type="password" paste-button>` renders a password input with a paste button that, when clicked, calls `navigator.clipboard.readText()` and inserts.
- `<af-status-badge status="done">` renders a green check; `status="failed"` renders a red x; `status="queued"` renders a blue spinner.
- `<af-card>` renders with shadow + 12px radius + 24px padding, slot accepts arbitrary content.
- A demo HTML file (just for verification, NOT shipped) shows all four components on a host page with `body { font-family: Comic Sans }` — components remain Inter via Shadow DOM.
- All 12+ component unit tests pass.

**Out of scope:** Form-state utilities. A11y audit beyond keyboard focus styles + `aria-*` for badges. Theming beyond AFFiNE light. Animations beyond the existing hover scale + click darken from spec.

---

## Phase 5 — Options page: Settings tab

**Goal:** Three-tab layout in the options page (Settings · History · Cookies — History tab is a placeholder until Phase 6, Cookies until Phase 7). The Settings tab uses Web Components from Phase 4 to render server URL + bearer token inputs, "Test connection" → green/red dot + version, Save.

**Files:**
- Modify: `browser-extension/options/options.html` (top tab nav with `Settings`, `History`, `Cookies`; tab switcher; mount Settings tab content as the first tab; URL hash routing `#settings` / `#history` / `#cookies`)
- Modify: `browser-extension/options/options.js` (tab switcher logic; Settings form: load from `lib/storage.js`, save back, "Test connection" calls `lib/api.js` → `GET /health`)
- Modify: `browser-extension/options/options.css` (page layout: max-width 960 px, generous padding per `--af-space-5`, tab bar styled per design system, soft-blue active-tab indicator)
- Add to: existing `lib/api.js` — `health()` method returning `{ok, queue_depth, worker_alive, version}`

**Acceptance:**
- Open `chrome-extension://<id>/options/options.html` → page loads with three tabs visible, Settings active by default.
- Tab nav clicks switch sections; URL hash updates; back/forward navigation works.
- Settings form pre-fills from `chrome.storage.local` if previously saved.
- "Test connection" with valid URL + token → green dot + version string ("v0.1.0") within 2s.
- "Test connection" with invalid token → red dot + "Token rejected".
- "Test connection" with unreachable URL → red dot + "Couldn't reach server".
- Save persists to `chrome.storage.local` and shows a "Saved" toast for 2s.
- Settings tab uses no inline styles — every styled element is either an `<af-*>` component or styled via `lib/design-tokens.css` variables in `options.css`.

**Out of scope:** History tab content (Phase 6). Cookies tab content (Phase 7). Multi-server profiles. QR-code onboarding (deferred per spec §10).

---

## Phase 6 — Options page: History tab + Detail sub-view

**Goal:** History tab shows the user's last 50 captures with platform icons + status badges + relative time, filterable by status pill, with hover Retry/Delete. Click a row → Detail sub-view with status timeline + classifier_reasoning + breadcrumb + Open/Retry/Delete actions.

**Files:**
- Create: `browser-extension/options/components/af-history-row.js` (Web Component: composed from `af-card` + `af-status-badge`; props: capture data; events: `retry`, `delete`, `open`)
- Create: `browser-extension/options/components/af-status-timeline.js` (5-step horizontal timeline with current step highlighted)
- Create: `browser-extension/options/components/af-breadcrumb.js` (renders `Sources / Socials / Instagram / Recipes` from a `topic_path` string)
- Modify: `browser-extension/options/options.html` (History tab content: filter pills row, list container, empty state, infinite scroll sentinel; Detail panel as a slide-in sub-view)
- Modify: `browser-extension/options/options.js` (History data fetch: `recentCaptures` cache → instant render, then `client.listCaptures` → replace; filter pill state in URL hash; row hover state; Retry/Delete handlers; Detail view: `client.getCapture(id)` on row click; Detail Open/Retry/Delete actions)
- Modify: `browser-extension/options/options.css` (history-row hover state, filter-pill active state, detail panel slide-in animation, status timeline geometry)
- Add to: `browser-extension/lib/icons.js` — platform icon mapping helper (`platformIcon(platform)` returns SVG string from the lib)

**Acceptance:**
- History tab opens → renders cached recent captures instantly, then refreshes from server.
- Each row: correct platform icon + title + `topic_path` + status badge + relative time (e.g., "2 min ago").
- Filter pills: clicking "Done" filters to status=done; "Failed" filters to status=failed; "In progress" filters to non-terminal statuses.
- Hover a row → Retry icon + Delete icon appear on the right; both have tooltips.
- Retry click → fires `POST /captures/{id}/retry` → row's status badge updates within 2s.
- Delete click → confirms with native `confirm()` → fires `DELETE /captures/{id}` → row removes from list with a brief slide-out.
- Click a row → Detail sub-view slides in from right, shows: large title, tappable `web_url`, 5-step status timeline with current step highlighted, classifier_reasoning callout (if present), topic_path breadcrumb, Open/Retry/Delete buttons.
- Detail's "Open in AFFiNE" → opens `web_url` in new tab.
- Empty state on first load: card with "No captures yet — try sharing something from a supported site or right-clicking on this page".

**Out of scope:** Pagination beyond first 50 (cursor support deferred — `next_cursor` from server is acknowledged but not consumed in v0.2). Persistent local DB. Search / sort beyond status filter. Bulk operations.

---

## Phase 7 — Options page: Cookies tab (restyled)

**Goal:** Move the v0.1 options-page cookie content into the Cookies tab of the new three-tab layout, restyled with AFFiNE design system tokens. **Functionally identical to v0.1.**

**Files:**
- Modify: `browser-extension/options/options.html` (Cookies tab content: last sync card, server verdict card, Sync now button, extended-scope checkbox; using `af-card` / `af-button` / `af-input` Web Components)
- Modify: `browser-extension/options/options.js` (Cookies tab handlers: load `lastSync`, render verdict colors per spec §4.1 footer, Sync now sends `{type:'sync-now'}` to background, extended-scope checkbox calls `chrome.permissions.request` / `remove`)
- Modify: `browser-extension/options/options.css` (verdict color states use `--af-success` / `--af-error` / warn = `#fff3cd` background)

**Acceptance:**
- Cookies tab shows: Last sync (browser-side timestamp + cookie count), Server verdict (with color: green for fresh, amber for stale, red for missing).
- Clicking Sync now triggers `cookies/sync.js` and updates the display within 2s.
- Extended-scope checkbox: ticking prompts for `accounts.google.com` permission; unticking calls `chrome.permissions.remove`. Verify in `chrome://extensions` → Details → Site access.
- Manual smoke: clear the cookies file in tmpfs server-side (`docker exec affine_ingest rm /run/cookies/youtube.txt`) → Cookies tab "Server" verdict turns red within 30s of next sync.
- No regression in v0.1 cookie behavior end-to-end (cobalt + yt-dlp still get the cookies file).

**Out of scope:** Cookie diff view (showing what changed between syncs). Per-cookie editing. Other-domain support beyond the existing YouTube + optional accounts.google.com.

---

## Phase 8 — Content scripts: shared `<af-pill>` + 5 site adapters

**Goal:** On YouTube / Instagram / X (Twitter) / TikTok / Reddit pages, an AFFiNE-styled "Save to AFFiNE" pill is injected at site-specific anchor points (per spec §4.4). On click, it captures the canonical per-item URL via the same `capture` message handler the popup uses. Pills are Shadow-DOM-encapsulated so host CSS can't break them.

**Files:**
- Create: `browser-extension/content/_shared/pill.js` (`<af-pill>` Custom Element: composed from a `af-button`-style internal button; click handler messages background; success → 1.5s checkmark animation; failure → 1.5s red x)
- Create: `browser-extension/content/_shared/pill.css` (Shadow DOM stylesheet, imports tokens via `adoptedStyleSheets`)
- Create: `browser-extension/content/youtube.js` (anchor: `ytd-watch-metadata #actions`; canonicalizer: strip playlist params, keep `?v=<id>`; observer for SPA route changes)
- Create: `browser-extension/content/instagram.js` (anchor: `article[role=presentation]` per post; canonicalizer: extract `/p/<id>/` or `/reel/<id>/` from `<a>` permalink; observer for feed scroll)
- Create: `browser-extension/content/twitter.js` (anchor: `article[data-testid=tweet]`; canonicalizer: extract `/<user>/status/<id>` from the timestamp link; observer for feed scroll; covers x.com + twitter.com)
- Create: `browser-extension/content/tiktok.js` (anchor: `div[data-e2e=recommend-list-item-container]`; canonicalizer: extract `@<user>/video/<id>`; observer for FYP scroll)
- Create: `browser-extension/content/reddit.js` (anchor: `shreddit-post` (new Reddit) or `[data-testid=post-container]` (legacy fallback); canonicalizer: extract permalink; covers reddit.com + old.reddit.com)
- Modify: `browser-extension/manifest.json` (add `content_scripts` declarations for the five matches; `run_at: document_idle`)
- Create: `browser-extension/content/__tests__/canonicalizers.test.js` (URL canonicalization unit tests: 5+ cases per site)

**Acceptance:**
- On `youtube.com/watch?v=...&list=...&index=...` → pill renders next to native Save/Share within 5s; click captures `https://www.youtube.com/watch?v=<id>` (no playlist params, no timestamp).
- On YouTube SPA navigation (clicking another video) → pill re-renders for the new video without page reload (observer working).
- On `instagram.com/` feed → hovering each post (or reel in feed) reveals the pill; click captures the `/p/<id>/` or `/reel/<id>/` URL.
- On `x.com/<user>` timeline → hovering each tweet reveals the pill; click captures the `/<user>/status/<id>` URL.
- On `tiktok.com/` FYP → hovering each card reveals the pill; click captures `@<user>/video/<id>`.
- On `reddit.com/r/<sub>` → hovering each post reveals the pill; click captures the comments-permalink.
- Visual: every pill is the same AFFiNE-blue rounded "Save to AFFiNE" component (Shadow-DOM-encapsulated). Verified by setting `body { all: initial !important }` in DevTools — pill remains correctly styled.
- Anchor-not-found behavior: each script logs a single `[AFFiNE Capture] anchor not found on <site>` warning if its observer hasn't found a match within 10s, then stops.
- No double-pills: re-running the observer over a page with an existing pill doesn't add a second.
- All 25+ canonicalizer unit tests pass (5 sites × 5+ URL variants each).

**Out of scope:** Saving DOM-extras as `shared_text` (e.g., the IG caption — server's extractors handle that). Customizing the pill per site (one component, one look). Twitter Threads. Instagram Stories (different DOM, ephemeral content — defer). YouTube Shorts (anchor differs — covered as a v0.3 follow-up unless time permits).

---

## Phase 9 — Polish + acceptance pass

**Goal:** Run every acceptance criterion in the spec end-to-end. Catch regressions and rough edges. Refresh icons. Update README. Tag v0.2.0.

**Files:**
- Modify: `browser-extension/icons/icon-{16,32,48,128}.png` (rebrand from cookie-only to multitool — simple AFFiNE-blue capture-arrow visual; Pillow script in `make-icons.py` per existing pattern)
- Modify: `browser-extension/README.md` (full rewrite for v0.2: install, setup, popup capture, context menu, content scripts list, options-page tour, troubleshooting, security model — preserving the v0.1 cookie-sync section as one feature among many)
- Modify: `browser-extension/manifest.json` → bump description to reflect multitool scope (e.g., "Save any web content to your self-hosted AFFiNE workspace. Also syncs YouTube cookies for cobalt / yt-dlp.")
- Optional: a single-page demo HTML (`browser-extension/demo.html`, not shipped but git-tracked) that mounts every Web Component for visual regression checks
- Walk through every checklist item in spec §8 and check them off in a handoff note (or commit message body)

**Acceptance:**
- Every checkbox in spec §8 ("Acceptance criteria (v0.2 ship gate)") passes when run manually on Chrome + Firefox.
- README covers: install (both browsers), first-run setup (URL + token), all four entry points (popup / context menu / pill / cookie-sync), troubleshooting (token rejected, anchor-not-found, badge meaning), security model.
- `manifest.json` description and name reflect multitool scope.
- Icons updated; no generic puzzle-piece in toolbar.
- Cookie subsystem regression: cobalt + yt-dlp still successfully fetch authenticated YT content (re-run a known-failing capture from before; verify success).
- Tag `git tag v0.2.0` in `portainer-stack/`.

**Out of scope:** Inter font bundling (v0.3). Dark mode (v0.3). Extension store submission (v0.3+). Keyboard shortcut via `commands` API (v0.3 stretch). Multi-account / multi-server. Push notifications.

---

## Per-phase task plans

Each phase above will receive its own task-level plan via `superpowers:writing-plans` immediately before execution. Suggested plan filenames:

- `docs/plans/2026-05-1X-phase-1-extension-refactor.md`
- `docs/plans/2026-05-1X-phase-2-capture-client-popup.md`
- `docs/plans/2026-05-1X-phase-3-context-menu.md`
- `docs/plans/2026-05-1X-phase-4-web-components.md`
- `docs/plans/2026-05-1X-phase-5-options-settings.md`
- `docs/plans/2026-05-1X-phase-6-options-history-detail.md`
- `docs/plans/2026-05-1X-phase-7-options-cookies-tab.md`
- `docs/plans/2026-05-1X-phase-8-content-scripts.md`
- `docs/plans/2026-05-1X-phase-9-polish-and-ship.md`

Phases 4 (Web Components), 5 (Settings), 6 (History/Detail), 7 (Cookies tab) and 8 (content scripts) are all candidates for **parallel subagent execution** once Phase 1 + Phase 2 are done — they touch disjoint file sets and only share the `lib/` core.
