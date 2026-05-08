import { describe, it, expect, vi } from 'vitest';

import { loadMcpToolsForAnthropic, runMcpToolLoop } from '../mcp-tool-loop.js';

// ── loadMcpToolsForAnthropic ────────────────────────────────────────

function makeMcpMock(tools: Array<{ name: string; description: string; inputSchema?: unknown }>) {
  return {
    rpc: vi.fn(async (method: string) => {
      if (method === 'tools/list') return { tools };
      throw new Error(`unexpected rpc: ${method}`);
    }),
  };
}

describe('loadMcpToolsForAnthropic', () => {
  it('renames inputSchema → input_schema', async () => {
    const mcp = makeMcpMock([
      { name: 'create_folder', description: 'Make a folder', inputSchema: { type: 'object', properties: { name: { type: 'string' } } } },
    ]);
    const tools = await loadMcpToolsForAnthropic(mcp as never);
    expect(tools[0]).toEqual({
      name: 'create_folder',
      description: 'Make a folder',
      input_schema: { type: 'object', properties: { name: { type: 'string' } } },
    });
  });

  it('filters by includePrefixes', async () => {
    const mcp = makeMcpMock([
      { name: 'create_folder', description: 'a' },
      { name: 'list_folder_tree', description: 'b' },
      { name: 'delete_doc', description: 'c' },
    ]);
    const tools = await loadMcpToolsForAnthropic(mcp as never, { includePrefixes: ['create_', 'list_folder'] });
    expect(tools.map(t => t.name).sort()).toEqual(['create_folder', 'list_folder_tree']);
  });

  it('drops by excludeNames', async () => {
    const mcp = makeMcpMock([
      { name: 'delete_doc', description: 'a' },
      { name: 'delete_folder', description: 'b' },
    ]);
    const tools = await loadMcpToolsForAnthropic(mcp as never, { excludeNames: ['delete_doc'] });
    expect(tools.map(t => t.name)).toEqual(['delete_folder']);
  });

  it('defaults input_schema when MCP omits it', async () => {
    const mcp = makeMcpMock([
      { name: 'no_args_tool', description: '' /* no inputSchema */ },
    ]);
    const tools = await loadMcpToolsForAnthropic(mcp as never);
    expect(tools[0].input_schema).toEqual({ type: 'object', properties: {} });
  });
});

// ── runMcpToolLoop ──────────────────────────────────────────────────

function makeAnthropicResponse(opts: {
  stopReason: 'end_turn' | 'tool_use';
  text?: string;
  toolUses?: Array<{ id: string; name: string; input: Record<string, unknown> }>;
}) {
  const content: unknown[] = [];
  if (opts.text) content.push({ type: 'text', text: opts.text });
  for (const tu of opts.toolUses ?? []) {
    content.push({ type: 'tool_use', id: tu.id, name: tu.name, input: tu.input });
  }
  return {
    id: 'msg_test',
    type: 'message',
    role: 'assistant',
    model: 'claude-opus-4-7',
    content,
    stop_reason: opts.stopReason,
    stop_sequence: null,
    usage: { input_tokens: 10, output_tokens: 5 },
  };
}

