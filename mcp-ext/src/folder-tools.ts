/**
 * Folder/organize tree tools — manipulate the AFFiNE sidebar's "Organize"
 * folder structure.
 *
 * The AFFiNE sidebar's Organize section is a tree of nodes:
 *   - folders         (named containers)
 *   - doc links       (point to a workspace doc by id — the "files in folders")
 *   - tag/collection  (links to a tag/collection definition; not exposed here)
 *
 * Storage is the `db$folders` Y.Doc — see folder-store.ts for the layout.
 *
 * Tools exposed:
 *   - list_folder_tree   — full tree (read-only, includes doc-link titles)
 *   - create_folder      — new folder (root or nested)
 *   - rename_folder      — change a folder's name
 *   - delete_folder      — soft-delete a folder, optionally cascading
 *   - move_folder        — relocate a folder + subtree
 *   - move_document      — file/refile a doc into a folder (or unfile)
 *
 * SAFETY: these tools only touch the organize tree. Doc CONTENT (blocks,
 * titles inside the doc, comments) is unaffected; deleting a folder NEVER
 * deletes the underlying documents — they remain in the workspace, just
 * unfiled. This mirrors AFFiNE's UI semantics.
 */

import { nanoid } from 'nanoid';
import { config } from './config.js';
import { listPages } from './doc-store.js';
import {
  FOLDERS_GUID,
  getRow,
  isAncestor,
  listAllRows,
  softDeleteRow,
  updateRow,
  writeRow,
  type FolderRow,
  type NodeType,
} from './folder-store.js';
import { indexAfterAll } from './index-utils.js';
import type { ToolDefinition } from './tools-shared.js';
import * as Y from 'yjs';
import { openDoc, readDoc } from './yjs-writer.js';

const wsId = () => config.workspaceId;

// ── Tree assembly ────────────────────────────────────────────────────

interface TreeNode {
  id: string;
  type: NodeType;
  /** For folders: the folder's name. For doc-links: the linked doc's title
   *  (resolved from workspace meta.pages). For tag/collection: target id. */
  name: string;
  /** Set on doc/tag/collection links — the linked target's id. */
  targetId?: string;
  index: string;
  children?: TreeNode[];
}

function buildTree(rows: FolderRow[], titleByDocId: Map<string, string>): TreeNode[] {
  // Group by parent and sort by fractional index.
  const byParent = new Map<string | null, FolderRow[]>();
  for (const r of rows) {
    const k = r.parentId;
    if (!byParent.has(k)) byParent.set(k, []);
    byParent.get(k)!.push(r);
  }
  for (const arr of byParent.values()) {
    arr.sort((a, b) => (a.index < b.index ? -1 : a.index > b.index ? 1 : 0));
  }

  const known = new Set(rows.map(r => r.id));

  function decorate(rs: FolderRow[]): TreeNode[] {
    return rs.map(r => {
      const node: TreeNode = {
        id: r.id,
        type: r.type,
        name:
          r.type === 'folder'
            ? r.data
            : r.type === 'doc'
              ? (titleByDocId.get(r.data) ?? '(untitled)')
              : r.data,
        index: r.index,
      };
      if (r.type !== 'folder') node.targetId = r.data;
      if (r.type === 'folder') {
        const kids = byParent.get(r.id);
        node.children = kids ? decorate(kids) : [];
      }
      return node;
    });
  }

  // Top level: parentId === null PLUS orphans (parent missing or deleted).
  const tops: FolderRow[] = [];
  for (const r of rows) {
    if (r.parentId === null) tops.push(r);
    else if (!known.has(r.parentId)) tops.push(r); // orphan → surface at root
  }
  tops.sort((a, b) => (a.index < b.index ? -1 : a.index > b.index ? 1 : 0));
  return decorate(tops);
}

// ── Tools ────────────────────────────────────────────────────────────

