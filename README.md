# AFFiNE + MCP Agent — Portainer Stack

Self-hosts the full AFFiNE workspace plus the `affine-mcp-agent` scheduler
(daily digest, stale-doc detection, comment summaries) as one Portainer stack.

## Contents

```
portainer-stack/
├── compose.yaml        # Full stack (affine, postgres, redis, migration, mcp_agent)
├── .env.example        # Environment template
├── .dockerignore
├── prepare.sh          # Stages ../affine-mcp-agent sources into ./mcp-agent
└── mcp-agent/
    ├── Dockerfile      # Runs the scheduler under node:22-alpine + tsx
    ├── package.json    # (staged by prepare.sh)
    ├── package-lock.json
    ├── tsconfig.json
    └── src/            # (staged by prepare.sh)
```

## One-time preparation

The `mcp-agent/` build context needs the agent sources alongside the Dockerfile
so Portainer can build it. Run from this folder on any machine with bash:

```bash
cd portainer-stack
./prepare.sh
```

That copies `src/`, `package.json`, `package-lock.json`, and `tsconfig.json`
out of `../affine-mcp-agent/`. Commit the result to the repo Portainer will
pull from (or include it in the tarball you upload).

## Deploying in Portainer

### Option A — Git repository (recommended)

1. Push this `portainer-stack/` folder to a git repo (after running
   `prepare.sh` so `mcp-agent/src/` is committed).
2. Portainer → **Stacks → Add stack → Repository**.
3. Repository URL + reference + compose path (`portainer-stack/compose.yaml`).
4. Under **Environment variables**, paste the contents of `.env.example` and
   fill in real values (see below).
5. Deploy.

### Option B — Web editor upload

1. Run `./prepare.sh` locally.
2. Portainer → **Stacks → Add stack → Web editor**.
3. Paste `compose.yaml`.
4. Fill in environment variables from `.env.example`.
5. Deploy.

   Note: the Web editor cannot build local contexts. For Option B either
   pre-build the `affine-mcp-agent:local` image on the Docker host
   (`docker build -t affine-mcp-agent:local ./mcp-agent`) before deploying,
   or use Option A.

## First-run workflow

The MCP agent needs an AFFiNE workspace ID and access token, which can only
be generated **after** AFFiNE is running.

1. Deploy the stack with `AFFINE_WORKSPACE_ID` and `AFFINE_ACCESS_TOKEN` left
   blank. The `mcp_agent` container will start but log that it's unconfigured.
2. Open `http://<your-host>:3010`, create the admin account, create or open a
   workspace.
3. Go to **Workspace Settings → Integration → MCP Server → Generate Token**
   and copy the `ut_…` token (shown once).
4. Copy the workspace ID from the URL: `/workspace/<THIS-ID>/...`.
5. In Portainer, edit the stack, fill in both values, **Update the stack**.
   Portainer will recreate the `mcp_agent` container with the new env.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `AFFINE_REVISION` | no (default `stable`) | AFFiNE image tag. Use `canary` if you need MCP write operations. |
| `PORT` | no (default `3010`) | Host port for AFFiNE. |
| `AFFINE_SERVER_EXTERNAL_URL` | yes in prod | Public URL behind your reverse proxy. |
| `DB_USERNAME` / `DB_PASSWORD` / `DB_DATABASE` | yes | Postgres credentials. **Set a real password.** |
| `MAILER_*` | no | SMTP config for invites/password reset. Leave blank to skip. |
| `AFFINE_WORKSPACE_ID` | after first login | Target workspace for the MCP agent. |
| `AFFINE_ACCESS_TOKEN` | after first login | `ut_…` token generated in Workspace Settings. |
| `TZ` | no (default `UTC`) | Timezone for cron schedules inside the agent container. |

## What runs inside `mcp_agent`

`npx tsx src/scheduler.ts` — the node-cron scheduler in
`affine-mcp-agent/src/scheduler.ts`. It talks to AFFiNE over the internal
Docker network at `http://affine:3010` (no host port needed). To run a
single automation manually from the host:

```bash
docker exec -it affine_mcp_agent npx tsx src/automations/daily-digest.ts
docker exec -it affine_mcp_agent npx tsx src/automations/stale-docs.ts
docker exec -it affine_mcp_agent npx tsx src/automations/comment-summary.ts
```

## Persistence

Named volumes (managed by Portainer):

- `postgres_data` — database
- `redis_data` — redis dump
- `affine_storage` — uploaded blobs/attachments (`/root/.affine/storage`)
- `affine_config` — AFFiNE runtime config (`/root/.affine/config`)

Back these up before upgrading `AFFINE_REVISION`.

## Connecting from another stack (n8n, etc.)

AFFiNE joins two Docker networks:

- `affine_net` — private, internal to this stack (postgres, redis, mcp_agent).
- `shared_net` — **external**, for reaching AFFiNE from other Portainer stacks.

### One-time setup on the Docker host

Before deploying this stack, create the shared network once:

```bash
docker network create shared_net
```

(If you prefer a different name, set `SHARED_NETWORK=my_name` in the stack env.
The network must already exist on the host — Portainer will not create it.)

### In your n8n stack's compose file

Add the same external network to the n8n service:

```yaml
services:
  n8n:
    # ...your existing n8n config...
    networks:
      - default       # keep whatever n8n had
      - shared_net

networks:
  shared_net:
    external: true
```

Redeploy the n8n stack. From any n8n workflow, AFFiNE is now reachable at:

```
http://affine:3010
```

Use that as the base URL in the HTTP Request node. Auth is the same `ut_…`
token the MCP agent uses — generate it in AFFiNE's Workspace Settings →
Integration → MCP Server and pass it as a bearer header.

### Quick sanity check

From the Portainer host:

```bash
docker network inspect shared_net --format '{{range .Containers}}{{.Name}} {{end}}'
```

You should see both `affine_server` and your n8n container listed.

## MCP Extension Proxy (`mcp-ext`)

The stack ships with a second MCP server at port `3100` — `affine-mcp-ext` —
that both **fills gaps in AFFiNE's built-in MCP** (listing docs, comments,
members, history) and provides **write tools** for creating and editing
content. All clients (the scheduler, n8n, Claude Desktop, Cursor) should
connect here, not to AFFiNE's native MCP directly.

### Tools

**Reads** (GraphQL-backed):
`list_documents`, `get_workspace_info`, `list_workspace_members`,
`list_comments`, `list_document_history`, `get_document_info`,
`list_notifications`, `list_blobs` · **Native forwards:** `read_document`,
`keyword_search`, `semantic_search`.

**Writes** (Yjs CRDT + GraphQL):

| Tool | Purpose |
|---|---|
| `create_doc` | Create a new document, optionally with initial blocks. |
| `append_blocks` | Append blocks to a doc. Supports `afterHeading:"AI summary"` to anchor under a template section. |
| `update_block_text` | Replace the text/style of an existing block. |
| `delete_block` | Remove a block (and descendants) from a doc. |
| `set_doc_title` | Rename a doc. |
| `delete_doc` | Soft-delete (trash) a doc. Recoverable from UI. |
| `list_doc_blocks` | Inspect a doc's block tree (ids, flavours, text previews). |
| `find_doc_by_title` | Resolve a title to a docId. |
| `create_comment` / `resolve_comment` / `delete_comment` | Comment CRUD. |
| `create_reply` / `delete_reply` | Reply CRUD. |

**Organize sidebar — folder tree** (Yjs CRDT, table `db$folders`):

| Tool | Purpose |
|---|---|
| `list_folder_tree` | Read the entire Organize sidebar as nested JSON. Doc-link names are resolved to current doc titles. Use this BEFORE any structural change. |
| `create_folder` | Create a new folder (top-level or nested under `parentFolderId`). Returns the new `folderId`. |
| `rename_folder` | Rename an existing folder. Doc/tag/collection links can't be renamed (they inherit from the target). |
| `delete_folder` | Soft-delete a folder. Refuses if non-empty unless `cascade:true`. **Never deletes the underlying docs** — they remain in the workspace, just unfiled. |
| `move_folder` | Move a folder + entire subtree under a different parent. Refuses cycles. |
| `move_document` | File a doc into a folder. Consolidates duplicate links if the doc was previously linked elsewhere. Pass empty `folderId` to unfile. |

