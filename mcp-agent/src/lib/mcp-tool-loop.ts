/**
 * Anthropic tool-use loop backed by mcp-ext.
 *
 * Why this exists: the existing automations (sources-reorg, daily-digest)
 * use the LLM-as-planner pattern — Claude returns a JSON plan, the agent
 * executes it manually. This wrapper inverts that: it loads tools FROM
 * mcp-ext via `tools/list`, hands them to Claude as `tools=[...]`, then
 * routes each tool_use block back through `mcp_ext.callTool()` until
 * `stop_reason === 'end_turn'`. Result: Claude can improvise — query
 * `list_folder_tree`, decide what to move, call `create_folder` and
 * `move_document` itself.
 *
 * Two ways to give Cloud AI access to mcp-ext exist:
 *   1. This loop  (client-side, no public URL needed) — what we use
 *   2. Anthropic Managed Agents `mcp_servers` parameter (beta) — requires
 *      mcp-ext to be reachable from Anthropic's edge with auth. Kept as
 *      a future option once an external proxy is in place.
 */

import Anthropic from '@anthropic-ai/sdk';
import type { AffineMcpClient } from '../mcp-client.js';

export interface AnthropicToolDefinition {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
}

export interface ToolLoopResult {
  /** Final assistant text (the "answer" after Claude stopped calling tools). */
  text: string;
  /** Number of tool calls executed during the loop (debug / cost tracking). */
  toolCallCount: number;
  /** Final stop_reason — `end_turn` is the success path; anything else is a hint of trouble. */
  stopReason: string | null;
  /** Last raw response (for unit tests + debugging). */
  finalResponse: Anthropic.Message;
}

const DEFAULT_MAX_ITERATIONS = 25;

/**
 * Pull MCP tools from mcp-ext and convert them to the Anthropic SDK shape.
 *
 * mcp-ext returns tools in MCP's standard `{name, description, inputSchema}`
 * shape; Anthropic uses `{name, description, input_schema}`. The conversion
 * is just a key rename + optional name-prefix filter.
 */
export async function loadMcpToolsForAnthropic(
  mcp: AffineMcpClient,
  opts: {
    /** If set, only include tools whose name starts with one of these prefixes. */
    includePrefixes?: string[];
    /** If set, exclude tools whose name starts with one of these prefixes. */
    excludePrefixes?: string[];
    /** Drop tools whose name is in this set. */
    excludeNames?: string[];
  } = {},
): Promise<AnthropicToolDefinition[]> {
  // mcp-client.ts's listTools() drops inputSchema; call the raw tools/list
  // RPC via callTool's underlying transport. Easiest: use the public
  // method if mcp-client.ts has been extended; otherwise rebuild here.
  const raw = await rawListTools(mcp);
  const out: AnthropicToolDefinition[] = [];
  for (const t of raw) {
    if (opts.excludeNames?.includes(t.name)) continue;
    if (opts.includePrefixes && !opts.includePrefixes.some(p => t.name.startsWith(p))) continue;
    if (opts.excludePrefixes?.some(p => t.name.startsWith(p))) continue;
    out.push({
      name: t.name,
      description: t.description ?? '',
      input_schema: (t.inputSchema as Record<string, unknown>) ?? {
        type: 'object',
        properties: {},
      },
    });
  }
  return out;
}

interface RawTool {
  name: string;
  description?: string;
  inputSchema?: unknown;
}

/**
 * Fetch the raw tools/list response (with inputSchema). The shipped
 * `AffineMcpClient.listTools()` strips inputSchema for terseness, which
 * doesn't fit Anthropic's tool format — so we peek at the private rpc
 * method. If the client is later extended to keep inputSchema, this can
 * delegate to the public method.
 */
async function rawListTools(mcp: AffineMcpClient): Promise<RawTool[]> {
  // Cast to any to reach the private `rpc` method without changing its
  // visibility on the shared client. `tools/list` is a stable MCP method.
  const result = (await (mcp as unknown as { rpc: (m: string, p?: object) => Promise<unknown> }).rpc('tools/list')) as {
    tools: RawTool[];
  };
  return result.tools ?? [];
}

/**
 * Run an Anthropic message with tool-use, routing every tool_use block
 * back through the MCP client until Claude stops calling tools or we
 * exceed maxIterations.
 *
 * Caller supplies the system prompt + first user message; tools are
 * loaded once at the start (Anthropic re-uses the same array across
 * iterations, so prompt caching stays warm).
 */
export async function runMcpToolLoop(opts: {
  anthropic: Anthropic;
  mcp: AffineMcpClient;
  tools: AnthropicToolDefinition[];
  model: string;
  system: string;
  userMessage: string;
  maxTokens?: number;
  maxIterations?: number;
  /** Called for each tool execution — useful for logging without changing the core loop. */
  onToolCall?: (call: { name: string; input: Record<string, unknown>; result: string; isError: boolean }) => void;
}): Promise<ToolLoopResult> {
  const maxIterations = opts.maxIterations ?? DEFAULT_MAX_ITERATIONS;
  const messages: Anthropic.MessageParam[] = [
    { role: 'user', content: opts.userMessage },
  ];

  let toolCallCount = 0;
  let response: Anthropic.Message | null = null;

  for (let iter = 0; iter < maxIterations; iter++) {
    response = await opts.anthropic.messages.create({
      model: opts.model,
      max_tokens: opts.maxTokens ?? 16000,
      // Mark the system prompt as cacheable so subsequent loop iterations
      // (and later runs within the 5-minute TTL) reuse the prefix.
      system: [
        {
          type: 'text',
          text: opts.system,
          cache_control: { type: 'ephemeral' },
        },
      ],
      tools: opts.tools as unknown as Anthropic.ToolUnion[],
      messages,
    });

    if (response.stop_reason === 'end_turn') break;
    if (response.stop_reason !== 'tool_use') {
      // pause_turn / max_tokens / refusal — surface and bail.
      break;
    }

    // Append the assistant turn (must include the full content array
    // with tool_use blocks so the next request links tool_results to ids).
    messages.push({ role: 'assistant', content: response.content });

    const toolResults: Anthropic.ToolResultBlockParam[] = [];
    for (const block of response.content) {
      if (block.type !== 'tool_use') continue;
      toolCallCount++;
      const toolInput = (block.input ?? {}) as Record<string, unknown>;

      let resultText: string;
      let isError = false;
      try {
        const out = await opts.mcp.callTool(block.name, toolInput);
        resultText = out.content?.[0]?.text ?? '';
      } catch (e) {
        isError = true;
        resultText = `error: ${e instanceof Error ? e.message : String(e)}`;
      }

      opts.onToolCall?.({
        name: block.name,
        input: toolInput,
        result: resultText,
        isError,
      });

      toolResults.push({
        type: 'tool_result',
        tool_use_id: block.id,
        content: resultText,
        is_error: isError,
      });
    }

    messages.push({ role: 'user', content: toolResults });
  }

  if (response === null) {
    throw new Error('runMcpToolLoop: no response was produced');
  }

  const finalText = response.content
    .filter((b): b is Anthropic.TextBlock => b.type === 'text')
    .map(b => b.text)
    .join('\n');

  return {
    text: finalText,
    toolCallCount,
    stopReason: response.stop_reason ?? null,
    finalResponse: response,
  };
}