const listFolderTree: ToolDefinition = {
  name: 'list_folder_tree',
  description:
    "List the workspace's full Organize sidebar tree (folders and the docs/tags/collections " +
    'linked into them) as nested JSON. Each node is { id, type, name, index, children?, targetId? }. ' +
    'Doc-link names are resolved to current doc titles. Use this BEFORE any structural change so ' +
    'the agent can reason about the existing layout.',
  inputSchema: { type: 'object', properties: {} },
  async handler(token) {
    const { doc } = await readDoc(token, FOLDERS_GUID);
    const rows = listAllRows(doc);

    // Resolve doc-link titles from the workspace root doc's page registry.
    const root = await readDoc(token, wsId());
    const titleByDocId = new Map<string, string>();
    for (const p of listPages(root.doc)) {
      if (!p.trash) titleByDocId.set(p.id, p.title);
    }

    const tree = buildTree(rows, titleByDocId);
    return JSON.stringify({ totalNodes: rows.length, tree }, null, 2);
  },
};

const createFolder: ToolDefinition = {
  name: 'create_folder',
  description:
    'Create a new folder in the Organize sidebar. Returns the new folderId. ' +
    'Pass parentFolderId to nest under another folder; omit it to create a top-level folder.',
  inputSchema: {
    type: 'object',
    properties: {
      name: { type: 'string', description: 'Folder name shown in the sidebar.' },
      parentFolderId: {
        type: 'string',
        description: 'Optional parent folder id. Omit (or pass empty) for a top-level folder.',
      },
    },
    required: ['name'],
  },
  async handler(token, args) {
    const name = String(args.name ?? '').trim();
    if (!name) throw new Error('"name" is required');
    const parentFolderId = args.parentFolderId ? String(args.parentFolderId) : null;

    const { doc, commit } = await openDoc(token, FOLDERS_GUID);
    if (parentFolderId) {
      const parent = getRow(doc, parentFolderId);
      if (!parent) throw new Error(`Parent folder "${parentFolderId}" not found`);
      if (parent.type !== 'folder') {
        throw new Error(`"${parentFolderId}" is not a folder (type=${parent.type})`);
      }
    }

    const siblingIndices = listAllRows(doc)
      .filter(r => r.parentId === parentFolderId)
      .map(r => r.index);

    const id = nanoid();
    const index = indexAfterAll(siblingIndices);
    writeRow(doc, id, { parentId: parentFolderId, type: 'folder', data: name, index });
    await commit();

    return JSON.stringify(
      { folderId: id, name, parentFolderId, index, ok: true },
      null,
      2,
    );
  },
};

const renameFolder: ToolDefinition = {
  name: 'rename_folder',
  description:
    "Rename a folder in the Organize sidebar. Only valid for folders — doc/tag/collection " +
    "links inherit their name from the target and can't be renamed here.",
  inputSchema: {
    type: 'object',
    properties: {
      folderId: { type: 'string' },
      newName: { type: 'string' },
    },
    required: ['folderId', 'newName'],
  },
  async handler(token, args) {
    const folderId = String(args.folderId ?? '');
    const newName = String(args.newName ?? '').trim();
    if (!folderId || !newName) {
      throw new Error('"folderId" and non-empty "newName" are required');
    }
    const { doc, commit } = await openDoc(token, FOLDERS_GUID);
    const row = getRow(doc, folderId);
    if (!row) throw new Error(`Folder "${folderId}" not found`);
    if (row.type !== 'folder') {
      throw new Error(`"${folderId}" is not a folder (type=${row.type})`);
    }
    updateRow(doc, folderId, { data: newName });
    await commit();
    return JSON.stringify({ folderId, newName, ok: true }, null, 2);
  },
};

