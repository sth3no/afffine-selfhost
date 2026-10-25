FROM node:22-alpine AS base
WORKDIR /app
RUN apk add --no-cache tini

# Production dependencies only, straight from the lockfile. tsx is a runtime
# dependency (the proxy runs TypeScript directly); typescript/@types are not
# shipped. `npm ci` fails loudly on a stale lockfile.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY tsconfig.json ./
COPY src ./src

ENV NODE_ENV=production
# Drop root — the proxy only reads its sources and talks HTTP/WebSocket.
USER node
EXPOSE 3100
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["npx", "tsx", "src/server.ts"]
