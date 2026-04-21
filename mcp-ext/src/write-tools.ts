/**
 * Write-capable tools for the MCP extension proxy.
 *
 * Scoped strictly to CONTENT INSIDE the workspace:
 *   - Create / modify / trash docs
 *   - Append / insert / update / delete blocks within docs
 *   - Comments CRUD
 *
 * Deliberately NOT implemented (safety boundary — workspace-level config
 * and membership are out of reach of any AI caller):
 *   - updateWorkspace / deleteWorkspace
 *   - inviteMembers / revokeMember / grantMember / leaveWorkspace
 *   - createInviteLink / revokeInviteLink
 *   - grantDocUserRoles / revokeDocUserRoles / updateDocUserRole
 *   - deleteBlob
 *   - publishDoc / revokePublicDoc  (exposes content publicly — out of scope)
 */

import * as Y from 'yjs';
import type { ToolDefinition } from './tools-shared.js';
import { config } from './config.js';
import { gql } from './graphql.js';
import { openDoc, readDoc } from './yjs-writer.js';
import {
  addBlockFromSpec,
  appendChildren,
  findFirstNoteBlockId,
  findHeadingByText,
  findPageBlockId,
  findParentBlockId,
  getBlocksMap,
  initializeEmptyDoc,
  insertChildrenAt,
  listBlocks,
  newDocGuid,
  readDocTitle,
  SYS_CHILDREN,
  SYS_FLAVOUR,
  type BlockSpec,
  type InlineOp,
} from './block-builder.js';
import { addPage, listPages, setPageTitle, trashPage } from './doc-store.js';

const wsId = () => config.workspaceId;

/** Normalize a block spec coming over JSON-RPC into a typed BlockSpec. */
function parseBlockSpec(raw: unknown, i: number): BlockSpec {
  if (!raw || typeof raw !== 'object') {
    throw new Error(`blocks[${i}]: expected an object`);
  }
  const b = raw as Record<string, unknown>;
  const type = b.type;
  const text = b.text;

  if (type === 'divider') return { type: 'divider' };

  if (type === 'paragraph') {
    if (typeof text !== 'string' && !Array.isArray(text)) {
      throw new Error(`blocks[${i}]: paragraph needs string or InlineOp[] "text"`);
    }
    const style = b.style;
    if (
      style !== undefined &&
      style !== 'text' &&
      style !== 'h1' && style !== 'h2' && style !== 'h3' &&
      style !== 'h4' && style !== 'h5' && style !== 'h6' &&
      style !== 'quote'
    ) {
      throw new Error(`blocks[${i}]: paragraph "style" must be text|h1..h6|quote`);
    }
    return {
      type: 'paragraph',
      style: style as 'text' | 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6' | 'quote' | undefined,
      text: text as string | InlineOp[],
    };
  }

  if (type === 'list') {
    const style = b.style;
    if (style !== 'bulleted' && style !== 'numbered' && style !== 'todo' && style !== 'toggle') {
      throw new Error(`blocks[${i}]: list "style" must be bulleted|numbered|todo|toggle`);
    }
    if (typeof text !== 'string' && !Array.isArray(text)) {
      throw new Error(`blocks[${i}]: list needs string or InlineOp[] "text"`);
    }
    return {
      type: 'list',
      style,
      text: text as string | InlineOp[],
      checked: b.checked === true,
    };
  }

  if (type === 'code') {
    if (typeof text !== 'string') {
      throw new Error(`blocks[${i}]: code needs string "text"`);
    }
    return {
      type: 'code',
      text,
      language: typeof b.language === 'string' ? b.language : 'plaintext',
    };
  }

  throw new Error(`blocks[${i}]: unknown type "${String(type)}". Expected paragraph|list|code|divider.`);
}

function parseBlocks(raw: unknown): BlockSpec[] {
  if (!Array.isArray(raw)) throw new Error('"blocks" must be an array');
  return raw.map((b, i) => parseBlockSpec(b, i));
}

const BLOCK_SPEC_SCHEMA = {
  type: 'array',
  description:
    'Array of block specs. Each: { type: "paragraph"|"list"|"code"|"divider", text?, style?, language?, checked? }. ' +
    'text can be a plain string OR an array of inline ops: [{text:"hello",bold:true}, {text:" see ",refDocId:"<docId>"}] — ' +
    'refDocId renders as an @DocName pill linking to that doc.',
  items: { type: 'object' },
};

// ── Doc content tools ───────────────────────────────────────────────

