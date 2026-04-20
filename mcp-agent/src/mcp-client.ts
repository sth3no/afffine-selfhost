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
  private sessionId: string | null = null;

  constructor(
    private baseUrl: string,
    private workspaceId: string,
    private accessToken: string
  ) {}

  get endpoint(): string {
    return `${this.baseUrl}/api/workspaces/${this.workspaceId}/mcp`;
  }

  /** Build headers for a request, including session id + streamable-http Accept */
  private headers(): Record<string, string> {
    const h: Record<string, string> = {
      'Content-Type': 'application/json',
      // Streamable HTTP transport (MCP spec 2025-03-26) requires the client
      // to accept BOTH application/json (single response) and
      // text/event-stream (streamed response). Omitting this yields 406.
      Accept: 'application/json, text/event-stream',
      Authorization: `Bearer ${this.accessToken}`,
    };
    if (this.sessionId) h['Mcp-Session-Id'] = this.sessionId;
    return h;
  }

  /** Parse an SSE body and return the first JSON-RPC `data:` frame */
  private async parseSseResponse(res: Response): Promise<JsonRpcResponse> {
    const text = await res.text();
    // SSE frames are separated by blank lines. Each frame has one or more
    // "data: <line>" lines. For our RPC use-case we expect exactly one frame
    // containing a single JSON-RPC response.
    const lines = text.split(/\r?\n/);
    const dataLines = lines
      .filter(l => l.startsWith('data:'))
      .map(l => l.slice(5).trimStart());
    if (dataLines.length === 0) {
      throw new Error(`SSE response contained no data frames: ${text.slice(0, 200)}`);
    }
    return JSON.parse(dataLines.join('\n')) as JsonRpcResponse;
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
      headers: this.headers(),
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const errText = await res.text().catch(() => '');
      throw new Error(
        `MCP request failed: ${res.status} ${res.statusText}${errText ? ` — ${errText.slice(0, 300)}` : ''}`
      );
    }

    // Capture session id from the initialize response for subsequent calls
    const newSession = res.headers.get('mcp-session-id');
    if (newSession) this.sessionId = newSession;

    const contentType = res.headers.get('content-type') ?? '';
    let json: JsonRpcResponse;
    if (contentType.includes('text/event-stream')) {
      json = await this.parseSseResponse(res);
    } else {
      json = (await res.json()) as JsonRpcResponse;
    }

    if (json.error) {
      throw new Error(`MCP error [${json.error.code}]: ${json.error.message}`);
    }
    return json.result;
  }

  /** Send a JSON-RPC notification (no id, no response expected) */
  private async notify(method: string, params?: Record<string, unknown>): Promise<void> {
    const res = await fetch(this.endpoint, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify({
        jsonrpc: '2.0',
        method,
        ...(params ? { params } : {}),
      }),
    });
    // Spec says servers SHOULD return 202 Accepted for notifications.
    // Anything in the 2xx range is fine; we don't read the body.
    if (!res.ok) {
      const errText = await res.text().catch(() => '');
      throw new Error(
        `MCP notification failed: ${res.status} ${res.statusText}${errText ? ` — ${errText.slice(0, 300)}` : ''}`
      );
    }
  }

  /** Initialize the MCP session (required before first use) */
  async initialize(): Promise<void> {
    await this.rpc('initialize', {
      protocolVersion: '2025-03-26',
      capabilities: {},
      clientInfo: { name: 'affine-mcp-agent', version: '1.0.0' },
    });
    await this.notify('notifications/initialized');
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
