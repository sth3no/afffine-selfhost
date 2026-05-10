# AFFiNE Capture — browser extension

A multitool extension for your self-hosted AFFiNE ingest service. Two
subsystems share one extension:

1. **Capture** — send any web content to your AFFiNE workspace via
   `POST /capture`:
   - **Toolbar popup** — one-click "Save to AFFiNE" on any tab.
   - **Right-click menu** — Save page / link / selection / image to AFFiNE.
   - **In-page pills** — hover a post on YouTube, X (Twitter), TikTok,
     or Reddit to get a "Save to AFFiNE" button on each item.
2. **YouTube cookie sync** — keeps YouTube cookies fresh on the server so
   cobalt + yt-dlp can fetch authenticated content (the v0.1 feature,
   preserved verbatim).

This extension is the desktop sibling of the iOS Capture app
([spec](../docs/ios-app-spec.md)). It uses the same `INGEST_API_TOKEN` and
the same `/capture` API contract.

---

## Quick start

1. Pull this repo on your machine.
2. **Chrome / Edge / Brave / Arc**: `chrome://extensions/` → toggle
   **Developer mode** ON → **Load unpacked** → select this
   `browser-extension/` folder. Pin the extension from the puzzle-piece menu.
3. **Firefox / Zen / LibreWolf** (any Firefox fork): use the sibling
   `browser-extension-firefox/` folder instead — same code, but the manifest
   uses `background.scripts` instead of MV3 `service_worker` (some Firefox
   builds, including current Zen, ship with `service_worker` disabled).
   Open `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on…**
   → select `browser-extension-firefox/manifest.json`. Temporary add-ons
   unload on restart; for persistent install, sign with `web-ext` — out
   of scope here.
4. Click the extension icon → "Open AFFiNE Capture →" in the popup footer
   → fills the URL + token under **Settings**:
   - **Ingest base URL**: `https://ingest.example.com:3200` (HTTPS required
     unless host is `localhost`).
   - **Bearer token**: same `INGEST_API_TOKEN` your iOS share / curl uses.
5. Click **Test connection** → green dot + version. Click **Save**.
6. Visit any web page → click the toolbar icon → **Save to AFFiNE** → row
   appears in your workspace under `Sources/<group>/<platform>` within ~60s.

---

## Capture surfaces

### Toolbar popup
- One primary button captures the active tab (URL + title).
- Status row shows ⏳ Capturing → ✓ Saved · Open ↗ → auto-closes after 2s.
- Footer shows cookie sync status + a deep-link to the options page.

### Right-click menu
On any page, right-click → 4 context-aware items:
- **Save page to AFFiNE** — captures the current URL.
- **Save link to AFFiNE** — captures the link's `href`, not the page URL.
- **Save selection to AFFiNE** — sends highlighted text as `shared_text`.
- **Save image to AFFiNE** — captures the image's `src` URL.

A native `chrome.notifications` toast appears with the result. Click the
toast body to open the AFFiNE doc.

### In-page pills (4 sites)

| Site | Anchor | Capture URL |
|---|---|---|
| YouTube | inline next to native Save/Share row on a watch page | `youtube.com/watch?v=<id>` (no playlist params) |
| Twitter / X | per tweet on hover | `x.com/<user>/status/<id>` |
| TikTok | per FYP card on hover | `tiktok.com/@<user>/video/<id>` |
| Reddit | per post on hover | `reddit.com/r/<sub>/comments/<id>/<slug>/` |

Pills are Shadow-DOM-rooted — host-page CSS can't break them, and our styles
can't leak into the host page.

---

## Options page

Three tabs (`chrome-extension://<id>/options/options.html`):

- **Settings** — server URL + bearer token + Test connection (green/red dot
  + version) + Save.
- **History** — last 50 captures with platform icons + status badges + filter
  pills (All / Done / Failed / In progress) + hover Retry/Delete. Click a
  row → Detail view with status timeline (queued → extracting → classifying
  → filing → done), classifier reasoning, topic-path breadcrumb, and Open /
  Retry / Delete actions.
- **Cookies** — last sync (browser side) + server verdict (fresh / stale /
  missing) + manual Sync now + extended-scope opt-in for
  `accounts.google.com` cookies (helps with age-gated and members-only
  videos).

---

## YouTube cookie sync (v0.1 feature, preserved)

Pushes cookies from the `*.youtube.com` scope to `${ingest_url}/youtube/cookies`
so cobalt + yt-dlp + youtube-transcript-api can fetch authenticated content.

Triggers:
- On install.
- Every time YouTube cookies change (debounced 30 s).
- Daily safety-net alarm.
- Manual "Sync now" from the **Cookies** tab.

Server-side, the cookie file lands on a tmpfs volume with chmod 600. Cookie
content is **never** logged — only `byte_count` and freshness metadata.

