/**
 * Sources Reorganizer
 *
 * Weekly sweep over Sources/* leaf folders. For each leaf folder with more
 * than REORG_THRESHOLD docs, asks Sonnet to propose 2–5 sub-clusters and
 * executes the splits via create_folder + move_document.
 *
 * Two executor modes:
 *   - 'plan'      (default) — Sonnet returns JSON plan; this code executes it.
 *                              Deterministic, fully tested, the conservative path.
 *   - 'tool-use'             — Sonnet has direct access to mcp-ext tools and
 *                              executes the splits itself via the tool-use loop
 *                              (lib/mcp-tool-loop.ts). Fewer round-trips, more
 *                              flexible (can read doc content), but the LLM
 *                              decides what gets created/moved.
 *
 * Schedule: Sunday at 03:00 UTC (registered in scheduler.ts).
 * Usage: npm run sources-reorg
 */

import Anthropic from '@anthropic-ai/sdk';

import { config } from '../config.js';
import { AffineMcpClient } from '../mcp-client.js';
import { proposeSplit, SplitProposal } from '../lib/anthropic.js';
import { loadMcpToolsForAnthropic, runMcpToolLoop, type AnthropicToolDefinition } from '../lib/mcp-tool-loop.js';

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

export type ReorgExecutor = 'plan' | 'tool-use';

export async function runSourcesReorg(opts: {
  client?: AffineMcpClient;
  proposeSplitFn?: typeof proposeSplit;
  threshold?: number;
  anthropicApiKey?: string;
  /** 'plan' (default): Sonnet → JSON plan → deterministic exec.
   *  'tool-use': Sonnet runs in a tool-use loop with direct mcp-ext access. */
  executor?: ReorgExecutor;
  /** Override Anthropic client (for tool-use mode tests). */
  anthropicClient?: Anthropic;
  model?: string;
} = {}): Promise<ReorgSummary> {
  const client = opts.client ?? new AffineMcpClient(
    config.baseUrl, config.workspaceId, config.accessToken, config.mcpEndpoint,
  );
  const proposeFn = opts.proposeSplitFn ?? proposeSplit;
  const threshold = opts.threshold ?? Number(process.env.REORG_THRESHOLD ?? REORG_THRESHOLD_DEFAULT);
  const apiKey = opts.anthropicApiKey ?? process.env.ANTHROPIC_API_KEY ?? '';
  const executor: ReorgExecutor = opts.executor ?? 'plan';
  const model = opts.model ?? 'claude-sonnet-4-6';

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
  console.log(`[Reorganizer] ${candidates.length} folder(s) over threshold ${threshold} (executor=${executor}).`);

  const summary: ReorgSummary = { scanned: candidates.length, splits: [] };

  // Tool-use mode: load mcp-ext tools once, restricted to safe operations.
  let toolUseTools: AnthropicToolDefinition[] | null = null;
  let anthropic: Anthropic | null = null;
  if (executor === 'tool-use') {
    anthropic = opts.anthropicClient ?? new Anthropic({ apiKey });
    toolUseTools = await loadMcpToolsForAnthropic(client, {
      // Restrict the tool set: never let Sonnet delete/rename/move folders.
      includePrefixes: ['create_folder', 'move_document', 'list_folder_tree'],
    });
  }

  for (const folder of candidates) {
    const docs = (folder.children ?? [])
      .filter(c => c.type === 'doc')
      .map(c => ({ docId: c.targetId ?? c.id, title: c.name }));

    if (executor === 'tool-use') {
      const split = await splitWithToolUse({
        anthropic: anthropic!,
        client,
        tools: toolUseTools!,
        model,
        folder,
        docs,
      });
      if (split && split.clusters.length > 0) summary.splits.push(split);
      continue;
    }

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

// ── Tool-use executor ────────────────────────────────────────────────

const SPLIT_TOOL_USE_SYSTEM = `You are reorganizing one over-full folder in a personal knowledge base.

The user will tell you the folder name and the list of docs in it (with docIds + titles).
Your job: decide whether 2–5 sub-cluster folders would help, and if so, CREATE them
and MOVE the docs.

You have these MCP tools:
  - create_folder(name, parentFolderId): create a sub-folder; returns folderId
  - move_document(docId, folderId): file a doc into a folder
  - list_folder_tree(): inspect the workspace tree if you need broader context

Rules:
  - Each new sub-folder must hold AT LEAST 3 docs. If you can't find 2-5 such
    clusters, do NOT create any folders — reply with a single sentence
    explaining why no split is appropriate.
  - Sub-folder names: 1-3 words, Title Case, intuitive sub-categories.
  - You may leave docs un-moved (they stay in the parent).
  - Never rename or delete anything.
  - When done, write a single sentence summary of what you did and stop.`;

interface SplitWithToolUseArgs {
  anthropic: Anthropic;
  client: AffineMcpClient;
  tools: AnthropicToolDefinition[];
  model: string;
  folder: FolderNode;
  docs: Array<{ docId: string; title: string }>;
}

async function splitWithToolUse(args: SplitWithToolUseArgs): Promise<ReorgSummary['splits'][0] | null> {
  const userMessage =
    `Folder: ${args.folder.name} (folderId=${args.folder.id})\n\n` +
    `Docs (${args.docs.length}):\n` +
    args.docs.map(d => `- ${d.docId}: ${d.title}`).join('\n') +
    '\n\n' +
    `Split if appropriate. Otherwise explain why not.`;

  // Track create_folder + move_document calls so we can report what landed.
  const createdFolders = new Map<string, { name: string; folderId: string; docCount: number }>();
  let moveCount = 0;

  try {
    await runMcpToolLoop({
      anthropic: args.anthropic,
      mcp: args.client,
      tools: args.tools,
      model: args.model,
      system: SPLIT_TOOL_USE_SYSTEM,
      userMessage,
      maxIterations: 30,  // 1 create + N moves per cluster, up to 5 clusters
      onToolCall: ({ name, input, result, isError }) => {
        if (isError) return;
        if (name === 'create_folder') {
          try {
            const parsed = JSON.parse(result) as { folderId?: string };
            if (parsed.folderId) {
              createdFolders.set(parsed.folderId, {
                name: String(input.name ?? '(unnamed)'),
                folderId: parsed.folderId,
                docCount: 0,
              });
            }
          } catch {/* ignore non-JSON */}
        } else if (name === 'move_document') {
          moveCount++;
          const folderId = String(input.folderId ?? '');
          const tracked = createdFolders.get(folderId);
          if (tracked) tracked.docCount++;
        }
      },
    });
  } catch (e) {
    console.warn(`[Reorganizer] tool-use exec failed for ${args.folder.name}:`, e);
    return null;
  }

  if (createdFolders.size === 0) {
    console.log(`[Reorganizer] No splits chosen for ${args.folder.name} (tool-use mode).`);
    return null;
  }

  console.log(`[Reorganizer] ${args.folder.name}: ${createdFolders.size} new folders, ${moveCount} moves.`);
  return {
    folderName: args.folder.name,
    folderId: args.folder.id,
    clusters: Array.from(createdFolders.values()),
  };
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
