FROM node:22-alpine AS build

WORKDIR /build
RUN corepack enable && corepack prepare pnpm@11.7.0 --activate

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/package.json
COPY services/infinite-canvas/web/package.json ./services/infinite-canvas/web/package.json
COPY services/infinite-canvas/canvas-agent/package.json ./services/infinite-canvas/canvas-agent/package.json
RUN pnpm install --frozen-lockfile --filter @cineforge/web... --filter infinite-canvas...

COPY apps/web ./apps/web
COPY services/infinite-canvas ./services/infinite-canvas
RUN pnpm build:web

FROM nginx:1.27-alpine

COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/apps/web/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
  CMD wget -qO- http://127.0.0.1/health >/dev/null || exit 1
