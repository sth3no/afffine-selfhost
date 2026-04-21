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

## Reverse proxy

`compose.yaml` only exposes the AFFiNE port on the host. For public
deployments, put it behind Caddy/Traefik/nginx, terminate TLS there, and set
`AFFINE_SERVER_EXTERNAL_URL=https://your.domain`.

## Updating

1. `git pull` the stack repo (or re-upload).
2. Re-run `./prepare.sh` if `affine-mcp-agent` changed.
3. In Portainer, **Pull and redeploy** (enable "Re-pull image" for the AFFiNE
   image; enable "Re-build" to pick up agent code changes).
