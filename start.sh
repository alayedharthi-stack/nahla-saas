#!/bin/bash
# Nahla SaaS — entrypoint
# Table creation is handled by the FastAPI startup event in main.py
echo ">>> Starting Nahla backend on port ${PORT:-8000}..."
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
