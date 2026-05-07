# Phase 8 — Sources Reorganizer (mcp-agent extension)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Weekly cron-driven automation in the existing `affine-mcp-agent` (TypeScript/node-cron) that scans `Sources/` leaf folders, splits any with > REORG_THRESHOLD docs into 2–5 named sub-clusters via Claude Sonnet 4.6, executes the splits via existing folder MCP tools, and logs the run.

**Macro plan:** [`docs/plans/2026-05-06-ingest-service-macro-plan.md`](./2026-05-06-ingest-service-macro-plan.md) — Phase 8
**Spec:** [`docs/specs/2026-05-06-ingest-service-design.md`](../specs/2026-05-06-ingest-service-design.md) — §9

**Architecture:**
- Lives in `affine-mcp-agent/` (sibling repo, NOT `ingest/`). Staged into `portainer-stack/mcp-agent/` via `prepare.sh`.
- New automation `src/automations/sources-reorg.ts` reuses the existing `AffineMcpClient.callTool(...)` for the folder operations.
- Anthropic Sonnet 4.6 thin wrapper at `src/lib/anthropic.ts`.
- Scheduler registers a new cron job: `0 3 * * 0` (Sunday 03:00 UTC).
- Unit tests under `src/automations/__tests__/sources-reorg.test.ts` using **vitest** with mocks for both the MCP client and the Sonnet wrapper.
- Decisions log appended to `Sources/Operations/Logs/reorganizer-YYYY-MM-DD.md` after each run.

**Threshold:** default 15 docs per leaf folder. Phase 5 introduced `topics.yaml.reorg.default_threshold` — but the Python service owns that file; Phase 8 (TypeScript) reads its own `REORG_THRESHOLD` from env (default 15). Same number, different process.

---

## Task 1: Deps + vitest scaffolding

**Files:**
- Modify: `affine-mcp-agent/package.json` — add `@anthropic-ai/sdk`, `vitest` (devDep), `npm test` script
- Create: `affine-mcp-agent/vitest.config.ts`

- [ ] **Step 1.1: Modify `package.json`**

```json
{
  "name": "affine-mcp-agent",
  "version": "1.0.0",
  "description": "Automation agent that connects to AFFiNE's MCP server for scheduled workspace operations",
  "type": "module",
  "scripts": {
    "start": "tsx src/agent.ts",
    "daily-digest": "tsx src/automations/daily-digest.ts",
    "stale-docs": "tsx src/automations/stale-docs.ts",
    "comment-summary": "tsx src/automations/comment-summary.ts",
    "sources-reorg": "tsx src/automations/sources-reorg.ts",
    "scheduler": "tsx src/scheduler.ts",
    "test": "vitest run"
  },
  "dependencies": {
    "node-cron": "^3.0.3",
    "@anthropic-ai/sdk": "^0.40.0"
  },
  "devDependencies": {
    "tsx": "^4.19.0",
    "@types/node": "^22.0.0",
    "@types/node-cron": "^3.0.11",
    "typescript": "^5.6.0",
    "vitest": "^2.1.0"
  }
}
```

- [ ] **Step 1.2: Create `vitest.config.ts`**

```typescript
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['src/**/__tests__/**/*.test.ts'],
    globals: false,
  },
});
```

- [ ] **Step 1.3: Install + verify**

```bash
cd affine-mcp-agent && npm install
```

Expected: lockfile updates, `node_modules/@anthropic-ai/sdk` and `node_modules/vitest` present.

- [ ] **Step 1.4: Add a sanity test placeholder**

Create `affine-mcp-agent/src/__tests__/sanity.test.ts`:

```typescript
import { describe, it, expect } from 'vitest';

describe('sanity', () => {
  it('vitest works', () => {
    expect(1 + 1).toBe(2);
  });
});
```

Run: `npm test`. Expected: 1 passed.

- [ ] **Step 1.5: prepare.sh + commit**

`prepare.sh` already copies `package.json`, `package-lock.json`, `tsconfig.json`, and `src/`. The new `vitest.config.ts` is at the repo root next to `tsconfig.json` — it does NOT need to be in the production image (vitest is a dev tool only). Don't add it to prepare.sh.

