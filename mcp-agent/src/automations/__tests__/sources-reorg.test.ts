import { describe, it, expect, vi } from 'vitest';

// Mock config before importing sources-reorg so process.exit is not called
vi.mock('../../config.js', () => ({
  config: {
    baseUrl: 'http://test',
    workspaceId: 'ws-test',
    accessToken: 'token-test',
    mcpEndpoint: '',
  },
}));

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
