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

# Chromium is ~400MB installed and most setups never open a page with it, so it
# is opt-in at build time. docker-compose passes this through from .env, which
# means `BROWSER_ENABLED=true` plus a rebuild is the whole switch — there is no
# second place to remember.
ARG BROWSER_ENABLED=false
RUN if [ "$BROWSER_ENABLED" = "true" ]; then \
      pip install "playwright>=1.47" && \
      playwright install --with-deps chromium; \
    fi

COPY server /app/server
COPY web /app/web

# /app/repo is where docker-compose mounts the checkout for self-improvement.
# Without it git refuses every operation there with "dubious ownership", since
# the directory belongs to the host user and this process runs as root.
RUN mkdir -p /data \
    && git config --global --add safe.directory /app \
    && git config --global --add safe.directory /app/repo

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "jarvis.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/app/server"]
