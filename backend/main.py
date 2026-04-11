"""
backend/main.py
───────────────
Nahla SaaS Backend — minimal entry point.

Responsibilities:
  • FastAPI app initialization
  • CORS configuration
  • Middleware registration
  • Router imports and mounting
  • Production startup guard
  • Lifespan / startup events

All business logic lives in routers/ and core/.
"""
import logging
import os
import sys

import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("nahla-backend")

# ── Path setup ────────────────────────────────────────────────────────────────
# Allow backend/ sub-packages to import from the repo root, database/ and each other.
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_BACKEND_DIR, ".."))
_DATABASE_DIR = os.path.join(_REPO_ROOT, "database")
for _p in (_REPO_ROOT, _BACKEND_DIR, _DATABASE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Config & middleware ────────────────────────────────────────────────────────
from core.config import ENVIRONMENT, IS_PRODUCTION  # noqa: E402
from core.middleware import (  # noqa: E402
    api_key_middleware,
    global_rate_limit_middleware,
    jwt_enforcement_middleware,
    multi_tenant_middleware,
    request_logging_middleware,
    salla_iframe_middleware,
    support_session_middleware,
)

# ── App init ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Nahla SaaS Backend",
    description="Multi-tenant SaaS API server — WhatsApp AI sales automation.",
    version="2.0.0",
)

# ── Middleware stack ───────────────────────────────────────────────────────────
# Registration order: LAST registered = OUTERMOST = first to process requests
# and LAST to process responses.
#
# Desired execution order (request direction →):
#   CORS → salla_iframe → jwt_enforcement → request_logging
#        → global_rate_limit → api_key → multi_tenant → Route handler
#
# To achieve CORS as outermost, register it LAST via add_middleware()
# (every add_middleware call wraps all previously registered middleware).
#
# Inner middleware (registered first → innermost):
app.middleware("http")(multi_tenant_middleware)
app.middleware("http")(api_key_middleware)
app.middleware("http")(global_rate_limit_middleware)
app.middleware("http")(request_logging_middleware)
# support_session_middleware runs AFTER jwt_enforcement so jwt_payload is already set.
# It rejects revoked support tokens and blocks sensitive paths.
app.middleware("http")(support_session_middleware)
app.middleware("http")(jwt_enforcement_middleware)
app.middleware("http")(salla_iframe_middleware)

# CORS must be outermost so it adds Access-Control-* headers to ALL responses,
# including 401 / 429 error responses returned by inner middleware.
# add_middleware() wraps everything above it → CORS becomes the outermost layer.
from core.config import CORS_ORIGINS  # noqa: E402
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Nahla-Error-Code"],
)



# ── Routers ───────────────────────────────────────────────────────────────────
# Previously extracted routers
from routers.health       import router as _health_router        # noqa: E402
from routers.admin        import router as _admin_router         # noqa: E402
from routers.auth         import router as _auth_router          # noqa: E402
from routers.settings     import router as _settings_router      # noqa: E402
from routers.templates    import router as _templates_router     # noqa: E402
from routers.campaigns    import router as _campaigns_router     # noqa: E402
from routers.automations  import router as _automations_router   # noqa: E402
from routers.analytics    import router as _analytics_router     # noqa: E402
from routers.conversations import router as _conversations_router # noqa: E402
from routers.coupons      import router as _coupons_router       # noqa: E402
from routers.orders       import router as _orders_router        # noqa: E402
from routers.intelligence import router as _intelligence_router  # noqa: E402

# Newly extracted routers
from routers.ai_sales          import router as _ai_sales_router         # noqa: E402
from routers.billing           import router as _billing_router          # noqa: E402
from routers.webhooks          import router as _webhooks_router         # noqa: E402
from routers.handoff           import router as _handoff_router          # noqa: E402
from routers.store_integration import router as _store_integration_router # noqa: E402
from routers.salla_oauth       import router as _salla_oauth_router      # noqa: E402
from routers.system            import router as _system_router           # noqa: E402
from routers.widget            import router as _widget_router           # noqa: E402
from routers.tracking          import router as _tracking_router         # noqa: E402
from routers.whatsapp_connect   import router as _wa_connect_router      # noqa: E402
from routers.whatsapp_embedded  import router as _wa_embedded_router     # noqa: E402
from routers.whatsapp_webhook  import router as _wa_webhook_router        # noqa: E402
from routers.store_sync        import router as _store_sync_router        # noqa: E402
from routers.zid_oauth         import router as _zid_oauth_router         # noqa: E402
from routers.integrations      import router as _integrations_router       # noqa: E402
from routers.support_access    import router as _support_access_router     # noqa: E402
from routers.addons            import router as _addons_router               # noqa: E402
from routers.widgets           import router as _widgets_router              # noqa: E402

