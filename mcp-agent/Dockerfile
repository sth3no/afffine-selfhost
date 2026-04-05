# Build context is the affine-mcp-agent folder copied next to this Dockerfile.
# See README — the `build.sh` / `prepare.sh` helper copies sources in, or you
# can commit `mcp-agent/src` to your stack repo directly.

FROM node:22-alpine AS base
WORKDIR /app

# System deps for tsx / native modules (kept minimal)
RUN apk add --no-cache tini

# Install dependencies first for better layer caching
COPY package.json package-lock.json* ./
RUN npm ci --omit=optional || npm install

# Copy sources
COPY tsconfig.json ./
COPY src ./src

ENV NODE_ENV=production
# Run under tini so node-cron / signals behave correctly in Docker
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["npx", "tsx", "src/scheduler.ts"]
