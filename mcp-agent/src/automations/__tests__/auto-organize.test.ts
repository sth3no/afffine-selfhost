import { describe, it, expect, vi } from 'vitest';

vi.mock('../../config.js', () => ({
  config: {
    baseUrl: 'http://test',
    workspaceId: 'ws-test',
    accessToken: 'token-test',
    mcpEndpoint: '',
  },
}));

import { runAutoOrganize } from '../auto-organize.js';

/** Tree fixture: Sources/Socials/Instagram has 2 unfiled docs + 1 sibling topic. */
function fakeTreeWithUnfiledDocs() {
  return JSON.stringify({
    totalNodes: 10,
    tree: [
      {
        id: 'f-sources', type: 'folder', name: 'Sources', children: [
          {
            id: 'f-socials', type: 'folder', name: 'Socials', children: [
              {
                id: 'f-ig', type: 'folder', name: 'Instagram', children: [
                  {
                    id: 'f-recipes', type: 'folder', name: 'Recipes',
                    children: [{ id: 'l-r1', type: 'doc', name: 'Salmon recipe', targetId: 'doc-r1' }],
                  },
                  // Two unfiled docs at the platform root:
                  { id: 'l-u1', type: 'doc', name: 'Travis Scott reel', targetId: 'doc-u1' },
                  { id: 'l-u2', type: 'doc', name: 'Carbonara recipe', targetId: 'doc-u2' },
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
    if (name === 'read_document') {
      return { content: [{ type: 'text', text: `(content of ${args.docId})` }] };
    }
    if (name === 'create_folder') {
      return {
        content: [{
          type: 'text',
          text: JSON.stringify({ folderId: `new-${args.name}`, ok: true }),
        }],
      };
    }
    if (name === 'move_document') {
      return { content: [{ type: 'text', text: JSON.stringify({ ok: true }) }] };
    }
    return { content: [{ type: 'text', text: '{}' }] };
  });
  const initialize = vi.fn(async () => {});
  // Stub rpc for tools/list
  const rpc = vi.fn(async (method: string) => {
    if (method === 'tools/list') {
      return {
        tools: [
          { name: 'create_folder', description: 'd', inputSchema: { type: 'object' } },
          { name: 'move_document', description: 'd', inputSchema: { type: 'object' } },
          { name: 'list_folder_tree', description: 'd', inputSchema: { type: 'object' } },
        ],
      };
    }
    throw new Error(`unexpected rpc: ${method}`);
  });
  return {
    callTool, initialize, calls, rpc,
    fakeClient: { callTool, initialize, rpc, listTools: vi.fn() } as any,
  };
}

describe('runAutoOrganize', () => {
  it('returns empty summary when no Sources folder exists', async () => {
    const empty = JSON.stringify({ totalNodes: 0, tree: [] });
    const { fakeClient } = makeClientMock(empty);

    const summary = await runAutoOrganize({
      client: fakeClient,
      anthropicApiKey: 'sk-test',
      anthropicClient: { messages: { create: vi.fn() } } as any,
    });

    expect(summary.scanned).toBe(0);
    expect(summary.filed).toBe(0);
  });

  it('moves an unfiled doc into an existing topic folder', async () => {
    const { fakeClient, calls } = makeClientMock(fakeTreeWithUnfiledDocs());

    // Anthropic decides: doc-u2 (Carbonara) → existing Recipes folder.
    // doc-u1 (Travis Scott) → skipped (not relevant).
    const create = vi.fn()
      // First doc: Travis Scott → skip (no tools)
      .mockResolvedValueOnce({
        id: 'm1', type: 'message', role: 'assistant', model: 'claude-sonnet-4-6',
        content: [{ type: 'text', text: 'skipped: not enough context' }],
        stop_reason: 'end_turn',
        usage: { input_tokens: 50, output_tokens: 10 },
      })
      // Second doc: Carbonara → move to f-recipes
      .mockResolvedValueOnce({
        id: 'm2', type: 'message', role: 'assistant', model: 'claude-sonnet-4-6',
        content: [
          { type: 'tool_use', id: 'tu_1', name: 'move_document',
            input: { docId: 'doc-u2', folderId: 'f-recipes' } },
        ],
        stop_reason: 'tool_use',
        usage: { input_tokens: 50, output_tokens: 20 },
      })
      .mockResolvedValueOnce({
        id: 'm3', type: 'message', role: 'assistant', model: 'claude-sonnet-4-6',
        content: [{ type: 'text', text: 'moved into Recipes' }],
        stop_reason: 'end_turn',
        usage: { input_tokens: 30, output_tokens: 10 },
      });

    const summary = await runAutoOrganize({
      client: fakeClient,
      anthropicApiKey: 'sk-test',
      anthropicClient: { messages: { create } } as any,
    });

    expect(summary.scanned).toBe(2);
    expect(summary.filed).toBe(1);
    expect(summary.newFoldersCreated).toBe(0);
    expect(summary.perDoc).toHaveLength(2);
    expect(summary.perDoc[0].action).toBe('skipped');
    expect(summary.perDoc[1].action).toBe('moved-existing');
    expect(summary.perDoc[1].targetFolder).toBe('f-recipes');

    expect(calls.filter(c => c.name === 'move_document')).toHaveLength(1);
    expect(calls.filter(c => c.name === 'create_folder')).toHaveLength(0);
  });

  it('creates a new topic folder when no sibling fits', async () => {
    const { fakeClient, calls } = makeClientMock(fakeTreeWithUnfiledDocs());

    // For doc-u1 (Travis Scott): create "Music" folder, then move doc into it.
    // For doc-u2: skip (we only test the new-folder path here).
    const create = vi.fn()
      // First doc: create + move
      .mockResolvedValueOnce({
        id: 'm1', type: 'message', role: 'assistant', model: 'claude-sonnet-4-6',
        content: [
          { type: 'tool_use', id: 'tu_1', name: 'create_folder',
            input: { name: 'Music', parentFolderId: 'f-ig' } },
          { type: 'tool_use', id: 'tu_2', name: 'move_document',
            input: { docId: 'doc-u1', folderId: 'new-Music' } },
        ],
        stop_reason: 'tool_use',
        usage: { input_tokens: 80, output_tokens: 30 },
      })
      .mockResolvedValueOnce({
        id: 'm2', type: 'message', role: 'assistant', model: 'claude-sonnet-4-6',
        content: [{ type: 'text', text: 'created Music and moved doc' }],
        stop_reason: 'end_turn',
        usage: { input_tokens: 40, output_tokens: 15 },
      })
      // Second doc: skip
      .mockResolvedValueOnce({
        id: 'm3', type: 'message', role: 'assistant', model: 'claude-sonnet-4-6',
        content: [{ type: 'text', text: 'skipped: ambiguous' }],
        stop_reason: 'end_turn',
        usage: { input_tokens: 50, output_tokens: 10 },
      });

    const summary = await runAutoOrganize({
      client: fakeClient,
      anthropicApiKey: 'sk-test',
      anthropicClient: { messages: { create } } as any,
    });

    expect(summary.filed).toBe(1);
    expect(summary.newFoldersCreated).toBe(1);
    expect(summary.perDoc[0].action).toBe('moved-new');
    expect(summary.perDoc[0].targetFolder).toBe('new-Music');

    expect(calls.filter(c => c.name === 'create_folder')).toHaveLength(1);
    expect(calls.filter(c => c.name === 'move_document')).toHaveLength(1);
  });

  it('respects maxDocs cap', async () => {
    const { fakeClient } = makeClientMock(fakeTreeWithUnfiledDocs());

    const create = vi.fn().mockResolvedValue({
      id: 'm1', type: 'message', role: 'assistant', model: 'claude-sonnet-4-6',
      content: [{ type: 'text', text: 'skipped' }],
      stop_reason: 'end_turn',
      usage: { input_tokens: 50, output_tokens: 10 },
    });

    const summary = await runAutoOrganize({
      client: fakeClient,
      anthropicApiKey: 'sk-test',
      anthropicClient: { messages: { create } } as any,
      maxDocs: 1,
    });

    expect(summary.scanned).toBe(1);  // capped at 1 even though tree has 2
  });

  it('throws without API key', async () => {
    const { fakeClient } = makeClientMock(fakeTreeWithUnfiledDocs());
    await expect(runAutoOrganize({
      client: fakeClient,
      anthropicApiKey: '',
    })).rejects.toThrow(/ANTHROPIC_API_KEY/);
  });
});
