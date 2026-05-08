# Affine YT Cookie Sync — browser extension

A tiny MV3 extension (Chrome 88+ / Firefox 109+) that pushes your current
YouTube cookies to a self-hosted Affine ingest service. Once installed,
it works in the background — `cobalt`, `yt-dlp`, and `youtube-transcript-api`
on the server side then bypass YouTube's bot detection (no more
`error.api.youtube.login` / "sign in to confirm you're not a bot" /
cloud-IP-blocked transcript fetches).

## What it does

1. Reads cookies from `*.youtube.com` via the `chrome.cookies` API
   (works on **httpOnly** cookies — that's the whole point of this
   extension; page JS can't see those, the extension API can).
2. Formats them as Netscape `cookies.txt` (the format yt-dlp + cobalt
   both consume natively).
3. POSTs to `${ingestUrl}/youtube/cookies` with a bearer token.

Triggers:
- On install (first sync).
- Every time YouTube cookies change in your browser, debounced to 30s
  (so navigation doesn't hammer the endpoint).
- Daily safety-net alarm (every 24h).
- Manual "Sync now" from the toolbar popup.

## What it doesn't do

- It is NOT signed and NOT in any extension store. Install via "Load
  unpacked" / "Load Temporary Add-on". For personal use only.
- It does NOT scrape `accounts.google.com` cookies (only
  `*.youtube.com` host_permissions). Some heavily-restricted videos
  may still fail.
- It does NOT alert you when cookies go stale. If captures suddenly
  start failing, log into YouTube in your browser and the next page
  navigation will fire `cookies.onChanged` → resync.

## Install

### Chrome / Edge / Brave / Arc

1. Go to `chrome://extensions/`
2. Top-right toggle: **Developer mode** ON
3. **Load unpacked** → select this `browser-extension/` folder
4. Click the puzzle-piece icon in the toolbar, pin "Affine YT Cookie Sync"
5. Click the extension icon → **Settings** → fill in:
   - **Ingest base URL**: e.g. `https://ingest.example.com:3200`
     (use HTTPS for anything other than localhost)
   - **Bearer token**: the same `INGEST_API_TOKEN` your iOS share uses
6. Click **Save** then **Sync now** to push cookies immediately

### Firefox

1. Go to `about:debugging#/runtime/this-firefox`
2. **Load Temporary Add-on…** → select `manifest.json` from this folder
3. Open a new tab, go to `about:addons` → find "Affine YT Cookie Sync"
   → **Preferences**, fill in URL + token (same as above)
4. Note: Firefox temporary add-ons are unloaded on browser restart.
   For persistent install, package + sign via `web-ext sign` — out of
   scope for this README.

## Verify it works

After clicking "Sync now":

```bash
docker logs affine_ingest --tail 20 --since 1m | grep "youtube cookies"
```

You should see one log line:

```
{"level":"INFO", "msg":"youtube cookies uploaded", "byte_count":12345, ...}
```

`byte_count` confirms the file landed; the value (no cookie content)
is what the server logs.

Also retry a previously-failing YT capture:

```bash
curl -X POST -H "Authorization: Bearer $INGEST_API_TOKEN" \
  $INGEST_BASE/captures/<id>/retry
docker logs -f affine_ingest --since 1m
```

You should now see cobalt succeed (no `error.api.youtube.login`) and
youtube-transcript-api succeed (no IP-block error). The doc body will
contain the real Whisper-transcribed audio rather than the
"Unavailable" placeholder.

## Security notes

- The bearer token sits in `chrome.storage.local`. Chrome's storage
  is encrypted at rest only on some configurations; Firefox encrypts
  via the OS keychain. Treat it as roughly equivalent to a password
  in your password manager.
- The extension reads ALL cookies for `youtube.com` (including
  httpOnly ones — that's required to capture session tokens). It
  does NOT read cookies from any other domain.
- Cookies are POSTed over HTTPS only (the options page rejects plain
  HTTP unless the host is `localhost`).
- The server writes cookies to a tmpfs volume with chmod 600 — they
  never hit disk and are wiped on container restart.
- Cookie body is **never** logged on the server side. Only `byte_count`.

## Icons

`icons/` contains 4 placeholder PNGs (16/32/48/128 px) generated via
Pillow. Replace with branded artwork by overwriting the files in place.
The script that generates them lives at the top of `make-icons.py`
(see git history) — re-run if you change the source design.