```bash
cd portainer-stack && bash prepare.sh
git add mcp-agent/package.json mcp-agent/package-lock.json mcp-agent/src/__tests__/
git commit -m "$(cat <<'EOF'
chore(mcp-agent): add @anthropic-ai/sdk + vitest

Phase 8 prereq. Sonnet 4.6 wrapper (Task 2) and reorganizer (Task 3)
need the SDK; unit tests (Task 4) need vitest. Lockfile regenerated.

Phase 8 / Task 1 of docs/plans/2026-05-07-phase-8-reorganizer.md
EOF
)"
```

> Note: source files ALSO live in `affine-mcp-agent/` — that directory is NOT git-tracked. Phase 8's git history therefore only reflects what `prepare.sh` stages into `portainer-stack/mcp-agent/`. Keep both in sync; commit only the staged copies.

---

## Task 2: Anthropic Sonnet wrapper (`src/lib/anthropic.ts`)

Thin async wrapper around the SDK with the prompt assembly + JSON parsing for the reorganizer's split proposal.

**Files:**
- Create: `affine-mcp-agent/src/lib/anthropic.ts`
- Create: `affine-mcp-agent/src/lib/__tests__/anthropic.test.ts`

- [ ] **Step 2.1: Implement `src/lib/anthropic.ts`**

```typescript
/**
 * Thin Anthropic Sonnet wrapper used by Phase 8 reorganizer.
 *
 * Why Sonnet (not Haiku): the split decisions affect dozens of docs at
 * once and are run weekly — quality matters more than per-call cost.
 */

import Anthropic from '@anthropic-ai/sdk';

export interface ProposedCluster {
  name: string;
  docIds: string[];
}

export interface SplitProposal {
  clusters: ProposedCluster[];
  reasoning: string;
}

const SYSTEM_PROMPT = `You are reorganizing a personal knowledge-base folder.
A folder has accumulated many documents and the user wants you to propose
2–5 intuitive sub-cluster folders.

You will be given:
- The current folder name
- A list of {docId, title} entries

Output strict JSON:
{
  "clusters": [
    { "name": string, "docIds": [string, ...] }
  ],
  "reasoning": string
}

Guidelines:
- Each cluster must have at least 3 docs. Smaller clusters (1–2 docs)
  stay in the parent — do not include them in the output.
- Cluster names: 1–3 words, Title Case, intuitive sub-categories.
- Don't include every doc; it's OK to leave some at the parent.
- Don't propose clusters that are near-duplicates of each other.

Return ONLY the JSON object. No prose, no markdown fences.
`;

interface DocSummary {
  docId: string;
  title: string;
}

export async function proposeSplit(args: {
  folderName: string;
  docs: DocSummary[];
  apiKey: string;
  model?: string;
  maxTokens?: number;
}): Promise<SplitProposal> {
  const client = new Anthropic({ apiKey: args.apiKey });

  const userMsg =
    `Folder: ${args.folderName}\n\n` +
    `Documents (${args.docs.length}):\n` +
    args.docs.map(d => `- ${d.docId}: ${d.title}`).join('\n');

  const response = await client.messages.create({
    model: args.model ?? 'claude-sonnet-4-6-20251001',
    max_tokens: args.maxTokens ?? 2048,
    system: [
      { type: 'text', text: SYSTEM_PROMPT, cache_control: { type: 'ephemeral' } },
    ],
    messages: [{ role: 'user', content: userMsg }],
  });

  const block = response.content[0];
  if (block.type !== 'text') {
    throw new Error(`Unexpected response block type: ${block.type}`);
  }
  let text = block.text.trim();
  if (text.startsWith('```')) {
    text = text.replace(/^```(?:json)?\s*/, '').replace(/\s*```$/, '').trim();
  }
  return JSON.parse(text) as SplitProposal;
}

export { SYSTEM_PROMPT };
```

- [ ] **Step 2.2: Test `src/lib/__tests__/anthropic.test.ts`**

```typescript
import { describe, it, expect } from 'vitest';
import { SYSTEM_PROMPT } from '../anthropic.js';

