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
    owner_merchant_scope_middleware,
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


# ── Ultra-light liveness probe ────────────────────────────────────────────────
# Registered BEFORE every middleware/router so it lives outside the CORS,
# rate-limit, JWT-enforcement, and request_logging layers. /alive does:
#
#   * NO database query           — cannot block on a stalled connection
#   * NO middleware ingestion     — proves the event loop and Starlette
#                                    request-cycle are still alive even
#                                    when one of our middlewares hangs
#   * NO async dependencies       — pure dict return
#
# Used by Railway's healthcheck and by ops when /healthz, /auth/ping or
# the rest of the API stop responding. If /alive itself stops responding
# the worker is genuinely frozen (event-loop deadlock, OS-level hang) —
# at which point the only correct response is a process restart.
@app.get("/alive", include_in_schema=False)
async def _alive() -> dict:
    import time as _t  # noqa: PLC0415
    return {"ok": True, "ts": _t.time()}


@app.get("/healthz", include_in_schema=False)
async def _healthz() -> dict:
    """Alias of /alive for upstream proxies that expect /healthz."""
    import time as _t  # noqa: PLC0415
    return {"ok": True, "ts": _t.time()}


# ── Fallback CORS preflight handler ──────────────────────────────────────────
# Belt-and-suspenders: CORSMiddleware (registered as the OUTERMOST layer
# below) handles every well-formed OPTIONS preflight on its own. This
# catch-all fires only when (a) CORSMiddleware decides not to intercept
# (e.g. the Origin is missing / mismatched) or (b) some operator has
# accidentally torn it out of the stack. We mirror the headers
# CORSMiddleware would have emitted, manually validating the Origin
# against CORS_ORIGINS / CORS_ORIGIN_REGEX so we never reflect an
# untrusted origin. Returns 204 No Content with the standard CORS
# preflight header set.
from fastapi import Request as _CorsReq  # noqa: E402
import re as _cors_re  # noqa: E402


