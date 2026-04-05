#!/bin/bash
# Nahla SaaS — entrypoint with startup diagnostics

echo ">>> Python: $(python --version)"
echo ">>> Working dir: $(pwd)"
echo ">>> PORT: ${PORT:-8000}"

# ── Startup import diagnostic ────────────────────────────────────────────────────
echo ">>> Testing backend.main import..."
python -c "
import sys, traceback
sys.path.insert(0, '/app/backend')
sys.path.insert(0, '/app/database')

try:
    import importlib
    mod = importlib.import_module('backend.main')
    print('>>> Import OK - app object:', getattr(mod, 'app', 'NOT FOUND'))
except Exception:
    print('>>> IMPORT FAILED:')
    traceback.print_exc()
    sys.exit(1)
"
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo ">>> FATAL: backend.main import failed. Check traceback above."
    exit 1
fi

echo ">>> Starting uvicorn on port ${PORT:-8000}..."
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
