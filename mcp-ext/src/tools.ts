/**
 * Tool registry for the MCP extension proxy.
 *
 * Each entry has:
 *  - name, description, inputSchema (exposed via MCP tools/list)
 *  - handler (called on tools/call, returns a string that will be wrapped
 *    into MCP's {content: [{type:"text", text}], isError?} envelope)
 *
 * There are two kinds of tools:
 *
 *  1. GraphQL-backed tools — we query AFFiNE's GraphQL API and return the
 *     data as JSON. These implement the read-only operations that
 *     affine-mcp-agent expects (list_documents, get_workspace_info, ...).
 *
 *  2. Native forwards — for doc-read / doc-keyword-search /
 *     doc-semantic-search we pass the call through to AFFiNE's built-in
 *     MCP server untouched. We register underscore-named aliases too
 *     (read_document, keyword_search, semantic_search) for compatibility
 *     with the agent's naming convention.
 */

import { gql } from './graphql.js';
import { config } from './config.js';
import { callNativeTool } from './forward.js';

export interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: {
    type: 'object';
    properties: Record<string, { type: string; description?: string }>;
    required?: string[];
  };
  handler: (token: string, args: Record<string, unknown>) => Promise<string>;
}

const wsId = () => config.workspaceId;

// ── GraphQL-backed tools ──────────────────────────────────────────

const listDocuments: ToolDefinition = {
  name: 'list_documents',
  description:
    'List documents in the AFFiNE workspace with their metadata (id, title, timestamps, author).',
  inputSchema: {
    type: 'object',
    properties: {
      limit: { type: 'number', description: 'Max results, default 20' },
      offset: { type: 'number', description: 'Pagination offset, default 0' },
    },
  },
  async handler(token, args) {
    const limit = Number(args.limit ?? 20);
    const offset = Number(args.offset ?? 0);
    const data = await gql<{
      workspace: {
        docs: {
          totalCount: number;
          pageInfo: { hasNextPage: boolean };
          edges: Array<{
            node: {
              id: string;
              title: string | null;
              createdAt: string;
              updatedAt: string;
              createdBy: { name: string; avatarUrl: string | null } | null;
            };
          }>;
        };
      };
    }>(
      token,
      `query ($id: String!, $first: Int!, $offset: Int!) {
        workspace(id: $id) {
          docs(pagination: { first: $first, offset: $offset }) {
            totalCount
            pageInfo { hasNextPage }
            edges { node {
              id
              title
              createdAt
              updatedAt
              createdBy { name avatarUrl }
            } }
          }
        }
      }`,
      { id: wsId(), first: limit, offset }
    );
    const documents = data.workspace.docs.edges.map(e => ({
      docId: e.node.id,
      title: e.node.title,
      createdAt: e.node.createdAt,
      updatedAt: e.node.updatedAt,
      createdBy: e.node.createdBy?.name ?? null,
    }));
    return JSON.stringify(
      {
        total: data.workspace.docs.totalCount,
        hasMore: data.workspace.docs.pageInfo.hasNextPage,
        documents,
      },
      null,
      2
    );
  },
};

const getWorkspaceInfo: ToolDefinition = {
  name: 'get_workspace_info',
  description: 'Get metadata about the AFFiNE workspace (name, owner, team status, member count).',
  inputSchema: { type: 'object', properties: {} },
  async handler(token) {
    const data = await gql<{
      workspace: {
        id: string;
        team: boolean;
        memberCount: number;
        owner: { id: string; name: string; email: string } | null;
      };
    }>(
      token,
      `query ($id: String!) {
        workspace(id: $id) {
          id
          team
          memberCount
          owner { id name email }
        }
      }`,
      { id: wsId() }
    );
    // AFFiNE stores the workspace display name inside the Yjs doc, not on
    // the server — GraphQL has no `name` field. Use the id as a stable
    // display fallback so `workspace.name` in callers isn't undefined.
    return JSON.stringify(
      {
        id: data.workspace.id,
        name: data.workspace.id,
        team: data.workspace.team,
        memberCount: data.workspace.memberCount,
        owner: data.workspace.owner,
      },
      null,
      2
    );
  },
};

