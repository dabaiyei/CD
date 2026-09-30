FROM node:22-bookworm-slim AS canvas-agent-build
WORKDIR /build
RUN corepack enable && corepack prepare pnpm@11.7.0 --activate
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/package.json
COPY services/infinite-canvas/web/package.json ./services/infinite-canvas/web/package.json
COPY services/infinite-canvas/canvas-agent/package.json ./services/infinite-canvas/canvas-agent/package.json
RUN pnpm install --frozen-lockfile --filter @basketikun/canvas-agent...
COPY services/infinite-canvas/canvas-agent ./services/infinite-canvas/canvas-agent
RUN pnpm build:canvas-agent && pnpm --filter @basketikun/canvas-agent deploy --prod --legacy /canvas-agent

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SKILLS_ROOT=/var/lib/cineforge/skills \
    UPLOADS_ROOT=/var/lib/cineforge/uploads \
    CANVAS_AGENT_ENTRY=/opt/infinite-canvas/canvas-agent/dist/index.js \
    CODEX_HOME=/var/lib/cineforge-agent-runtime/codex

WORKDIR /srv/cineforge-api

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=canvas-agent-build /usr/local/bin/node /usr/local/bin/node
COPY --from=canvas-agent-build /canvas-agent /opt/infinite-canvas/canvas-agent
COPY apps/api/pyproject.toml ./
COPY apps/api/app ./app
COPY apps/api/alembic.ini ./
COPY apps/api/migrations ./migrations
COPY apps/api/skills /var/lib/cineforge/skills

RUN python -m pip install --no-cache-dir ".[postgres,s3]" \
    && groupadd --system --gid 10001 cineforge \
    && useradd --system --uid 10001 --gid cineforge --create-home cineforge \
    && mkdir -p /var/lib/cineforge/uploads /var/lib/cineforge/site-backups /var/lib/cineforge-agent-runtime/codex \
    && chown -R cineforge:cineforge /srv/cineforge-api /var/lib/cineforge /var/lib/cineforge-agent-runtime

USER cineforge

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
