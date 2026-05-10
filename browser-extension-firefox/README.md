# AFFiNE Capture — Firefox variant

This folder is the **Firefox / Zen / LibreWolf build** of the extension.
It mirrors `../browser-extension/` exactly, with one manifest difference:

```jsonc
// Chrome (../browser-extension/manifest.json):
"background": { "service_worker": "background.js", "type": "module" }

// Firefox (this folder):
"background": { "scripts": ["background.js"], "type": "module" }
```

Why: stock Firefox 121+ supports MV3 `service_worker`, but current Zen
(based on Firefox 150) ships with it disabled — the loader errors with
`background.service_worker is currently disabled. Add background.scripts.`
The event-page form works in all Firefox MV3 builds, so we use it here.

## Install (temporary)

`about:debugging#/runtime/this-firefox` → **Load Temporary Add-on…** →
select this folder's `manifest.json`. Unloads on browser restart; for
persistent install, package + sign with `web-ext`.

## Maintenance

This folder is currently maintained as a near-copy of `../browser-extension/`.
When you change anything in the Chrome folder, mirror the change here
(except the `background` block). Tests live in the Chrome folder only.

For full feature docs, see [`../browser-extension/README.md`](../browser-extension/README.md).