const listMembers: ToolDefinition = {
  name: 'list_workspace_members',
  description: 'List members of the AFFiNE workspace with their role (owner/admin/member).',
  inputSchema: {
    type: 'object',
    properties: {
      limit: { type: 'number', description: 'Max results, default 20' },
    },
  },
  async handler(token, args) {
    const take = Number(args.limit ?? 20);
    const data = await gql<{
      workspace: {
        members: Array<{ id: string; name: string; email: string; permission: string }>;
      };
    }>(
      token,
      `query ($id: String!, $take: Int!) {
        workspace(id: $id) {
          members(skip: 0, take: $take) {
            id name email permission
          }
        }
      }`,
      { id: wsId(), take }
    );
    // Wrap in { total, members } so callers that read .total don't see
    // undefined. This uses the returned length; for exact count across
    // pagination, AFFiNE exposes workspace.memberCount separately
    // (see get_workspace_info).
    return JSON.stringify(
      {
        total: data.workspace.members.length,
        members: data.workspace.members,
      },
      null,
      2
    );
  },
};

const listComments: ToolDefinition = {
  name: 'list_comments',
  description: 'List comments on a specific document in the AFFiNE workspace.',
  inputSchema: {
    type: 'object',
    properties: {
      docId: { type: 'string', description: 'Document ID' },
      limit: { type: 'number', description: 'Max results, default 20' },
    },
    required: ['docId'],
  },
  async handler(token, args) {
    const docId = String(args.docId);
    const limit = Number(args.limit ?? 20);
    const data = await gql<{
      workspace: {
        comments: {
          edges: Array<{
            node: {
              id: string;
              content: string;
              resolved: boolean;
              createdAt: string;
              user: { name: string } | null;
            };
          }>;
          pageInfo: { hasNextPage: boolean };
        };
      };
    }>(
      token,
      `query ($id: String!, $docId: String!, $first: Int!) {
        workspace(id: $id) {
          comments(docId: $docId, pagination: { first: $first }) {
            edges { node {
              id content resolved createdAt
              user { name }
            } }
            pageInfo { hasNextPage }
          }
        }
      }`,
      { id: wsId(), docId, first: limit }
    );
    const comments = data.workspace.comments.edges.map(e => ({
      id: e.node.id,
      content: e.node.content,
      resolved: e.node.resolved,
      author: e.node.user?.name ?? null,
      createdAt: e.node.createdAt,
    }));
    return JSON.stringify(
      {
        total: comments.length, // in-page count; for exact count add totalCount to the query if AFFiNE exposes it
        comments,
        hasMore: data.workspace.comments.pageInfo.hasNextPage,
      },
      null,
      2
    );
  },
};

const listDocumentHistory: ToolDefinition = {
  name: 'list_document_history',
  description: 'List version history (snapshots by timestamp) for a document.',
  inputSchema: {
    type: 'object',
    properties: {
      docId: { type: 'string', description: 'Document GUID' },
      limit: { type: 'number', description: 'Max results, default 20' },
    },
    required: ['docId'],
  },
  async handler(token, args) {
    const docId = String(args.docId);
    const take = Number(args.limit ?? 20);
    const data = await gql<{
      workspace: {
        histories: Array<{
          id: string;
          timestamp: string;
          editor: { name: string } | null;
        }>;
      };
    }>(
      token,
      `query ($id: String!, $guid: String!, $take: Int!) {
        workspace(id: $id) {
          histories(guid: $guid, take: $take) {
            id timestamp editor { name }
          }
        }
      }`,
      { id: wsId(), guid: docId, take }
    );
    return JSON.stringify(data.workspace.histories, null, 2);
  },
};

