# syntax=docker/dockerfile:1
# Production frontend image. The dev server image stays frontend.Dockerfile
# for the local `ui` compose profile; this one is what CI publishes and the
# production compose runs. The two are deliberately separate files, not one
# multi-target file -- dev and prod differ in more than a flag (dev mounts no
# build, runs pnpm; prod ships only server.js and runs bare node).

########################  deps  ########################
FROM node:24-slim AS deps
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml ./
# Full install, dev deps included: `next build` needs tailwind, typescript,
# eslint-config-next. The standalone output does the runtime pruning.
RUN pnpm install --frozen-lockfile

########################  build  ########################
FROM node:24-slim AS build
WORKDIR /app
RUN corepack enable
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* vars are INLINED INTO THE CLIENT BUNDLE at build time -- unlike
# the dev server, a built image cannot pick them up from runtime env. REQUIRED,
# deliberately no default: a forgotten --build-arg would otherwise fall through
# to lib/api.ts's localhost:8000 fallback, which in a visitor's browser is THEIR
# machine -- an image that builds green and fails every API call. The
# same-origin edge build passes `--build-arg NEXT_PUBLIC_API_URL=/api`.
ARG NEXT_PUBLIC_API_URL
RUN test -n "$NEXT_PUBLIC_API_URL" || { echo "NEXT_PUBLIC_API_URL build arg is required (e.g. /api)" >&2; exit 1; }
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}
RUN pnpm build

########################  runtime  ########################
FROM node:24-slim
ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0
WORKDIR /app
# output:"standalone" emits server.js + a pruned node_modules, but NOT
# .next/static or public -- Next's deployment contract requires copying both
# beside it (they are served from disk by the standalone server).
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
COPY --from=build --chown=node:node /app/public ./public
# node:24-slim ships the non-root `node` user (uid 1000). No pnpm, no dev
# server, no source tree in this stage -- just the standalone server.
USER node
EXPOSE 3000
CMD ["node", "server.js"]
