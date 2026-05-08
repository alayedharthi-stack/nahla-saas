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

# Railway / edge diagnostics: stdout is always visible in platform logs.
print("[BOOT/net] PORT=", os.getenv("PORT"), flush=True)
logger.warning(
    "[BOOT/net] PORT=%s — uvicorn must bind host=0.0.0.0 port=$PORT (see start.sh)",
    os.getenv("PORT"),
)

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

# ── Pure-ASGI fast path ────────────────────────────────────────────────────
# Sits OUTSIDE every BaseHTTPMiddleware layer above. For /alive, /healthz
# and /auth/ping it synthesises the response directly with ASGI ``send``
# events, bypassing every layer that could be blocked by:
#   * a congested BaseHTTPMiddleware chain (the chain that produces
#     "RuntimeError: No response returned" on client disconnect)
#   * a JWT decode / DB session / rate-limit Redis lookup
#   * a stuck downstream route handler
# This is the ONE guarantee Railway and the dashboard depend on: liveness
# and the connectivity probe ALWAYS answer in <5 ms regardless of what
# the rest of the worker is doing.
from core.fast_path_middleware import FastPathMiddleware, DEFAULT_FAST_PATHS  # noqa: E402
app.add_middleware(FastPathMiddleware, fast_paths=DEFAULT_FAST_PATHS)

