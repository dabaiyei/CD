FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SKILLS_ROOT=/var/lib/cineforge/skills \
    UPLOADS_ROOT=/var/lib/cineforge/uploads

WORKDIR /srv/cineforge-api

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY skills /var/lib/cineforge/skills

RUN python -m pip install --no-cache-dir ".[postgres,s3]" \
    && groupadd --system --gid 10001 cineforge \
    && useradd --system --uid 10001 --gid cineforge --create-home cineforge \
    && mkdir -p /var/lib/cineforge/uploads \
    && chown -R cineforge:cineforge /srv/cineforge-api /var/lib/cineforge

USER cineforge

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
