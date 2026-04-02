#!/bin/bash
set -e

echo ">>> Creating database tables..."
cd /app && python -c "
import sys
sys.path.insert(0, '/app')
from database.session import engine
from database.models import Base
from sqlalchemy import text
Base.metadata.create_all(engine)
# Add new user columns to existing deployments (safe: IF NOT EXISTS)
with engine.connect() as conn:
    conn.execute(text(\"ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR\"))
    conn.execute(text(\"ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR NOT NULL DEFAULT 'merchant'\"))
    conn.execute(text(\"ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true\"))
    conn.execute(text(\"ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP\"))
    conn.execute(text(\"ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT false\"))
    conn.commit()
print('Tables ready.')
"

echo ">>> Starting backend server..."
exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}