describe('runMcpToolLoop', () => {
  it('returns immediately when first response stops with end_turn', async () => {
    const anthropic = {
      messages: {
        create: vi.fn().mockResolvedValue(
          makeAnthropicResponse({ stopReason: 'end_turn', text: 'done' }),
        ),
      },
    };
    const mcp = { callTool: vi.fn() };

    const result = await runMcpToolLoop({
      anthropic: anthropic as never,
      mcp: mcp as never,
      tools: [],
      model: 'claude-opus-4-7',
      system: 'you are helpful',
      userMessage: 'hello',
    });

    expect(result.text).toBe('done');
    expect(result.toolCallCount).toBe(0);
    expect(mcp.callTool).not.toHaveBeenCalled();
    expect(anthropic.messages.create).toHaveBeenCalledOnce();
  });

  it('routes tool_use blocks through MCP and feeds tool_result back', async () => {
    const anthropic = {
      messages: {
        create: vi.fn()
          // First call: model asks to create a folder
          .mockResolvedValueOnce(makeAnthropicResponse({
            stopReason: 'tool_use',
            text: 'Creating folder...',
            toolUses: [{ id: 'toolu_1', name: 'create_folder', input: { name: 'NewFolder' } }],
          }))
          // Second call: model wraps up
          .mockResolvedValueOnce(makeAnthropicResponse({
            stopReason: 'end_turn',
            text: 'Done — created NewFolder',
          })),
      },
    };
    const mcp = {
      callTool: vi.fn().mockResolvedValue({
        content: [{ type: 'text', text: '{"folderId":"f-new","ok":true}' }],
      }),
    };

    const observed: Array<{ name: string; input: Record<string, unknown> }> = [];
    const result = await runMcpToolLoop({
      anthropic: anthropic as never,
      mcp: mcp as never,
      tools: [{ name: 'create_folder', description: 'd', input_schema: { type: 'object' } }],
      model: 'claude-opus-4-7',
      system: 'you organize folders',
      userMessage: 'add a NewFolder',
      onToolCall: ({ name, input }) => observed.push({ name, input }),
    });

    expect(result.toolCallCount).toBe(1);
    expect(result.stopReason).toBe('end_turn');
    expect(result.text).toBe('Done — created NewFolder');
    expect(mcp.callTool).toHaveBeenCalledWith('create_folder', { name: 'NewFolder' });
    expect(observed).toEqual([{ name: 'create_folder', input: { name: 'NewFolder' } }]);

    // Verify the tool_result was fed back as a user message
    const secondCallMessages = (anthropic.messages.create as unknown as { mock: { calls: Array<[Record<string, unknown>]> } }).mock.calls[1][0].messages as Array<{ role: string; content: unknown }>;
    expect(secondCallMessages).toHaveLength(3);
    expect(secondCallMessages[2].role).toBe('user');
    const toolResultBlocks = secondCallMessages[2].content as Array<{ type: string; tool_use_id: string }>;
    expect(toolResultBlocks[0]).toMatchObject({ type: 'tool_result', tool_use_id: 'toolu_1' });
  });

  it('records is_error=true when MCP throws and continues the loop', async () => {
    const anthropic = {
      messages: {
        create: vi.fn()
          .mockResolvedValueOnce(makeAnthropicResponse({
            stopReason: 'tool_use',
            toolUses: [{ id: 'toolu_1', name: 'broken_tool', input: {} }],
          }))
          .mockResolvedValueOnce(makeAnthropicResponse({
            stopReason: 'end_turn',
            text: 'recovered',
          })),
      },
    };
    const mcp = {
      callTool: vi.fn().mockRejectedValue(new Error('boom')),
    };

    const result = await runMcpToolLoop({
      anthropic: anthropic as never,
      mcp: mcp as never,
      tools: [{ name: 'broken_tool', description: '', input_schema: { type: 'object' } }],
      model: 'claude-opus-4-7',
      system: 's',
      userMessage: 'try it',
    });

    expect(result.toolCallCount).toBe(1);
    expect(result.text).toBe('recovered');

    // Tool result must carry is_error: true so Claude can adapt
    const secondCall = (anthropic.messages.create as unknown as { mock: { calls: Array<[Record<string, unknown>]> } }).mock.calls[1][0];
    const messages = secondCall.messages as Array<{ role: string; content: unknown }>;
    const toolResult = (messages[2].content as Array<Record<string, unknown>>)[0];
    expect(toolResult.is_error).toBe(true);
    expect((toolResult.content as string)).toContain('boom');
  });

  it('stops at maxIterations to avoid infinite loops', async () => {
    const anthropic = {
      messages: {
        create: vi.fn().mockResolvedValue(makeAnthropicResponse({
          stopReason: 'tool_use',
          toolUses: [{ id: 'toolu_x', name: 'noop', input: {} }],
        })),
      },
    };
    const mcp = {
      callTool: vi.fn().mockResolvedValue({ content: [{ type: 'text', text: 'ok' }] }),
    };

    const result = await runMcpToolLoop({
      anthropic: anthropic as never,
      mcp: mcp as never,
      tools: [{ name: 'noop', description: '', input_schema: { type: 'object' } }],
      model: 'claude-opus-4-7',
      system: 's',
      userMessage: 'go',
      maxIterations: 3,
    });

    expect(result.toolCallCount).toBe(3);
    expect(anthropic.messages.create).toHaveBeenCalledTimes(3);
  });

  it('marks the system prompt as cacheable', async () => {
    const anthropic = {
      messages: {
        create: vi.fn().mockResolvedValue(makeAnthropicResponse({ stopReason: 'end_turn', text: 'k' })),
      },
    };
    const mcp = { callTool: vi.fn() };

    await runMcpToolLoop({
      anthropic: anthropic as never,
      mcp: mcp as never,
      tools: [],
      model: 'claude-opus-4-7',
      system: 'a long system prompt that benefits from caching',
      userMessage: 'hi',
    });

    const args = (anthropic.messages.create as unknown as { mock: { calls: Array<[Record<string, unknown>]> } }).mock.calls[0][0];
    const system = args.system as Array<{ type: string; text: string; cache_control?: { type: string } }>;
    expect(Array.isArray(system)).toBe(true);
    expect(system[0].cache_control).toEqual({ type: 'ephemeral' });
  });
});
