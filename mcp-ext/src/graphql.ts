/**
 * Tiny GraphQL client wrapped around fetch.
 *
 * We don't pull in graphql-request / apollo-client etc. — the entire
 * surface area we need is POST with JSON body, and error unwrapping.
 */

import { graphqlUrl } from './config.js';

export interface GraphQLError {
  message: string;
  path?: string[];
}

/** Execute a GraphQL query against AFFiNE using the caller's bearer token. */
export async function gql<T>(
  token: string,
  query: string,
  variables: Record<string, unknown> = {}
): Promise<T> {
  const res = await fetch(graphqlUrl(), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ query, variables }),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(
      `GraphQL HTTP ${res.status} ${res.statusText}${text ? ` — ${text.slice(0, 400)}` : ''}`
    );
  }

  const body = (await res.json()) as { data?: T; errors?: GraphQLError[] };
  if (body.errors && body.errors.length > 0) {
    const msg = body.errors.map(e => e.message).join('; ');
    throw new Error(`GraphQL error: ${msg}`);
  }
  if (!body.data) {
    throw new Error('GraphQL response contained no data');
  }
  return body.data;
}
