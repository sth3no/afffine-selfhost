# AFFiNE + MCP + Ingest — Portainer Stack

Self-hosts the full AFFiNE workspace, the AFFiNE MCP server
([DAWNCR0W/affine-mcp-server](https://github.com/DAWNCR0W/affine-mcp-server),
official image) and the `ingest` capture service as one Portainer stack.

## Contents

```
portainer-stack/
├── compose.yaml        # affine, postgres, redis, manticore, mcp_server, mcp_internal, ingest, cobalt, ...
├── .env.example        # Environment template
├── ingest/             # Python capture service (built by the stack)
├── browser-extension/  # Capture extension (Chrome) + browser-extension-firefox/
└── docs/
```

## Deploying in Portainer

1. Portainer → **Stacks → Add stack → Repository**.
2. Repository URL + reference + compose path (`compose.yaml`).
3. Under **Environment variables**, paste the contents of `.env.example` and
   fill in real values (see below).
4. Deploy. `ingest` is built from `./ingest`, so use the Repository method
   (the Web editor cannot build local contexts).

## First-run workflow

The MCP services need an AFFiNE workspace ID and a service account, which can
only be set up **after** AFFiNE is running.

1. Deploy the stack with `AFFINE_WORKSPACE_ID`, `AFFINE_MCP_EMAIL` and
   `AFFINE_MCP_PASSWORD` left blank. The MCP servers log that they are
   unconfigured until step 5.
2. Open `http://<your-host>:3010`, create the admin account, create or open a
   workspace.
3. Create a **dedicated account for the MCP services** (e.g. `mcp-bot@…`,
   Admin panel → Accounts) with a strong password and invite it to the
   workspace (member/editor is enough).
4. Copy the workspace ID from the URL: `/workspace/<THIS-ID>/...`.
5. Set up an OAuth issuer for the public MCP endpoint (see
   [MCP servers](#mcp-servers)), then fill in `AFFINE_WORKSPACE_ID`,
   `AFFINE_MCP_EMAIL`, `AFFINE_MCP_PASSWORD`, `AFFINE_MCP_HTTP_TOKEN`
   (`openssl rand -hex 32`), `AFFINE_MCP_PUBLIC_BASE_URL`,
   `AFFINE_OAUTH_ISSUER_URL` and `AFFINE_OAUTH_ALLOW_SERVICE_WRITES=true`,
   then **Update the stack**.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `AFFINE_REVISION` | no (default `stable`) | AFFiNE image tag. |
| `PORT` | no (default `3010`) | Host port for AFFiNE. |
| `AFFINE_SERVER_EXTERNAL_URL` | yes in prod | Public URL behind your reverse proxy. |
| `DB_USERNAME` / `DB_PASSWORD` / `DB_DATABASE` | yes | Postgres credentials. **Set a real password.** |
| `MAILER_*` | no | SMTP config for invites/password reset. Leave blank to skip. |
| `AFFINE_WORKSPACE_ID` | after first login | Target workspace for the MCP services. |
| `AFFINE_MCP_EMAIL` / `AFFINE_MCP_PASSWORD` | after first login | Dedicated AFFiNE account both MCP servers sign in as. |
| `AFFINE_MCP_PUBLIC_BASE_URL` / `AFFINE_OAUTH_ISSUER_URL` | yes | Public HTTPS URL of `mcp_server` (without `/mcp`) and its OAuth issuer. |
| `AFFINE_OAUTH_ALLOW_SERVICE_WRITES` | for writes | `true` lets OAuth callers use write tools (as the service account). |
| `AFFINE_MCP_HTTP_TOKEN` | yes | Bearer secret of `mcp_internal` (used by `ingest` and scripts). |
| `INGEST_SOURCES_ROOT` | no (default `Sources`) | Sidebar path the ingest platform folders live under, e.g. `Agenti/Sources`. |
| `TZ` | no (default `UTC`) | Timezone of the containers. |

## Persistence

Named volumes (managed by Portainer):

- `postgres_data` — database
- `redis_data` — redis dump
- `affine_storage` — uploaded blobs/attachments (`/root/.affine/storage`)
- `affine_config` — AFFiNE runtime config (`/root/.affine/config`)

Back these up before upgrading `AFFINE_REVISION`.

## Connecting from another stack (n8n, etc.)

AFFiNE joins two Docker networks:

- `affine_net` — private, internal to this stack (postgres, redis, MCP servers, ingest).
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

Use that as the base URL in the HTTP Request node. AFFiNE 0.27 no longer
issues `ut_…` personal access tokens: for MCP from n8n, connect to the public
`mcp_server` endpoint over OAuth, or attach n8n to `affine_net` and call
`http://mcp_internal:3000/mcp` with `Authorization: Bearer <AFFINE_MCP_HTTP_TOKEN>`.

### Quick sanity check

From the Portainer host:

```bash
docker network inspect shared_net --format '{{range .Containers}}{{.Name}} {{end}}'
```

You should see both `affine_server` and your n8n container listed.

## MCP servers

Both run the official `ghcr.io/dawncr0w/affine-mcp-server` image (~107 tools:
docs, edgeless canvas, databases, templates, tags, collections, organize
folders, journals) and sign in to AFFiNE as the dedicated service account
(`AFFINE_MCP_EMAIL` / `AFFINE_MCP_PASSWORD`, sessions renewed automatically).

| Service | Auth | Reachable at | For |
|---|---|---|---|
| `mcp_server` | OAuth (`AFFINE_MCP_AUTH_MODE=oauth`) | `${MCP_SERVER_PORT:-3300}` on the host, behind your HTTPS proxy | claude.ai / ChatGPT connectors, Claude Code, Codex, any OAuth MCP client |
| `mcp_internal` | Bearer `AFFINE_MCP_HTTP_TOKEN` | `http://mcp_internal:3000/mcp` (stack network), `127.0.0.1:${MCP_INTERNAL_PORT:-3301}` | `ingest`, admin scripts over an SSH tunnel |

### OAuth issuer requirements

`mcp_server` publishes `/.well-known/oauth-protected-resource` (pointing at
`AFFINE_OAUTH_ISSUER_URL`) and answers unauthenticated requests with `401` +
`WWW-Authenticate`, so clients discover everything themselves. The issuer must:

- publish OAuth/OIDC discovery with a `jwks_uri` and sign **JWT** access tokens (RS/ES/PS algorithms);
- put `<AFFINE_MCP_PUBLIC_BASE_URL>/mcp` (or the base URL) into `aud` and `mcp` into `scope`;
- ideally allow **dynamic client registration** (RFC 7591), so connectors register themselves.
  The claude.ai callback is `https://claude.ai/api/mcp/auth_callback`; CLI clients use `http://localhost:<port>/…`.

Keycloak works well — but **gate the audience by a role**, never hand it to
every token: with anonymous dynamic registration anyone can register a
`client_credentials` client, and a hardcoded *Audience* mapper would give its
service account full MCP access without any user login. Instead:

- create a bearer-only client named after the resource (`https://mcp.example.com/mcp`) with a client role `access`, and grant that role only to the users who may use MCP;
- make a client scope `mcp` a realm default and give it a *scope mapping* to that role — the built-in *audience resolve* mapper (scope `roles`) then adds the client to `aud` only for tokens that carry the role;
- limit the anonymous client-registration policy *Trusted Hosts* to the redirect hosts above (without requiring the sender to match, since connectors register from their own servers) and lower *Max Clients*.

Check it: an anonymously registered `client_credentials` client must get a
token without the MCP audience, and `mcp_server` must answer it with `401`. Serve discovery also at the RFC 8414
path-insert URL (`/.well-known/oauth-authorization-server/realms/<realm>`) on
the issuer host — some clients try it first.

Every OAuth caller acts as the same service account. Write tools need
`AFFINE_OAUTH_ALLOW_SERVICE_WRITES=true` (with the default `full` profile the
server refuses to start without it).

### Recovery

If someone revokes the service account's sessions (password change, "sign out
everywhere"), restart both servers: `docker restart affine_mcp_server affine_mcp_internal`.

## Ingest Service

Python/FastAPI sidecar that captures URLs from the iOS share extension
and files them into AFFiNE under `${INGEST_SOURCES_ROOT:-Sources}/`. Talks to
AFFiNE through `mcp_internal`. Ports `${INGEST_PORT:-3200}` on the host.

### What it does

1. iOS share sheet POSTs a URL to `/capture`.
2. Service creates a stub doc in `Sources/<group>/<platform>/` (always within 500ms) and returns 202 with the doc URL — iOS shows it immediately.
3. Background asyncio worker picks the row up:
   - Extracts content (yt-dlp / markitdown / oEmbed / reddit JSON depending on URL host).
   - Classifies into a topic via Claude Haiku 4.5 with prompt caching.
   - Uses cosine-similarity on folder-name embeddings (OpenAI text-embedding-3-small) to dedup near-duplicate folders ("Cooking" → existing "Recipes").
   - Moves the doc into the topic folder + appends the extracted body via `mcp_internal`.

### Required environment variables

| Variable | Purpose |
|---|---|
| `INGEST_API_TOKEN` | Bearer token iOS sends. Generate: `openssl rand -hex 32`. |
| `ANTHROPIC_API_KEY` | Classifier (Haiku 4.5) + renderer (Sonnet 4.6). |
| `AFFINE_MCP_HTTP_TOKEN` | Bearer secret of `mcp_internal`. |
| `INGEST_SOURCES_ROOT` | Sidebar path of the platform folders (default `Sources`). |
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
- `mcp_internal` unhealthy. Check `docker compose ps mcp_internal` and `GET /health/deep` on ingest.

**Capture stuck at `failed`**
- Look at `error` field in the capture detail. Common causes:
  - `OPENAI_API_KEY` missing → Whisper or embedding call failed. Fix env, re-deploy.
  - `ANTHROPIC_API_KEY` missing → classifier failed.
  - URL behind login → extractor returned empty body. Manual workaround: capture as `shared_text` instead of URL, or skip.
- After 3 retries (60s, 5min, 30min backoff) the row stays at `failed` until you `POST /captures/{id}/retry` manually.

**Doc landed in `Sources/<platform>/` root instead of a topic folder**
- Classifier returned `confidence < 0.6`. Check `classifier_reasoning` in the detail and move the doc by hand (or with an MCP client).

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
2. In Portainer, **Pull and redeploy** (enable "Re-pull image" for the AFFiNE
   and MCP images; `ingest` is rebuilt from source).
