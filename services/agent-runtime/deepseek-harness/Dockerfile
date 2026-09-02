FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_RUNTIME_DATA_ROOT=/var/lib/cineforge-agent-runtime

WORKDIR /srv/agent-runtime
COPY pyproject.toml ./
COPY runtime ./runtime
RUN python -m pip install --no-cache-dir ".[harness]"

RUN useradd --system --uid 10001 --create-home runtime \
    && mkdir -p /var/lib/cineforge-agent-runtime \
    && chown -R runtime:runtime /var/lib/cineforge-agent-runtime /srv/agent-runtime
USER runtime

EXPOSE 8010
CMD ["uvicorn", "runtime.main:app", "--host", "0.0.0.0", "--port", "8010"]