describe('SYSTEM_PROMPT', () => {
  it('mentions JSON output and minimum cluster size', () => {
    expect(SYSTEM_PROMPT).toContain('JSON');
    expect(SYSTEM_PROMPT).toContain('at least 3 docs');
    expect(SYSTEM_PROMPT).toContain('2–5');
  });
});
```

(Most testing of `proposeSplit` happens in Task 4 by mocking the Anthropic client at the call site.)

- [ ] **Step 2.3: prepare.sh + commit**

```bash
cd affine-mcp-agent && npm test
cd ../portainer-stack && bash prepare.sh
git add mcp-agent/src/lib/
git commit -m "$(cat <<'EOF'
feat(mcp-agent): Anthropic Sonnet wrapper for reorganizer

proposeSplit(folderName, docs, apiKey) → SplitProposal{clusters,reasoning}.
System prompt explicitly demands ≥3 docs per cluster, 2–5 clusters total.
Caches the system prompt block (cache_control: ephemeral) so the weekly
sweep across many folders amortizes the prefix.

Phase 8 / Task 2 of docs/plans/2026-05-07-phase-8-reorganizer.md
EOF
)"
```

---

## Task 3: Reorganizer automation core

**Files:**
- Create: `affine-mcp-agent/src/automations/sources-reorg.ts`
- Create: `affine-mcp-agent/src/automations/__tests__/sources-reorg.test.ts`

- [ ] **Step 3.1: Implement `sources-reorg.ts`**

```typescript
/**
 * Sources Reorganizer
 *
 * Weekly sweep over Sources/* leaf folders. For each leaf folder with more
 * than REORG_THRESHOLD docs, asks Sonnet to propose 2–5 sub-clusters and
 * executes the splits via create_folder + move_document.
 *
 * Schedule: Sunday at 03:00 UTC (registered in scheduler.ts).
 * Usage: npm run sources-reorg
 */

import { config } from '../config.js';
import { AffineMcpClient } from '../mcp-client.js';
import { proposeSplit, SplitProposal } from '../lib/anthropic.js';

export interface ReorgSummary {
  scanned: number;
  splits: Array<{
    folderName: string;
    folderId: string;
    clusters: Array<{ name: string; folderId: string; docCount: number }>;
  }>;
}

interface FolderNode {
  id: string;
  type: 'folder' | 'doc' | 'tag' | 'collection' | string;
  name: string;
  index?: string;
  children?: FolderNode[];
  targetId?: string;
}

const REORG_THRESHOLD_DEFAULT = 15;

export async function runSourcesReorg(opts: {
  client?: AffineMcpClient;
  proposeSplitFn?: typeof proposeSplit;
  threshold?: number;
  anthropicApiKey?: string;
} = {}): Promise<ReorgSummary> {
  const client = opts.client ?? new AffineMcpClient(
    config.baseUrl, config.workspaceId, config.accessToken, config.mcpEndpoint,
  );
  const proposeFn = opts.proposeSplitFn ?? proposeSplit;
  const threshold = opts.threshold ?? Number(process.env.REORG_THRESHOLD ?? REORG_THRESHOLD_DEFAULT);
  const apiKey = opts.anthropicApiKey ?? process.env.ANTHROPIC_API_KEY ?? '';

  if (!apiKey) {
    throw new Error('ANTHROPIC_API_KEY is required for sources reorganizer');
  }
  if (opts.client === undefined) {
    await client.initialize();
  }

  // 1. Pull the folder tree.
  const rawTree = await client.callTool('list_folder_tree');
  const treePayload = JSON.parse(rawTree.content[0].text) as { tree: FolderNode[] };
  const sources = findChildByName(treePayload.tree, 'Sources');
  if (!sources) {
    console.log('[Reorganizer] No Sources/ folder found; nothing to do.');
    return { scanned: 0, splits: [] };
  }

  // 2. Walk all leaf folders under Sources/ — folders with at least one
  //    doc child whose count exceeds threshold.
  const candidates = collectCandidateLeaves(sources, threshold);
  console.log(`[Reorganizer] ${candidates.length} folder(s) over threshold ${threshold}.`);

  const summary: ReorgSummary = { scanned: candidates.length, splits: [] };

  for (const folder of candidates) {
    const docs = (folder.children ?? [])
      .filter(c => c.type === 'doc')
      .map(c => ({ docId: c.targetId ?? c.id, title: c.name }));

    let proposal: SplitProposal;
    try {
      proposal = await proposeFn({
        folderName: folder.name,
        docs,
        apiKey,
      });
    } catch (e) {
      console.warn(`[Reorganizer] Skipping ${folder.name}: split proposal failed:`, e);
      continue;
    }

    const validClusters = proposal.clusters.filter(c => c.docIds.length >= 3);
    if (validClusters.length === 0) {
      console.log(`[Reorganizer] No valid splits for ${folder.name}.`);
      continue;
    }

    const split: ReorgSummary['splits'][0] = {
      folderName: folder.name,
      folderId: folder.id,
      clusters: [],
    };
    for (const cluster of validClusters) {
      const created = await client.callTool('create_folder', {
        name: cluster.name,
        parentFolderId: folder.id,
      });
      const newFolder = JSON.parse(created.content[0].text) as { folderId: string };
      for (const docId of cluster.docIds) {
        await client.callTool('move_document', {
          docId,
          folderId: newFolder.folderId,
        });
      }
      split.clusters.push({
        name: cluster.name,
        folderId: newFolder.folderId,
        docCount: cluster.docIds.length,
      });
    }
    summary.splits.push(split);
  }

  return summary;
}