@app.options("/{full_path:path}", include_in_schema=False)
async def _cors_preflight_fallback(full_path: str, _req: _CorsReq):
    from core.config import CORS_ORIGINS as _co, CORS_ORIGIN_REGEX as _cr  # noqa: E402, PLC0415
    from fastapi.responses import Response as _Resp  # noqa: PLC0415
    origin = _req.headers.get("origin", "")
    acr_method  = _req.headers.get("access-control-request-method",  "")
    acr_headers = _req.headers.get("access-control-request-headers", "")
    logger.info(
        "[CORS] FALLBACK OPTIONS /%s origin=%s acr_method=%s acr_headers=%s",
        full_path, origin, acr_method, acr_headers,
    )

    allowed_origin: str = ""
    if origin:
        if origin in _co or "*" in _co:
            allowed_origin = origin
        elif _cr:
            try:
                if _cors_re.fullmatch(_cr, origin):
                    allowed_origin = origin
            except Exception:
                pass

    headers: dict = {}
    if allowed_origin:
        headers["Access-Control-Allow-Origin"]      = allowed_origin
        headers["Access-Control-Allow-Credentials"] = "true"
        headers["Access-Control-Allow-Methods"]     = (
            acr_method or "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        )
        headers["Access-Control-Allow-Headers"]     = (
            acr_headers or "authorization, content-type, x-nahla-key, x-tenant-id"
        )
        headers["Access-Control-Max-Age"]           = "86400"
        headers["Vary"]                             = "Origin"
    else:
        # Untrusted origin — reply 204 with NO CORS headers so the
        # browser blocks the call. Same behaviour CORSMiddleware would
        # produce for a non-allowlisted origin.
        logger.warning(
            "[CORS] preflight from untrusted origin=%s path=/%s — replying without CORS headers",
            origin, full_path,
        )

    return _Resp(status_code=204, headers=headers)


# ── Global exception handler ──────────────────────────────────────────────────
# When an unhandled exception reaches ServerErrorMiddleware (the outermost layer),
# it sends the error response using the raw ASGI send — OUTSIDE the CORSMiddleware
# layer. This means the browser sees a 500 without Access-Control-Allow-Origin and
# reports it as a CORS error instead of the real problem.
#
# Fix: @app.exception_handler(Exception) is given to ServerErrorMiddleware, so
# CORSMiddleware never touches its response. We must manually embed CORS headers
# directly into the JSONResponse object so they survive the bypass.
from fastapi import Request as _Request  # noqa: E402
from fastapi.responses import JSONResponse as _JSONResponse  # noqa: E402
import re as _re  # noqa: E402


@app.exception_handler(Exception)
async def _global_exception_handler(_req: _Request, exc: Exception) -> _JSONResponse:
    """
    Last-resort response builder for any exception that escapes the
    middleware chain (or the route itself).

    Two specific cases are handled deliberately here:

    1. ``RuntimeError("No response returned.")`` — raised by
       Starlette's BaseHTTPMiddleware when an inner ASGI task ends
       without sending a response. Most often triggered by a client
       disconnect mid-request (``asyncio.CancelledError`` in a
       middleware that doesn't catch it). We log this loudly so the
       operator can spot the pattern, then return a soft response.

    2. Webhook paths (``/webhook/*``) — providers like 360dialog and
       Meta interpret any non-2xx as a delivery failure and retry,
       which compounds load on the worker that just failed. We
       return HTTP 200 with ``ok=false`` so retries do NOT happen,
       while keeping the failure visible in the logs and audit
       trail. Non-webhook paths still get the canonical 500.
    """
    path = _req.url.path or ""
    is_no_response = isinstance(exc, RuntimeError) and "No response returned" in str(exc)
    is_webhook = path.startswith("/webhook/")

    if is_no_response:
        logger.error(
            "[GlobalExceptionHandler] 'No response returned' on %s "
            "(BaseHTTPMiddleware end-of-chain w/o response, usually a "
            "client disconnect). Returning safe response.",
            path,
            exc_info=True,
        )
    else:
        logger.error(
            "[GlobalExceptionHandler] Unhandled exception on %s: %s",
            path, exc, exc_info=True,
        )

    from core.config import CORS_ORIGINS as _co, CORS_ORIGIN_REGEX as _cr  # noqa: E402
    origin = _req.headers.get("origin", "")
    cors_headers: dict = {}
    if origin and (
        origin in _co
        or "*" in _co
        or (_cr and _re.fullmatch(_cr, origin))
    ):
        cors_headers = {
            "Access-Control-Allow-Origin":      origin,
            "Access-Control-Allow-Credentials": "true",
        }

    if is_webhook:
        # Always 200 for webhooks — provider must NOT retry on what is
        # almost always a transient cancellation or a payload our BG
        # task already accepted. The actual error is in the log line
        # above.
        return _JSONResponse(
            status_code=200,
            content={"ok": False, "error": "webhook_processing_error"},
            headers=cors_headers,
        )

    return _JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "code": "internal_error"},
        headers=cors_headers,
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
# owner_merchant_scope_middleware runs AFTER jwt_enforcement, BEFORE the route
# handler. It is the framework-level guard that refuses platform-admin tokens
# on merchant-scoped endpoints — defense in depth on top of any per-endpoint
# require_merchant_scope dependency.
app.middleware("http")(owner_merchant_scope_middleware)
app.middleware("http")(jwt_enforcement_middleware)
app.middleware("http")(salla_iframe_middleware)

# CORS must be outermost so it adds Access-Control-* headers to ALL responses,
# including 401 / 429 error responses returned by inner middleware.
# add_middleware() wraps everything above it → CORS becomes the outermost layer.
from core.config import CORS_ORIGINS, CORS_ORIGIN_REGEX  # noqa: E402
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Nahla-Error-Code", "X-Nahla-Error-Type"],
)



