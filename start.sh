#!/bin/bash

echo ">>> Nahla SaaS — startup"

# ── Database migration (non-fatal: log errors but always start uvicorn) ─────────
echo ">>> Creating / migrating database tables..."
python -c "
import sys, os
sys.path.insert(0, '/app')
try:
    from database.session import engine
    from database.models import Base
    from sqlalchemy import text

    Base.metadata.create_all(engine)

    safe_alters = [
        # users
        \"ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR\",
        \"ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR NOT NULL DEFAULT 'merchant'\",
        \"ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true\",
        \"ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP\",
        \"ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT false\",
        # tenants — base
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
        # tenants — billing (migration 0010)
        \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR\",
        \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR\",
        \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_price_id VARCHAR\",
        \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS current_period_end TIMESTAMP\",
        \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS hyperpay_payment_id VARCHAR\",
        \"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS billing_status VARCHAR\",
    ]
    with engine.connect() as conn:
        for stmt in safe_alters:
            try:
                conn.execute(text(stmt))
            except Exception as e:
                print(f'  [WARN] alter skipped: {e}')
        conn.commit()
    print('>>> Tables ready.')
except Exception as e:
    print(f'>>> [ERROR] DB migration failed: {e}')
    print('>>> Continuing startup — app will retry DB connection at runtime.')
" || echo ">>> [WARN] Migration script exited with error — continuing anyway."

# ── Start backend ────────────────────────────────────────────────────────────────
echo ">>> Starting uvicorn on port ${PORT:-8000}..."
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}"
