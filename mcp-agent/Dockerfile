# Build context is the affine-mcp-agent folder copied next to this Dockerfile.
# See README — the `build.sh` / `prepare.sh` helper copies sources in, or you
# can commit `mcp-agent/src` to your stack repo directly.

FROM node:22-alpine AS base
WORKDIR /app

# System deps for tsx / native modules (kept minimal)
RUN apk add --no-cache tini

# Install production dependencies only, straight from the lockfile. tsx is a
# runtime dependency (the agent runs TypeScript directly), so it stays; the
# test/type tooling (vitest, typescript, @types/*) is not shipped. `npm ci`
# fails loudly on a stale lockfile instead of silently re-resolving. The
# `test -x` guard fails the build (instead of crash-looping at runtime) if a
# package.json without tsx in `dependencies` is ever staged in again, e.g.
# by prepare.sh copying an older upstream copy.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev \
    && test -x node_modules/.bin/tsx \
    && npm cache clean --force

# Copy sources
COPY tsconfig.json ./
COPY src ./src

ENV NODE_ENV=production
# Drop root — the agent only reads its sources and makes outbound HTTP calls.
USER node
# Run under tini so node-cron / signals behave correctly in Docker
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["npx", "tsx", "src/scheduler.ts"]