const createDoc: ToolDefinition = {
  name: 'create_doc',
  description:
    'Create a new document in the workspace. Returns the new docId. Optional initialBlocks are appended after the page title.',
  inputSchema: {
    type: 'object',
    properties: {
      title: { type: 'string', description: 'Document title (shown in sidebar and as page heading).' },
      initialBlocks: BLOCK_SPEC_SCHEMA,
    },
    required: ['title'],
  },
  async handler(token, args) {
    const title = String(args.title ?? '').trim();
    if (!title) throw new Error('"title" is required');
    const initialBlocks = args.initialBlocks ? parseBlocks(args.initialBlocks) : [];

    const docId = newDocGuid();

    // 1. Create the new doc's Yjs content
    const newDoc = await openDoc(token, docId);
    const { noteId } = initializeEmptyDoc(newDoc.doc, title);

    if (initialBlocks.length > 0) {
      const note = getBlocksMap(newDoc.doc).get(noteId)!;
      const newIds = initialBlocks.map(spec => addBlockFromSpec(newDoc.doc, spec));
      appendChildren(note, newIds);
    }
    await newDoc.commit();

    // 2. Register the doc in the workspace root doc's meta.pages
    const root = await openDoc(token, wsId());
    addPage(root.doc, docId, title);
    await root.commit();

    return JSON.stringify({ docId, title, blocksAdded: initialBlocks.length }, null, 2);
  },
};

const appendBlocks: ToolDefinition = {
  name: 'append_blocks',
  description:
    'Append blocks to an existing document. By default appends to the end of the first note block. ' +
    'If `afterHeading` is given, finds a heading (h1..h6) with that exact text (case-insensitive) and inserts ' +
    'immediately after it — ideal for appending under a template section like "AI summary". ' +
    'Alternatively pass `afterBlockId` for an explicit anchor.',
  inputSchema: {
    type: 'object',
    properties: {
      docId: { type: 'string', description: 'Target document id.' },
      blocks: BLOCK_SPEC_SCHEMA,
      afterHeading: {
        type: 'string',
        description: 'Optional: heading text to anchor against (case-insensitive exact match).',
      },
      afterBlockId: {
        type: 'string',
        description: 'Optional: explicit block id to insert after. Takes precedence over afterHeading.',
      },
    },
    required: ['docId', 'blocks'],
  },
  async handler(token, args) {
    const docId = String(args.docId ?? '');
    if (!docId) throw new Error('"docId" is required');
    const blocks = parseBlocks(args.blocks);
    if (blocks.length === 0) return JSON.stringify({ docId, blocksAdded: 0 }, null, 2);

    const { doc, commit } = await openDoc(token, docId);
    if (getBlocksMap(doc).size === 0) {
      throw new Error(`Document ${docId} not found or empty on server`);
    }

    let anchorId: string | null = null;
    if (args.afterBlockId) {
      anchorId = String(args.afterBlockId);
      if (!getBlocksMap(doc).has(anchorId)) {
        throw new Error(`afterBlockId "${anchorId}" not found in doc`);
      }
    } else if (args.afterHeading) {
      anchorId = findHeadingByText(doc, String(args.afterHeading));
      if (!anchorId) {
        throw new Error(`No heading matching "${args.afterHeading}" found in doc`);
      }
    }

    const newIds = blocks.map(spec => addBlockFromSpec(doc, spec));

    if (anchorId) {
      const parentId = findParentBlockId(doc, anchorId);
      if (!parentId) throw new Error(`Anchor block ${anchorId} has no parent in doc`);
      const parent = getBlocksMap(doc).get(parentId)!;
      const children = parent.get(SYS_CHILDREN) as Y.Array<string>;
      const idx = children.toArray().indexOf(anchorId);
      insertChildrenAt(parent, idx + 1, newIds);
    } else {
      const noteId = findFirstNoteBlockId(doc);
      if (!noteId) throw new Error(`Doc ${docId} has no note block to append into`);
      const note = getBlocksMap(doc).get(noteId)!;
      appendChildren(note, newIds);
    }

    await commit();
    return JSON.stringify({ docId, blocksAdded: newIds.length, blockIds: newIds }, null, 2);
  },
};

