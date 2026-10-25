/**
 * Server-side authentication to AFFiNE 0.27+.
 *
 * AFFiNE 0.27 removed legacy personal access tokens (`ut_…`). What is left:
 *
 *  - GraphQL, REST and the Socket.IO sync gateway accept a cookie session
 *    (`affine_session` + `affine_user_id`) issued by `POST /api/auth/sign-in`
 *    — the same endpoint the web client uses for email/password sign-in.
 *    → {@link AffineSession}
 *
 *  - The native workspace MCP endpoint (`/api/workspaces/:id/mcp`) only
 *    accepts MCP credentials (`Authorization: Bearer aff_mcp_v1.<id>.<secret>`)
 *    minted with the GraphQL `createMcpCredential` mutation. They are
 *    workspace-scoped and expire (30/90/365 days).
 *    → {@link McpCredentialManager}
 *
 * Everything is kept in memory only. Credentials, cookies and tokens are
 * never logged — log lines carry at most a credential id prefix.
 */

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface Logger {
  info(msg: string): void;
  warn(msg: string): void;
}

const defaultLogger: Logger = {
  info: msg => console.log(`[mcp-ext:auth] ${msg}`),
  warn: msg => console.warn(`[mcp-ext:auth] ${msg}`),
};

const SESSION_COOKIE = 'affine_session';
const DEFAULT_CLIENT_VERSION = '0.27.4';
const DEFAULT_LOGIN_COOLDOWN_MS = 5_000;

/** AFFiNE rejected (or no longer accepts) our credentials. Triggers a retry. */
export class AffineAuthError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'AffineAuthError';
  }
}

/** AFFiNE's "not signed in" wording across HTTP, GraphQL and Socket.IO. */
export function isAuthRequiredMessage(message: string): boolean {
  return /you must sign in first|authentication required|access token has expired/i.test(message);
}

export interface GraphQLErrorShape {
  message: string;
  extensions?: { status?: number; type?: string; name?: string };
}

/** GraphQL answers auth failures with HTTP 200 + `extensions.status: 401`. */
export function isAuthRequiredGraphQLError(error: GraphQLErrorShape): boolean {
  const ext = error.extensions ?? {};
  return (
    ext.status === 401 ||
    ext.type === 'AUTHENTICATION_REQUIRED' ||
    ext.name === 'AUTHENTICATION_REQUIRED' ||
    isAuthRequiredMessage(error.message ?? '')
  );
}

export function isAuthError(err: unknown): boolean {
  if (err instanceof AffineAuthError) return true;
  return err instanceof Error && isAuthRequiredMessage(err.message);
}

/** Pull a short, credential-free reason out of an AFFiNE error body. */
function describeErrorBody(text: string): string {
  try {
    const body = JSON.parse(text) as { name?: string; type?: string; message?: string };
    const label = body.name ?? body.type;
    if (body.message) return ` (${label ? `${label}: ` : ''}${body.message})`;
  } catch {
    // not JSON
  }
  return '';
}

export interface AffineSessionOptions {
  /** AFFiNE base URL, e.g. http://affine:3010 (no trailing slash needed). */
  baseUrl: string;
  email: string;
  password: string;
  /** Sent as `x-affine-version` (only enforced when AFFiNE version control is on). */
  clientVersion?: string;
  /** Minimum gap between two failed sign-in attempts (protects AFFiNE's auth throttle). */
  loginCooldownMs?: number;
  fetch?: FetchLike;
  now?: () => number;
  logger?: Logger;
}

export interface GraphQLRequestInit {
  headers?: Record<string, string>;
  body: BodyInit;
}

/**
 * One email/password cookie session, shared by every tool call.
 *
 * - Signs in lazily on first use (single-flight under concurrency).
 * - Keeps the cookie jar current from every `Set-Cookie` AFFiNE sends
 *   (AFFiNE re-issues `affine_session` when it extends the session).
 * - On a 401 / "You must sign in first" it signs in again **once** and
 *   retries the request **once**.
 */
export class AffineSession {
  readonly baseUrl: string;
  readonly clientVersion: string;
  private readonly cookies = new Map<string, string>();
  private sessionExpiresAt: number | null = null;
  private generation = 0;
  private login: Promise<void> | null = null;
  private lastFailure: { at: number; error: Error } | null = null;
  private readonly fetchImpl: FetchLike;
  private readonly now: () => number;
  private readonly log: Logger;
  private readonly cooldownMs: number;