const getDocumentInfo: ToolDefinition = {
  name: 'get_document_info',
  description: 'Get metadata for a single document (title, timestamps, author) without its content.',
  inputSchema: {
    type: 'object',
    properties: {
      docId: { type: 'string', description: 'Document ID' },
    },
    required: ['docId'],
  },
  async handler(token, args) {
    const docId = String(args.docId);
    // AFFiNE's workspace.docs doesn't filter by id directly; easiest portable
    // approach is to look it up in the paginated list. For workspaces with
    // many docs this is suboptimal — swap to a direct lookup if a dedicated
    // resolver (workspace.doc(id:)) is added upstream later.
    const data = await gql<{
      workspace: {
        docs: {
          edges: Array<{
            node: {
              id: string;
              title: string | null;
              createdAt: string;
              updatedAt: string;
              createdBy: { name: string } | null;
            };
          }>;
        };
      };
    }>(
      token,
      `query ($id: String!) {
        workspace(id: $id) {
          docs(pagination: { first: 1000, offset: 0 }) {
            edges { node {
              id title createdAt updatedAt
              createdBy { name }
            } }
          }
        }
      }`,
      { id: wsId() }
    );
    const hit = data.workspace.docs.edges.find(e => e.node.id === docId);
    if (!hit) {
      throw new Error(`Document ${docId} not found in workspace ${wsId()}`);
    }
    return JSON.stringify(
      {
        docId: hit.node.id,
        title: hit.node.title,
        createdAt: hit.node.createdAt,
        updatedAt: hit.node.updatedAt,
        createdBy: hit.node.createdBy?.name ?? null,
      },
      null,
      2
    );
  },
};

// ── Stubs (intentionally empty, for agent compatibility) ────────────

const listNotifications: ToolDefinition = {
  name: 'list_notifications',
  description:
    'List user notifications. Stubbed — returns an empty list in the shape callers expect.',
  inputSchema: {
    type: 'object',
    properties: { limit: { type: 'number' } },
  },
  async handler() {
    return JSON.stringify(
      { total: 0, notifications: [], note: 'Not implemented in proxy yet' },
      null,
      2
    );
  },
};

const listBlobs: ToolDefinition = {
  name: 'list_blobs',
  description:
    'List blobs (attachments) in the workspace. Stubbed — returns an empty list in the shape callers expect.',
  inputSchema: { type: 'object', properties: {} },
  async handler() {
    return JSON.stringify(
      { total: 0, blobs: [], note: 'Not implemented in proxy yet' },
      null,
      2
    );
  },
};

// ── Native forwards with underscore aliases ─────────────────────────

function forwardAlias(
  alias: string,
  nativeName: string,
  description: string,
  inputSchema: ToolDefinition['inputSchema']
): ToolDefinition {
  return {
    name: alias,
    description,
    inputSchema,
    async handler(token, args) {
      const result = (await callNativeTool(token, nativeName, args)) as {
        content: Array<{ type: string; text: string }>;
        isError?: boolean;
      };
      // Native MCP returns { content: [{type:text, text}], isError }.
      // Our dispatcher will re-wrap handler's string return into that
      // shape, so unwrap it here and rethrow on isError.
      if (result.isError) {
        throw new Error(result.content?.[0]?.text ?? 'native tool failed');
      }
      return result.content?.[0]?.text ?? '';
    },
  };
}

const readDocument = forwardAlias(
  'read_document',
  'doc-read',
  'Read the full content of a document by its ID (forwarded to AFFiNE native MCP).',
  {
    type: 'object',
    properties: { docId: { type: 'string', description: 'Document ID' } },
    required: ['docId'],
  }
);

const keywordSearch = forwardAlias(
  'keyword_search',
  'doc-keyword-search',
  'Fuzzy keyword search across workspace documents (forwarded to AFFiNE native MCP).',
  {
    type: 'object',
    properties: { query: { type: 'string', description: 'Search query' } },
    required: ['query'],
  }
);

const semanticSearch = forwardAlias(
  'semantic_search',
  'doc-semantic-search',
  'Vector semantic similarity search across documents (forwarded to AFFiNE native MCP).',
  {
    type: 'object',
    properties: { query: { type: 'string', description: 'Search query' } },
    required: ['query'],
  }
);

// ── Registry ────────────────────────────────────────────────────────

export const tools: ToolDefinition[] = [
  listDocuments,
  getWorkspaceInfo,
  listMembers,
  listComments,
  listDocumentHistory,
  getDocumentInfo,
  listNotifications,
  listBlobs,
  readDocument,
  keywordSearch,
  semanticSearch,
];

export const toolByName = new Map(tools.map(t => [t.name, t] as const));
