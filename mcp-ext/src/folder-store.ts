/**
 * Folder/organize tree store — read & write the AFFiNE sidebar folder tree.
 *
 * AFFiNE stores its "Organize" sidebar as rows in a Yjs-backed table called
 * `folders`. Internally each table is its own Y.Doc with guid `db$<table>`,
 * synced through the same Socket.IO gateway as page docs. Within that Y.Doc
 * each row is a TOP-LEVEL Y.Map — the row id is the Y.Doc share key, and the
 * map fields are the row columns.
 *
 *   Y.Doc(guid='db$folders')
 *     ├─ Y.Map<rowId-1>   { id, parentId?, type, data, index, $$DELETED? }
 *     ├─ Y.Map<rowId-2>   …
 *     └─ …
 *
 * Schema (from packages/frontend/core/src/modules/db/schema/schema.ts —
 * AFFiNE_WORKSPACE_DB_SCHEMA.folders):
 *
 *   id        string  primary key (nanoid by default)
 *   parentId  string  optional — null/missing means top-level
 *   type      'folder' | 'doc' | 'tag' | 'collection'
 *   data      string  folder name (when type='folder') OR target id (otherwise)
 *   index     string  fractional-index for ordering siblings
 *
 * Soft delete: the YjsTableAdapter sets `$$DELETED: true` on a row instead of
 * removing the Y.Map. We honour that — listings filter out deleted rows, and
 * any row write clears the flag (matching AFFiNE's revive-on-update semantics).
 *
 * SAFETY BOUNDARY:
 * This module ONLY reads/writes the `folders` Y.Doc. It does not touch
 * workspace settings, members, permissions, doc content, or any other table.
 * AI-facing tools that import this can safely reorganize the sidebar without
 * any path to escalate scope.
 */

import * as Y from 'yjs';

/** Y.Doc guid that holds the organize folder tree. */
export const FOLDERS_GUID = 'db$folders';

/** Soft-delete tombstone key set by YjsTableAdapter on row deletion. */
const DELETE_FLAG = '$$DELETED';

export type NodeType = 'folder' | 'doc' | 'tag' | 'collection';

export interface FolderRow {
  id: string;
  /** null/undefined = root. */
  parentId: string | null;
  type: NodeType;
  /** Folder name when type='folder', otherwise the linked target's id. */
  data: string;
  /** Fractional-index sort key among siblings under the same parent. */
  index: string;
}

/**
 * Yjs exposes the top-level type registry as `Doc.share`. The public typings
 * don't include it on Doc, so we widen the type locally where we need to
 * iterate / probe row keys.
 */
type SharedDoc = Y.Doc & { share: Map<string, Y.AbstractType<unknown>> };

function shareOf(doc: Y.Doc): Map<string, Y.AbstractType<unknown>> {
  return (doc as SharedDoc).share;
}

function isLive(row: Y.Map<unknown>): boolean {
  return row.get(DELETE_FLAG) !== true;
}

function decodeRow(id: string, row: Y.Map<unknown>): FolderRow | null {
  if (!isLive(row)) return null;
  const type = row.get('type');
  if (
    type !== 'folder' &&
    type !== 'doc' &&
    type !== 'tag' &&
    type !== 'collection'
  ) {
    return null;
  }
  const parentRaw = row.get('parentId');
  const parentId =
    typeof parentRaw === 'string' && parentRaw.length > 0 ? parentRaw : null;
  return {
    id,
    parentId,
    type,
    data: typeof row.get('data') === 'string' ? (row.get('data') as string) : '',
    index: typeof row.get('index') === 'string' ? (row.get('index') as string) : '',
  };
}

/** All live rows in the folders doc. Soft-deleted rows are filtered out. */
export function listAllRows(doc: Y.Doc): FolderRow[] {
  const out: FolderRow[] = [];
  for (const [id, val] of shareOf(doc)) {
    if (!(val instanceof Y.Map)) continue;
    const r = decodeRow(id, val as Y.Map<unknown>);
    if (r) out.push(r);
  }
  return out;
}

/** Look up one live row by id. Returns null if absent or soft-deleted. */
export function getRow(doc: Y.Doc, id: string): FolderRow | null {
  const m = shareOf(doc).get(id);
  if (!(m instanceof Y.Map)) return null;
  return decodeRow(id, m as Y.Map<unknown>);
}

/**
 * Walk the parent chain from `descendant` upward and return true if
 * `maybeAncestor` appears anywhere on the path. Used to refuse cyclic moves.
 */
export function isAncestor(
  doc: Y.Doc,
  maybeAncestor: string,
  descendant: string,
): boolean {
  let cur: string | null = descendant;
  const seen = new Set<string>();
  while (cur) {
    if (cur === maybeAncestor) return true;
    if (seen.has(cur)) return false; // defensive cycle guard
    seen.add(cur);
    const row = getRow(doc, cur);
    cur = row?.parentId ?? null;
  }
  return false;
}

/**
 * Insert (or revive) a row. Caller picks the id (typically nanoid()) and the
 * fractional index. Always clears the soft-delete flag.
 */
export function writeRow(
  doc: Y.Doc,
  id: string,
  fields: { parentId: string | null; type: NodeType; data: string; index: string },
): void {
  const m = doc.getMap(id);
  if (m.get(DELETE_FLAG) === true) m.delete(DELETE_FLAG);
  // AFFiNE's ORM stores `id` as a column too (matches the Y.Map key) — we
  // mirror that so reads from the AFFiNE FE see a self-consistent row.
  m.set('id', id);
  if (fields.parentId === null) {
    if (m.has('parentId')) m.delete('parentId');
  } else {
    m.set('parentId', fields.parentId);
  }
  m.set('type', fields.type);
  m.set('data', fields.data);
  m.set('index', fields.index);
}

/** Patch an existing row. Only fields present in `patch` are touched. */
export function updateRow(
  doc: Y.Doc,
  id: string,
  patch: Partial<{ parentId: string | null; data: string; index: string }>,
): void {
  const m = doc.getMap(id);
  if (m.get(DELETE_FLAG) === true) m.delete(DELETE_FLAG);
  if (patch.parentId !== undefined) {
    if (patch.parentId === null) {
      if (m.has('parentId')) m.delete('parentId');
    } else {
      m.set('parentId', patch.parentId);
    }
  }
  if (patch.data !== undefined) m.set('data', patch.data);
  if (patch.index !== undefined) m.set('index', patch.index);
}

/** Soft-delete a row — sets $$DELETED so AFFiNE's adapter hides it. */
export function softDeleteRow(doc: Y.Doc, id: string): void {
  const m = doc.getMap(id);
  m.set(DELETE_FLAG, true);
}