# ── Routers ───────────────────────────────────────────────────────────────────
# Previously extracted routers
from routers.health       import router as _health_router        # noqa: E402
from routers.admin        import router as _admin_router         # noqa: E402
from routers.admin_debug  import router as _admin_debug_router   # noqa: E402
from routers.admin_salla_token import router as _admin_salla_token_router  # noqa: E402
from routers.auth         import router as _auth_router          # noqa: E402
from routers.settings     import router as _settings_router      # noqa: E402
from routers.templates    import router as _templates_router     # noqa: E402
from routers.campaigns    import router as _campaigns_router     # noqa: E402
from routers.campaign_wizard import router as _campaign_wizard_router  # noqa: E402
from routers.automations  import router as _automations_router   # noqa: E402
from routers.analytics    import router as _analytics_router     # noqa: E402
from routers.conversations import router as _conversations_router # noqa: E402
from routers.coupons      import router as _coupons_router       # noqa: E402
from routers.promotions   import router as _promotions_router    # noqa: E402
from routers.offer_decisions import router as _offer_decisions_router  # noqa: E402
from routers.orders       import router as _orders_router        # noqa: E402
from routers.intelligence import router as _intelligence_router  # noqa: E402
from routers.customers    import router as _customers_router     # noqa: E402
from routers.customer_import import router as _customer_import_router  # noqa: E402

# Newly extracted routers
from routers.ai_sales          import router as _ai_sales_router         # noqa: E402
from routers.billing           import router as _billing_router          # noqa: E402
from routers.webhooks          import router as _webhooks_router         # noqa: E402
from routers.handoff           import router as _handoff_router          # noqa: E402
from routers.store_integration import router as _store_integration_router # noqa: E402
from routers.salla_oauth         import router as _salla_oauth_router        # noqa: E402
from routers.salla_app_settings  import router as _salla_app_settings_router  # noqa: E402
from routers.salla_subscription  import router as _salla_subscription_router   # noqa: E402
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
from routers.notification_logs import router as _notification_logs_router  # noqa: E402
from routers.addons            import router as _addons_router               # noqa: E402
from routers.widgets           import router as _widgets_router              # noqa: E402
from routers.product_interests import router as _product_interests_router    # noqa: E402

# TEMPORARY: token-gated public debug router. Safe to delete once the
# abandoned-cart investigation is closed. See routers/debug_public.py.
from routers.debug_public      import router as _debug_public_router       # noqa: E402

app.include_router(_health_router)
app.include_router(_debug_public_router)
app.include_router(_admin_router)
app.include_router(_admin_debug_router)
app.include_router(_admin_salla_token_router)
app.include_router(_auth_router)
app.include_router(_settings_router)
app.include_router(_templates_router)
app.include_router(_campaigns_router)
app.include_router(_campaign_wizard_router)
app.include_router(_automations_router)
app.include_router(_analytics_router)
app.include_router(_conversations_router)
app.include_router(_coupons_router)
app.include_router(_promotions_router)
app.include_router(_offer_decisions_router)
app.include_router(_orders_router)
app.include_router(_intelligence_router)
app.include_router(_customers_router)
app.include_router(_customer_import_router)
app.include_router(_ai_sales_router)
app.include_router(_billing_router)
app.include_router(_webhooks_router)
app.include_router(_handoff_router)
app.include_router(_store_integration_router)
app.include_router(_salla_oauth_router)
app.include_router(_salla_app_settings_router)
app.include_router(_salla_subscription_router)
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
app.include_router(_notification_logs_router)
app.include_router(_addons_router)
app.include_router(_widgets_router)
app.include_router(_product_interests_router)