  constructor(private readonly opts: AffineSessionOptions) {
    if (!opts.email || !opts.password) {
      throw new Error('AffineSession requires an email and a password');
    }
    this.baseUrl = opts.baseUrl.replace(/\/+$/, '');
    this.clientVersion = opts.clientVersion || DEFAULT_CLIENT_VERSION;
    this.fetchImpl = opts.fetch ?? ((input, init) => fetch(input, init));
    this.now = opts.now ?? Date.now;
    this.log = opts.logger ?? defaultLogger;
    this.cooldownMs = opts.loginCooldownMs ?? DEFAULT_LOGIN_COOLDOWN_MS;
  }

  /** True once a sign-in succeeded and the session cookie has not expired. */
  get signedIn(): boolean {
    return this.cookies.has(SESSION_COOKIE) && !this.expired();
  }

  private expired(): boolean {
    return this.sessionExpiresAt !== null && this.now() >= this.sessionExpiresAt;
  }

  private cookieHeader(): string {
    return [...this.cookies].map(([k, v]) => `${k}=${v}`).join('; ');
  }

  /** Headers every authenticated request to AFFiNE carries. */
  private authHeaders(): Record<string, string> {
    return { Cookie: this.cookieHeader(), 'x-affine-version': this.clientVersion };
  }

  private absorbSetCookies(res: Response): void {
    const headers = res.headers as Headers & { getSetCookie?: () => string[] };
    const list =
      typeof headers.getSetCookie === 'function'
        ? headers.getSetCookie()
        : (res.headers.get('set-cookie') ?? '').split(/,(?=\s*[A-Za-z0-9_-]+=)/).filter(Boolean);
    for (const raw of list) {
      const [pair, ...attrs] = raw.split(';');
      const eq = pair.indexOf('=');
      if (eq <= 0) continue;
      const name = pair.slice(0, eq).trim();
      const value = pair.slice(eq + 1).trim();
      let expiresAt: number | null = null;
      for (const attr of attrs) {
        const [k, ...rest] = attr.trim().split('=');
        const v = rest.join('=');
        if (/^max-age$/i.test(k)) expiresAt = this.now() + Number(v) * 1000;
        else if (/^expires$/i.test(k) && expiresAt === null) {
          const t = Date.parse(v);
          if (Number.isFinite(t)) expiresAt = t;
        }
      }
      if (/[\r\n]/.test(value)) continue; // never forward header-injection material
      if (!value || (expiresAt !== null && expiresAt <= this.now())) {
        this.cookies.delete(name);
        if (name === SESSION_COOKIE) this.sessionExpiresAt = null;
        continue;
      }
      this.cookies.set(name, value);
      if (name === SESSION_COOKIE) this.sessionExpiresAt = expiresAt;
    }
  }

