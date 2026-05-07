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
