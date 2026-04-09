#!/usr/bin/env bash
set -e

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo ">>> Running database migrations..."
cd "$REPO_ROOT/database"
alembic upgrade head

echo ">>> Starting Nahla backend on port ${PORT:-8000}..."
cd "$REPO_ROOT"
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