# ── Startup events ────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup() -> None:
    """Run database migrations and start background scheduler."""
    # ── Deployment fingerprint ────────────────────────────────────────────
    # Emit the git commit SHA we are running on. Lets us prove from
    # Railway logs whether a hot-fix actually shipped or if the platform
    # is still serving an older build.
    try:
        import subprocess as _subp
        _commit_sha = (
            os.environ.get("RAILWAY_GIT_COMMIT_SHA")
            or os.environ.get("GIT_COMMIT_SHA")
            or os.environ.get("COMMIT_SHA")
            or _subp.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=_REPO_ROOT,
                stderr=_subp.DEVNULL,
                timeout=5,
            ).decode("utf-8").strip()
        )
    except Exception:
        _commit_sha = "unknown"
    _git_branch = (
        os.environ.get("RAILWAY_GIT_BRANCH")
        or os.environ.get("GIT_BRANCH")
        or "unknown"
    )
    logger.warning(
        "[BOOT] git_commit=%s branch=%s build=%s",
        _commit_sha,
        _git_branch,
        os.environ.get("RAILWAY_DEPLOYMENT_ID", "local"),
    )
    # 0. Blocking bootstrap — MUST run before any code issues SQL that references
    #    new columns (e.g. integrations.external_store_id).  Railway may start
    #    `uvicorn …` directly without start.sh; background safe_alters are too late.
    _skip = os.environ.get("NAHLA_SKIP_DB_BOOTSTRAP", "").lower() in ("1", "true", "yes")
    if _skip:
        logger.info("NAHLA_SKIP_DB_BOOTSTRAP set — skipping cleanup + Alembic bootstrap.")
    else:
        try:

            def _bootstrap_db_schema() -> None:
                import subprocess
                from sqlalchemy import create_engine, text as _text

                # ── Step A: Salla duplicate cleanup (must run before 0017) ──────────────
                cleanup = os.path.join(_REPO_ROOT, "scripts", "cleanup_salla_duplicates.py")
                r1 = subprocess.run(
                    [sys.executable, cleanup, "--execute"],
                    cwd=_REPO_ROOT,
                    check=False,
                    env=os.environ.copy(),
                )
                if r1.returncode != 0:
                    logger.warning(
                        "cleanup_salla_duplicates.py exited %d — continuing to Alembic; "
                        "migration 0017 will fail loudly if duplicates remain.",
                        r1.returncode,
                    )

                # ── Step B: Stamp Alembic to 0016 if tables exist but alembic_version
                #    doesn't.  The DB was previously managed by Base.metadata.create_all();
                #    without this stamp, 'alembic upgrade head' tries to run 0001 which
                #    immediately fails with "relation tenants already exists".
                _db_url = os.environ.get("DATABASE_URL", "")
                if _db_url:
                    try:
                        _eng = create_engine(_db_url)
                        with _eng.connect() as _conn:
                            has_alembic = _conn.execute(_text(
                                "SELECT 1 FROM information_schema.tables "
                                "WHERE table_schema='public' AND table_name='alembic_version'"
                            )).scalar()
                            has_tenants = _conn.execute(_text(
                                "SELECT 1 FROM information_schema.tables "
                                "WHERE table_schema='public' AND table_name='tenants'"
                            )).scalar()
                        _eng.dispose()

                        if has_tenants and not has_alembic:
                            logger.warning(
                                "alembic_version table missing but 'tenants' exists — "
                                "DB was built by create_all().  Stamping to revision 0016 "
                                "so that only new migrations (0017+) are applied."
                            )
                            subprocess.run(
                                [sys.executable, "-m", "alembic", "stamp", "0016"],
                                cwd=_DATABASE_DIR,
                                check=True,
                                env=os.environ.copy(),
                            )
                    except Exception as _stamp_exc:
                        logger.warning("Alembic stamp pre-check failed (non-fatal): %s", _stamp_exc)

                # ── Step C: Apply any pending migrations (0017, 0018, …) ───────────────
                # NOTE: capture stdout+stderr and surface them via the Python logger
                # before re-raising. Previously we relied on `check=True` alone which
                # crashed with `CalledProcessError` but left the actual Alembic stack
                # trace (e.g. "Multiple head revisions present", "DuplicateColumn",
                # "relation X already exists") only on Railway's raw stdout — invisible
                # in the dashboard once `Application startup failed` was the last line
                # the UI cropped to. Logging the captured output guarantees the real
                # cause shows up in Railway's structured logs every time.
                _alembic = subprocess.run(
                    [sys.executable, "-m", "alembic", "upgrade", "head"],
                    cwd=_DATABASE_DIR,
                    check=False,
                    env=os.environ.copy(),
                    capture_output=True,
                    text=True,
                )
                if _alembic.stdout:
                    logger.info("[alembic upgrade head] stdout:\n%s", _alembic.stdout.strip())
                if _alembic.returncode != 0:
                    logger.error(
                        "[alembic upgrade head] FAILED rc=%d\n--- stderr ---\n%s\n--- stdout ---\n%s",
                        _alembic.returncode,
                        (_alembic.stderr or "").strip(),
                        (_alembic.stdout or "").strip(),
                    )
                    raise RuntimeError(
                        f"alembic upgrade head failed (rc={_alembic.returncode}); "
                        "see logged stderr above"
                    )

            await asyncio.get_running_loop().run_in_executor(None, _bootstrap_db_schema)
            logger.info("Database bootstrap (Salla cleanup + Alembic) completed.")
        except Exception as exc:
            logger.exception("Database bootstrap failed — refusing to start: %s", exc)
            raise

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
                # ── whatsapp_connections (migration 0016+) ────────────────────
                "ALTER TABLE whatsapp_connections ADD COLUMN IF NOT EXISTS connection_type VARCHAR DEFAULT 'direct'",
                "ALTER TABLE whatsapp_connections ADD COLUMN IF NOT EXISTS provider VARCHAR DEFAULT 'meta'",
                "UPDATE whatsapp_connections SET provider='meta' WHERE provider IS NULL OR provider=''",
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
                # ── Salla: one active binding per store_id (migration 0020) ────
                """DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_indexes
                        WHERE indexname = 'uq_salla_active_store'
                    ) THEN
                        CREATE UNIQUE INDEX uq_salla_active_store
                        ON integrations ((config->>'store_id'))
                        WHERE provider = 'salla'
                          AND enabled = true
                          AND config->>'store_id' IS NOT NULL;
                    END IF;
                END $$""",
                # ── Tenant Integrity (migration 0022) ──────────────────────────
                """DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_indexes
                        WHERE tablename='whatsapp_connections'
                        AND indexname='uq_wa_conn_waba_id'
                    ) THEN
                        CREATE UNIQUE INDEX uq_wa_conn_waba_id
                        ON whatsapp_connections (whatsapp_business_account_id)
                        WHERE whatsapp_business_account_id IS NOT NULL;
                    END IF;
                END $$""",
                """CREATE TABLE IF NOT EXISTS integrity_events (
                    id              SERIAL PRIMARY KEY,
                    event           VARCHAR NOT NULL,
                    tenant_id       INTEGER,
                    other_tenant_id INTEGER,
                    phone_number_id VARCHAR,
                    waba_id         VARCHAR,
                    store_id        VARCHAR,
                    provider        VARCHAR,
                    action          VARCHAR,
                    result          VARCHAR,
                    detail          TEXT,
                    actor           VARCHAR,
                    dry_run         BOOLEAN,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""",
                "CREATE INDEX IF NOT EXISTS ix_integrity_events_event ON integrity_events (event)",
                "CREATE INDEX IF NOT EXISTS ix_integrity_events_tenant_id ON integrity_events (tenant_id)",
                "CREATE INDEX IF NOT EXISTS ix_integrity_events_created_at ON integrity_events (created_at)",
                # ── Webhook Guardian (migration 0021) ─────────────────────────
                "ALTER TABLE whatsapp_connections ADD COLUMN IF NOT EXISTS last_webhook_received_at TIMESTAMPTZ",
                """CREATE TABLE IF NOT EXISTS webhook_guardian_log (
                    id              SERIAL PRIMARY KEY,
                    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    phone_number_id VARCHAR,
                    waba_id         VARCHAR,
                    event           VARCHAR NOT NULL,
                    success         BOOLEAN NOT NULL DEFAULT true,
                    detail          TEXT,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )""",
                "CREATE INDEX IF NOT EXISTS ix_webhook_guardian_log_tenant_created ON webhook_guardian_log (tenant_id, created_at)",
                "CREATE INDEX IF NOT EXISTS ix_webhook_guardian_log_event ON webhook_guardian_log (event)",
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

    # 2. Subscribe platform phone number to app (ensures webhooks are delivered).
    #    Per Meta Cloud API docs the subscription must target the
    #    PHONE_NUMBER_ID, not the WABA_ID. Falls back to WABA only if no
    #    PHONE_NUMBER_ID is configured (legacy installs).
    try:
        import httpx as _httpx  # noqa: PLC0415
        from core.config import (  # noqa: PLC0415
            WA_TOKEN,
            WA_PHONE_ID,
            WA_BUSINESS_ACCOUNT_ID,
            META_GRAPH_API_VERSION,
        )
        target_id   = WA_PHONE_ID or WA_BUSINESS_ACCOUNT_ID
        target_kind = "phone" if WA_PHONE_ID else "waba"
        if WA_TOKEN and target_id:
            async def _subscribe_platform_phone():
                url = (
                    f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
                    f"/{target_id}/subscribed_apps"
                )
                async with _httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {WA_TOKEN}"},
                        json={"subscribed_fields": ["messages", "messaging_postbacks", "message_echoes"]},
                    )
                logger.info(
                    "[Startup] platform %s subscribed_apps id=%s status=%s body=%s",
                    target_kind, target_id, resp.status_code, resp.text[:200],
                )
            asyncio.create_task(_subscribe_platform_phone())
    except Exception as exc:
        logger.warning("[Startup] webhook subscription skipped: %s", exc)

    # ── Runtime heartbeat (always on) ───────────────────────────────────────
    # Emits a structured INFO line every 10s with event-loop lag, in-flight
    # background tasks, active HTTP requests, thread count and RSS. If the
    # heartbeats stop, the event loop is frozen — at which point we know
    # exactly *when* the freeze happened.
    try:
        from core.runtime_perf import run_runtime_heartbeat  # noqa: PLC0415
        asyncio.create_task(run_runtime_heartbeat())
        logger.info("[RuntimeHeartbeat] task scheduled.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RuntimeHeartbeat] could not start: %s", exc)

    # ── Emergency kill-switch ───────────────────────────────────────────────
    # When NAHLA_DISABLE_SCHEDULERS=1 we skip every scheduler launch below,
    # leaving only the heartbeat + healthcheck. This is the surgical
    # isolation switch ops uses when the worker is saturated to prove
    # whether the load comes from the schedulers or from inbound HTTP.
    _skip_schedulers = (
        os.environ.get("NAHLA_DISABLE_SCHEDULERS", "").strip().lower() in ("1", "true", "yes")
    )
    if _skip_schedulers:
        logger.warning(
            "[Startup] NAHLA_DISABLE_SCHEDULERS=1 — every scheduler is "
            "DISABLED for this run. Only /alive, /healthz, /auth, and "
            "the runtime heartbeat are guaranteed to work. Unset the env "
            "var and redeploy to restore normal behaviour."
        )
        return

    # ── Staggered scheduler startup ─────────────────────────────────────────
    # Previously every scheduler was created via asyncio.create_task() at
    # the same instant, so the first 5–10 s of process life was burnt
    # importing heavy ORM modules and opening 10+ DB sessions in parallel.
    # That latency sat directly on top of /healthz and the very first
    # /auth/login response. We now register each scheduler through
    # core.runtime_perf.schedule_with_delay(...) so the wall clock is
    # spread out: hot path schedulers (webhook dispatcher, orders poller)
    # come up first, latency-tolerant ones (token refresh, coupon pool,
    # template sync, daily report) sit at the back of the queue. Each
    # registration is wrapped in its own try/except so an import error
    # in one scheduler can never take the rest of them down.
    from core.runtime_perf import schedule_with_delay  # noqa: PLC0415

    def _start(name: str, factory, delay_s: float) -> None:
        try:
            schedule_with_delay(factory, name=name, delay_seconds=delay_s)
            logger.info("[Scheduler] %s queued (delay=%.0fs).", name, delay_s)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Scheduler] %s could not start: %s", name, exc)

    # Tier 1 (≤ 5s) — critical, time-sensitive paths
    def _f_dispatcher():
        from core.webhook_dispatcher import run_dispatcher_loop  # noqa: PLC0415
        return run_dispatcher_loop()
    _start("webhook_dispatcher", _f_dispatcher, 2)

    def _f_salla_poller():
        from services.salla_orders_poller import run_salla_orders_poller_scheduler  # noqa: PLC0415
        return run_salla_orders_poller_scheduler()
    _start("salla_orders_poller", _f_salla_poller, 5)

    # Tier 2 (≤ 15s) — periodic business loops
    def _f_scheduler():
        from core.scheduler import run_scheduler  # noqa: PLC0415
        return run_scheduler()
    _start("billing_scheduler", _f_scheduler, 8)

    def _f_automation_engine():
        from core.scheduler import run_automation_engine_scheduler  # noqa: PLC0415
        return run_automation_engine_scheduler()
    _start("automation_engine", _f_automation_engine, 10)

    def _f_campaign_dispatcher():
        from core.scheduler import run_campaign_dispatcher_scheduler  # noqa: PLC0415
        return run_campaign_dispatcher_scheduler()
    _start("campaign_dispatcher", _f_campaign_dispatcher, 12)

    def _f_abandoned_cart():
        from core.abandoned_cart_scheduler import run_abandoned_cart_scheduler  # noqa: PLC0415
        return run_abandoned_cart_scheduler()
    _start("abandoned_cart", _f_abandoned_cart, 15)

    # Tier 3 (≤ 30s) — periodic syncs / lower-cadence loops
    def _f_store_sync():
        from core.scheduler import run_store_sync_scheduler  # noqa: PLC0415
        return run_store_sync_scheduler()
    _start("store_sync", _f_store_sync, 20)

    def _f_emitters():
        from core.scheduler import run_automation_emitters_scheduler  # noqa: PLC0415
        return run_automation_emitters_scheduler()
    _start("automation_emitters", _f_emitters, 25)

    def _f_webhook_guardian():
        from core.scheduler import run_webhook_guardian_scheduler  # noqa: PLC0415
        return run_webhook_guardian_scheduler()
    _start("webhook_guardian", _f_webhook_guardian, 30)

    # Tier 4 (≤ 60s) — slow housekeeping
    def _f_template_sync():
        from core.scheduler import run_template_sync_scheduler  # noqa: PLC0415
        return run_template_sync_scheduler()
    _start("template_sync", _f_template_sync, 40)

    def _f_wa_refresh():
        from core.scheduler import run_wa_token_refresh_scheduler  # noqa: PLC0415
        return run_wa_token_refresh_scheduler()
    _start("wa_token_refresh", _f_wa_refresh, 45)

    def _f_salla_refresh():
        from core.scheduler import run_salla_token_refresh_scheduler  # noqa: PLC0415
        return run_salla_token_refresh_scheduler()
    _start("salla_token_refresh", _f_salla_refresh, 50)

    def _f_coupon_pool():
        from core.scheduler import run_coupon_generator_scheduler  # noqa: PLC0415
        return run_coupon_generator_scheduler()
    _start("coupon_pool", _f_coupon_pool, 55)

    def _f_daily_report():
        from core.scheduler import run_daily_report_scheduler  # noqa: PLC0415
        return run_daily_report_scheduler()
    _start("daily_report", _f_daily_report, 60)

    def _f_startup_health():
        from core.webhook_guardian import run_startup_webhook_health_check  # noqa: PLC0415
        return run_startup_webhook_health_check()
    _start("startup_webhook_health", _f_startup_health, 35)

    # 10. Post-deploy tenant integrity scan — detects cross-tenant conflicts
    async def _run_integrity_check():
        await asyncio.sleep(90)  # let DB migrations and WA startup checks settle first
        try:
            from core.database import SessionLocal as _SL  # noqa: PLC0415
            from core.tenant_integrity import run_post_deploy_check  # noqa: PLC0415
            _db = _SL()
            try:
                result = run_post_deploy_check(_db)
                logger.info("[Startup] Tenant integrity check complete: %s", result.get("summary", {}))
            finally:
                _db.close()
        except Exception as _exc:
            logger.warning("[Startup] Tenant integrity check error: %s", _exc)

    try:
        asyncio.create_task(_run_integrity_check())
        logger.info("Post-deploy tenant integrity check scheduled.")
    except Exception as exc:
        logger.warning("Tenant integrity check could not start: %s", exc)

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