app.include_router(_health_router)
app.include_router(_admin_router)
app.include_router(_auth_router)
app.include_router(_settings_router)
app.include_router(_templates_router)
app.include_router(_campaigns_router)
app.include_router(_automations_router)
app.include_router(_analytics_router)
app.include_router(_conversations_router)
app.include_router(_coupons_router)
app.include_router(_orders_router)
app.include_router(_intelligence_router)
app.include_router(_ai_sales_router)
app.include_router(_billing_router)
app.include_router(_webhooks_router)
app.include_router(_handoff_router)
app.include_router(_store_integration_router)
app.include_router(_salla_oauth_router)
app.include_router(_system_router)
app.include_router(_widget_router)
app.include_router(_tracking_router)
app.include_router(_wa_connect_router)
app.include_router(_wa_embedded_router)
app.include_router(_wa_webhook_router)
app.include_router(_store_sync_router)
app.include_router(_zid_oauth_router)
app.include_router(_integrations_router)
app.include_router(_support_access_router)
app.include_router(_addons_router)
app.include_router(_widgets_router)


# ── Startup events ────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup() -> None:
    """Run database migrations and start background scheduler."""
    # 1. DB table creation / column migrations (non-fatal)
    try:
        from database.session import engine  # noqa: PLC0415
        from database.models import Base     # noqa: PLC0415
        from sqlalchemy import text          # noqa: PLC0415

        def _run_migrations():
            Base.metadata.create_all(engine)
            safe_alters = [
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR NOT NULL DEFAULT 'merchant'",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT false",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS billing_provider VARCHAR",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS subscription_status VARCHAR",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMP",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMP",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan_name VARCHAR",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan_price FLOAT",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS max_messages_per_month INTEGER DEFAULT 1000",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS whatsapp_phone_id VARCHAR",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS whatsapp_token VARCHAR",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS salla_access_token VARCHAR",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS salla_store_id VARCHAR",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_price_id VARCHAR",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS current_period_end TIMESTAMP",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS hyperpay_payment_id VARCHAR",
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS billing_status VARCHAR",
                # ── whatsapp_usage (migration 0012 → 0013) ───────────────────
                "ALTER TABLE whatsapp_usage ADD COLUMN IF NOT EXISTS service_conversations_used INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE whatsapp_usage ADD COLUMN IF NOT EXISTS marketing_conversations_used INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE whatsapp_usage ADD COLUMN IF NOT EXISTS alert_80_sent BOOLEAN NOT NULL DEFAULT false",
                "ALTER TABLE whatsapp_usage ADD COLUMN IF NOT EXISTS alert_100_sent BOOLEAN NOT NULL DEFAULT false",
                "ALTER TABLE whatsapp_usage ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP",
                # old column: set default so INSERT without it doesn't violate NOT NULL
                "ALTER TABLE whatsapp_usage ALTER COLUMN conversations_used SET DEFAULT 0",
                "ALTER TABLE whatsapp_usage ALTER COLUMN conversations_used DROP NOT NULL",
                # ── merchant_addons (migration 0014) ──────────────────────────
                """CREATE TABLE IF NOT EXISTS merchant_addons (
                    id             SERIAL PRIMARY KEY,
                    tenant_id      INTEGER NOT NULL REFERENCES tenants(id),
                    addon_key      VARCHAR(64) NOT NULL,
                    is_enabled     BOOLEAN NOT NULL DEFAULT false,
                    settings_json  JSONB,
                    created_at     TIMESTAMP DEFAULT NOW(),
                    updated_at     TIMESTAMP DEFAULT NOW()
                )""",
                "CREATE INDEX IF NOT EXISTS ix_merchant_addons_tenant_id ON merchant_addons (tenant_id)",
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_merchant_addon_tenant_key') THEN ALTER TABLE merchant_addons ADD CONSTRAINT uq_merchant_addon_tenant_key UNIQUE (tenant_id, addon_key); END IF; END $$",
                # ── merchant_widgets (migration 0015) ─────────────────────────
                """CREATE TABLE IF NOT EXISTS merchant_widgets (
                    id             SERIAL PRIMARY KEY,
                    tenant_id      INTEGER NOT NULL REFERENCES tenants(id),
                    widget_key     VARCHAR(64) NOT NULL,
                    is_enabled     BOOLEAN NOT NULL DEFAULT false,
                    settings_json  JSONB,
                    display_rules  JSONB,
                    created_at     TIMESTAMP DEFAULT NOW(),
                    updated_at     TIMESTAMP DEFAULT NOW()
                )""",
                "CREATE INDEX IF NOT EXISTS ix_merchant_widgets_tenant_id ON merchant_widgets (tenant_id)",
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_merchant_widget_tenant_key') THEN ALTER TABLE merchant_widgets ADD CONSTRAINT uq_merchant_widget_tenant_key UNIQUE (tenant_id, widget_key); END IF; END $$",
                # ── whatsapp_connections (migration 0016) ─────────────────────
                "ALTER TABLE whatsapp_connections ADD COLUMN IF NOT EXISTS connection_type VARCHAR DEFAULT 'direct'",
                # Ensure phone_number_id is unique per non-null value (one phone = one tenant)
                """DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_indexes
                        WHERE tablename='whatsapp_connections'
                        AND indexname='uq_wa_conn_phone_number_id'
                    ) THEN
                        CREATE UNIQUE INDEX uq_wa_conn_phone_number_id
                        ON whatsapp_connections (phone_number_id)
                        WHERE phone_number_id IS NOT NULL;
                    END IF;
                END $$""",
                "ALTER TABLE coupons DROP CONSTRAINT IF EXISTS coupons_code_key",
                """DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname='uq_coupons_tenant_code'
                    ) THEN
                        ALTER TABLE coupons
                        ADD CONSTRAINT uq_coupons_tenant_code UNIQUE (tenant_id, code);
                    END IF;
                END $$""",
                "SELECT setval('tenants_id_seq', COALESCE((SELECT MAX(id) FROM tenants), 1), EXISTS (SELECT 1 FROM tenants))",
                "SELECT setval('users_id_seq', COALESCE((SELECT MAX(id) FROM users), 1), EXISTS (SELECT 1 FROM users))",
            ]
            for stmt in safe_alters:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(stmt))
                except Exception as exc:
                    logger.warning("Startup migration skipped statement: %s | error=%s", stmt[:120], exc)

        # Fire-and-forget: run migrations in background so startup doesn't block healthcheck
        async def _migrate_background():
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _run_migrations)
                logger.info("Database tables ready.")
            except Exception as exc:
                logger.warning("DB migration skipped (non-fatal): %s", exc)

        asyncio.create_task(_migrate_background())
        logger.info("Database migration task started in background.")
    except Exception as exc:
        logger.warning("DB migration skipped (non-fatal): %s", exc)

    # 2. Subscribe platform WABA to app (ensures webhooks are delivered)
    try:
        import httpx as _httpx  # noqa: PLC0415
        from core.config import WA_TOKEN, WA_BUSINESS_ACCOUNT_ID, META_GRAPH_API_VERSION  # noqa: PLC0415
        if WA_TOKEN and WA_BUSINESS_ACCOUNT_ID:
            async def _subscribe_platform_waba():
                url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{WA_BUSINESS_ACCOUNT_ID}/subscribed_apps"
                async with _httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(url, headers={"Authorization": f"Bearer {WA_TOKEN}"})
                logger.info("[Startup] platform WABA subscribed_apps: %s %s", resp.status_code, resp.text[:200])
            asyncio.create_task(_subscribe_platform_waba())
    except Exception as exc:
        logger.warning("[Startup] WABA subscription skipped: %s", exc)

    # 3. Background scheduler (billing/subscription checks)
    try:
        from core.scheduler import run_scheduler  # noqa: PLC0415
        asyncio.create_task(run_scheduler())
        logger.info("Background scheduler started.")
    except Exception as exc:
        logger.warning("Scheduler could not start: %s", exc)

    # 4. Hourly store sync scheduler
    try:
        from core.scheduler import run_store_sync_scheduler  # noqa: PLC0415
        asyncio.create_task(run_store_sync_scheduler())
        logger.info("Store sync scheduler started (hourly).")
    except Exception as exc:
        logger.warning("Store sync scheduler could not start: %s", exc)

