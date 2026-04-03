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
    # Tenant billing columns added in later migration
    conn.execute(text(\"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS billing_provider VARCHAR\"))
    conn.execute(text(\"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS subscription_status VARCHAR\"))
    conn.execute(text(\"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMP\"))
    conn.execute(text(\"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMP\"))
    conn.execute(text(\"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan_name VARCHAR\"))
    conn.execute(text(\"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan_price FLOAT\"))
    conn.execute(text(\"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS max_messages_per_month INTEGER DEFAULT 1000\"))
    conn.execute(text(\"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS whatsapp_phone_id VARCHAR\"))
    conn.execute(text(\"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS whatsapp_token VARCHAR\"))
    conn.execute(text(\"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS salla_access_token VARCHAR\"))
    conn.execute(text(\"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS salla_store_id VARCHAR\"))
    conn.commit()
print('Tables ready.')
"

echo ">>> Starting backend server..."
exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}