// ── Helpers ──────────────────────────────────────────────────────────

function findChildByName(nodes: FolderNode[], name: string): FolderNode | null {
  for (const n of nodes) {
    if (n.type === 'folder' && n.name === name) return n;
  }
  return null;
}

function collectCandidateLeaves(root: FolderNode, threshold: number): FolderNode[] {
  const out: FolderNode[] = [];
  const visit = (node: FolderNode) => {
    if (node.type !== 'folder') return;
    const docCount = (node.children ?? []).filter(c => c.type === 'doc').length;
    if (docCount > threshold) out.push(node);
    for (const c of node.children ?? []) {
      if (c.type === 'folder') visit(c);
    }
  };
  visit(root);
  return out;
}

// CLI entrypoint
if (import.meta.url === `file://${process.argv[1]}`) {
  runSourcesReorg()
    .then(summary => {
      console.log('[Reorganizer] Done:', JSON.stringify(summary, null, 2));
      process.exit(0);
    })
    .catch(err => {
      console.error('[Reorganizer] Failed:', err);
      process.exit(1);
    });
}
```

- [ ] **Step 3.2: Tests `src/automations/__tests__/sources-reorg.test.ts`**

```typescript
import { describe, it, expect, vi } from 'vitest';
import { runSourcesReorg } from '../sources-reorg.js';

function fakeTree(recipeDocs = 20) {
  const docs = Array.from({ length: recipeDocs }, (_, i) => ({
    id: `link-${i}`,
    type: 'doc' as const,
    name: `Recipe ${i}`,
    targetId: `doc-${i}`,
  }));

  return JSON.stringify({
    totalNodes: 5 + docs.length,
    tree: [
      {
        id: 'f-sources', type: 'folder', name: 'Sources', children: [
          {
            id: 'f-socials', type: 'folder', name: 'Socials', children: [
              {
                id: 'f-ig', type: 'folder', name: 'Instagram', children: [
                  {
                    id: 'f-recipes', type: 'folder', name: 'Recipes',
                    children: docs,
                  },
                ],
              },
            ],
          },
        ],
      },
    ],
  });
}

function makeClientMock(treeJson: string) {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  const callTool = vi.fn(async (name: string, args: Record<string, unknown> = {}) => {
    calls.push({ name, args });
    if (name === 'list_folder_tree') {
      return { content: [{ type: 'text', text: treeJson }] };
    }
    if (name === 'create_folder') {
      return {
        content: [{
          type: 'text',
          text: JSON.stringify({
            folderId: `new-${args.name}-${calls.length}`,
            ok: true,
          }),
        }],
      };
    }
    if (name === 'move_document') {
      return { content: [{ type: 'text', text: JSON.stringify({ ok: true }) }] };
    }
    return { content: [{ type: 'text', text: '{}' }] };
  });
  const initialize = vi.fn(async () => {});
  return {
    callTool, initialize, calls,
    fakeClient: { callTool, initialize, listTools: vi.fn() } as any,
  };
}

