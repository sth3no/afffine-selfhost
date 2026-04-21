/**
 * Yjs-based write adapter for AFFiNE docs — Socket.IO transport.
 *
 * AFFiNE's GraphQL `applyDocUpdates` mutation turned out to be an
 * AI-assisted markdown editor, not a raw CRDT sync path. Actual doc writes
 * go through the Socket.IO sync gateway defined in
 *   packages/backend/server/src/core/sync/gateway.ts
 *
 * Protocol (spec v0.26+):
 *   1. Connect Socket.IO to the AFFiNE server with Bearer auth.
 *   2. emit('space:join', { spaceType, spaceId, clientVersion })   → ack
 *   3. emit('space:load-doc', { ..., docId, stateVector? })        → { state, missing, timestamp }
 *      Throws `DocNotFound` on server if doc is new — we treat that as
 *      "create from scratch".
 *   4. Apply both `state` and `missing` onto a local Y.Doc, mutate, diff
 *      against pre-state vector, base64 the diff.
 *   5. emit('space:push-doc-update', { ..., docId, update })       → ack
 *
 * Auth:
 *   - `attachPresenceUserId` in gateway.ts reads `request.token?.user.id`
 *     which is set by AFFiNE's auth middleware when a Bearer token is on
 *     the socket's handshake. Socket.IO passes headers via `extraHeaders`
 *     during the initial HTTP upgrade.
 *
 * Shape of the returned DocHandle is unchanged from the earlier GraphQL
 * implementation so write-tools.ts doesn't need to know the transport.
 */

import * as Y from 'yjs';
import { io, type Socket } from 'socket.io-client';
import { config } from './config.js';

const SPACE_TYPE_WORKSPACE = 'workspace';
// AFFiNE accepts any client reporting >= 0.25.0. 0.26.0 enables the newer
// batch-broadcast protocol (sync-026) but we're a pure write client so it
// doesn't matter which room we land in.
const CLIENT_VERSION = '0.26.0';

interface JoinAck {
  data: { clientId: string; success: boolean };
}

interface LoadDocAck {
  data: { state: string; missing: string; timestamp: number };
  error?: { name?: string; message?: string };
}

interface PushAck {
  data: { accepted: boolean; timestamp?: number };
  error?: { name?: string; message?: string };
}

function timeoutPromise<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
    promise.then(
      v => { clearTimeout(t); resolve(v); },
      e => { clearTimeout(t); reject(e); },
    );
  });
}

/**
 * Open a Socket.IO connection to AFFiNE and join the workspace. Returns
 * a Session object that can be used to load / push multiple docs.
 */
export interface DocSession {
  load(docId: string): Promise<Y.Doc | null>;
  push(docId: string, update: Uint8Array): Promise<void>;
  close(): void;
}

async function connect(token: string): Promise<Socket> {
  const socket = io(config.affineBaseUrl, {
    transports: ['websocket'],
    path: '/socket.io/',
    extraHeaders: { Authorization: `Bearer ${token}` },
    // Bearer cookies: socket.io-client also forwards `auth` during the
    // initial upgrade handshake — some AFFiNE builds accept it there too.
    auth: { token: `Bearer ${token}` },
    reconnection: false,
    timeout: 10000,
  });

  await new Promise<void>((resolve, reject) => {
    const onConnect = () => { cleanup(); resolve(); };
    const onError = (err: Error) => { cleanup(); reject(new Error(`Socket connect failed: ${err.message}`)); };
    const cleanup = () => {
      socket.off('connect', onConnect);
      socket.off('connect_error', onError);
    };
    socket.on('connect', onConnect);
    socket.on('connect_error', onError);
  });

  return socket;
}

async function join(socket: Socket, spaceId: string): Promise<void> {
  const ack = await timeoutPromise(
    socket.emitWithAck('space:join', {
      spaceType: SPACE_TYPE_WORKSPACE,
      spaceId,
      clientVersion: CLIENT_VERSION,
    }) as Promise<JoinAck>,
    10000,
    `space:join(${spaceId})`,
  );
  if (!ack?.data?.success) {
    throw new Error(`Failed to join workspace ${spaceId} — server rejected handshake`);
  }
}

