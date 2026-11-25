FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /usr/local/bin/uv

RUN groupadd --system ignitionmcp \
    && useradd --system --gid ignitionmcp --create-home ignitionmcp

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable \
    && mkdir -p /artifacts \
    && chown -R ignitionmcp:ignitionmcp /app /artifacts

USER ignitionmcp

ENV IGNITION_TRANSPORT=http \
    IGNITION_HOST=0.0.0.0 \
    IGNITION_PORT=8080 \
    IGNITION_READONLY=true \
    IGNITION_SCAN_NETWORK=false

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, socket; s = socket.create_connection(('127.0.0.1', int(os.environ.get('IGNITION_PORT', '8080'))), timeout=3); s.close()"

ENTRYPOINT ["/app/.venv/bin/ignition-mcp"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8080"]
