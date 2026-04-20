FROM node:22-alpine AS base
WORKDIR /app
RUN apk add --no-cache tini

COPY package.json package-lock.json* ./
RUN npm install --omit=optional

COPY tsconfig.json ./
COPY src ./src

ENV NODE_ENV=production
EXPOSE 3100
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["npx", "tsx", "src/server.ts"]
