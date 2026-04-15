#!/usr/bin/env bash
set -e
# Schema bootstrap (cleanup + alembic) runs synchronously inside backend/main.py
# on_startup so it works even when the platform starts `uvicorn` directly.
cd /app
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
