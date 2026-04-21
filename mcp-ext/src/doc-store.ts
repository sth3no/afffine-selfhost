/**
 * Workspace root-doc manipulation — scoped strictly to the page registry.
 *
 * The workspace root Y.Doc lives at guid = workspace_id. It holds a top-level
 * `meta` Y.Map with a `pages` Y.Array<Y.Map>. Each page map has:
 *    id           string
 *    title        string
 *    createDate   number   (epoch ms)
 *    updatedDate? number   (epoch ms)
 *    trash?       boolean
 *    trashDate?   number   (epoch ms)
 *    headerImage? string
 *    tags         Y.Array<string>
 *
 * Reference:
 *    packages/common/native/src/doc_parser/write/root_doc.rs
 *
 * SAFETY BOUNDARY:
 * This module ONLY touches `meta.pages` entries — it does NOT modify
 * workspace name, members, permissions, settings, properties, collections,
 * or anything else. AI-facing tools must not be able to alter workspace
 * configuration or membership.
 */

import * as Y from 'yjs';

export const DEFAULT_DOC_TITLE = 'Untitled';

function ensureMetaMap(doc: Y.Doc): Y.Map<unknown> {
  return doc.getMap<unknown>('meta');
}

function ensurePagesArray(doc: Y.Doc): Y.Array<Y.Map<unknown>> {
  const meta = ensureMetaMap(doc);
  let pages = meta.get('pages');
  if (pages instanceof Y.Array) {
    return pages as Y.Array<Y.Map<unknown>>;
  }
  pages = new Y.Array<Y.Map<unknown>>();
  meta.set('pages', pages);
  return pages as Y.Array<Y.Map<unknown>>;
}

export interface PageMeta {
  id: string;
  title: string;
  createDate?: number;
  updatedDate?: number;
  trash?: boolean;
  trashDate?: number;
}

function readPage(page: Y.Map<unknown>): PageMeta {
  return {
    id: String(page.get('id') ?? ''),
    title: String(page.get('title') ?? ''),
    createDate: typeof page.get('createDate') === 'number' ? (page.get('createDate') as number) : undefined,
    updatedDate: typeof page.get('updatedDate') === 'number' ? (page.get('updatedDate') as number) : undefined,
    trash: page.get('trash') === true,
    trashDate: typeof page.get('trashDate') === 'number' ? (page.get('trashDate') as number) : undefined,
  };
}

/** List all page entries in the workspace root doc. */
export function listPages(rootDoc: Y.Doc): PageMeta[] {
  const pages = ensurePagesArray(rootDoc);
  const out: PageMeta[] = [];
  for (const page of pages) out.push(readPage(page));
  return out;
}

/** Find a page entry by its doc id. Returns both the index and the map. */
export function findPage(rootDoc: Y.Doc, docId: string): { index: number; page: Y.Map<unknown> } | null {
  const pages = ensurePagesArray(rootDoc);
  let i = 0;
  for (const page of pages) {
    if (page.get('id') === docId) return { index: i, page };
    i++;
  }
  return null;
}

/**
 * Add a new page entry to the workspace root doc.
 * Idempotent — if the docId already exists, updates its title instead.
 */
export function addPage(rootDoc: Y.Doc, docId: string, title: string): void {
  const existing = findPage(rootDoc, docId);
  const now = Date.now();
  if (existing) {
    existing.page.set('title', title || DEFAULT_DOC_TITLE);
    existing.page.set('updatedDate', now);
    return;
  }

  const pages = ensurePagesArray(rootDoc);
  const page = new Y.Map<unknown>();
  pages.push([page]);
  page.set('id', docId);
  page.set('title', title || DEFAULT_DOC_TITLE);
  page.set('createDate', now);
  page.set('tags', new Y.Array<string>());
}

/** Update a page's title in the workspace meta. */
export function setPageTitle(rootDoc: Y.Doc, docId: string, title: string): boolean {
  const found = findPage(rootDoc, docId);
  if (!found) return false;
  found.page.set('title', title);
  found.page.set('updatedDate', Date.now());
  return true;
}

/** Soft-delete: mark a page as trashed. Recoverable from the UI trash bin. */
export function trashPage(rootDoc: Y.Doc, docId: string): boolean {
  const found = findPage(rootDoc, docId);
  if (!found) return false;
  found.page.set('trash', true);
  found.page.set('trashDate', Date.now());
  return true;
}

/** Reverse of trashPage — restore from trash. */
export function restorePage(rootDoc: Y.Doc, docId: string): boolean {
  const found = findPage(rootDoc, docId);
  if (!found) return false;
  found.page.set('trash', false);
  found.page.delete('trashDate');
  return true;
}
