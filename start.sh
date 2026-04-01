#!/bin/bash
set -e

echo ">>> Creating database tables..."
cd /app && python -c "
import sys
sys.path.insert(0, '/app')
from database.session import engine
from database.models import Base
Base.metadata.create_all(engine)
print('Tables ready.')
"

echo ">>> Starting backend server..."
exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}