These mutate the `db$folders` Yjs sub-doc that backs AFFiNE's "Organize" sidebar. Same Socket.IO transport as the doc tools — see [folder-store.ts](mcp-ext/src/folder-store.ts) for the row schema.

### Writing rich content

Block `text` can be a plain string or an array of inline ops with formatting
and inline doc references (the `@DocName` pill):

```json
{
  "type": "paragraph",
  "text": [
    { "text": "Today I finished " },
    { "text": " ", "refDocId": "3fa85f64-5717-4562-b3fc-2c963f66afa6" },
    { "text": " and linked it to " },
    { "text": "planning", "bold": true }
  ]
}
```

### Example: AI daily summary

```jsonc
// 1. Find the target journal doc
find_doc_by_title({ "title": "Journal 2026-04-21" })
// → { matches: [{ id: "<docId>", title: "Journal 2026-04-21" }] }

// 2. Append the generated summary under the static heading
append_blocks({
  "docId": "<docId>",
  "afterHeading": "AI summary",
  "blocks": [
    { "type": "paragraph", "text": "Dneska hlavně..." },
    { "type": "list", "style": "bulleted", "text": [
        { "text": "Dodělal jsem write tools v " },
        { "text": " ", "refDocId": "<mcp-ext-docId>" }
    ]}
  ]
})
```

### Safety boundary

Write tools are scoped strictly to **content inside** the workspace.
The following are intentionally **not** exposed — AI clients cannot:

- modify workspace settings (`updateWorkspace`, `deleteWorkspace`)
- invite, remove, or change permissions of members
- manage invite links
- change per-doc roles or sharing
- permanently delete docs or blobs (only soft-trash)
- publish private docs to the web

Rotate `AFFINE_ACCESS_TOKEN` in AFFiNE → Settings → Integration → MCP Server
whenever you suspect leakage — tokens carry the creating user's full scope.

### How writes work

`mcp-ext` uses Yjs directly (no `@blocksuite/*` dependency, so it's not
coupled to a particular AFFiNE image version):

1. `GET /api/workspaces/:wsId/docs/:guid` returns the current Yjs binary.
2. The proxy loads it into an in-memory `Y.Doc`, mutates the block tree
   using AFFiNE's on-disk schema (`sys:flavour`, `sys:children`, `prop:text`
   as `Y.Text` deltas with `reference` attributes for doc links, …).
3. The diff vs. the pre-mutation state vector is pushed via the GraphQL
   `applyDocUpdates` mutation.

For `create_doc`, a fresh `Y.Doc` (`page` → `surface` + `note`) is
constructed and the new doc id is also registered in the workspace root
doc's `meta.pages` array.

## Exposing MCP over HTTPS (claude.ai connectors)

Hosted MCP clients — claude.ai custom connectors, ChatGPT, n8n Cloud —
only accept a **public `https://` URL with a valid certificate**. Neither
`mcp_ext` (3100) nor `mcp_server` (3300) terminates TLS, so their host
ports cannot be used as a connector URL directly.

Which endpoint to point at:

| | `mcp_server` | `mcp_ext` |
|---|---|---|
| Path | `/mcp` | `/` |
| Container | `mcp_server:3000` | `mcp_ext:3100` |
| Auth | `AFFINE_MCP_HTTP_TOKEN`, header **or** `?token=` | `AFFINE_ACCESS_TOKEN`, header only |
| Tools | 85 | 11 |

Use `mcp_server`. Its bearer guard also accepts the token as a query
parameter (`mcp-server/src/httpAuth.ts`), which matters because the
claude.ai connector dialog has no field for a request header — it offers
OAuth only. `mcp_ext` has no such fallback: it falls back to the
`AFFINE_ACCESS_TOKEN` env var instead (`mcp-ext/src/server.ts`), so
publishing it means **anyone who reaches the port is authenticated**. Keep
`MCP_EXT_BIND=127.0.0.1` unless you specifically need it on the LAN.

### If this host already runs a reverse proxy

