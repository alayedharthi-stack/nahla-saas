#!/bin/bash
# Nahla SaaS — entrypoint
echo ">>> Starting Nahla backend on port ${PORT:-8000}..."
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
