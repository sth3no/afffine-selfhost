/**
 * Tiny GraphQL helper.
 *
 * We don't pull in graphql-request / apollo-client etc. — the entire
 * surface area we need is POST with JSON body, and error unwrapping. The
 * request itself (cookie session, re-login on 401) lives in
 * AffineSession.graphql (affine-auth.ts).
 */

import type { AffineContext } from './tools-shared.js';

export interface GraphQLError {
  message: string;
  path?: string[];
}

/** Execute a GraphQL query against AFFiNE with mcp-ext's own session. */
export async function gql<T>(
  ctx: AffineContext,
  query: string,
  variables: Record<string, unknown> = {}
): Promise<T> {
  return ctx.session.graphql<T>(query, variables);
}