Add one proxy host — `mcp.example.com` → `mcp_server:3000` (join the proxy
to `affine_net`, or target `127.0.0.1:3300`). Two settings are not optional:

- **Disable response buffering.** The Streamable HTTP transport streams
  `text/event-stream`; buffered, the client hangs on `initialize`.
  nginx: `proxy_buffering off;` · Caddy: `flush_interval -1`.
- **Pass `mcp-session-id` through** in both directions. The transport keys
  its session off that header.

### If it does not

`compose.yaml` ships an optional Caddy terminator that handles both, plus
Let's Encrypt issuance and renewal. It stays dormant until you opt in:

1. Point an A record for `MCP_PUBLIC_DOMAIN` at this host.
2. Open ports 80 and 443 (80 is required for the HTTP-01 challenge).
3. In the stack env set `COMPOSE_PROFILES=mcp-proxy`, `MCP_PUBLIC_DOMAIN`,
   and `ACME_EMAIL`.
4. Redeploy, then check issuance: `docker logs affine_mcp_proxy`.

### Adding the connector

URL — the token is `AFFINE_MCP_HTTP_TOKEN`:

```
https://mcp.example.com/mcp?token=<AFFINE_MCP_HTTP_TOKEN>
```

Leave the OAuth fields in the dialog empty; this server is in bearer mode,
and a client ID there would only make the connector attempt a flow it
cannot complete. If a connector already exists with a wrong URL, delete and
re-add it — "Reconnect" re-runs the OAuth handshake and will not change a
stored URL.

### Verifying before you touch the UI

From the Docker host:

```bash
curl -s http://localhost:3300/healthz

curl -s -X POST "http://localhost:3300/mcp?token=$AFFINE_MCP_HTTP_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

`initialize` must return `serverInfo`. Then repeat the same two calls
against `https://mcp.example.com/…` from a machine outside your network —
that is what proves DNS, TLS, and the proxy hop, which is where this
usually fails rather than in the MCP layer itself.

Failure modes worth knowing:

| Symptom | Cause |
|---|---|
| `Unauthorized: Invalid or missing token` | Token mismatch, or `?token=` was dropped by the proxy. |
| 404 on `/mcp` | Hit `mcp_ext` (its endpoint is `/`) or AFFiNE itself (3010) by mistake. |
| Connects, then hangs | Response buffering in the proxy — see above. |
| `Forbidden: Origin not allowed` | Browser-side origin check. Set `AFFINE_MCP_HTTP_ALLOWED_ORIGINS=https://claude.ai`. |

## Ingest Service

Python/FastAPI sidecar that captures URLs from the iOS share extension
and files them into AFFiNE under `Sources/`. Runs alongside `mcp_ext` +
`mcp_agent` in the same stack. Ports `${INGEST_PORT:-3200}` on the host.

### What it does

1. iOS share sheet POSTs a URL to `/capture`.
2. Service creates a stub doc in `Sources/<group>/<platform>/` (always within 500ms) and returns 202 with the doc URL — iOS shows it immediately.
3. Background asyncio worker picks the row up:
   - Extracts content (yt-dlp / markitdown / oEmbed / reddit JSON depending on URL host).
   - Classifies into a topic via Claude Haiku 4.5 with prompt caching.
   - Uses cosine-similarity on folder-name embeddings (OpenAI text-embedding-3-small) to dedup near-duplicate folders ("Cooking" → existing "Recipes").
   - Moves the doc into the topic folder + appends the extracted body via `mcp_ext`.
4. Weekly Sunday 03:00 UTC cron in `mcp_agent` (`sources-reorg`) sweeps `Sources/*` for over-15-doc leaf folders and proposes 2–5 sub-clusters via Claude Sonnet 4.6.

### Required environment variables

| Variable | Purpose |
|---|---|
| `INGEST_API_TOKEN` | Bearer token iOS sends. Generate: `openssl rand -hex 32`. |
| `ANTHROPIC_API_KEY` | Classifier (Haiku 4.5) + reorganizer (Sonnet 4.6). |
| `OPENAI_API_KEY` | Whisper API (audio transcription) + embeddings. |
| `INGEST_PORT` | Host port (default 3200). |
| `INGEST_BIND` | Bind interface (default 0.0.0.0; set 127.0.0.1 to restrict). |
| `MAX_TRANSCRIPT_MIN` | Whisper cost guard (default 30 min/video). |
| `REORG_THRESHOLD_DEFAULT` | Reorganizer threshold per leaf folder (default 15 docs). |