# ── Production startup guard ───────────────────────────────────────────────────
# Fail fast if critical secrets are missing in production.
_REQUIRED_PROD_VARS = ("JWT_SECRET", "ADMIN_EMAIL", "ADMIN_PASSWORD")

if IS_PRODUCTION:
    _missing = [v for v in _REQUIRED_PROD_VARS if not os.environ.get(v)]
    if _missing:
        logger.warning(
            "SECURITY WARNING — required env vars not configured: %s\n"
            "Set them in Railway → Variables.",
            ", ".join(_missing),
        )
    if os.environ.get("ADMIN_PASSWORD") == "nahla-admin-2026":
        logger.warning(
            "SECURITY WARNING — default ADMIN_PASSWORD 'nahla-admin-2026' is in use. "
            "Change it in Railway → Variables."
        )
    if os.environ.get("JWT_SECRET", "").startswith("dev-"):
        logger.warning(
            "SECURITY WARNING — JWT_SECRET looks like a dev placeholder. "
            "Set a random 64-char secret in Railway → Variables."
        )
    logger.info("Production startup completed — check warnings above if any.")
else:
    logger.info("Running in %s mode", ENVIRONMENT)

# ── Dev entrypoint ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn  # noqa: PLC0415
    port = int(os.environ.get("PORT", 8000))
    logger.info("Starting Nahla SaaS Backend API on port %s …", port)
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