const deleteFolder: ToolDefinition = {
  name: 'delete_folder',
  description:
    'Soft-delete a folder from the Organize sidebar. By default refuses if the folder has children — ' +
    'pass cascade:true to delete the entire subtree (folders and their links). ' +
    'IMPORTANT: this only removes the FOLDER STRUCTURE — underlying documents are NOT deleted ' +
    'and remain in the workspace, just unfiled.',
  inputSchema: {
    type: 'object',
    properties: {
      folderId: { type: 'string' },
      cascade: {
        type: 'boolean',
        description: 'Default false. If true, soft-delete all descendants in the subtree.',
      },
    },
    required: ['folderId'],
  },
  async handler(token, args) {
    const folderId = String(args.folderId ?? '');
    if (!folderId) throw new Error('"folderId" is required');
    const cascade = args.cascade === true;

    const { doc, commit } = await openDoc(token, FOLDERS_GUID);
    const row = getRow(doc, folderId);
    if (!row) throw new Error(`Folder "${folderId}" not found`);
    if (row.type !== 'folder') {
      throw new Error(`"${folderId}" is not a folder (type=${row.type})`);
    }

    const all = listAllRows(doc);
    const childrenOf = new Map<string, FolderRow[]>();
    for (const r of all) {
      if (!r.parentId) continue;
      if (!childrenOf.has(r.parentId)) childrenOf.set(r.parentId, []);
      childrenOf.get(r.parentId)!.push(r);
    }

    const directKids = childrenOf.get(folderId) ?? [];
    if (directKids.length > 0 && !cascade) {
      throw new Error(
        `Folder "${folderId}" has ${directKids.length} child node(s). ` +
        'Pass cascade:true to delete the whole subtree, or move them out first.',
      );
    }

    // BFS to collect the full subtree under cascade.
    const toDelete: string[] = [folderId];
    if (cascade) {
      const queue = [folderId];
      while (queue.length > 0) {
        const cur = queue.shift()!;
        for (const k of childrenOf.get(cur) ?? []) {
          toDelete.push(k.id);
          if (k.type === 'folder') queue.push(k.id);
        }
      }
    }

    for (const id of toDelete) softDeleteRow(doc, id);
    await commit();
    return JSON.stringify(
      { deleted: toDelete, count: toDelete.length, cascade, ok: true },
      null,
      2,
    );
  },
};

const moveFolder: ToolDefinition = {
  name: 'move_folder',
  description:
    'Move a folder (and its entire subtree) under a different parent folder. ' +
    'Pass parentFolderId omitted/empty to move to the top level. ' +
    'Refuses cycles — a folder cannot be moved into its own descendant.',
  inputSchema: {
    type: 'object',
    properties: {
      folderId: { type: 'string' },
      parentFolderId: {
        type: 'string',
        description: 'Target parent folder id. Omit/empty to move to top level.',
      },
    },
    required: ['folderId'],
  },
  async handler(token, args) {
    const folderId = String(args.folderId ?? '');
    if (!folderId) throw new Error('"folderId" is required');
    const parentFolderId = args.parentFolderId ? String(args.parentFolderId) : null;

    const { doc, commit } = await openDoc(token, FOLDERS_GUID);
    const row = getRow(doc, folderId);
    if (!row) throw new Error(`Folder "${folderId}" not found`);
    if (row.type !== 'folder') {
      throw new Error(`"${folderId}" is not a folder (type=${row.type})`);
    }
    if (parentFolderId) {
      if (parentFolderId === folderId) {
        throw new Error('Cannot move a folder into itself');
      }
      const parent = getRow(doc, parentFolderId);
      if (!parent) throw new Error(`Parent folder "${parentFolderId}" not found`);
      if (parent.type !== 'folder') {
        throw new Error(`"${parentFolderId}" is not a folder (type=${parent.type})`);
      }
      if (isAncestor(doc, folderId, parentFolderId)) {
        throw new Error('Cannot move a folder into its own descendant');
      }
    }

    const siblingIndices = listAllRows(doc)
      .filter(r => r.parentId === parentFolderId && r.id !== folderId)
      .map(r => r.index);
    const newIndex = indexAfterAll(siblingIndices);
    updateRow(doc, folderId, { parentId: parentFolderId, index: newIndex });
    await commit();
    return JSON.stringify(
      { folderId, parentFolderId, index: newIndex, ok: true },
      null,
      2,
    );
  },
};

