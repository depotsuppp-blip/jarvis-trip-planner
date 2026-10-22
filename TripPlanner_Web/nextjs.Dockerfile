# syntax=docker/dockerfile:1

# Trip Planner web app (Next.js) - production image.
#
# Build (from the repo root, which also wires up the secret below):
#     docker compose build frontend
#
# Three stages, so the final image holds only Next's traced `standalone`
# output - no devDependencies, no source, no npm cache.

ARG NODE_VERSION=22

# --- deps: install node_modules ------------------------------------------
FROM node:${NODE_VERSION}-bookworm-slim AS deps
WORKDIR /app

# package.json's `postinstall` is `prisma generate`, so the schema and the
# Prisma config have to be on disk when `npm ci` runs. prisma.config.ts refuses
# to load without DIRECT_URL even though generating never connects anywhere, so
# this one command gets a placeholder (not a real URL, not kept in the image).
COPY package.json package-lock.json ./
COPY prisma ./prisma
COPY prisma.config.ts ./
RUN --mount=type=cache,target=/root/.npm \
    DIRECT_URL="postgresql://placeholder:placeholder@localhost:5432/placeholder" npm ci

# --- builder: next build ---------------------------------------------------
FROM node:${NODE_VERSION}-bookworm-slim AS builder
WORKDIR /app

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# next.config.ts reads NEXT_OUTPUT; "standalone" makes `next build` emit
# .next/standalone/server.js plus only the node_modules files it traced.
ENV NEXT_TELEMETRY_DISABLED=1 \
    NEXT_OUTPUT=standalone

# NEXT_PUBLIC_* values (Google Maps browser key + Map ID, LIFF id) are inlined
# into the JS bundle at build time, so `next build` has to see .env.local.
# It arrives as a BuildKit secret: mounted for this one RUN, never copied into a
# layer or into the image history. Optional - without it the build still works
# and the map/LIFF features simply report themselves as unconfigured.
RUN --mount=type=secret,id=web_env,target=/app/.env.local,required=false \
    npm run build

# --- runner: what actually ships ----------------------------------------------
FROM node:${NODE_VERSION}-bookworm-slim AS runner
WORKDIR /app

# HOSTNAME matters: Docker sets it to the container id, and server.js would then
# listen on that one interface only - unreachable from the published port and
# from the healthcheck. 0.0.0.0 listens on all of them.
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# The node image ships an unprivileged `node` user (uid 1000).
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public

USER node
EXPOSE 3000

CMD ["node", "server.js"]