  /**
   * Sign in with email/password. Concurrent callers share one attempt.
   * `failedGeneration` is the session generation a caller saw rejected: if a
   * newer session already exists, it is reused instead of signing in again.
   */
  async signIn(failedGeneration?: number): Promise<void> {
    if (failedGeneration !== undefined && failedGeneration < this.generation && this.signedIn) return;
    if (this.login) return this.login;
    if (this.lastFailure && this.now() - this.lastFailure.at < this.cooldownMs) {
      throw this.lastFailure.error;
    }

    this.login = (async () => {
      const res = await this.fetchImpl(`${this.baseUrl}/api/auth/sign-in`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
          'x-affine-version': this.clientVersion,
        },
        body: JSON.stringify({ email: this.opts.email, password: this.opts.password }),
      });
      const text = await res.text().catch(() => '');
      if (!res.ok) {
        throw new AffineAuthError(`AFFiNE sign-in failed: HTTP ${res.status}${describeErrorBody(text)}`);
      }
      this.cookies.clear();
      this.sessionExpiresAt = null;
      this.absorbSetCookies(res);
      if (!this.cookies.has(SESSION_COOKIE)) {
        throw new AffineAuthError('AFFiNE sign-in succeeded but returned no session cookie');
      }
      this.generation += 1;
      this.lastFailure = null;
      this.log.info('signed in to AFFiNE (cookie session established)');
    })();

    try {
      await this.login;
    } catch (err) {
      this.lastFailure = { at: this.now(), error: err as Error };
      throw err;
    } finally {
      this.login = null;
    }
  }

  /** Make sure a usable session exists; returns the generation in use. */
  private async ensure(): Promise<number> {
    if (!this.signedIn) await this.signIn(this.generation);
    return this.generation;
  }

  /** Drop the session (next request signs in again). */
  invalidate(): void {
    this.cookies.clear();
    this.sessionExpiresAt = null;
  }

  /**
   * Run `fn` with the current session headers. If it fails with an auth
   * error, sign in again once and retry once. Used directly by the
   * Socket.IO writer; `fetch`/`graphql` are built on top of it.
   */
  async withSession<T>(
    fn: (headers: Record<string, string>) => Promise<T>,
    authError: (err: unknown) => boolean = isAuthError,
  ): Promise<T> {
    const generation = await this.ensure();
    try {
      return await fn(this.authHeaders());
    } catch (err) {
      if (!authError(err)) throw err;
      this.log.warn('AFFiNE rejected the session — signing in again and retrying once');
      this.invalidate();
      await this.signIn(generation);
      return await fn(this.authHeaders());
    }
  }

  /** `fetch` against AFFiNE with the session cookie (re-login + retry once on HTTP 401). */
  async fetch(path: string, init: RequestInit = {}): Promise<Response> {
    const url = /^https?:\/\//.test(path) ? path : `${this.baseUrl}${path}`;
    return this.withSession(async headers => {
      const res = await this.fetchImpl(url, {
        ...init,
        headers: { ...(init.headers as Record<string, string> | undefined), ...headers },
      });
      this.absorbSetCookies(res);
      if (res.status === 401) {
        const text = await res.text().catch(() => '');
        throw new AffineAuthError(`AFFiNE HTTP 401${describeErrorBody(text)}`);
      }
      return res;
    });
  }

  /**
   * POST to AFFiNE's GraphQL endpoint. `makeInit` is called per attempt so
   * the body can be rebuilt for the retry. Auth failures (HTTP 401 or a
   * GraphQL error with `extensions.status: 401`) re-login once and retry.
   */
  async graphqlRequest<T>(makeInit: () => GraphQLRequestInit): Promise<T> {
    return this.withSession(async headers => {
      const init = makeInit();
      const res = await this.fetchImpl(`${this.baseUrl}/graphql`, {
        method: 'POST',
        headers: { ...init.headers, ...headers },
        body: init.body,
      });
      this.absorbSetCookies(res);
      const text = await res.text().catch(() => '');
      if (res.status === 401) {
        throw new AffineAuthError(`GraphQL HTTP 401${describeErrorBody(text)}`);
      }
      if (!res.ok) {
        throw new Error(
          `GraphQL HTTP ${res.status} ${res.statusText}${text ? ` — ${text.slice(0, 400)}` : ''}`,
        );
      }
      let body: { data?: T; errors?: GraphQLErrorShape[] };
      try {
        body = JSON.parse(text);
      } catch {
        throw new Error(`GraphQL response was not JSON: ${text.slice(0, 200)}`);
      }
      if (body.errors && body.errors.length > 0) {
        const msg = body.errors.map(e => e.message).join('; ');
        if (body.errors.some(isAuthRequiredGraphQLError)) {
          throw new AffineAuthError(`GraphQL error: ${msg}`);
        }
        throw new Error(`GraphQL error: ${msg}`);
      }
      if (!body.data) throw new Error('GraphQL response contained no data');
      return body.data;
    });
  }

  /** JSON GraphQL query/mutation. */
  graphql<T>(query: string, variables: Record<string, unknown> = {}): Promise<T> {
    return this.graphqlRequest<T>(() => ({
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ query, variables }),
    }));
  }
}

// ── MCP credentials for AFFiNE's native workspace MCP endpoint ──────

const CREATE_MCP_CREDENTIAL = `
  mutation CreateMcpCredential($input: CreateMcpCredentialInput!) {
    createMcpCredential(input: $input) {
      token
      credential { id expiresAt }
    }
  }
`;

const LIST_MCP_CREDENTIALS = `
  query McpCredentials($workspaceId: String!) {
    mcpCredentials(workspaceId: $workspaceId) { id name status }
  }
`;

const REVOKE_MCP_CREDENTIAL = `
  mutation RevokeMcpCredential($id: ID!, $workspaceId: String!) {
    revokeMcpCredential(id: $id, workspaceId: $workspaceId)
  }
`;

const DAY_MS = 24 * 60 * 60 * 1000;

export interface McpCredentialOptions {
  workspaceId: string;
  /**
   * Name of the credentials this process manages. Other credentials of the
   * same account with this exact name are treated as leftovers (previous
   * container runs, replaced credentials) and revoked.
   */
  name?: string;
  /** Lifetime requested from AFFiNE. AFFiNE accepts 30, 90 or 365. */
  expirationDays?: 30 | 90 | 365;
  /** Re-create the credential when it has less than this much life left. */
  refreshMarginMs?: number;
  now?: () => number;
  logger?: Logger;
}

interface CachedCredential {
  id: string;
  token: string;
  expiresAt: number;
}

