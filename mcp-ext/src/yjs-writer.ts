/**
 * Yjs-based write adapter for AFFiNE docs.
 *
 * AFFiNE stores every doc (including the workspace root doc) as a Y.Doc. The
 * public API lets us:
 *   - FETCH the current Yjs binary state via
 *       GET /api/workspaces/:wsId/docs/:guid  → application/octet-stream
 *   - PUSH updates via GraphQL:
 *       mutation { applyDocUpdates(docId, op, updates, workspaceId) }
 *     where `updates` is a base64 Yjs update blob and `op` is "merge" | "push".
 *
 * This module wraps that round-trip:
 *
 *     const { doc, commit } = await openDoc(token, docId);
 *     doc.transact(() => { ...mutate Y structures... });
 *     await commit();
 *
 * `commit` computes a Yjs diff against the state that was loaded from AFFiNE
 * and pushes just that diff. We don't dump the full state every time — that
 * would bulldoze concurrent edits and also waste bandwidth.
 *
 * We deliberately avoid a BlockSuite dependency. AFFiNE's block schema is
 * conveyed as conventional keys inside plain Y.Map/Y.Text structures and has
 * been stable for years; pinning a specific @blocksuite/store version would
 * couple us to a particular AFFiNE image tag.
 */

import * as Y from 'yjs';
import { config } from './config.js';
import { gql } from './graphql.js';

/** Bytes returned from AFFiNE REST. Empty buffer means "doc doesn't exist yet". */
async function fetchDocBinary(token: string, guid: string): Promise<Uint8Array> {
  const url = `${config.affineBaseUrl}/api/workspaces/${config.workspaceId}/docs/${guid}`;
  const res = await fetch(url, {
    headers: {
      Accept: 'application/octet-stream',
      Authorization: `Bearer ${token}`,
    },
  });
  if (res.status === 404) return new Uint8Array();
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`Fetch doc ${guid} failed: HTTP ${res.status} ${res.statusText} ${text.slice(0, 200)}`);
  }
  const buf = await res.arrayBuffer();
  return new Uint8Array(buf);
}

function toBase64(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString('base64');
}

/**
 * Open an existing doc for editing, or an empty Y.Doc if it doesn't exist.
 * Returns:
 *  - `doc`: the in-memory Y.Doc (mutate through its top-level maps)
 *  - `existed`: whether the doc was already persisted on the server
 *  - `commit()`: computes a diff vs. the initial server state and pushes it
 */
export interface DocHandle {
  doc: Y.Doc;
  existed: boolean;
  commit: () => Promise<void>;
}

export async function openDoc(token: string, guid: string): Promise<DocHandle> {
  const initial = await fetchDocBinary(token, guid);
  const doc = new Y.Doc({ guid });
  const existed = initial.length > 0;
  if (existed) {
    Y.applyUpdate(doc, initial);
  }
  // Snapshot the state vector BEFORE any local mutation so we can diff later.
  const preVector = Y.encodeStateVector(doc);

  async function commit(): Promise<void> {
    const diff = Y.encodeStateAsUpdate(doc, preVector);
    // Y.encodeStateAsUpdate always returns some bytes (at least a structural
    // header) — check emptiness by comparing to a no-op update's length.
    if (diff.length <= 2) return; // nothing meaningful changed
    await gql<{ applyDocUpdates: string }>(
      token,
      `mutation ($docId: String!, $op: String!, $updates: String!, $workspaceId: String!) {
        applyDocUpdates(docId: $docId, op: $op, updates: $updates, workspaceId: $workspaceId)
      }`,
      {
        docId: guid,
        op: existed ? 'merge' : 'push',
        updates: toBase64(diff),
        workspaceId: config.workspaceId,
      }
    );
  }

  return { doc, existed, commit };
}

/** Convenience: load a doc read-only for introspection (no commit). */
export async function readDoc(token: string, guid: string): Promise<{ doc: Y.Doc; existed: boolean }> {
  const initial = await fetchDocBinary(token, guid);
  const doc = new Y.Doc({ guid });
  const existed = initial.length > 0;
  if (existed) Y.applyUpdate(doc, initial);
  return { doc, existed };
}