export async function openSession(token: string): Promise<DocSession> {
  const socket = await connect(token);
  await join(socket, config.workspaceId);

  return {
    async load(docId: string): Promise<Y.Doc | null> {
      try {
        const ack = await timeoutPromise(
          socket.emitWithAck('space:load-doc', {
            spaceType: SPACE_TYPE_WORKSPACE,
            spaceId: config.workspaceId,
            docId,
          }) as Promise<LoadDocAck>,
          15000,
          `space:load-doc(${docId})`,
        );
        if (ack?.error) {
          // DocNotFound is expected for new docs — signal with null.
          if (ack.error.name === 'DocNotFound' || /not found/i.test(ack.error.message ?? '')) {
            return null;
          }
          throw new Error(`load-doc(${docId}) failed: ${ack.error.message ?? ack.error.name}`);
        }
        if (!ack?.data) return null;

        // AFFiNE sync protocol response shape (classical Yjs two-step sync):
        //   - `missing` is the actual Yjs update the server has that we
        //     don't — apply this to populate our local doc.
        //   - `state` is the SERVER'S state vector (its encodeStateVector),
        //     which a live client would use to send back the structs the
        //     server is missing. For one-shot writers (us) we don't need it.
        const doc = new Y.Doc({ guid: docId });
        const missing = ack.data.missing ? Buffer.from(ack.data.missing, 'base64') : null;
        if (missing && missing.length > 0) Y.applyUpdate(doc, missing);
        return doc;
      } catch (err) {
        const msg = (err as Error).message ?? String(err);
        if (/DocNotFound|not found/i.test(msg)) return null;
        throw err;
      }
    },

    async push(docId: string, update: Uint8Array): Promise<void> {
      if (update.length === 0) return;
      const ack = await timeoutPromise(
        socket.emitWithAck('space:push-doc-update', {
          spaceType: SPACE_TYPE_WORKSPACE,
          spaceId: config.workspaceId,
          docId,
          update: Buffer.from(update).toString('base64'),
        }) as Promise<PushAck>,
        15000,
        `space:push-doc-update(${docId})`,
      );
      if (ack?.error) {
        throw new Error(`push-doc-update(${docId}) failed: ${ack.error.message ?? ack.error.name}`);
      }
      if (!ack?.data?.accepted) {
        throw new Error(`push-doc-update(${docId}): server did not accept the update`);
      }
    },

    close(): void {
      socket.disconnect();
    },
  };
}

/**
 * Convenience shape matching the previous GraphQL-era API so write-tools.ts
 * doesn't need transport-specific code. Each open/openDoc owns its own
 * Socket.IO connection; the connection is closed on commit (or manually).
 */
export interface DocHandle {
  doc: Y.Doc;
  existed: boolean;
  commit: () => Promise<void>;
}

/**
 * Open a single doc for editing. Opens a private Socket.IO connection,
 * loads the current state, returns a handle. `commit()` diffs against the
 * loaded state vector and pushes, then closes the socket.
 */
export async function openDoc(token: string, guid: string): Promise<DocHandle> {
  const session = await openSession(token);
  let loaded: Y.Doc | null;
  try {
    loaded = await session.load(guid);
  } catch (err) {
    session.close();
    throw err;
  }

  const doc = loaded ?? new Y.Doc({ guid });
  const existed = loaded !== null;
  const preVector = Y.encodeStateVector(doc);

  return {
    doc,
    existed,
    async commit(): Promise<void> {
      try {
        const diff = Y.encodeStateAsUpdate(doc, preVector);
        if (diff.length > 2) {
          await session.push(guid, diff);
        }
      } finally {
        session.close();
      }
    },
  };
}

/** Read a doc without intending to write. Loads and closes the socket. */
export async function readDoc(token: string, guid: string): Promise<{ doc: Y.Doc; existed: boolean }> {
  const session = await openSession(token);
  try {
    const loaded = await session.load(guid);
    if (loaded) return { doc: loaded, existed: true };
    return { doc: new Y.Doc({ guid }), existed: false };
  } finally {
    session.close();
  }
}