/**
 * Mints and caches the MCP credential used for the native MCP endpoint.
 *
 * - Created lazily via `createMcpCredential` (READ_ONLY — the only mode
 *   stable AFFiNE allows; the forwarded native tools are read-only anyway).
 * - Re-created when it gets within `refreshMarginMs` of `expiresAt`, and
 *   when the native endpoint answers 401 (callers retry once).
 * - After each re-creation the replaced credential — and any other
 *   leftover credential with the same managed name — is revoked, so they
 *   don't pile up across rotations and container restarts.
 *
 * `rotateMcpCredential` is deliberately not used: AFFiNE keeps the rotated
 * predecessor alive for a grace period and `revokeMcpCredential` revokes a
 * whole credential family, so the old one could not be revoked without
 * killing its replacement.
 */
export class McpCredentialManager {
  readonly name: string;
  private current: CachedCredential | null = null;
  private pending: Promise<CachedCredential> | null = null;
  private readonly expirationDays: number;
  private readonly refreshMarginMs: number;
  private readonly now: () => number;
  private readonly log: Logger;

  constructor(
    private readonly session: AffineSession,
    private readonly opts: McpCredentialOptions,
  ) {
    this.name = opts.name || 'affine-mcp-ext';
    this.expirationDays = opts.expirationDays ?? 30;
    this.refreshMarginMs = opts.refreshMarginMs ?? 3 * DAY_MS;
    this.now = opts.now ?? Date.now;
    this.log = opts.logger ?? defaultLogger;
  }

  /** Bearer token for the native MCP endpoint (creating / renewing as needed). */
  async token(): Promise<string> {
    const cur = this.current;
    if (cur && this.now() < cur.expiresAt - this.refreshMarginMs) return cur.token;
    return (await this.renew(cur ? 'expiring soon' : 'initial')).token;
  }

  /**
   * The native endpoint rejected `rejectedToken` (HTTP 401). Returns a fresh
   * token; if another caller already replaced it, returns that one.
   */
  async rejected(rejectedToken: string): Promise<string> {
    const cur = this.current;
    if (cur && cur.token !== rejectedToken) return cur.token;
    return (await this.renew('rejected by AFFiNE')).token;
  }

  private renew(reason: string): Promise<CachedCredential> {
    if (this.pending) return this.pending;
    const replacing = this.current;
    this.pending = (async () => {
      let created: { createMcpCredential: { token: string; credential: { id: string; expiresAt: string } } };
      try {
        created = await this.session.graphql(CREATE_MCP_CREDENTIAL, {
          input: {
            workspaceId: this.opts.workspaceId,
            name: this.name,
            accessMode: 'READ_ONLY',
            expirationDays: this.expirationDays,
          },
        });
      } catch (err) {
        const msg = (err as Error).message;
        if (/copilot is disabled/i.test(msg)) {
          throw new Error(
            'AFFiNE native MCP is unavailable: AI (copilot) is disabled on this server. ' +
              'Enable it in AFFiNE Admin panel → Settings → AI (copilot.enabled = true).',
          );
        }
        throw new Error(`Could not create an AFFiNE MCP credential: ${msg}`);
      }
      const { token, credential } = created.createMcpCredential;
      const expiresAt = Date.parse(credential.expiresAt);
      const next: CachedCredential = {
        id: credential.id,
        token,
        expiresAt: Number.isFinite(expiresAt) ? expiresAt : this.now() + this.expirationDays * DAY_MS,
      };
      this.current = next;
      this.log.info(
        `MCP credential ${next.id.slice(0, 8)}… issued (${reason}); expires ${new Date(next.expiresAt).toISOString()}`,
      );
      await this.revokeLeftovers(next.id, replacing?.id);
      return next;
    })();
    const done = this.pending;
    done.then(
      () => (this.pending = null),
      () => (this.pending = null),
    );
    return done;
  }

  /** Revoke the replaced credential and other leftovers with our name. Never throws. */
  private async revokeLeftovers(keepId: string, replacedId?: string): Promise<void> {
    const ids = new Set<string>();
    if (replacedId && replacedId !== keepId) ids.add(replacedId);
    try {
      const { mcpCredentials } = await this.session.graphql<{
        mcpCredentials: Array<{ id: string; name: string; status: string }>;
      }>(LIST_MCP_CREDENTIALS, { workspaceId: this.opts.workspaceId });
      for (const c of mcpCredentials) {
        if (c.id === keepId || c.name !== this.name) continue;
        if (c.status === 'REVOKED' || c.status === 'EXPIRED') continue;
        ids.add(c.id);
      }
    } catch (err) {
      this.log.warn(`could not list MCP credentials for cleanup: ${(err as Error).message}`);
    }
    for (const id of ids) {
      try {
        await this.session.graphql(REVOKE_MCP_CREDENTIAL, { id, workspaceId: this.opts.workspaceId });
        this.log.info(`revoked replaced MCP credential ${id.slice(0, 8)}…`);
      } catch (err) {
        this.log.warn(`could not revoke MCP credential ${id.slice(0, 8)}…: ${(err as Error).message}`);
      }
    }
  }
}