const moveDocument: ToolDefinition = {
  name: 'move_document',
  description:
    'File a document into a folder in the Organize sidebar. If the doc is already linked elsewhere ' +
    'in the tree, ALL existing links are consolidated into one fresh link under the target folder. ' +
    'Pass folderId omitted/empty to UNFILE the doc — remove all its organize links (the doc itself ' +
    'is unaffected and remains in the workspace).',
  inputSchema: {
    type: 'object',
    properties: {
      docId: { type: 'string', description: 'The document id to file.' },
      folderId: {
        type: 'string',
        description: 'Target folder id. Omit/empty to unfile the doc from the organize tree.',
      },
    },
    required: ['docId'],
  },
  async handler(token, args) {
    const docId = String(args.docId ?? '');
    if (!docId) throw new Error('"docId" is required');
    const folderId = args.folderId ? String(args.folderId) : null;

    const { doc, commit } = await openDoc(token, FOLDERS_GUID);
    if (folderId) {
      const folder = getRow(doc, folderId);
      if (!folder) throw new Error(`Folder "${folderId}" not found`);
      if (folder.type !== 'folder') {
        throw new Error(`"${folderId}" is not a folder (type=${folder.type})`);
      }
    }

    const all = listAllRows(doc);
    const existingLinks = all.filter(r => r.type === 'doc' && r.data === docId);

    if (folderId === null) {
      for (const link of existingLinks) softDeleteRow(doc, link.id);
      await commit();
      return JSON.stringify(
        { docId, folderId: null, removedLinks: existingLinks.length, ok: true },
        null,
        2,
      );
    }

    let linkId: string;
    if (existingLinks.length === 0) {
      // No prior link — create a fresh one.
      const siblings = all.filter(r => r.parentId === folderId).map(r => r.index);
      linkId = nanoid();
      writeRow(doc, linkId, {
        parentId: folderId,
        type: 'doc',
        data: docId,
        index: indexAfterAll(siblings),
      });
    } else {
      // Reuse the first link, retire any duplicates.
      const [first, ...rest] = existingLinks as [FolderRow, ...FolderRow[]];
      const siblings = all
        .filter(r => r.parentId === folderId && r.id !== first.id)
        .map(r => r.index);
      updateRow(doc, first.id, {
        parentId: folderId,
        index: indexAfterAll(siblings),
      });
      for (const dup of rest) softDeleteRow(doc, dup.id);
      linkId = first.id;
    }

    await commit();
    return JSON.stringify(
      {
        docId,
        folderId,
        linkId,
        replacedLinks: existingLinks.length,
        ok: true,
      },
      null,
      2,
    );
  },
};

// ── Debug ────────────────────────────────────────────────────────────

const debugFoldersDoc: ToolDefinition = {
  name: 'debug_folders_doc',
  description:
    'Diagnostic tool: dump the raw structure of the folders Y.Doc as the MCP server sees it. ' +
    'Reports the GUID, whether the load returned a populated doc, and the top-level share map ' +
    'keys with their Y type + (for Y.Maps) field keys. Use to debug why list_folder_tree may ' +
    'return empty when folders exist in the AFFiNE UI.',
  inputSchema: {
    type: 'object',
    properties: {
      docGuid: {
        type: 'string',
        description:
          'Optional alternative Y.Doc guid to inspect. Defaults to "db$folders" — the workspace folders DB.',
      },
    },
  },
  async handler(token, args) {
    const guid = args.docGuid ? String(args.docGuid) : FOLDERS_GUID;
    const { doc, existed } = await readDoc(token, guid);

    const stateUpdate = Y.encodeStateAsUpdate(doc);
    const stateVector = Y.encodeStateVector(doc);

    const share = (doc as Y.Doc & { share: Map<string, Y.AbstractType<unknown>> }).share;
    const shareEntries: Array<{
      key: string;
      yType: string;
      mapKeys?: string[];
      arrayLen?: number;
      previewFields?: Record<string, unknown>;
    }> = [];

    for (const [key, val] of share) {
      const entry: (typeof shareEntries)[number] = {
        key,
        yType: val.constructor?.name ?? 'unknown',
      };
      if (val instanceof Y.Map) {
        const m = val as Y.Map<unknown>;
        entry.mapKeys = Array.from(m.keys());
        // peek at the first 6 fields for type-of-content sanity
        const preview: Record<string, unknown> = {};
        let i = 0;
        for (const [k, v] of m.entries()) {
          if (i++ >= 6) break;
          preview[k] = typeof v === 'string' && v.length > 40 ? v.slice(0, 40) + '…' : v;
        }
        entry.previewFields = preview;
      } else if (val instanceof Y.Array) {
        entry.arrayLen = (val as Y.Array<unknown>).length;
      }
      shareEntries.push(entry);
    }

    return JSON.stringify(
      {
        guid,
        existedOnServer: existed,
        stateUpdateBytes: stateUpdate.length,
        stateVectorBytes: stateVector.length,
        shareEntryCount: shareEntries.length,
        shareEntries: shareEntries.slice(0, 50), // cap to keep response sane
      },
      null,
      2,
    );
  },
};

export const folderTools: ToolDefinition[] = [
  listFolderTree,
  createFolder,
  renameFolder,
  deleteFolder,
  moveFolder,
  moveDocument,
  debugFoldersDoc,
];