const updateBlockText: ToolDefinition = {
  name: 'update_block_text',
  description:
    'Replace the text content of an existing paragraph/list/code block. The new text may be a plain string ' +
    'or an array of inline ops (same format as blocks in create_doc / append_blocks). ' +
    'For paragraphs you can also change the `style` (text|h1..h6|quote).',
  inputSchema: {
    type: 'object',
    properties: {
      docId: { type: 'string' },
      blockId: { type: 'string' },
      text: {
        type: 'array',
        description: 'String or array of inline ops. Plain string is accepted too.',
        items: { type: 'object' },
      },
      style: { type: 'string', description: 'Optional new style for paragraph blocks.' },
    },
    required: ['docId', 'blockId', 'text'],
  },
  async handler(token, args) {
    const docId = String(args.docId ?? '');
    const blockId = String(args.blockId ?? '');
    if (!docId || !blockId) throw new Error('"docId" and "blockId" are required');

    const { doc, commit } = await openDoc(token, docId);
    const block = getBlocksMap(doc).get(blockId);
    if (!block) throw new Error(`Block ${blockId} not found in doc ${docId}`);
    const flavour = String(block.get(SYS_FLAVOUR) ?? '');

    const textInput = args.text;
    // Build new delta
    const delta: Array<{ insert: string; attributes?: Record<string, unknown> }> = [];
    if (typeof textInput === 'string') {
      if (textInput.length > 0) delta.push({ insert: textInput });
    } else if (Array.isArray(textInput)) {
      for (const op of textInput) {
        if (typeof op === 'string') {
          if (op.length > 0) delta.push({ insert: op });
          continue;
        }
        if (!op || typeof op !== 'object') continue;
        const o = op as Record<string, unknown>;
        const text = typeof o.text === 'string' ? o.text : '';
        if (!text) continue;
        const attrs: Record<string, unknown> = {};
        if (o.bold) attrs.bold = true;
        if (o.italic) attrs.italic = true;
        if (o.underline) attrs.underline = true;
        if (o.strike) attrs.strike = true;
        if (o.code) attrs.code = true;
        if (typeof o.link === 'string') attrs.link = o.link;
        if (typeof o.refDocId === 'string') {
          attrs.reference = { type: 'LinkedPage', pageId: o.refDocId };
        }
        const d: { insert: string; attributes?: Record<string, unknown> } = { insert: text };
        if (Object.keys(attrs).length > 0) d.attributes = attrs;
        delta.push(d);
      }
    } else {
      throw new Error('"text" must be a string or array of inline ops');
    }

    const existingText = block.get('prop:text');
    if (!(existingText instanceof Y.Text)) {
      throw new Error(`Block ${blockId} has no prop:text (flavour=${flavour})`);
    }
    // Replace: delete all, insert fresh delta
    if (existingText.length > 0) existingText.delete(0, existingText.length);
    if (delta.length > 0) existingText.applyDelta(delta);

    if (typeof args.style === 'string' && flavour === 'affine:paragraph') {
      block.set('prop:type', args.style);
    }

    await commit();
    return JSON.stringify({ docId, blockId, ok: true }, null, 2);
  },
};

const deleteBlock: ToolDefinition = {
  name: 'delete_block',
  description:
    'Delete a block from a document. Removes it from the blocks map AND from its parent\'s children array. ' +
    'Cannot delete the page block or the only note block.',
  inputSchema: {
    type: 'object',
    properties: {
      docId: { type: 'string' },
      blockId: { type: 'string' },
    },
    required: ['docId', 'blockId'],
  },
  async handler(token, args) {
    const docId = String(args.docId ?? '');
    const blockId = String(args.blockId ?? '');
    if (!docId || !blockId) throw new Error('"docId" and "blockId" are required');

    const { doc, commit } = await openDoc(token, docId);
    const blocks = getBlocksMap(doc);
    const block = blocks.get(blockId);
    if (!block) throw new Error(`Block ${blockId} not found`);
    const flavour = block.get(SYS_FLAVOUR);
    if (flavour === 'affine:page') throw new Error('Refusing to delete the page block');
    if (flavour === 'affine:note' && findFirstNoteBlockId(doc) === blockId) {
      throw new Error('Refusing to delete the only note block — the doc would become unusable');
    }

    const parentId = findParentBlockId(doc, blockId);
    if (parentId) {
      const parent = blocks.get(parentId)!;
      const children = parent.get(SYS_CHILDREN) as Y.Array<string>;
      const idx = children.toArray().indexOf(blockId);
      if (idx >= 0) children.delete(idx, 1);
    }
    // Also remove any grandchildren recursively.
    const toRemove: string[] = [blockId];
    const visited = new Set<string>();
    while (toRemove.length > 0) {
      const id = toRemove.pop()!;
      if (visited.has(id)) continue;
      visited.add(id);
      const b = blocks.get(id);
      if (!b) continue;
      const c = b.get(SYS_CHILDREN);
      if (c instanceof Y.Array) for (const cid of (c as Y.Array<string>).toArray()) toRemove.push(cid);
      blocks.delete(id);
    }

    await commit();
    return JSON.stringify({ docId, deleted: [...visited] }, null, 2);
  },
};

