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
# Allow backend/ sub-packages to import from database/ and from each other.
_BACKEND_DIR  = os.path.dirname(os.path.abspath(__file__))
_DATABASE_DIR = os.path.abspath(os.path.join(_BACKEND_DIR, "..", "database"))
for _p in (_BACKEND_DIR, _DATABASE_DIR):
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
)

# ── App init ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Nahla SaaS Backend",
    description="Multi-tenant SaaS API server — WhatsApp AI sales automation.",
    version="2.0.0",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
from core.config import CORS_ORIGINS  # noqa: E402 (after app init)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Middleware stack ───────────────────────────────────────────────────────────
# Registration order matters: last registered = outermost = executes first.
# Stack (outermost → innermost):
#   jwt_enforcement → request_logging → global_rate_limit → api_key → multi_tenant
app.middleware("http")(multi_tenant_middleware)
app.middleware("http")(api_key_middleware)
app.middleware("http")(global_rate_limit_middleware)
app.middleware("http")(request_logging_middleware)
app.middleware("http")(jwt_enforcement_middleware)
app.middleware("http")(salla_iframe_middleware)



# ── Routers ───────────────────────────────────────────────────────────────────
# Previously extracted routers
from routers.health       import router as _health_router        # noqa: E402
from routers.admin        import router as _admin_router         # noqa: E402
from routers.auth         import router as _auth_router          # noqa: E402
from routers.settings     import router as _settings_router      # noqa: E402
from routers.templates    import router as _templates_router     # noqa: E402
from routers.campaigns    import router as _campaigns_router     # noqa: E402
from routers.automations  import router as _automations_router   # noqa: E402
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
from routers.whatsapp_connect  import router as _wa_connect_router       # noqa: E402
from routers.whatsapp_webhook  import router as _wa_webhook_router        # noqa: E402
from routers.store_sync        import router as _store_sync_router        # noqa: E402

app.include_router(_health_router)
app.include_router(_admin_router)
app.include_router(_auth_router)
app.include_router(_settings_router)
app.include_router(_templates_router)
app.include_router(_campaigns_router)
app.include_router(_automations_router)
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
app.include_router(_wa_webhook_router)
app.include_router(_store_sync_router)


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
            ]
            with engine.connect() as conn:
                for stmt in safe_alters:
                    try:
                        conn.execute(text(stmt))
                    except Exception:
                        pass
                conn.commit()

        # Run in thread pool to avoid blocking the event loop
        await asyncio.get_event_loop().run_in_executor(None, _run_migrations)
        logger.info("Database tables ready.")
    except Exception as exc:
        logger.warning("DB migration skipped (non-fatal): %s", exc)

    # 2. Background scheduler
    try:
        from core.scheduler import run_scheduler  # noqa: PLC0415
        asyncio.create_task(run_scheduler())
        logger.info("Background scheduler started.")
    except Exception as exc:
        logger.warning("Scheduler could not start: %s", exc)

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