A red `!` badge on the toolbar means either:
- the server's cookie file is older than 24 h ("stale"), or
- the file is missing (server tmpfs lost on container restart, or token
  rejected).

Click the icon → the popup tells you which.

### Extended scope (opt-in)

Open **Cookies** tab → tick **"Also include `accounts.google.com` cookies"**.
The browser asks for permission. This covers age-gated / members-only / some
music-with-territory-restrictions videos that auth via Google's central
account domain. Untick to revoke (the extension calls
`chrome.permissions.remove`, so site access is genuinely removed — verify
in `chrome://extensions/` → Details → Site access).

---

## Security model

- HTTPS-only POST (rejected if request is plain HTTP and origin isn't `localhost`).
- Bearer token in `chrome.storage.local`. Treat as roughly equivalent to a
  password manager entry.
- Cookies POSTed via `text/plain`; capture payloads via `application/json`.
- Cookie body is **never** logged on the server. Capture payload is logged
  only at the URL level (no extracted content).
- Content scripts run in 5 sites only (declared `host_permissions`). The
  extension does NOT scrape or persist any page content; it only reads the
  canonical URL when the user clicks the pill.

---

## Architecture

```
browser-extension/
├── manifest.json                 # MV3, 5 content_scripts entries
├── background.js                 # Slim router (~50 lines)
├── lib/                          # Shared core
│   ├── api.js                    # HTTP client, IngestError, health()
│   ├── storage.js                # Typed chrome.storage helpers
│   ├── badge.js                  # Toolbar badge state
│   ├── icons.js                  # SVG library + platform mapping
│   └── design-tokens.css         # AFFiNE design system as CSS variables
├── cookies/                      # v0.1 cookie sync, relocated
│   ├── sync.js
│   └── netscape.js
├── capture/                      # Capture flow
│   ├── client.js                 # POST /capture, list/get/retry/delete
│   ├── handler.js                # performCapture — used by popup + context-menu
│   ├── context-menu.js           # 4 right-click items + notifications
│   └── payload.js                # Tab → CaptureRequest builder
├── content/                      # Content scripts (4 sites)
│   ├── _shared/                  # <af-pill> + canonicalizers + dispatch
│   ├── youtube.js
│   ├── twitter.js
│   ├── tiktok.js
│   └── reddit.js
├── popup/                        # Toolbar popup
├── options/                      # Options page (3 tabs)
│   └── components/               # 7 Web Components (af-button, af-input,
│                                 #   af-card, af-status-badge,
│                                 #   af-history-row, af-status-timeline,
│                                 #   af-breadcrumb)
└── icons/                        # 16/32/48/128 — placeholder; v0.3 rebrand
```

---

## Tests

```sh
cd browser-extension
npm install
npm test
```

86 unit tests cover:
- Storage helpers + 50-entry truncation
- HTTP client + 4 error kinds (`invalid_token`, `rate_limited`, `server`, `network`, `config`)
- Netscape cookies.txt formatter
- Capture payload builder (popup / link / selection / image)
- 7 Web Components (registration, attribute reflection, custom events)
- 5 per-site URL canonicalizers (25 assertions)

DOM injection (in-page pills, popup capture, context menu) is verified via
manual smoke — covered by the spec §8 acceptance checklist.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Red `!` badge on toolbar icon | Cookies stale/missing OR last capture failed (token rejected). Open popup to see which. |
| "Token rejected — open Settings" in popup | Server's `INGEST_API_TOKEN` rotated. Update under **Settings** → Save. |
| Pill not appearing on YouTube/IG/etc. | Site changed its DOM. Open the site, F12, "Inspect" on the extension service worker → look for `[AFFiNE Capture] anchor not found on <site>`. Selectors live in `content/<site>.js`. |
| "Couldn't reach ingest" in popup or notification | Network or wrong URL. Test connection in **Settings**. |
| Captures land but Whisper transcripts fail | Cookies might be stale. Open **Cookies** tab → Sync now. |

---

## What's deferred

- Inter font bundling (system stack used today)
- Dark mode
- Extension-store distribution (Chrome Web Store + Firefox AMO)
- Keyboard shortcut (`commands` API)
- Multi-account / multi-server config
- Push notifications when capture finishes
- Custom icons (current ones are v0.1 placeholders)
- File / image-blob upload (only image URLs in v0.2)

See [`docs/specs/2026-05-10-browser-extension-multitool-design.md`](../docs/specs/2026-05-10-browser-extension-multitool-design.md) §9 + §10.

---

## License & distribution

Personal-use, in-tree, unpacked install only. Not signed, not store-listed.
For multi-user distribution, package + sign via `web-ext sign` (Firefox) or
submit to Chrome Web Store — out of scope for v0.2.
