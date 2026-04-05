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

## Reverse proxy

`compose.yaml` only exposes the AFFiNE port on the host. For public
deployments, put it behind Caddy/Traefik/nginx, terminate TLS there, and set
`AFFINE_SERVER_EXTERNAL_URL=https://your.domain`.

## Updating

1. `git pull` the stack repo (or re-upload).
2. Re-run `./prepare.sh` if `affine-mcp-agent` changed.
3. In Portainer, **Pull and redeploy** (enable "Re-pull image" for the AFFiNE
   image; enable "Re-build" to pick up agent code changes).