### Bring-up

```bash
# 1. Make sure your .env has the keys above (copy from .env.example).
# 2. Build + deploy the stack via Portainer (Stacks → Update).
# 3. Verify the service comes up healthy:
docker compose ps                       # affine_ingest = healthy
curl http://localhost:3200/health       # {"ok": true, ...}

# 4. End-to-end smoke (sends 3 URLs, checks they land in AFFiNE):
INGEST_BASE=http://localhost:3200 \
INGEST_API_TOKEN=$INGEST_API_TOKEN \
bash ingest/scripts/smoke.sh
```

### Inspecting captures

```bash
# Recent captures (any status)
curl -H "Authorization: Bearer $INGEST_API_TOKEN" \
  http://localhost:3200/captures?limit=20 | jq

# Single capture detail
curl -H "Authorization: Bearer $INGEST_API_TOKEN" \
  http://localhost:3200/captures/<id> | jq

# Retry a failed capture
curl -X POST -H "Authorization: Bearer $INGEST_API_TOKEN" \
  http://localhost:3200/captures/<id>/retry

# In Postgres directly
docker exec -it affine_postgres psql -U affine -d affine_ingest \
  -c "SELECT id, platform, status, topic_path, retry_count FROM captures ORDER BY created_at DESC LIMIT 20;"
```

### Logs

Structured JSON, one line per record. Pipe through jq:

```bash
docker logs affine_ingest --since 10m -f | jq -c '. | {ts, level, msg, capture_id, step, topic}'
```

Lines emitted while a capture is being processed include `capture_id` so you can grep a single flow:

```bash
docker logs affine_ingest --since 1h | jq -c 'select(.capture_id == "01J-X")'
```

### YouTube cookies

Captures of YouTube videos that hit `Sign in to confirm you're not a bot`
or get blocked transcripts are fixed by uploading the user's browser-side
YouTube cookies via the [Affine YT Cookie Sync](browser-extension/) MV3
extension. Install once, configure ingest URL + token, and the extension
auto-syncs on every YT cookie change.

