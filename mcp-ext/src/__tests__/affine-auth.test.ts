/**
 * Unit tests for mcp-ext's AFFiNE 0.27 auth layer — fetch is mocked, no
 * network. Run with `npm test` (node:test via tsx).
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import {
  AffineAuthError,
  AffineSession,
  McpCredentialManager,
  type FetchLike,
  type Logger,
} from '../affine-auth.js';
import { createClientGate, parseBearer } from '../client-auth.js';
import { NativeMcpClient } from '../forward.js';

const BASE = 'http://affine.test:3010';
const EMAIL = 'mcp-bot@example.com';
const PASSWORD = 'pw-super-secret-123';
const WS = 'ws-1';

interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string;
}

type Handler = (call: Call, n: number) => Response | Promise<Response>;

function json(body: unknown, init: ResponseInit & { cookies?: string[] } = {}): Response {
  const headers = new Headers({ 'content-type': 'application/json' });
  for (const c of init.cookies ?? []) headers.append('set-cookie', c);
  return new Response(JSON.stringify(body), { status: init.status ?? 200, headers });
}

const SESSION_COOKIES = (v: string, now = Date.now()) => [
  `affine_session=${v}; Path=/; Expires=${new Date(now + 15 * 864e5).toUTCString()}; HttpOnly; SameSite=Lax`,
  `affine_csrf_token=csrf-${v}; Path=/; SameSite=Lax`,
  'affine_user_id=user-1; Path=/; SameSite=Lax',
];

const AUTH_REQUIRED = {
  errors: [
    {
      message: 'You must sign in first to access this resource.',
      extensions: { status: 401, code: 'Unauthorized', type: 'AUTHENTICATION_REQUIRED', name: 'AUTHENTICATION_REQUIRED' },
    },
  ],
  data: null,
};

/** A scripted fetch: `routes` picks a handler by URL; every call is recorded. */
function mockFetch(routes: Record<string, Handler>) {
  const calls: Call[] = [];
  const counts = new Map<string, number>();
  const fetchImpl: FetchLike = async (url, init = {}) => {
    const headers: Record<string, string> = {};
    new Headers(init.headers as HeadersInit).forEach((v, k) => (headers[k] = v));
    const body = typeof init.body === 'string' ? init.body : init.body ? '[form]' : '';
    const call = { url, method: init.method ?? 'GET', headers, body };
    calls.push(call);
    const path = url.replace(BASE, '');
    const route = Object.keys(routes).find(r => path.startsWith(r));
    if (!route) throw new Error(`unexpected fetch ${url}`);
    const n = (counts.get(route) ?? 0) + 1;
    counts.set(route, n);
    return routes[route](call, n);
  };
  return { fetchImpl, calls, count: (route: string) => counts.get(route) ?? 0 };
}

function captureLogger() {
  const lines: string[] = [];
  const logger: Logger = { info: m => lines.push(m), warn: m => lines.push(m) };
  return { logger, lines };
}

function newSession(fetchImpl: FetchLike, extra: Partial<ConstructorParameters<typeof AffineSession>[0]> = {}) {
  return new AffineSession({
    baseUrl: BASE,
    email: EMAIL,
    password: PASSWORD,
    fetch: fetchImpl,
    logger: captureLogger().logger,
    loginCooldownMs: 0,
    ...extra,
  });
}

// ── client gate ────────────────────────────────────────────────────

describe('client gate (MCP_EXT_HTTP_TOKEN)', () => {
  const gate = createClientGate('s3cret-token-0123456789abcdef');

  it('accepts the exact bearer token (scheme is case-insensitive)', () => {
    assert.equal(gate('Bearer s3cret-token-0123456789abcdef'), true);
    assert.equal(gate('bearer s3cret-token-0123456789abcdef'), true);
    assert.equal(gate(['Bearer s3cret-token-0123456789abcdef']), true);
  });

  it('rejects wrong, truncated, longer, missing or non-bearer credentials', () => {
    assert.equal(gate('Bearer s3cret-token-0123456789abcdeX'), false);
    assert.equal(gate('Bearer s3cret'), false);
    assert.equal(gate('Bearer s3cret-token-0123456789abcdef-and-more'), false);
    assert.equal(gate(undefined), false);
    assert.equal(gate(''), false);
    assert.equal(gate('Basic czNjcmV0'), false);
    assert.equal(gate('s3cret-token-0123456789abcdef'), false);
  });

  it('refuses to be built without a token', () => {
    assert.throws(() => createClientGate(''));
  });

  it('parses bearer headers', () => {
    assert.equal(parseBearer('Bearer abc'), 'abc');
    assert.equal(parseBearer('Bearer   abc  '), 'abc');
    assert.equal(parseBearer('Bearer a b'), null);
    assert.equal(parseBearer('Token abc'), null);
  });
});

