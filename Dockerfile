# syntax=docker/dockerfile:1
FROM python:3.11-slim

LABEL org.opencontainers.image.source="https://github.com/Lolaplex/agents-sync"
LABEL description="agents-sync unified cloud vault server"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGENTS_SYNC_PORT=8765 \
    AGENTS_SYNC_HOST=0.0.0.0 \
    AGENTS_SYNC_TOKEN="" \
    AGENTS_SYNC_VAULT_DIR="/root/.agents-sync-vault"

WORKDIR /app

COPY pyproject.toml README.md AGENTS.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir -e ".[server]"

VOLUME ["/root/.agents-sync-vault"]

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request, os, sys; port=os.environ.get('AGENTS_SYNC_PORT','8765'); req=urllib.request.Request(f'http://127.0.0.1:{port}/health'); res=urllib.request.urlopen(req, timeout=3); sys.exit(0 if res.status==200 else 1)"

ENTRYPOINT ["sh", "-c", "exec agents-sync serve ${AGENTS_SYNC_PORT} --host ${AGENTS_SYNC_HOST} --token \"${AGENTS_SYNC_TOKEN}\""]
