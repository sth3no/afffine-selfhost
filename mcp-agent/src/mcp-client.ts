/**
 * AFFiNE MCP Client
 *
 * Lightweight JSON-RPC 2.0 client for AFFiNE's MCP endpoint.
 * Handles authentication, request formatting, and response parsing.
 */

export interface McpToolResult {
  content: Array<{ type: 'text'; text: string }>;
  isError?: boolean;
}

interface JsonRpcResponse {
  jsonrpc: '2.0';
  id: number;
  result?: unknown;
  error?: { code: number; message: string };
}

export class AffineMcpClient {
  private requestId = 0;

  constructor(
    private baseUrl: string,
    private workspaceId: string,
    private accessToken: string
  ) {}

  get endpoint(): string {
    return `${this.baseUrl}/api/workspaces/${this.workspaceId}/mcp`;
  }

  /** Send a raw JSON-RPC request */
  private async rpc(method: string, params?: Record<string, unknown>): Promise<unknown> {
    const id = ++this.requestId;
    const body = {
      jsonrpc: '2.0',
      id,
      method,
      params: params ?? {},
    };

    const res = await fetch(this.endpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${this.accessToken}`,
      },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      throw new Error(`MCP request failed: ${res.status} ${res.statusText}`);
    }

    const json = (await res.json()) as JsonRpcResponse;

    if (json.error) {
      throw new Error(`MCP error [${json.error.code}]: ${json.error.message}`);
    }

    return json.result;
  }

  /** Initialize the MCP session (required before first use) */
  async initialize(): Promise<void> {
    await this.rpc('initialize', {
      protocolVersion: '2025-03-26',
      capabilities: {},
      clientInfo: { name: 'affine-mcp-agent', version: '1.0.0' },
    });
    // Send initialized notification (no id = notification)
    await fetch(this.endpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${this.accessToken}`,
      },
      body: JSON.stringify({
        jsonrpc: '2.0',
        method: 'notifications/initialized',
      }),
    });
  }

  /** List all available tools */
  async listTools(): Promise<Array<{ name: string; title: string; description: string }>> {
    const result = (await this.rpc('tools/list')) as {
      tools: Array<{ name: string; title: string; description: string }>;
    };
    return result.tools;
  }

  /** Call a tool by name with arguments */
  async callTool(name: string, args: Record<string, unknown> = {}): Promise<McpToolResult> {
    const result = (await this.rpc('tools/call', { name, arguments: args })) as McpToolResult;
    return result;
  }

  // ── Convenience methods ─────────────────────────────────────

  async listDocuments(limit = 20, offset = 0) {
    const result = await this.callTool('list_documents', { limit, offset });
    return JSON.parse(result.content[0].text);
  }

  async readDocument(docId: string): Promise<string> {
    const result = await this.callTool('read_document', { docId });
    return result.content[0].text;
  }

  async getDocumentInfo(docId: string) {
    const result = await this.callTool('get_document_info', { docId });
    return JSON.parse(result.content[0].text);
  }

  async getWorkspaceInfo() {
    const result = await this.callTool('get_workspace_info');
    return JSON.parse(result.content[0].text);
  }

  async listMembers(limit = 20) {
    const result = await this.callTool('list_workspace_members', { limit });
    return JSON.parse(result.content[0].text);
  }

  async listComments(docId: string, limit = 20) {
    const result = await this.callTool('list_comments', { docId, limit });
    return JSON.parse(result.content[0].text);
  }

  async listBlobs() {
    const result = await this.callTool('list_blobs');
    return JSON.parse(result.content[0].text);
  }

  async listDocumentHistory(docId: string, limit = 20) {
    const result = await this.callTool('list_document_history', { docId, limit });
    return JSON.parse(result.content[0].text);
  }

  async listNotifications(limit = 20) {
    const result = await this.callTool('list_notifications', { limit });
    return JSON.parse(result.content[0].text);
  }

  async keywordSearch(query: string) {
    const result = await this.callTool('keyword_search', { query });
    return result.content.map(c => JSON.parse(c.text));
  }

  async semanticSearch(query: string) {
    const result = await this.callTool('semantic_search', { query });
    return result.content.map(c => c.text);
  }

  async advancedSearch(query: string, limit = 10) {
    const result = await this.callTool('advanced_search', { query, limit });
    return JSON.parse(result.content[0].text);
  }

  // Write operations (dev/canary only)

  async createDocument(title: string, content: string) {
    const result = await this.callTool('create_document', { title, content });
    return JSON.parse(result.content[0].text);
  }

  async updateDocument(docId: string, content: string) {
    const result = await this.callTool('update_document', { docId, content });
    return JSON.parse(result.content[0].text);
  }

  async deleteDocument(docId: string) {
    const result = await this.callTool('delete_document', { docId });
    return JSON.parse(result.content[0].text);
  }

  async createComment(docId: string, content: string) {
    const result = await this.callTool('create_comment', { docId, content });
    return JSON.parse(result.content[0].text);
  }

  async resolveComment(commentId: string, resolved = true) {
    const result = await this.callTool('resolve_comment', { commentId, resolved });
    return JSON.parse(result.content[0].text);
  }

  async markNotificationRead(notificationId?: string, all = false) {
    const result = await this.callTool('mark_notification_read', {
      notificationId,
      all,
    });
    return JSON.parse(result.content[0].text);
  }
}
