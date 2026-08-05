FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# git is needed at runtime: the autonomy engine branches and commits its own work.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/pyproject.toml /app/server/pyproject.toml
COPY server/jarvis/__init__.py /app/server/jarvis/__init__.py
RUN pip install --upgrade pip && pip install -e /app/server

COPY server /app/server
COPY web /app/web

RUN mkdir -p /data && git config --global --add safe.directory /app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "jarvis.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/app/server"]
