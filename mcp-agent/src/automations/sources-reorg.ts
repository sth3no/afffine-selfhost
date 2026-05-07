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