// ── AFFiNE cookie session ──────────────────────────────────────────

describe('AffineSession', () => {
  it('signs in lazily and sends the session cookie + x-affine-version on GraphQL', async () => {
    const m = mockFetch({
      '/api/auth/sign-in': () => json({ id: 'user-1' }, { cookies: SESSION_COOKIES('s1') }),
      '/graphql': () => json({ data: { ok: true } }),
    });
    const s = newSession(m.fetchImpl, { clientVersion: '0.27.4' });
    assert.equal(m.calls.length, 0, 'no network before first use');

    const data = await s.graphql<{ ok: boolean }>('{ ok }');
    assert.deepEqual(data, { ok: true });

    const [signIn, gqlCall] = m.calls;
    assert.equal(signIn.method, 'POST');
    assert.deepEqual(JSON.parse(signIn.body), { email: EMAIL, password: PASSWORD });
    assert.equal(gqlCall.headers['cookie'], 'affine_session=s1; affine_csrf_token=csrf-s1; affine_user_id=user-1');
    assert.equal(gqlCall.headers['x-affine-version'], '0.27.4');
    assert.equal(gqlCall.headers['authorization'], undefined, 'no bearer token is sent to AFFiNE');
    assert.ok(!gqlCall.body.includes(PASSWORD));
  });

  it('re-logins once and retries when GraphQL answers "You must sign in first"', async () => {
    const m = mockFetch({
      '/api/auth/sign-in': (_c, n) => json({ id: 'user-1' }, { cookies: SESSION_COOKIES(`s${n}`) }),
      '/graphql': (call, n) =>
        n === 1 ? json(AUTH_REQUIRED) : json({ data: { cookieSeen: call.headers['cookie'] } }),
    });
    const s = newSession(m.fetchImpl);
    const data = await s.graphql<{ cookieSeen: string }>('{ x }');
    assert.equal(m.count('/api/auth/sign-in'), 2);
    assert.equal(m.count('/graphql'), 2);
    assert.match(data.cookieSeen, /affine_session=s2/, 'retry uses the new session');
  });

  it('re-logins once and retries on HTTP 401 (REST fetch)', async () => {
    const m = mockFetch({
      '/api/auth/sign-in': (_c, n) => json({ id: 'user-1' }, { cookies: SESSION_COOKIES(`s${n}`) }),
      '/api/workspaces/ws-1/blobs': (_c, n) => (n === 1 ? json({}, { status: 401 }) : json({ ok: 1 })),
    });
    const s = newSession(m.fetchImpl);
    const res = await s.fetch('/api/workspaces/ws-1/blobs');
    assert.equal(res.status, 200);
    assert.equal(m.count('/api/auth/sign-in'), 2);
  });

  it('gives up after one retry (no login loop)', async () => {
    const m = mockFetch({
      '/api/auth/sign-in': (_c, n) => json({ id: 'user-1' }, { cookies: SESSION_COOKIES(`s${n}`) }),
      '/graphql': () => json(AUTH_REQUIRED),
    });
    const s = newSession(m.fetchImpl);
    await assert.rejects(s.graphql('{ x }'), (err: Error) => err instanceof AffineAuthError);
    assert.equal(m.count('/api/auth/sign-in'), 2);
    assert.equal(m.count('/graphql'), 2);
  });

  it('does not re-login on ordinary GraphQL errors', async () => {
    const m = mockFetch({
      '/api/auth/sign-in': () => json({ id: 'user-1' }, { cookies: SESSION_COOKIES('s1') }),
      '/graphql': () => json({ errors: [{ message: 'Doc not found', extensions: { status: 404 } }] }),
    });
    const s = newSession(m.fetchImpl);
    await assert.rejects(s.graphql('{ x }'), /GraphQL error: Doc not found/);
    assert.equal(m.count('/api/auth/sign-in'), 1);
    assert.equal(m.count('/graphql'), 1);
  });

  it('shares one sign-in between concurrent requests', async () => {
    const m = mockFetch({
      '/api/auth/sign-in': async () => {
        await new Promise(r => setTimeout(r, 20));
        return json({ id: 'user-1' }, { cookies: SESSION_COOKIES('s1') });
      },
      '/graphql': () => json({ data: { ok: true } }),
    });
    const s = newSession(m.fetchImpl);
    await Promise.all([s.graphql('{a}'), s.graphql('{b}'), s.graphql('{c}')]);
    assert.equal(m.count('/api/auth/sign-in'), 1);
    assert.equal(m.count('/graphql'), 3);
  });

  it('concurrent 401s trigger a single re-login', async () => {
    const m = mockFetch({
      '/api/auth/sign-in': async (_c, n) => {
        await new Promise(r => setTimeout(r, 10));
        return json({ id: 'user-1' }, { cookies: SESSION_COOKIES(`s${n}`) });
      },
      '/graphql': call =>
        call.headers['cookie']?.includes('affine_session=s1') ? json(AUTH_REQUIRED) : json({ data: { ok: true } }),
    });
    const s = newSession(m.fetchImpl);
    await s.signIn();
    await Promise.all([s.graphql('{a}'), s.graphql('{b}'), s.graphql('{c}')]);
    assert.equal(m.count('/api/auth/sign-in'), 2);
  });

  it('keeps the cookie jar current from Set-Cookie (AFFiNE session refresh)', async () => {
    const m = mockFetch({
      '/api/auth/sign-in': () => json({ id: 'user-1' }, { cookies: SESSION_COOKIES('s1') }),
      '/graphql': (call, n) =>
        n === 1
          ? json({ data: { n } }, { cookies: SESSION_COOKIES('s1-refreshed') })
          : json({ data: { cookie: call.headers['cookie'] } }),
    });
    const s = newSession(m.fetchImpl);
    await s.graphql('{a}');
    const second = await s.graphql<{ cookie: string }>('{b}');
    assert.match(second.cookie, /affine_session=s1-refreshed/);
    assert.equal(m.count('/api/auth/sign-in'), 1);
  });

  it('signs in again proactively once the session cookie has expired', async () => {
    let now = Date.parse('2026-10-25T12:00:00Z');
    const m = mockFetch({
      '/api/auth/sign-in': (_c, n) =>
        json({ id: 'user-1' }, {
          cookies: [`affine_session=s${n}; Path=/; Expires=${new Date(now + 60_000).toUTCString()}; HttpOnly`],
        }),
      '/graphql': () => json({ data: { ok: true } }),
    });
    const s = newSession(m.fetchImpl, { now: () => now });
    await s.graphql('{a}');
    now += 120_000;
    await s.graphql('{b}');
    assert.equal(m.count('/api/auth/sign-in'), 2);
    assert.equal(m.count('/graphql'), 2, 'no wasted request with the expired cookie');
  });

  it('retries a Socket.IO-style operation once after an auth failure', async () => {
    const m = mockFetch({
      '/api/auth/sign-in': (_c, n) => json({ id: 'user-1' }, { cookies: SESSION_COOKIES(`s${n}`) }),
    });
    const s = newSession(m.fetchImpl);
    const seen: string[] = [];
    const result = await s.withSession(async headers => {
      seen.push(headers['Cookie']);
      if (seen.length === 1) throw new AffineAuthError('Socket connect failed: You must sign in first to access this resource.');
      return 'joined';
    });
    assert.equal(result, 'joined');
    assert.equal(seen.length, 2);
    assert.match(seen[1], /affine_session=s2/);
  });

  it('reports sign-in failures without leaking the password, and backs off', async () => {
    let now = 1_000_000;
    const m = mockFetch({
      '/api/auth/sign-in': () =>
        json({ name: 'WRONG_SIGN_IN_CREDENTIALS', message: 'Wrong user email or password: mcp-bot@example.com' }, { status: 400 }),
    });
    const { logger, lines } = captureLogger();
    const s = newSession(m.fetchImpl, { logger, loginCooldownMs: 5_000, now: () => now });
    await assert.rejects(s.graphql('{a}'), (err: Error) => {
      assert.match(err.message, /sign-in failed: HTTP 400 \(WRONG_SIGN_IN_CREDENTIALS/);
      assert.ok(!err.message.includes(PASSWORD));
      return true;
    });
    await assert.rejects(s.graphql('{b}'));
    assert.equal(m.count('/api/auth/sign-in'), 1, 'cooldown: no second attempt within 5s');
    now += 6_000;
    await assert.rejects(s.graphql('{c}'));
    assert.equal(m.count('/api/auth/sign-in'), 2);
    assert.ok(lines.every(l => !l.includes(PASSWORD)));
  });

  it('never logs the password or the session cookie', async () => {
    const m = mockFetch({
      '/api/auth/sign-in': (_c, n) => json({ id: 'user-1' }, { cookies: SESSION_COOKIES(`cookie-value-${n}`) }),
      '/graphql': (_c, n) => (n === 1 ? json(AUTH_REQUIRED) : json({ data: { ok: 1 } })),
    });
    const { logger, lines } = captureLogger();
    const s = newSession(m.fetchImpl, { logger });
    await s.graphql('{a}');
    assert.ok(lines.length > 0);
    for (const line of lines) {
      assert.ok(!line.includes(PASSWORD), line);
      assert.ok(!line.includes('cookie-value-'), line);
      assert.ok(!line.includes(EMAIL), line);
    }
  });
});

// ── MCP credentials ────────────────────────────────────────────────

const DAY = 24 * 60 * 60 * 1000;

/** AFFiNE stand-in: sign-in + the three MCP credential GraphQL operations. */
function credentialBackend(now: () => number, opts: { leftovers?: Array<{ id: string; name: string; status: string }> } = {}) {
  let seq = 0;
  const issued: string[] = [];
  const revoked: string[] = [];
  const createInputs: Array<Record<string, unknown>> = [];
  const m = mockFetch({
    '/api/auth/sign-in': () => json({ id: 'user-1' }, { cookies: SESSION_COOKIES('s1', now()) }),
    '/graphql': call => {
      const { query, variables } = JSON.parse(call.body);
      if (query.includes('createMcpCredential')) {
        seq += 1;
        const id = `cred-${seq}`;
        issued.push(id);
        createInputs.push(variables.input);
        return json({
          data: {
            createMcpCredential: {
              token: `aff_mcp_v1.${id}.secret${seq}`,
              credential: { id, expiresAt: new Date(now() + 30 * DAY).toISOString() },
            },
          },
        });
      }
      if (query.includes('mcpCredentials')) {
        const live = issued.map(id => ({ id, name: 'affine-mcp-ext', status: revoked.includes(id) ? 'REVOKED' : 'ACTIVE' }));
        return json({ data: { mcpCredentials: [...live, ...(opts.leftovers ?? [])] } });
      }
      if (query.includes('revokeMcpCredential')) {
        revoked.push(variables.id);
        return json({ data: { revokeMcpCredential: true } });
      }
      throw new Error(`unexpected query ${query}`);
    },
  });
  return { m, issued, revoked, createInputs };
}

function newManager(fetchImpl: FetchLike, now: () => number) {
  const { logger } = captureLogger();
  const session = newSession(fetchImpl, { now, logger });
  return new McpCredentialManager(session, { workspaceId: WS, now, logger, refreshMarginMs: 3 * DAY });
}

describe('McpCredentialManager', () => {
  it('creates a READ_ONLY workspace credential lazily and caches it', async () => {
    let now = Date.parse('2026-10-25T12:00:00Z');
    const b = credentialBackend(() => now);
    const mgr = newManager(b.m.fetchImpl, () => now);

    const t1 = await mgr.token();
    const t2 = await mgr.token();
    assert.equal(t1, 'aff_mcp_v1.cred-1.secret1');
    assert.equal(t2, t1);
    assert.deepEqual(b.issued, ['cred-1']);
    assert.deepEqual(b.createInputs[0], {
      workspaceId: WS,
      name: 'affine-mcp-ext',
      accessMode: 'READ_ONLY',
      expirationDays: 30,
    });
  });

  it('re-creates the credential before expiresAt and revokes the one it replaced', async () => {
    let now = Date.parse('2026-10-25T12:00:00Z');
    const b = credentialBackend(() => now);
    const mgr = newManager(b.m.fetchImpl, () => now);

    const first = await mgr.token();
    now += 26 * DAY; // 4 days left — outside the 3-day margin
    assert.equal(await mgr.token(), first);
    now += 2 * DAY; // 2 days left — inside the margin
    const second = await mgr.token();

    assert.notEqual(second, first);
    assert.equal(second, 'aff_mcp_v1.cred-2.secret2');
    assert.deepEqual(b.revoked, ['cred-1']);
  });

  it('re-creates on 401 (rejected) and revokes the rejected credential', async () => {
    let now = Date.parse('2026-10-25T12:00:00Z');
    const b = credentialBackend(() => now);
    const mgr = newManager(b.m.fetchImpl, () => now);

    const first = await mgr.token();
    const fresh = await mgr.rejected(first);
    assert.equal(fresh, 'aff_mcp_v1.cred-2.secret2');
    assert.deepEqual(b.revoked, ['cred-1']);
    assert.equal(await mgr.token(), fresh, 'cached afterwards');

    // A late 401 for the already-replaced token must not mint yet another one.
    assert.equal(await mgr.rejected(first), fresh);
    assert.deepEqual(b.issued, ['cred-1', 'cred-2']);
  });

  it('revokes leftovers with the managed name from earlier runs, not other credentials', async () => {
    let now = Date.parse('2026-10-25T12:00:00Z');
    const b = credentialBackend(() => now, {
      leftovers: [
        { id: 'old-run', name: 'affine-mcp-ext', status: 'ACTIVE' },
        { id: 'old-expiring', name: 'affine-mcp-ext', status: 'EXPIRING' },
        { id: 'already-revoked', name: 'affine-mcp-ext', status: 'REVOKED' },
        { id: 'users-own', name: 'Claude Desktop', status: 'ACTIVE' },
      ],
    });
    const mgr = newManager(b.m.fetchImpl, () => now);
    await mgr.token();
    assert.deepEqual(b.revoked.sort(), ['old-expiring', 'old-run']);
  });

  it('shares one creation between concurrent callers', async () => {
    let now = Date.parse('2026-10-25T12:00:00Z');
    const b = credentialBackend(() => now);
    const mgr = newManager(b.m.fetchImpl, () => now);
    const tokens = await Promise.all([mgr.token(), mgr.token(), mgr.token()]);
    assert.equal(new Set(tokens).size, 1);
    assert.deepEqual(b.issued, ['cred-1']);
  });

  it('explains a disabled copilot plugin', async () => {
    const m = mockFetch({
      '/api/auth/sign-in': () => json({ id: 'user-1' }, { cookies: SESSION_COOKIES('s1') }),
      '/graphql': () => json({ errors: [{ message: 'Copilot is disabled.', extensions: { status: 403 } }] }),
    });
    const mgr = newManager(m.fetchImpl, Date.now);
    await assert.rejects(mgr.token(), /copilot\.enabled/);
  });
});

// ── Native MCP forwarding ──────────────────────────────────────────

describe('NativeMcpClient', () => {
  const URL_ = `${BASE}/api/workspaces/${WS}/mcp`;

  function nativeBackend(valid: Set<string>) {
    return mockFetch({
      [`/api/workspaces/${WS}/mcp`]: call => {
        const token = (call.headers['authorization'] ?? '').replace(/^Bearer /, '');
        if (!valid.has(token)) {
          return json({ jsonrpc: '2.0', error: { code: -32000, message: 'Authentication failed' }, id: null }, { status: 401 });
        }
        const msg = JSON.parse(call.body);
        if (msg.id === undefined) return new Response(null, { status: 202 });
        if (msg.method === 'tools/call') {
          return json({ jsonrpc: '2.0', id: msg.id, result: { content: [{ type: 'text', text: `# ${msg.params.arguments.docId}` }] } });
        }
        return json({ jsonrpc: '2.0', id: msg.id, result: {} });
      },
    });
  }

  it('re-creates the credential on 401 and retries once', async () => {
    const valid = new Set(['new-token']);
    const m = nativeBackend(valid);
    let current = 'old-token';
    const rejectedCalls: string[] = [];
    const client = new NativeMcpClient(URL_, {
      token: async () => current,
      rejected: async t => {
        rejectedCalls.push(t);
        current = 'new-token';
        return current;
      },
    }, m.fetchImpl);

    const result = (await client.callTool('read_document', { docId: 'doc-1' })) as { content: Array<{ text: string }> };
    assert.equal(result.content[0].text, '# doc-1');
    assert.deepEqual(rejectedCalls, ['old-token']);
    assert.ok(m.calls.every(c => !c.headers['cookie']), 'native MCP gets no session cookie');
  });

  it('fails (instead of looping) when the fresh credential is rejected too', async () => {
    const m = nativeBackend(new Set());
    let n = 0;
    const client = new NativeMcpClient(URL_, {
      token: async () => `tok-${n}`,
      rejected: async () => `tok-${++n}`,
    }, m.fetchImpl);
    await assert.rejects(client.callTool('read_document', { docId: 'x' }), /Native MCP HTTP 401/);
    assert.equal(n, 1);
  });
});
