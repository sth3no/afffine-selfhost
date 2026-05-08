/**
 * Auto-organize: Cloud AI files unfiled captures into topic folders.
 *
 * Targets the docs that the per-capture classifier didn't confidently file —
 * they sit at `Sources/<group>/<platform>/` directly, with no topic
 * subfolder. The classifier might have been below the confidence floor,
 * or the topic might not have existed yet at capture time.
 *
 * For each unfiled doc, this automation:
 *   1. Reads the doc's full content via mcp-ext (`read_document`).
 *   2. Hands Claude {title, content excerpt, existing sibling topics}.
 *   3. Lets Claude either move it into an existing topic, create a new
 *      topic + move, or leave it (e.g. content too ambiguous).
 *
 * Differences vs sources-reorg:
 *   - sources-reorg splits OVER-FULL leaf folders into sub-clusters.
 *   - auto-organize files the PARENT-LEVEL strays into topic folders.
 *
 * Claude has direct mcp-ext access via the tool-use loop — when this run
 * finishes, all changes have already been applied to the workspace.
 *
 * Schedule: opt-in only. Run via `npm run auto-organize` or wire into the
 * scheduler if you want a recurring sweep.
 */

import Anthropic from '@anthropic-ai/sdk';

import { config } from '../config.js';
import { AffineMcpClient } from '../mcp-client.js';
import { loadMcpToolsForAnthropic, runMcpToolLoop, type AnthropicToolDefinition } from '../lib/mcp-tool-loop.js';

// ── Public API ───────────────────────────────────────────────────────

export interface AutoOrganizeSummary {
  scanned: number;
  filed: number;
  newFoldersCreated: number;
  perDoc: Array<{
    docId: string;
    docTitle: string;
    platform: string;
    /** "moved-existing", "moved-new", "skipped". */
    action: 'moved-existing' | 'moved-new' | 'skipped';
    targetFolder?: string;
  }>;
}

interface FolderNode {
  id: string;
  type: 'folder' | 'doc' | string;
  name: string;
  children?: FolderNode[];
  targetId?: string;
}

const ORGANIZE_SYSTEM = `You file ONE captured doc into the right topic folder under its platform.

You'll be given:
  - Platform path (e.g. "Sources/Socials/Instagram")
  - Platform parent folderId
  - Existing topic sub-folders under that platform (name + folderId)
  - The doc's title, docId, and a content excerpt

Decide ONE of:
  A) Move into an existing topic folder. Use move_document with that folderId.
  B) Create a new topic folder under the platform, then move the doc into it.
     Topic name: 1–2 words, Title Case (e.g. "Recipes", "AI", "Memes").
  C) Leave it alone — only if the content is genuinely ambiguous or off-topic.
     In that case, do NOT call any tools and just say "skipped: <reason>".

Rules:
  - PREFER existing topics. Only create a new one if no sibling fits.
  - Don't propose semantic duplicates (e.g. don't make "Cooking" if "Recipes" exists).
  - Never rename or delete anything.
  - Reply with ONE short sentence summarizing what you did, then stop.`;

const PLATFORMS_TO_ORGANIZE: Array<{ group: string; platform: string }> = [
  { group: 'Socials', platform: 'Instagram' },
  { group: 'Socials', platform: 'YouTube' },
  { group: 'Socials', platform: 'TikTok' },
  { group: 'Socials', platform: 'X' },
  { group: 'Socials', platform: 'Reddit' },
  { group: 'Socials', platform: 'Vimeo' },
  { group: 'Research Papers', platform: 'arXiv' },
  { group: 'Podcasts', platform: 'Apple Podcasts' },
  { group: 'Podcasts', platform: 'Spotify' },
  { group: 'Websites', platform: 'General' },
];

/** Maximum docs to process per run (cost cap; bypass with maxDocs=0). */
const DEFAULT_MAX_DOCS = 25;