**Important:** Cookies alone are NOT enough on cloud / VPS IPs since
late 2024 — YouTube enforces a Proof-of-Origin (PO) token on most
clients. The stack ships a `yt_session_server` sidecar
([yt-session-generator](https://github.com/imputnet/yt-session-generator))
that mints PO tokens via BotGuard, plus a tiny `yt_session_adapter`
that translates between cobalt's `POST /get_pot` and the session
server's `GET /token` (the two upstreams drifted apart). yt-dlp uses
bot-resistant extractor clients (`web_embedded`, `tv_simply`) that
don't require PO tokens. With both in place, cookies + PO token +
a logged-in YT account is the working combination.

**Cobalt's startup race — handled automatically by `cobalt_watchdog`:**

Cobalt reads `/cookies/cobalt.json` exactly ONCE at startup, and
fetches the PO token from `yt_session_server` exactly ONCE at startup.
Neither reloads. On a fresh stack deploy the cookies file doesn't
exist yet (the extension syncs AFTER the stack is up), so cobalt's
first boot is always cookieless.

The `cobalt_watchdog` sidecar service handles this: it polls the
shared volume every 10s, and the first time it sees a valid
`cobalt.json` (file present + non-empty + contains `"youtube"`), it
runs `docker restart affine_cobalt` exactly once. The restarted cobalt
loads both cookies and poToken cleanly.

**On a fresh deploy you only have to:**

1. Wait for the stack to come up healthy.
2. Click **Sync now** in the extension popup.
3. Within ~10 seconds, watchdog restarts cobalt automatically. Confirm:
   ```bash
   docker logs affine_cobalt_watchdog 2>&1 | tail -5
   # expect: [watchdog] cobalt.json valid; restarting affine_cobalt

   docker logs affine_cobalt 2>&1 | tail -20 | grep -iE 'cookies|poToken|visitor'
   # expect: [✓] cookies loaded successfully!
   #         (and a successful poToken fetch — exact wording varies)
   ```

After that, cobalt's in-memory cookie state self-rotates as YouTube
sends `Set-Cookie` headers — no more restarts. The watchdog stays
running and only restarts cobalt again if the cookies file goes
missing then comes back (e.g. another full stack recreate).

**Security note:** the watchdog mounts `/var/run/docker.sock`. That's
root-equivalent access on the host. Acceptable for a single-user
self-hosted stack — the stack already holds your YT auth — but
something to be aware of if you ever expose this stack to other
users.

**Quick verification:**

```bash
docker logs affine_cobalt 2>&1 | grep -iE 'cookies|\[!\]'
# expect: [✓] cookies loaded successfully!
```

### Residential proxy (the cloud-IP unlock)

**Cookies + PO token + perfect config aren't enough on their own.**
YouTube blocks most cloud / VPS IPs from generating PO tokens via
BotGuard AND from fetching auto-captions via youtube-transcript-api,
regardless of cookie quality. From a Hetzner / AWS / Google Cloud /
DigitalOcean IP you'll see `error.api.youtube.login` from cobalt and
`RequestBlocked` from transcript-api on niche / age-gated / tutorial
content.

The stack is wired to read a single env var `RESIDENTIAL_PROXY_URL`
and propagate it to **all three YT-touching containers** (`cobalt`,
`yt_session_server`, `ingest`). When set, traffic egresses via the
proxy; when empty, current direct-connection behavior is preserved.

Two ways to provide that residential proxy:

#### Option A: Self-hosted via home machine (recommended)

A spare Pi (3B+, 4, or 5 — anything with ethernet) running on your
home internet, joined to the same Tailscale network as your VPS.
Phase 13 video downloads can saturate ~750 GB/month — at $4-15/GB
on commercial residential proxies that's $200-1500/mo, while a Pi
costs $0 in bandwidth (it's your home plan) and $2/year in power.

Two host paths, same architecture:

- **Raspberry Pi (production-grade always-on)** — see
  [docs/setup/raspberry-pi-5-spec.md](docs/setup/raspberry-pi-5-spec.md)
  for shopping list and full setup walkthrough.
- **Windows 11 PC via WSL2 (instant testing path)** — see
  [docs/setup/windows-residential-tunnel.md](docs/setup/windows-residential-tunnel.md).
  Same script runs unchanged inside WSL2 Ubuntu. Caveat: tunnel
  drops when the PC sleeps; great for testing, not for 24/7.

**Quick path on a Linux box (Pi or any other always-on Linux machine):**

```bash
sudo bash scripts/pi-residential-tunnel-setup.sh
```
Installs Tailscale + gost (HTTP proxy on :8118), runs both as
systemd services. You'll authenticate Tailscale once via a browser
URL the script prints. The script reports the host's Tailscale IP
when done.

**On the VPS:**
```bash
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up --hostname=affine-vps
```
Same Tailscale account → both machines on the same private network.

**Then in your stack `.env`:**
```bash
RESIDENTIAL_PROXY_URL=http://<host-tailscale-ip>:8118
```
Redeploy. All YT traffic now egresses via your home internet,
making it indistinguishable from a real user watching from a phone.

#### Option B: Commercial residential proxy

If you don't have a Pi or always-on home box, sign up for a paid
residential proxy. Recommended at personal scale:
- **Webshare** — cheapest entry, 1 GB pay-as-you-go ~$3.50
- **Decodo** (formerly Smartproxy) — ~$4/GB, cleaner pool, better
  YT pass rate than Webshare
- **DataImpulse** — pay-as-you-go, credits don't expire, ~$3/GB

Audit your monthly bandwidth before picking. **Audio-only captures**
through proxy = ~5-10 MB/video → ~30 GB/month at 100/day → cheap
($40-120/mo). **Phase 13 video captures** = 100-500 MB/video →
150-750 GB/month → expensive ($600-3000/mo). At video-download scale,
self-hosted Pi is the only sustainable answer.

Format:
```bash
RESIDENTIAL_PROXY_URL=http://USER:PASS@gateway.provider.com:PORT
```

Don't paste these credentials into chat — set them directly in
Portainer's stack environment or your `.env` file.

#### Verify the proxy is active

```bash
docker exec affine_cobalt env | grep -i proxy
# expect: HTTP_PROXY=http://...   HTTPS_PROXY=http://...   NO_PROXY=...

docker exec affine_ingest python3 -c \
  'import urllib.request; print(urllib.request.urlopen("https://api.ipify.org",timeout=10).read().decode())'
# expect: your home IP (Option A) or a residential ISP IP (Option B)
# NOT: your VPS IP
```

If the egress IP is still your VPS, the proxy isn't being applied —
recheck `RESIDENTIAL_PROXY_URL` in the stack env and `docker compose
up -d` to force the containers to pick up the new env.

**Cookie diagnostic** (names only, never values — safe to share for debugging):

```bash
curl -H "Authorization: Bearer $INGEST_API_TOKEN" \
  "$INGEST_BASE/youtube/cookies/diagnostic" | jq .
```
Look for `__Secure-3PSID`, `SID`, `LOGIN_INFO` in the cookie names list — those are the auth tokens cobalt + yt-dlp need.

**Verify server-side freshness** (added in 12.5):

```bash
curl -H "Authorization: Bearer $INGEST_API_TOKEN" \
  "$INGEST_BASE/youtube/cookies/status"
# → {"exists": true, "age_seconds": 1342, "mtime": "...Z", "byte_count": 12345}
```

If `exists: false`, the tmpfs is empty — usually because the ingest
container restarted. Click **Sync now** in the extension popup to
repopulate. The popup also surfaces this state automatically as a red
toolbar badge + `Server: cookies missing` line.

### Troubleshooting

**Capture stuck at `queued`**
- Worker not running. Check `/health` — `worker_alive` should be `true`. If false, restart `affine_ingest`.
- `mcp_ext` unhealthy. Check `docker compose ps mcp_ext`.

**Capture stuck at `failed`**
- Look at `error` field in the capture detail. Common causes:
  - `OPENAI_API_KEY` missing → Whisper or embedding call failed. Fix env, re-deploy.
  - `ANTHROPIC_API_KEY` missing → classifier failed.
  - URL behind login → extractor returned empty body. Manual workaround: capture as `shared_text` instead of URL, or skip.
- After 3 retries (60s, 5min, 30min backoff) the row stays at `failed` until you `POST /captures/{id}/retry` manually.

**Doc landed in `Sources/<platform>/` root instead of a topic folder**
- Classifier returned `confidence < 0.6`. Check `classifier_reasoning` in the detail. The weekly reorganizer (Sunday 03:00 UTC) revisits these.

**Reorganizer didn't run**
- Check `mcp_agent` logs for the `Sources Reorg` cron entry on Sunday. Manual trigger:
  ```bash
  docker exec -it affine_mcp_agent npx tsx src/automations/sources-reorg.ts
  ```

**Same URL captured twice**
- Should not happen — `url_hash` is UNIQUE. The second POST returns the existing capture. If you see duplicates, check that URL normalization (`utm_*` strip, lowercase host) is working: `python -c "from src.models import normalized_url; print(normalized_url('https://...'))"` inside the container.

**Cost spike on Whisper**
- Set `MAX_TRANSCRIPT_MIN` lower (default 30). Long videos auto-skip transcription.
- Or rotate `OPENAI_API_KEY` to one with budget alerts.

## Reverse proxy

`compose.yaml` only exposes the AFFiNE port on the host. For public
deployments, put it behind Caddy/Traefik/nginx, terminate TLS there, and set
`AFFINE_SERVER_EXTERNAL_URL=https://your.domain`.

## Updating

1. `git pull` the stack repo (or re-upload).
2. Re-run `./prepare.sh` if `affine-mcp-agent` changed.
3. In Portainer, **Pull and redeploy** (enable "Re-pull image" for the AFFiNE
   image; enable "Re-build" to pick up agent code changes).
