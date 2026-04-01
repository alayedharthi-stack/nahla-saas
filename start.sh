#!/bin/bash
set -e

echo ">>> Running database migrations..."
cd /app && alembic -c database/alembic.ini upgrade head

echo ">>> Starting backend server..."
exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}