const setDocTitle: ToolDefinition = {
  name: 'set_doc_title',
  description: 'Rename a document. Updates both the page block\'s prop:title and the workspace sidebar entry.',
  inputSchema: {
    type: 'object',
    properties: {
      docId: { type: 'string' },
      title: { type: 'string' },
    },
    required: ['docId', 'title'],
  },
  async handler(token, args) {
    const docId = String(args.docId ?? '');
    const title = String(args.title ?? '').trim();
    if (!docId || !title) throw new Error('"docId" and non-empty "title" are required');

    const { doc, commit } = await openDoc(token, docId);
    const pageId = findPageBlockId(doc);
    if (!pageId) throw new Error(`Doc ${docId} has no page block`);
    const page = getBlocksMap(doc).get(pageId)!;
    const titleText = page.get('prop:title');
    if (titleText instanceof Y.Text) {
      if (titleText.length > 0) titleText.delete(0, titleText.length);
      if (title.length > 0) titleText.insert(0, title);
    } else {
      const t = new Y.Text();
      if (title.length > 0) t.insert(0, title);
      page.set('prop:title', t);
    }
    await commit();

    const root = await openDoc(token, wsId());
    setPageTitle(root.doc, docId, title);
    await root.commit();

    return JSON.stringify({ docId, title, ok: true }, null, 2);
  },
};

const trashDoc: ToolDefinition = {
  name: 'delete_doc',
  description:
    'Soft-delete a document — moves it to the workspace trash. Recoverable from AFFiNE UI. ' +
    'Does NOT permanently destroy content. No hard-delete tool is provided by design.',
  inputSchema: {
    type: 'object',
    properties: { docId: { type: 'string' } },
    required: ['docId'],
  },
  async handler(token, args) {
    const docId = String(args.docId ?? '');
    if (!docId) throw new Error('"docId" is required');
    const root = await openDoc(token, wsId());
    const ok = trashPage(root.doc, docId);
    if (!ok) throw new Error(`Doc ${docId} not found in workspace meta.pages`);
    await root.commit();
    return JSON.stringify({ docId, trashed: true }, null, 2);
  },
};

// ── Read helpers for write-planning agents ──────────────────────────

const listDocBlocks: ToolDefinition = {
  name: 'list_doc_blocks',
  description:
    'List the block structure of a doc: id, flavour, style (for paragraph/list), and a text preview. ' +
    'Useful for locating a heading to anchor appended content against.',
  inputSchema: {
    type: 'object',
    properties: { docId: { type: 'string' } },
    required: ['docId'],
  },
  async handler(token, args) {
    const docId = String(args.docId ?? '');
    if (!docId) throw new Error('"docId" is required');
    const { doc, existed } = await readDoc(token, docId);
    if (!existed) throw new Error(`Doc ${docId} not found`);
    return JSON.stringify(
      { docId, title: readDocTitle(doc), blocks: listBlocks(doc) },
      null,
      2
    );
  },
};

const findDocByTitle: ToolDefinition = {
  name: 'find_doc_by_title',
  description:
    'Look up a doc id by its title (from the workspace page registry). Case-insensitive; returns closest match if ' +
    '`fuzzy` is true, otherwise only an exact match. Honors trashed docs with `includeTrash`.',
  inputSchema: {
    type: 'object',
    properties: {
      title: { type: 'string' },
      fuzzy: { type: 'boolean', description: 'Match substrings; default false (exact).' },
      includeTrash: { type: 'boolean', description: 'Include trashed docs; default false.' },
    },
    required: ['title'],
  },
  async handler(token, args) {
    const q = String(args.title ?? '').trim().toLowerCase();
    if (!q) throw new Error('"title" is required');
    const fuzzy = args.fuzzy === true;
    const includeTrash = args.includeTrash === true;

    const root = await readDoc(token, wsId());
    const all = listPages(root.doc).filter(p => includeTrash || !p.trash);

    const exact = all.filter(p => p.title.toLowerCase() === q);
    const matches = exact.length > 0
      ? exact
      : fuzzy
        ? all.filter(p => p.title.toLowerCase().includes(q))
        : [];

    return JSON.stringify({ query: q, count: matches.length, matches }, null, 2);
  },
};

// ── Comment tools (GraphQL-backed) ───────────────────────────────────