export async function runAutoOrganize(opts: {
  client?: AffineMcpClient;
  anthropicClient?: Anthropic;
  anthropicApiKey?: string;
  model?: string;
  /** Cap on docs processed per run. 0 = unlimited. Default 25. */
  maxDocs?: number;
  /** Only consider these (group, platform) pairs. Defaults to all. */
  platforms?: Array<{ group: string; platform: string }>;
} = {}): Promise<AutoOrganizeSummary> {
  const client = opts.client ?? new AffineMcpClient(
    config.baseUrl, config.workspaceId, config.accessToken, config.mcpEndpoint,
  );
  const apiKey = opts.anthropicApiKey ?? process.env.ANTHROPIC_API_KEY ?? '';
  if (!apiKey) {
    throw new Error('ANTHROPIC_API_KEY is required for auto-organize');
  }
  if (opts.client === undefined) await client.initialize();

  const anthropic = opts.anthropicClient ?? new Anthropic({ apiKey });
  const model = opts.model ?? 'claude-sonnet-4-6';
  const maxDocs = opts.maxDocs ?? DEFAULT_MAX_DOCS;
  const platforms = opts.platforms ?? PLATFORMS_TO_ORGANIZE;

  // 1. Pull tree → identify unfiled docs at the platform root.
  const rawTree = await client.callTool('list_folder_tree');
  const treePayload = JSON.parse(rawTree.content[0].text) as { tree: FolderNode[] };
  const sources = treePayload.tree.find(n => n.type === 'folder' && n.name === 'Sources');
  if (!sources) {
    console.log('[AutoOrganize] No Sources/ folder found.');
    return { scanned: 0, filed: 0, newFoldersCreated: 0, perDoc: [] };
  }

  const candidates: Array<{
    platform: string;
    parentFolder: FolderNode;
    siblingTopics: Array<{ name: string; folderId: string }>;
    doc: FolderNode;
  }> = [];

  for (const { group, platform } of platforms) {
    const groupNode = sources.children?.find(n => n.type === 'folder' && n.name === group);
    if (!groupNode) continue;
    const platformNode = groupNode.children?.find(n => n.type === 'folder' && n.name === platform);
    if (!platformNode) continue;

    const siblingTopics = (platformNode.children ?? [])
      .filter(c => c.type === 'folder')
      .map(c => ({ name: c.name, folderId: c.id }));
    const unfiledDocs = (platformNode.children ?? []).filter(c => c.type === 'doc');

    for (const doc of unfiledDocs) {
      candidates.push({
        platform: `${group}/${platform}`,
        parentFolder: platformNode,
        siblingTopics,
        doc,
      });
    }
  }

  console.log(`[AutoOrganize] ${candidates.length} unfiled docs across ${platforms.length} platforms.`);
  if (candidates.length === 0) {
    return { scanned: 0, filed: 0, newFoldersCreated: 0, perDoc: [] };
  }

  // Cost cap.
  const slice = maxDocs > 0 ? candidates.slice(0, maxDocs) : candidates;

  // 2. Load tools once (restricted to safe operations).
  const tools = await loadMcpToolsForAnthropic(client, {
    includePrefixes: ['create_folder', 'move_document', 'list_folder_tree'],
  });

  // 3. Process each doc through the tool-use loop.
  const summary: AutoOrganizeSummary = { scanned: slice.length, filed: 0, newFoldersCreated: 0, perDoc: [] };
  for (const cand of slice) {
    const outcome = await organizeOneDoc({
      anthropic,
      client,
      tools,
      model,
      cand,
    });
    summary.perDoc.push(outcome);
    if (outcome.action !== 'skipped') summary.filed++;
    if (outcome.action === 'moved-new') summary.newFoldersCreated++;
  }

  return summary;
}

interface OrganizeOneArgs {
  anthropic: Anthropic;
  client: AffineMcpClient;
  tools: AnthropicToolDefinition[];
  model: string;
  cand: {
    platform: string;
    parentFolder: FolderNode;
    siblingTopics: Array<{ name: string; folderId: string }>;
    doc: FolderNode;
  };
}

async function organizeOneDoc(args: OrganizeOneArgs): Promise<AutoOrganizeSummary['perDoc'][0]> {
  const { cand } = args;
  const docId = cand.doc.targetId ?? cand.doc.id;

  // Pull doc content excerpt (best-effort — read_document is text-only).
  let excerpt = '(content unavailable)';
  try {
    const read = await args.client.callTool('read_document', { docId });
    excerpt = (read.content?.[0]?.text ?? '').slice(0, 4000);
  } catch (e) {
    console.warn(`[AutoOrganize] read_document failed for ${docId}: ${e}`);
  }

  const siblingsBlock = cand.siblingTopics.length
    ? cand.siblingTopics.map(s => `- ${s.name} (folderId=${s.folderId})`).join('\n')
    : '(none — this would be the first topic folder under this platform)';

  const userMessage =
    `Platform path: ${cand.platform}\n` +
    `Parent folderId: ${cand.parentFolder.id}\n` +
    `\n` +
    `Existing topic sub-folders:\n${siblingsBlock}\n` +
    `\n` +
    `Doc to file:\n` +
    `- docId: ${docId}\n` +
    `- title: ${cand.doc.name}\n` +
    `\n` +
    `Content excerpt (first 4000 chars):\n\n${excerpt}\n`;

  let movedToFolderId: string | null = null;
  let createdNewFolder = false;

  try {
    await runMcpToolLoop({
      anthropic: args.anthropic,
      mcp: args.client,
      tools: args.tools,
      model: args.model,
      system: ORGANIZE_SYSTEM,
      userMessage,
      maxIterations: 8,
      onToolCall: ({ name, input, result, isError }) => {
        if (isError) return;
        if (name === 'create_folder') {
          createdNewFolder = true;
        } else if (name === 'move_document' && input.docId === docId) {
          movedToFolderId = String(input.folderId ?? '') || null;
        }
      },
    });
  } catch (e) {
    console.warn(`[AutoOrganize] tool-use failed for ${docId}:`, e);
    return {
      docId,
      docTitle: cand.doc.name,
      platform: cand.platform,
      action: 'skipped',
    };
  }

  if (movedToFolderId === null) {
    return {
      docId,
      docTitle: cand.doc.name,
      platform: cand.platform,
      action: 'skipped',
    };
  }

  return {
    docId,
    docTitle: cand.doc.name,
    platform: cand.platform,
    action: createdNewFolder ? 'moved-new' : 'moved-existing',
    targetFolder: movedToFolderId,
  };
}

// CLI entrypoint
if (import.meta.url === `file://${process.argv[1]}`) {
  runAutoOrganize()
    .then(summary => {
      console.log('[AutoOrganize] Done:', JSON.stringify(summary, null, 2));
      process.exit(0);
    })
    .catch(err => {
      console.error('[AutoOrganize] Failed:', err);
      process.exit(1);
    });
}
