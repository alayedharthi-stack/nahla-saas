#!/usr/bin/env bash
set -e

# Repo is always at /app (set by WORKDIR in Dockerfile)
REPO_ROOT="/app"

echo ">>> Running database migrations..."
cd "$REPO_ROOT/database"
alembic upgrade head || echo "!!! Migration warning (continuing anyway): $?"

echo ">>> Starting Nahla backend on port ${PORT:-8000}..."
cd "$REPO_ROOT"
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