# CORS must be outermost so it adds Access-Control-* headers to ALL responses,
# including the FastPath responses above and the 401 / 429 error responses
# returned by inner middleware. add_middleware() wraps everything above it
# → the LAST add_middleware call becomes the outermost layer.
from core.config import CORS_ORIGINS, CORS_ORIGIN_REGEX  # noqa: E402
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Nahla-Error-Code", "X-Nahla-Error-Type", "X-Fast-Path"],
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
    """
    Lifespan startup — MUST return as fast as possible.

    Every line in this function runs *before* uvicorn starts dispatching
    HTTP requests. If anything here blocks (sync I/O, slow import, DB
    connect, subprocess), the entire worker is invisible to the network
    even though TCP is bound. That is exactly the
    ``api.nahlah.ai/alive returns 0 bytes`` symptom.

    Hard rules for this function:

    1. NO synchronous subprocess calls.
    2. NO DB connections.
    3. NO heavy imports beyond what is already imported at module load.
    4. EVERY phase emits a ``[BOOT/lifespan]`` log line before AND after
       so the operator can pinpoint the wedged step from Railway logs.
    5. The function MUST reach ``[BOOT/lifespan] complete`` within a
       few hundred milliseconds. Anything slower belongs in a background
       task scheduled via ``asyncio.create_task``.
    """
    import time as _bt
    _t_lifespan = _bt.monotonic()
    logger.warning("[BOOT/lifespan] begin — preparing background tasks")

    # ── Deployment fingerprint (env-only, no subprocess) ──────────────────
    # We deliberately AVOID `git rev-parse HEAD` here: subprocess.check_output
    # is synchronous and on a misbehaving container can stall the event loop
    # for the full timeout (or longer if git itself is wedged on Railway's
    # ephemeral filesystem). The deployment env vars are populated by Railway
    # for every build, so we use them and only fall back to "unknown".
    _commit_sha = (
        os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or os.environ.get("GIT_COMMIT_SHA")
        or os.environ.get("COMMIT_SHA")
        or "unknown"
    )
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
    logger.info("[BOOT/lifespan] phase=0_bootstrap_dispatch t+%.3fs", _bt.monotonic() - _t_lifespan)
    # 0. DB bootstrap — runs in BACKGROUND so it cannot block the ASGI
    #    lifespan startup event. Previously this was awaited inline,
    #    which meant uvicorn would not serve a single HTTP request
    #    (not even /alive, /healthz, /auth/ping) until the entire
    #    cleanup_salla_duplicates + ``alembic upgrade head`` chain
    #    finished. On Railway this is observed as: TCP connects fine,
    #    request bytes are sent fine, but the client gets ``0 bytes
    #    received`` and times out — because uvicorn refuses to
    #    dispatch HTTP requests until lifespan.startup.complete.
    #
    #    Running it in the background means the worker is responsive
    #    INSTANTLY. The trade-off: routes that issue SQL referencing
    #    columns added by a pending migration will fail until the
    #    migration completes, but those failures surface as a clear
    #    SQL error instead of a service-wide hang. Liveness endpoints
    #    + the ack-first webhooks do NOT touch DB columns, so they
    #    are unaffected.
    _skip = os.environ.get("NAHLA_SKIP_DB_BOOTSTRAP", "").lower() in ("1", "true", "yes")
    if _skip:
        logger.info("[BOOT/db] NAHLA_SKIP_DB_BOOTSTRAP set — skipping cleanup + Alembic bootstrap.")
    else:
        try:
            # ── Per-step timeouts (seconds) — every subprocess.run gets one,
            # so a wedged migration can NEVER hold a thread forever.
            _T_CLEANUP = int(os.environ.get("NAHLA_BOOTSTRAP_CLEANUP_TIMEOUT", "60"))
            _T_STAMP   = int(os.environ.get("NAHLA_BOOTSTRAP_STAMP_TIMEOUT",   "30"))
            _T_UPGRADE = int(os.environ.get("NAHLA_BOOTSTRAP_UPGRADE_TIMEOUT", "180"))
            # Whole-bootstrap watchdog — if the entire chain exceeds this,
            # we abandon it in the background. Defaults to 5 min, override
            # via env if a one-off migration genuinely needs longer.
            _T_OVERALL = int(os.environ.get("NAHLA_BOOTSTRAP_OVERALL_TIMEOUT", "300"))

            def _bootstrap_db_schema() -> None:
                """
                Idempotent DB bootstrap for the worker.

                Hardened against the production hang we saw on
                ``api.nahlah.ai/alive`` returning 0 bytes:

                * Every subprocess.run carries an explicit ``timeout=``
                  so a stuck Alembic upgrade cannot hold the executor
                  thread indefinitely. ``TimeoutExpired`` is logged
                  loudly and the function returns — bootstrap is
                  always best-effort.
                * Each step prints its OWN ``[BOOT/db] ...`` log line
                  before AND after, so the operator can tell from
                  Railway exactly which step (if any) hung.
                * The single SQLAlchemy connection used for the
                  Alembic-stamp pre-check is explicitly disposed,
                  so a stuck migration cannot leak a DB connection
                  into the live request pool.
                """
                import subprocess
                import time as _t
                from sqlalchemy import create_engine, text as _text

                # ── Step A: Salla duplicate cleanup (must run before 0017) ──
                cleanup = os.path.join(_REPO_ROOT, "scripts", "cleanup_salla_duplicates.py")
                logger.info("[BOOT/db] Step A: cleanup_salla_duplicates.py (timeout=%ds)", _T_CLEANUP)
                _t0 = _t.monotonic()
                try:
                    r1 = subprocess.run(
                        [sys.executable, cleanup, "--execute"],
                        cwd=_REPO_ROOT,
                        check=False,
                        env=os.environ.copy(),
                        timeout=_T_CLEANUP,
                    )
                    logger.info(
                        "[BOOT/db] Step A: done rc=%d elapsed=%.1fs",
                        r1.returncode, _t.monotonic() - _t0,
                    )
                    if r1.returncode != 0:
                        logger.warning(
                            "[BOOT/db] cleanup_salla_duplicates.py exited %d — continuing to Alembic; "
                            "migration 0017 will fail loudly if duplicates remain.",
                            r1.returncode,
                        )
                except subprocess.TimeoutExpired as _to:
                    logger.error(
                        "[BOOT/db] Step A TIMEOUT after %ds (cleanup hung) — abandoning bootstrap. err=%s",
                        _T_CLEANUP, _to,
                    )
                    return

                # ── Step B: Stamp Alembic to 0016 if tables exist but alembic_version
                #    doesn't.  The DB was previously managed by Base.metadata.create_all();
                #    without this stamp, 'alembic upgrade head' tries to run 0001 which
                #    immediately fails with "relation tenants already exists".
                _db_url = os.environ.get("DATABASE_URL", "")
                if _db_url:
                    logger.info("[BOOT/db] Step B: alembic stamp pre-check")
                    try:
                        # connect_args.connect_timeout caps the libpq TCP connect.
                        # Without it, a network blip can leave _eng.connect()
                        # blocked for the kernel's TCP retry budget (~120s+).
                        _eng = create_engine(
                            _db_url,
                            pool_pre_ping=False,
                            pool_recycle=-1,
                            connect_args={"connect_timeout": 10},
                        )
                        try:
                            with _eng.connect() as _conn:
                                has_alembic = _conn.execute(_text(
                                    "SELECT 1 FROM information_schema.tables "
                                    "WHERE table_schema='public' AND table_name='alembic_version'"
                                )).scalar()
                                has_tenants = _conn.execute(_text(
                                    "SELECT 1 FROM information_schema.tables "
                                    "WHERE table_schema='public' AND table_name='tenants'"
                                )).scalar()
                        finally:
                            # Always dispose, even on exception, so a stuck
                            # connection cannot leak into the request pool.
                            try:
                                _eng.dispose()
                            except Exception:
                                pass

                        if has_tenants and not has_alembic:
                            logger.warning(
                                "[BOOT/db] Step B: alembic_version missing but 'tenants' exists — "
                                "stamping to 0016 (timeout=%ds)", _T_STAMP,
                            )
                            try:
                                _stamp = subprocess.run(
                                    [sys.executable, "-m", "alembic", "stamp", "0016"],
                                    cwd=_DATABASE_DIR,
                                    check=False,
                                    env=os.environ.copy(),
                                    capture_output=True,
                                    text=True,
                                    timeout=_T_STAMP,
                                )
                                logger.info(
                                    "[BOOT/db] Step B: stamp done rc=%d", _stamp.returncode,
                                )
                                if _stamp.returncode != 0 and _stamp.stderr:
                                    logger.warning(
                                        "[BOOT/db] alembic stamp stderr:\n%s",
                                        _stamp.stderr.strip(),
                                    )
                            except subprocess.TimeoutExpired as _to:
                                logger.error(
                                    "[BOOT/db] Step B TIMEOUT after %ds — abandoning bootstrap. err=%s",
                                    _T_STAMP, _to,
                                )
                                return
                        else:
                            logger.info(
                                "[BOOT/db] Step B: no stamp needed (has_alembic=%s has_tenants=%s)",
                                bool(has_alembic), bool(has_tenants),
                            )
                    except Exception as _stamp_exc:
                        logger.warning(
                            "[BOOT/db] Step B failed (non-fatal): %s — continuing to upgrade",
                            _stamp_exc,
                        )

                # ── Step C: Apply any pending migrations (0017, 0018, …) ────
                # capture_output so the real Alembic error surfaces in
                # Railway logs instead of being silently swallowed.
                logger.info(
                    "[BOOT/db] Step C: alembic upgrade head (timeout=%ds)", _T_UPGRADE,
                )
                _t0 = _t.monotonic()
                try:
                    _alembic = subprocess.run(
                        [sys.executable, "-m", "alembic", "upgrade", "head"],
                        cwd=_DATABASE_DIR,
                        check=False,
                        env=os.environ.copy(),
                        capture_output=True,
                        text=True,
                        timeout=_T_UPGRADE,
                    )
                except subprocess.TimeoutExpired as _to:
                    logger.error(
                        "[BOOT/db] Step C TIMEOUT after %ds — alembic upgrade hung. "
                        "Worker remains responsive but migrations did NOT run. err=%s",
                        _T_UPGRADE, _to,
                    )
                    return
                _elapsed = _t.monotonic() - _t0
                if _alembic.stdout:
                    logger.info(
                        "[BOOT/db] Step C: alembic upgrade head stdout (rc=%d, elapsed=%.1fs):\n%s",
                        _alembic.returncode, _elapsed, _alembic.stdout.strip(),
                    )
                if _alembic.returncode != 0:
                    logger.error(
                        "[BOOT/db] Step C FAILED rc=%d elapsed=%.1fs\n"
                        "--- stderr ---\n%s\n--- stdout ---\n%s",
                        _alembic.returncode, _elapsed,
                        (_alembic.stderr or "").strip(),
                        (_alembic.stdout or "").strip(),
                    )
                    return  # do NOT raise — caller already logs and continues
                logger.info(
                    "[BOOT/db] Step C: alembic upgrade head OK rc=0 elapsed=%.1fs",
                    _elapsed,
                )

            async def _bootstrap_db_schema_bg() -> None:
                """
                Background bootstrap runner.

                Three guarantees, in order of importance:

                1. **Isolated executor.** A dedicated single-thread
                   ``ThreadPoolExecutor`` is used instead of the
                   default executor. If this thread gets stuck on a
                   wedged subprocess, the default executor (used by
                   ``asyncio.to_thread`` for bcrypt during
                   /auth/login, sync DB calls inside route handlers,
                   etc.) is unaffected.

                2. **Overall timeout.** ``asyncio.wait_for(...,
                   timeout=NAHLA_BOOTSTRAP_OVERALL_TIMEOUT)``
                   (default 5 min) ensures we never wait past a
                   reasonable bound. On timeout we log loudly and
                   return; the dedicated executor is shut down with
                   ``cancel_futures=True`` so its thread can be
                   reclaimed once the underlying subprocess
                   terminates.

                3. **Errors never propagate.** This task ALWAYS
                   returns cleanly. Any exception is logged via
                   ``logger.exception`` so /alive, /healthz,
                   /auth/ping, and the ack-first webhooks keep
                   serving regardless of what alembic decides to do.
                """
                from concurrent.futures import ThreadPoolExecutor as _TPX  # noqa: PLC0415
                executor = _TPX(max_workers=1, thread_name_prefix="nahla-bootstrap")
                logger.info(
                    "[BOOT/db] Dispatching bootstrap on isolated executor "
                    "(overall_timeout=%ds, cleanup=%ds, stamp=%ds, upgrade=%ds)",
                    _T_OVERALL, _T_CLEANUP, _T_STAMP, _T_UPGRADE,
                )
                try:
                    loop = asyncio.get_running_loop()
                    await asyncio.wait_for(
                        loop.run_in_executor(executor, _bootstrap_db_schema),
                        timeout=_T_OVERALL,
                    )
                    logger.info("[BOOT/db] Bootstrap completed cleanly.")
                except asyncio.TimeoutError:
                    logger.error(
                        "[BOOT/db] Overall bootstrap timeout (%ds) — alembic appears to be wedged. "
                        "Abandoning bootstrap; /alive + ack-first webhooks remain available. "
                        "Set NAHLA_SKIP_DB_BOOTSTRAP=1 and run migrations manually to clear the lock.",
                        _T_OVERALL,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "[BOOT/db] Bootstrap FAILED in background: %s — "
                        "/alive and ack-first webhooks remain available; "
                        "DB-bound routes may 5xx until the operator fixes the schema.",
                        exc,
                    )
                finally:
                    # cancel_futures=True prevents queued work from
                    # running, but if the executor's only thread is
                    # blocked in subprocess.wait(), shutdown will
                    # wait until that subprocess returns. We pass
                    # wait=False so this finally block returns
                    # immediately; the OS will reap the thread once
                    # the subprocess exits.
                    try:
                        executor.shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        pass

            asyncio.create_task(_bootstrap_db_schema_bg())
            logger.info(
                "[BOOT/db] Bootstrap dispatched as background task — "
                "ASGI startup completes immediately so /alive + /healthz "
                "are reachable regardless of alembic state.",
            )
        except Exception as exc:
            logger.exception(
                "[BOOT/db] Failed to schedule bootstrap (non-fatal): %s", exc,
            )

    logger.info("[BOOT/lifespan] phase=1_safe_alters_dispatch t+%.3fs", _bt.monotonic() - _t_lifespan)
    # 1. DB table creation / column migrations (non-fatal)
    #
    # IMPORTANT: every import below is a *local* import. If something
    # heavy in database.session or database.models hangs at module
    # load (e.g. a top-level engine.connect()), it would freeze
    # lifespan startup and produce the 0-bytes-received hang. We
    # therefore push the imports themselves into the background task
    # so even a wedged module load cannot block /alive.
    try:
        from sqlalchemy import text          # noqa: PLC0415  (lightweight)

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

        # Fire-and-forget: run migrations on a *dedicated* executor so a
        # wedged ALTER TABLE cannot starve the default executor used by
        # bcrypt during /auth/login or any other asyncio.to_thread caller.
        async def _migrate_background():
            from concurrent.futures import ThreadPoolExecutor as _TPX  # noqa: PLC0415
            _exec = _TPX(max_workers=1, thread_name_prefix="nahla-safe-alters")
            try:
                # Heavy imports moved here so a slow/wedged module load
                # cannot block lifespan startup.
                from database.session import engine as _engine_lazy  # noqa: PLC0415
                from database.models import Base as _Base_lazy        # noqa: PLC0415
                # Inject into the closure that _run_migrations expects.
                # _run_migrations references `engine` and `Base` from the
                # enclosing scope; bind them here lazily.
                nonlocal_globals = globals()  # not used, kept for clarity
                # Reassign module-level names via builtins:
                _run_migrations.__globals__["engine"] = _engine_lazy
                _run_migrations.__globals__["Base"] = _Base_lazy
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(
                    loop.run_in_executor(_exec, _run_migrations),
                    timeout=int(os.environ.get("NAHLA_SAFE_ALTERS_TIMEOUT", "300")),
                )
                logger.info("[BOOT/safe_alters] Database tables ready.")
            except asyncio.TimeoutError:
                logger.error(
                    "[BOOT/safe_alters] timeout — safe_alters appear wedged. "
                    "Worker remains responsive; DB-bound routes may 5xx until "
                    "the operator drains the lock."
                )
            except Exception as exc:
                logger.warning("[BOOT/safe_alters] skipped (non-fatal): %s", exc)
            finally:
                try:
                    _exec.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass

        asyncio.create_task(_migrate_background())
        logger.info("[BOOT/safe_alters] task dispatched in background.")
    except Exception as exc:
        logger.warning("[BOOT/safe_alters] dispatch skipped (non-fatal): %s", exc)

    logger.info("[BOOT/lifespan] phase=2_meta_subscribe_dispatch t+%.3fs", _bt.monotonic() - _t_lifespan)
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

    logger.info("[BOOT/lifespan] phase=3_heartbeat t+%.3fs", _bt.monotonic() - _t_lifespan)
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

    logger.info("[BOOT/lifespan] phase=4_kill_switch t+%.3fs", _bt.monotonic() - _t_lifespan)
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
        logger.warning(
            "[BOOT/lifespan] complete (schedulers DISABLED) total=%.3fs — "
            "uvicorn will now mark startup_complete and begin dispatching HTTP.",
            _bt.monotonic() - _t_lifespan,
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

    logger.warning(
        "[BOOT/lifespan] complete total=%.3fs — uvicorn will now mark "
        "startup_complete and begin dispatching HTTP.",
        _bt.monotonic() - _t_lifespan,
    )


def _raw_asgi_should_log(scope: dict) -> bool:
    """Outbound diagnostic before FastAPI. Controlled by NAHLA_RAW_ASGI_LOG.

    * **unset / ``0`` / ``off``** — disabled (default; quiet production logs).
    * ``get`` — log only GET + HEAD (cheap probe for browser vs webhook).
    * ``all`` / ``1`` / ``true`` — log every HTTP request (noisy).
    """
    if scope.get("type") != "http":
        return False
    raw = os.environ.get("NAHLA_RAW_ASGI_LOG", "").strip().lower()
    if raw in ("", "0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "yes", "all"):
        return True
    if raw == "get":
        return scope.get("method") in ("GET", "HEAD")
    return False


# ── Outermost ASGI wrapper (edge / Railway diagnostics) ───────────────────────
# Declared AFTER the full FastAPI graph is built (routes + middleware + events).
# If Railway logs show POST traffic inside FastAPI but never print ``RAW_ASGI``
# for GET, the connection is dying **before** this Python callable runs.
_FASTAPI_APPLICATION = app


async def app(scope, receive, send):  # noqa: A001 — intentional uvicorn export name
    if _raw_asgi_should_log(scope):
        meth = scope.get("method")
        path = scope.get("path")
        qs = scope.get("query_string", b"")
        if isinstance(qs, bytes):
            qs_preview = qs[:80]
        else:
            qs_preview = repr(qs)[:80]
        msg = (
            f"[RAW_ASGI] type=http method={meth!r} path={path!r} "
            f"client={scope.get('client')!r} scheme={scope.get('scheme')!r} "
            f"http_version={scope.get('http_version')!r} qs={qs_preview!r}"
        )
        print("RAW_SCOPE", scope.get("type"), meth, path, flush=True)
        logger.warning(msg)
    await _FASTAPI_APPLICATION(scope, receive, send)


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
    port = int(os.environ.get("PORT", "8000"))
    logger.info("Starting Nahla SaaS Backend API on 0.0.0.0:%s …", port)
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        http=os.environ.get("UVICORN_HTTP", os.environ.get("NAHLA_UVICORN_HTTP", "auto")),
    )
