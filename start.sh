#!/bin/bash

echo ">>> Nahla SaaS — startup"

# ── Database migration (max 30 seconds, non-fatal) ───────────────────────────────
echo ">>> Running DB migration (30s timeout)..."
timeout 30 python -c "
import sys, os
sys.path.insert(0, '/app')
sys.path.insert(0, '/app/database')

DATABASE_URL = os.environ.get('DATABASE_URL', '')
if not DATABASE_URL:
    print('>>> [WARN] DATABASE_URL not set — skipping migration')
    sys.exit(0)

from sqlalchemy import create_engine, text

# Short connect timeout so we fail fast instead of hanging
engine = create_engine(
    DATABASE_URL,
    connect_args={'connect_timeout': 8},
    pool_timeout=8,
)

try:
    from database.models import Base
    Base.metadata.create_all(engine)
    print('>>> Base tables created/verified')
except Exception as e:
    print(f'>>> [WARN] create_all: {e}')

safe_alters = [
    \"ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR\",
    \"ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR NOT NULL DEFAULT 'merchant'\",
    \"ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true\",
    \"ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP\",
    \"ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT false\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS billing_provider VARCHAR\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS subscription_status VARCHAR\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMP\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMP\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan_name VARCHAR\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan_price FLOAT\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS max_messages_per_month INTEGER DEFAULT 1000\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS whatsapp_phone_id VARCHAR\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS whatsapp_token VARCHAR\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS salla_access_token VARCHAR\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS salla_store_id VARCHAR\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_price_id VARCHAR\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS current_period_end TIMESTAMP\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS hyperpay_payment_id VARCHAR\",
    \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS billing_status VARCHAR\",
]
try:
    with engine.connect() as conn:
        for stmt in safe_alters:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass
        conn.commit()
    print('>>> Columns verified.')
except Exception as e:
    print(f'>>> [WARN] Alter columns: {e}')

print('>>> Migration done.')
" || echo ">>> [WARN] Migration timed out or errored — proceeding to start server."

# ── Start backend ────────────────────────────────────────────────────────────────
echo ">>> Starting uvicorn on port ${PORT:-8000}..."
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