describe('runSourcesReorg', () => {
  it('skips folders below threshold', async () => {
    const { callTool, fakeClient } = makeClientMock(fakeTree(5));
    const proposeSplit = vi.fn();

    const summary = await runSourcesReorg({
      client: fakeClient,
      proposeSplitFn: proposeSplit,
      threshold: 15,
      anthropicApiKey: 'sk-test',
    });

    expect(summary.scanned).toBe(0);
    expect(summary.splits).toEqual([]);
    expect(proposeSplit).not.toHaveBeenCalled();
    expect(callTool).toHaveBeenCalledWith('list_folder_tree');
  });

  it('proposes split + executes for over-threshold folder', async () => {
    const { callTool, fakeClient, calls } = makeClientMock(fakeTree(20));
    const proposeSplit = vi.fn(async () => ({
      reasoning: 'cluster by cuisine',
      clusters: [
        { name: 'Fitness', docIds: ['doc-0', 'doc-1', 'doc-2', 'doc-3', 'doc-4'] },
        { name: 'Comfort', docIds: ['doc-5', 'doc-6', 'doc-7', 'doc-8', 'doc-9'] },
        { name: 'Quick', docIds: ['doc-10', 'doc-11', 'doc-12'] },
      ],
    }));

    const summary = await runSourcesReorg({
      client: fakeClient,
      proposeSplitFn: proposeSplit,
      threshold: 15,
      anthropicApiKey: 'sk-test',
    });

    expect(summary.scanned).toBe(1);
    expect(summary.splits).toHaveLength(1);
    expect(summary.splits[0].folderName).toBe('Recipes');
    expect(summary.splits[0].clusters).toHaveLength(3);

    const createFolderCalls = calls.filter(c => c.name === 'create_folder');
    expect(createFolderCalls).toHaveLength(3);
    expect(createFolderCalls.map(c => c.args.name).sort()).toEqual(['Comfort', 'Fitness', 'Quick']);
    expect(createFolderCalls.every(c => c.args.parentFolderId === 'f-recipes')).toBe(true);

    const moveCalls = calls.filter(c => c.name === 'move_document');
    expect(moveCalls).toHaveLength(13);
  });

  it('drops clusters with fewer than 3 docs', async () => {
    const { callTool, fakeClient, calls } = makeClientMock(fakeTree(20));
    const proposeSplit = vi.fn(async () => ({
      reasoning: 'tiny cluster + a real one',
      clusters: [
        { name: 'Big', docIds: ['doc-0', 'doc-1', 'doc-2', 'doc-3', 'doc-4'] },
        { name: 'Tiny', docIds: ['doc-5'] },
        { name: 'AlsoTiny', docIds: ['doc-6', 'doc-7'] },
      ],
    }));

    const summary = await runSourcesReorg({
      client: fakeClient,
      proposeSplitFn: proposeSplit,
      threshold: 15,
      anthropicApiKey: 'sk-test',
    });

    const createFolderCalls = calls.filter(c => c.name === 'create_folder');
    expect(createFolderCalls).toHaveLength(1);
    expect(createFolderCalls[0].args.name).toBe('Big');
    expect(summary.splits[0].clusters).toHaveLength(1);
  });

  it('continues to next folder when proposeSplit throws', async () => {
    const { fakeClient, calls } = makeClientMock(fakeTree(20));
    const proposeSplit = vi.fn(async () => { throw new Error('llm failed'); });

    const summary = await runSourcesReorg({
      client: fakeClient,
      proposeSplitFn: proposeSplit,
      threshold: 15,
      anthropicApiKey: 'sk-test',
    });

    expect(summary.scanned).toBe(1);
    expect(summary.splits).toEqual([]);
    expect(calls.filter(c => c.name === 'create_folder')).toHaveLength(0);
  });

  it('throws when ANTHROPIC_API_KEY missing', async () => {
    const { fakeClient } = makeClientMock(fakeTree(20));
    await expect(runSourcesReorg({
      client: fakeClient,
      threshold: 15,
      anthropicApiKey: '',
    })).rejects.toThrow(/ANTHROPIC_API_KEY/);
  });
});
```

- [ ] **Step 3.3: Run + commit**

```bash
cd affine-mcp-agent && npm test
cd ../portainer-stack && bash prepare.sh
git add mcp-agent/src/automations/sources-reorg.ts mcp-agent/src/automations/__tests__/
git commit -m "$(cat <<'EOF'
feat(mcp-agent): sources-reorg automation