const createComment: ToolDefinition = {
  name: 'create_comment',
  description: 'Create a top-level comment on a document.',
  inputSchema: {
    type: 'object',
    properties: {
      docId: { type: 'string' },
      content: { type: 'string', description: 'Comment body (plain text).' },
    },
    required: ['docId', 'content'],
  },
  async handler(token, args) {
    const docId = String(args.docId ?? '');
    const content = String(args.content ?? '').trim();
    if (!docId || !content) throw new Error('"docId" and non-empty "content" required');
    // AFFiNE stores comment content as a Y.Doc-compatible delta in a string.
    // For plain text we wrap it as a minimal doc: { type:"doc", content:[{type:"paragraph",content:[{type:"text",text:"..."}]}] }.
    const tiptap = JSON.stringify({
      type: 'doc',
      content: [{ type: 'paragraph', content: [{ type: 'text', text: content }] }],
    });
    const data = await gql<{ createComment: { id: string; createdAt: string } }>(
      token,
      `mutation ($input: CommentCreateInput!) {
        createComment(input: $input) { id createdAt }
      }`,
      { input: { workspaceId: wsId(), docId, docMode: 'page', content: tiptap } }
    );
    return JSON.stringify({ commentId: data.createComment.id, createdAt: data.createComment.createdAt }, null, 2);
  },
};

const resolveComment: ToolDefinition = {
  name: 'resolve_comment',
  description: 'Mark a comment as resolved (or un-resolved).',
  inputSchema: {
    type: 'object',
    properties: {
      commentId: { type: 'string' },
      resolved: { type: 'boolean', description: 'default true' },
    },
    required: ['commentId'],
  },
  async handler(token, args) {
    const commentId = String(args.commentId ?? '');
    if (!commentId) throw new Error('"commentId" required');
    const resolved = args.resolved === undefined ? true : Boolean(args.resolved);
    await gql<{ resolveComment: boolean }>(
      token,
      `mutation ($input: CommentResolveInput!) { resolveComment(input: $input) }`,
      { input: { id: commentId, resolved } }
    );
    return JSON.stringify({ commentId, resolved }, null, 2);
  },
};

const deleteComment: ToolDefinition = {
  name: 'delete_comment',
  description: 'Delete a comment.',
  inputSchema: {
    type: 'object',
    properties: { commentId: { type: 'string' } },
    required: ['commentId'],
  },
  async handler(token, args) {
    const commentId = String(args.commentId ?? '');
    if (!commentId) throw new Error('"commentId" required');
    await gql<{ deleteComment: boolean }>(
      token,
      `mutation ($id: String!) { deleteComment(id: $id) }`,
      { id: commentId }
    );
    return JSON.stringify({ commentId, deleted: true }, null, 2);
  },
};

const createReply: ToolDefinition = {
  name: 'create_reply',
  description: 'Reply to an existing comment.',
  inputSchema: {
    type: 'object',
    properties: {
      commentId: { type: 'string' },
      content: { type: 'string' },
    },
    required: ['commentId', 'content'],
  },
  async handler(token, args) {
    const commentId = String(args.commentId ?? '');
    const content = String(args.content ?? '').trim();
    if (!commentId || !content) throw new Error('"commentId" and non-empty "content" required');
    const tiptap = JSON.stringify({
      type: 'doc',
      content: [{ type: 'paragraph', content: [{ type: 'text', text: content }] }],
    });
    const data = await gql<{ createReply: { id: string } }>(
      token,
      `mutation ($input: ReplyCreateInput!) { createReply(input: $input) { id } }`,
      { input: { commentId, content: tiptap } }
    );
    return JSON.stringify({ replyId: data.createReply.id }, null, 2);
  },
};

const deleteReply: ToolDefinition = {
  name: 'delete_reply',
  description: 'Delete a reply to a comment.',
  inputSchema: {
    type: 'object',
    properties: { replyId: { type: 'string' } },
    required: ['replyId'],
  },
  async handler(token, args) {
    const replyId = String(args.replyId ?? '');
    if (!replyId) throw new Error('"replyId" required');
    await gql<{ deleteReply: boolean }>(
      token,
      `mutation ($id: String!) { deleteReply(id: $id) }`,
      { id: replyId }
    );
    return JSON.stringify({ replyId, deleted: true }, null, 2);
  },
};

export const writeTools: ToolDefinition[] = [
  createDoc,
  appendBlocks,
  updateBlockText,
  deleteBlock,
  setDocTitle,
  trashDoc,
  listDocBlocks,
  findDocByTitle,
  createComment,
  resolveComment,
  deleteComment,
  createReply,
  deleteReply,
];
