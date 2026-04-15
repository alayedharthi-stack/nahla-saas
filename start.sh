#!/usr/bin/env bash
set -e

echo ">>> [startup] Cleaning up duplicate Salla integrations (safe --execute, idempotent)..."
cd /app
python scripts/cleanup_salla_duplicates.py --execute

echo ">>> [startup] Running database migrations..."
cd /app/database
alembic upgrade head

echo ">>> [startup] Starting Nahla backend on port ${PORT:-8000}..."
cd /app
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