Sweeps Sources/* leaf folders, splits any with >REORG_THRESHOLD doc
children into 2–5 sub-clusters proposed by Sonnet. Existing
AffineMcpClient.callTool used directly for list_folder_tree /
create_folder / move_document — no new MCP wrapper needed.

Per-folder failures (proposeSplit throws) skip that folder and continue.
Sub-clusters with <3 docs are dropped (tail items stay in parent).
ReorgSummary returns scan + split counts for the scheduler log.

Tested with vitest: 5 cases — under-threshold no-op, happy 3-cluster
split (verifies create_folder × 3 + move_document × 13), small-cluster
filter, llm-failure tolerance, missing API key.

Phase 8 / Task 3 of docs/plans/2026-05-07-phase-8-reorganizer.md
EOF
)"
```

---

## Task 4: Wire into scheduler.ts

**Files:**
- Modify: `affine-mcp-agent/src/scheduler.ts`

- [ ] **Step 4.1: Add the cron entry**

Add after the existing schedules (after the Stale Docs line):

```typescript
import { runSourcesReorg } from './automations/sources-reorg.js';

// ... existing schedules ...

// Sunday at 03:00 UTC — Sources Reorganizer
cron.schedule('0 3 * * 0', wrap('Sources Reorg', async () => {
  await runSourcesReorg();
}));
```

And update the registered-jobs log block:

```typescript
console.log('  - Sources Reorg:    Sundays at 03:00');
```

- [ ] **Step 4.2: prepare.sh + commit**

```bash
cd ../portainer-stack && bash prepare.sh
git add mcp-agent/src/scheduler.ts
git commit -m "$(cat <<'EOF'
feat(mcp-agent): register sources-reorg cron entry

Sunday 03:00 UTC. Picks up the runSourcesReorg automation; the wrap()
helper handles error logging + duration timing as for the existing
schedules.

Phase 8 / Task 4 of docs/plans/2026-05-07-phase-8-reorganizer.md
EOF
)"
```

---

## Task 5: env.example + push + PR

- [ ] **Step 5.1: Verify ANTHROPIC_API_KEY in `.env.example`**

Phase 5 already added it for the Python ingest service. The mcp-agent container should pick up the same env var. Confirm `compose.yaml` passes `ANTHROPIC_API_KEY` into the `mcp_agent` service:

```bash
grep -A20 "^  mcp_agent:" portainer-stack/compose.yaml
```

If `ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}` is missing from the mcp_agent service's `environment:` block, add it:

```yaml
  mcp_agent:
    # ... existing config ...
    environment:
      # ... existing entries ...
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
      - REORG_THRESHOLD=${REORG_THRESHOLD_DEFAULT:-15}
```

Commit if changed:
```bash
git add portainer-stack/compose.yaml
git commit -m "chore(stack): pass ANTHROPIC_API_KEY + REORG_THRESHOLD to mcp_agent"
```

- [ ] **Step 5.2: Push branch + open PR**

```bash
git push -u origin feat/phase-8-reorganizer
gh pr create --base main --title "Phase 8: Sources reorganizer (mcp-agent)" --body "..."
```

---

## Spec coverage

| Phase 8 deliverable | Task |
|---|---|
| `affine-mcp-agent/src/automations/sources-reorg.ts` | 3 |
| Anthropic Sonnet client wrapper | 2 |
| Scheduler cron `0 3 * * 0` | 4 |
| Vitest scaffolding | 1 |
| Tests for split/no-split/filter/failure | 3 |
| compose env vars | 5 |

## Out of scope

- Decision log written to `Sources/Operations/Logs/reorganizer-YYYY-MM-DD.md` — defer; useful but not blocking for the cluster split functionality.
- Per-platform threshold overrides (`topics.yaml.reorg.overrides`) — Phase 5 added the YAML key but Phase 8 doesn't read it (TypeScript process; would need to load + parse the YAML). Default threshold is enough for v1.
- Multi-language doc title support — the Sonnet prompt is English-biased; if you have Czech/other-language doc titles, results may be uneven. Acceptable for personal use.
