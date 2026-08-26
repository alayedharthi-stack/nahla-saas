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

from core.log_redaction import SecretRedactingFilter  # noqa: E402

_secret_redact_filter = SecretRedactingFilter()
for _logger_name in ("httpx", "httpcore"):
    logging.getLogger(_logger_name).addFilter(_secret_redact_filter)

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

# ── Sentry (Phase 1A) ──────────────────────────────────────────────────────────
# Initialised BEFORE FastAPI() so any exception raised during app setup
# (router import, middleware registration) is captured. Setup is a no-op
# when SENTRY_DSN is unset, so this is safe in dev.
from core.observability_sentry import init_sentry  # noqa: E402
init_sentry()

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


# Global build marker — bump whenever we ship a fix that needs ops
# verification from a 500 body without ssh-execing into the container.
# This is intentionally checked into source so a deployed image can be
# attributed to a commit just by reading a failed response.
BACKEND_BUILD_MARKER = "2026-05-21_500diag_v2"
logger.info("[main] backend loaded build_marker=%s", BACKEND_BUILD_MARKER)


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

    Diagnostic exposure (2026-05-21 #500diag): the response body now
    always carries ``code``, ``exc_class``, ``path``, ``build_marker``
    and a per-request ``incident_id`` so the dashboard can render an
    actionable error instead of a generic "Internal server error".
    NO exception MESSAGE is exposed — only the type name — to avoid
    leaking row values from psycopg2/SQLAlchemy errors.
    """
    import uuid as _uuid  # noqa: PLC0415

    path = _req.url.path or ""
    method = _req.method or "GET"
    is_no_response = isinstance(exc, RuntimeError) and "No response returned" in str(exc)
    is_webhook = path.startswith("/webhook/")
    incident_id = _uuid.uuid4().hex[:12]
    exc_class = type(exc).__name__

    if is_no_response:
        logger.error(
            "[GlobalExceptionHandler] incident=%s 'No response returned' on %s %s "
            "(BaseHTTPMiddleware end-of-chain w/o response, usually a "
            "client disconnect). Returning safe response.",
            incident_id, method, path,
            exc_info=True,
        )
    else:
        logger.error(
            "[GlobalExceptionHandler] incident=%s exc=%s on %s %s: %s",
            incident_id, exc_class, method, path, exc, exc_info=True,
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
            content={
                "ok": False,
                "error": "webhook_processing_error",
                "incident_id": incident_id,
            },
            headers=cors_headers,
        )

    # ``detail`` is a structured object so frontend ApiClient picks up
    # every field (it walks `detail` and copies scalar keys onto the
    # Error object — see dashboard/src/api/client.ts:buildApiError).
    return _JSONResponse(
        status_code=500,
        content={
            "detail": {
                "code":         "internal_error_unhandled",
                "exc_class":    exc_class,
                "path":         path,
                "method":       method,
                "build_marker": BACKEND_BUILD_MARKER,
                "incident_id":  incident_id,
                "message": (
                    "حدث خطأ غير متوقع. "
                    "تواصل مع الدعم وأرسل لهم رقم الحادثة وفئة الخطأ المعروضين."
                ),
            },
            # Top-level ``code`` kept for backwards compat with the
            # frontend ApiClient's older error-code path.
            "code": "internal_error_unhandled",
        },
        headers=cors_headers,
    )

# ── Optional ASGI stack isolation (binary search after proving networking OK) ──
# minimal_asgi works but backend.main does not → bisect middleware layers.
#   full             — default production stack (BaseHTTPMiddleware + FastPath + CORS)
#   cors_only        — CORSMiddleware only (no FastPath, no BaseHTTPMiddleware)
#   cors_fastpath    — FastPath + CORS only (isolates BaseHTTPMiddleware chain)
#   full_no_fastpath — full BaseHTTPMiddleware but NO FastPath (isolates FastPath)
_ASGI_STACK = os.environ.get("NAHLA_ASGI_STACK", "full").strip().lower()
if _ASGI_STACK in ("", "production", "prod"):
    _ASGI_STACK = "full"
_VALID_ASGI_STACKS = frozenset({"full", "cors_only", "cors_fastpath", "full_no_fastpath"})
if _ASGI_STACK not in _VALID_ASGI_STACKS:
    logger.warning(
        "[BOOT/asgi] Unknown NAHLA_ASGI_STACK=%r — falling back to full",
        _ASGI_STACK,
    )
    _ASGI_STACK = "full"

# Partial stacks (cors_only / cors_fastpath / full_no_fastpath) DISABLE
# tenant + JWT + owner middleware. That caused production breakage when
# `cors_fastpath` was set on a live deploy: every merchant route started
# erroring out with `resolve_tenant_id: no tenant scope` (no customers,
# AI panel blank). Refuse to honour a partial stack unless an operator
# explicitly opts in via NAHLA_ALLOW_PARTIAL_STACK=1 — meant for local /
# bisect debugging only.
if _ASGI_STACK != "full":
    _allow_partial = os.environ.get("NAHLA_ALLOW_PARTIAL_STACK", "").strip().lower() in ("1", "true", "yes")
    if not _allow_partial:
        logger.critical(
            "[BOOT/asgi] NAHLA_ASGI_STACK=%s requested but NAHLA_ALLOW_PARTIAL_STACK is NOT set — "
            "forcing back to 'full'. Partial stacks disable multi_tenant/jwt/owner middleware "
            "and break merchant routes (resolve_tenant_id no scope, customers blank, AI panel blank). "
            "Set NAHLA_ALLOW_PARTIAL_STACK=1 only for local middleware bisect debugging.",
            _ASGI_STACK,
        )
        _ASGI_STACK = "full"
    else:
        logger.warning(
            "[BOOT/asgi] NAHLA_ASGI_STACK=%s with NAHLA_ALLOW_PARTIAL_STACK=1 — "
            "DEBUG mode: tenant/JWT/owner middleware will be DISABLED. "
            "DO NOT use this in production.",
            _ASGI_STACK,
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
_use_http_mw = _ASGI_STACK in ("full", "full_no_fastpath")
_use_fastpath = _ASGI_STACK in ("full", "cors_fastpath")

if _use_http_mw:
    app.middleware("http")(multi_tenant_middleware)
    app.middleware("http")(api_key_middleware)
    app.middleware("http")(global_rate_limit_middleware)
    app.middleware("http")(request_logging_middleware)
    app.middleware("http")(support_session_middleware)
    app.middleware("http")(owner_merchant_scope_middleware)
    app.middleware("http")(jwt_enforcement_middleware)
    app.middleware("http")(salla_iframe_middleware)
elif _ASGI_STACK in ("cors_only", "cors_fastpath"):
    logger.warning(
        "[BOOT/asgi] NAHLA_ASGI_STACK=%s — ALL BaseHTTPMiddleware layers DISABLED "
        "(multi_tenant, api_key, rate_limit, request_logging, support_session, "
        "owner_scope, jwt_enforcement, salla_iframe).",
        _ASGI_STACK,
    )

if _use_fastpath:
    from core.fast_path_middleware import FastPathMiddleware, DEFAULT_FAST_PATHS  # noqa: E402
    app.add_middleware(FastPathMiddleware, fast_paths=DEFAULT_FAST_PATHS)
elif _ASGI_STACK == "full_no_fastpath":
    logger.warning(
        "[BOOT/asgi] NAHLA_ASGI_STACK=full_no_fastpath — FastPathMiddleware DISABLED "
        "(GET /alive goes through full BaseHTTPMiddleware chain).",
    )

# CORS must be outermost so it adds Access-Control-* headers to ALL responses,
# including FastPath responses and any error responses from inner middleware.
# add_middleware() wraps everything above it → the LAST add_middleware call
# becomes the outermost layer.
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
from routers.discovery_settings import router as _discovery_settings_router  # noqa: E402
from routers.catalog_intelligence import router as _catalog_intelligence_router  # noqa: E402
from routers.templates    import router as _templates_router     # noqa: E402
from routers.order_updates import router as _order_updates_router  # noqa: E402
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
from routers.ai_playground import router as _ai_playground_router  # noqa: E402
from routers.intelligence_libraries import router as _intelligence_libraries_router  # noqa: E402
from routers.knowledge    import router as _knowledge_router      # noqa: E402
from routers.inbound_media import router as _inbound_media_router  # noqa: E402
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
from routers.catalog           import (                                     # noqa: E402
    admin_router    as _admin_catalog_router,
    merchant_router as _merchant_catalog_router,
)
from routers.support_access    import router as _support_access_router     # noqa: E402
# Phase 2A Sprint 1 — TOTP 2FA enrol/confirm/disable + status.
from routers.twofa             import router as _twofa_router               # noqa: E402
from routers.notification_logs import router as _notification_logs_router  # noqa: E402
from routers.addons            import router as _addons_router               # noqa: E402
from routers.widgets           import router as _widgets_router              # noqa: E402
from routers.product_interests import router as _product_interests_router    # noqa: E402
# Delivery Quality Intelligence Layer (Phase 2 — analytical only).
# Read-only endpoints; no send-behaviour side effects.
from routers.delivery_quality   import router as _delivery_quality_router    # noqa: E402
from routers.operations_center  import router as _operations_center_router   # noqa: E402

# TEMPORARY: token-gated public debug router. Safe to delete once the
# abandoned-cart investigation is closed. See routers/debug_public.py.
from routers.debug_public      import router as _debug_public_router       # noqa: E402
from routers.public_catalog    import router as _public_catalog_router     # noqa: E402

app.include_router(_health_router)
app.include_router(_public_catalog_router)
app.include_router(_debug_public_router)
app.include_router(_admin_router)
app.include_router(_admin_debug_router)
app.include_router(_admin_salla_token_router)
from routers.admin_webhook_security import router as _admin_webhook_security_router  # noqa: E402
app.include_router(_admin_webhook_security_router)
from routers.admin_ai_quality import router as _admin_ai_quality_router  # noqa: E402
app.include_router(_admin_ai_quality_router)
from routers.admin_inbound_debug import router as _admin_inbound_debug_router  # noqa: E402
app.include_router(_admin_inbound_debug_router)
app.include_router(_auth_router)
app.include_router(_twofa_router)
app.include_router(_settings_router)
app.include_router(_discovery_settings_router)
app.include_router(_catalog_intelligence_router)
app.include_router(_templates_router)
app.include_router(_order_updates_router)
app.include_router(_campaigns_router)
app.include_router(_delivery_quality_router)
app.include_router(_operations_center_router)
app.include_router(_campaign_wizard_router)
app.include_router(_automations_router)
app.include_router(_analytics_router)
app.include_router(_conversations_router)
app.include_router(_coupons_router)
app.include_router(_promotions_router)
app.include_router(_offer_decisions_router)
app.include_router(_orders_router)
app.include_router(_intelligence_router)
app.include_router(_ai_playground_router)
app.include_router(_intelligence_libraries_router)
app.include_router(_knowledge_router)
app.include_router(_inbound_media_router)
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
app.include_router(_merchant_catalog_router)
app.include_router(_admin_catalog_router)
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

    async def _coexistence_client_id_repair_bg() -> None:
        """Clear bogus coexistence client_id values (UI labels / JS sentinels)."""
        await asyncio.sleep(5)
        try:
            from core.database import SessionLocal  # noqa: PLC0415
            from core.coexistence_repair import repair_coexistence_placeholder_client_ids  # noqa: PLC0415

            db = SessionLocal()
            try:
                n = repair_coexistence_placeholder_client_ids(db)
                if n:
                    logger.info("[BOOT/coexistence] startup repair cleared bogus client_id on %s row(s)", n)
            finally:
                db.close()
        except Exception as exc:
            logger.warning("[BOOT/coexistence] client_id startup repair skipped (non-fatal): %s", exc)

    asyncio.create_task(_coexistence_client_id_repair_bg())

    # 0. DB bootstrap — runs in BACKGROUND so it cannot block the ASGI
    #    lifespan startup event. Previously this was awaited inline,
    #    which meant uvicorn would not serve a single HTTP request
    #    (not even /alive, /healthz, /auth/ping) until the entire
    #    cleanup_salla_duplicates + ``alembic upgrade 0089`` chain
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
                #    without this stamp, 'alembic upgrade 0089' tries to run 0001 which
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

                # ── Step C: Apply normal application migrations through 0089 ─
                # 0088 is a sibling maintenance-only validation branch from 0087
                # and must only run via the guarded 0087→0088 operator.
                from scripts.operators.bootstrap_migration_contract import (  # noqa: PLC0415
                    build_normal_bootstrap_upgrade_argv,
                )

                _bootstrap_upgrade_cmd = build_normal_bootstrap_upgrade_argv(
                    python_executable=sys.executable,
                )
                # capture_output so the real Alembic error surfaces in
                # Railway logs instead of being silently swallowed.
                logger.info(
                    "[BOOT/db] Step C: alembic upgrade 0089 (timeout=%ds)", _T_UPGRADE,
                )
                _t0 = _t.monotonic()
                try:
                    _alembic = subprocess.run(
                        _bootstrap_upgrade_cmd,
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
                        "[BOOT/db] Step C: alembic upgrade 0089 stdout (rc=%d, elapsed=%.1fs):\n%s",
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
                    "[BOOT/db] Step C: alembic upgrade 0089 OK rc=0 elapsed=%.1fs",
                    _elapsed,
                )

                # ── Step D: Coexistence OAuth nonce table (0101) ─────────────
                from sqlalchemy import inspect as _sqla_inspect  # noqa: PLC0415
                from scripts.operators.bootstrap_migration_contract import (  # noqa: PLC0415
                    COEXISTENCE_NONCE_MIGRATION_TARGET, COEXISTENCE_NONCE_TABLE, assert_coexistence_nonce_migration_applied, build_coexistence_nonce_upgrade_argv,
                )

                _nonce_missing = False
                try:
                    _eng_d = create_engine(_db_url, pool_pre_ping=True)
                    with _eng_d.connect() as _conn_d:
                        _nonce_missing = COEXISTENCE_NONCE_TABLE not in _sqla_inspect(_conn_d).get_table_names()
                    _eng_d.dispose()
                except Exception as _d_probe_exc:
                    logger.warning(
                        "[BOOT/db] Step D: nonce table probe failed (non-fatal): %s",
                        _d_probe_exc,
                    )

                if _nonce_missing:
                    _nonce_upgrade_cmd = build_coexistence_nonce_upgrade_argv(
                        python_executable=sys.executable,
                    )
                    logger.info(
                        "[BOOT/db] Step D: alembic upgrade %s (timeout=%ds)",
                        COEXISTENCE_NONCE_MIGRATION_TARGET,
                        _T_UPGRADE,
                    )
                    _t0d = _t.monotonic()
                    try:
                        _alembic_d = subprocess.run(
                            _nonce_upgrade_cmd,
                            cwd=_DATABASE_DIR,
                            check=False,
                            env=os.environ.copy(),
                            capture_output=True,
                            text=True,
                            timeout=_T_UPGRADE,
                        )
                    except subprocess.TimeoutExpired as _to_d:
                        logger.error(
                            "[BOOT/db] Step D TIMEOUT after %ds — nonce migration did NOT run. err=%s",
                            _T_UPGRADE,
                            _to_d,
                        )
                        return
                    _elapsed_d = _t.monotonic() - _t0d
                    if _alembic_d.stdout:
                        logger.info(
                            "[BOOT/db] Step D stdout (rc=%d, elapsed=%.1fs):
%s",
                            _alembic_d.returncode,
                            _elapsed_d,
                            _alembic_d.stdout.strip(),
                        )
                    if _alembic_d.returncode != 0:
                        logger.error(
                            "[BOOT/db] Step D FAILED rc=%d elapsed=%.1fs
--- stderr ---
%s
--- stdout ---
%s",
                            _alembic_d.returncode,
                            _elapsed_d,
                            (_alembic_d.stderr or "").strip(),
                            (_alembic_d.stdout or "").strip(),
                        )
                        return
                    try:
                        _eng_chk = create_engine(_db_url, pool_pre_ping=True)
                        with _eng_chk.connect() as _c_chk:
                            assert_coexistence_nonce_migration_applied(_c_chk)
                        _eng_chk.dispose()
                    except Exception as _d_assert_exc:
                        logger.error("[BOOT/db] Step D post-check FAILED: %s", _d_assert_exc)
                        return
                    logger.info(
                        "[BOOT/db] Step D: alembic upgrade %s OK rc=0 elapsed=%.1fs",
                        COEXISTENCE_NONCE_TABLE,
                        _elapsed_d,
                    )
                else:
                    logger.info(
                        "[BOOT/db] Step D: skipped (%s already present)",
                        COEXISTENCE_NONCE_TABLE,
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
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS first_whatsapp_connected_at TIMESTAMP",
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
                "ALTER TABLE whatsapp_usage ADD COLUMN IF NOT EXISTS subscription_id INTEGER REFERENCES billing_subscriptions(id)",
                "CREATE INDEX IF NOT EXISTS ix_whatsapp_usage_tenant_subscription ON whatsapp_usage (tenant_id, subscription_id)",
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

    # ── Phase 2.5: Moyasar pending-subs reconciliation sweep ─────────────────
    # Why: Moyasar invoice ``callback_url`` is a *browser* redirect, not a
    # server webhook. If a merchant pays in another tab/device and never
    # comes back to /billing/payment-result, our DB stays
    # ``pending_payment`` while the funds were captured. The
    # ``/billing/status`` endpoint already self-heals on every dashboard
    # request, but a tenant that never opens the dashboard between
    # paying-and-now would still be stuck.
    #
    # This sweep runs once per process boot, ~30s after lifespan ends so
    # we don't slow startup, and asks Moyasar's invoice API about every
    # ``pending_payment`` Moyasar sub in the DB. Activation is idempotent
    # (already-paid → activate; still pending → no-op).
    try:
        async def _reconcile_pending_moyasar_sweep():
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                return
            try:
                from sqlalchemy.orm import Session as _Session  # noqa: PLC0415
                from core.database import SessionLocal  # noqa: PLC0415
                from models import BillingSubscription as _BS  # noqa: PLC0415
                from services.billing_activation import (  # noqa: PLC0415
                    _LAZY_RECONCILE_LAST,
                    _is_revivable_cancelled,
                    lazy_reconcile_tenant_pending_subs,
                )
                db: _Session = SessionLocal()
                try:
                    # Find every tenant that has at least one
                    # reconcilable sub (pending OR revivable cancelled)
                    # with a Moyasar invoice id. We iterate the
                    # tenants — not the subs — and delegate to the
                    # same lazy reconciler used by /billing/status
                    # and /billing/entitlements, so all four entry
                    # points (boot sweep, status, entitlements,
                    # debug) share one definition of "what needs
                    # reconciling". Keeps tenant 33's class of bug
                    # from re-emerging through the back door.
                    candidates = (
                        db.query(_BS)
                        .filter(_BS.status.in_(["pending_payment", "cancelled"]))
                        .all()
                    )
                    tenant_ids: list[int] = []
                    seen: set[int] = set()
                    for s in candidates:
                        meta = s.extra_metadata or {}
                        if not meta.get("moyasar_invoice_id"):
                            continue
                        if s.status == "cancelled" and not _is_revivable_cancelled(s):
                            continue
                        if s.tenant_id in seen:
                            continue
                        seen.add(s.tenant_id)
                        tenant_ids.append(s.tenant_id)

                    logger.info(
                        "[BOOT/billing-sweep] %d tenant(s) with reconcilable Moyasar sub(s)",
                        len(tenant_ids),
                    )

                    # The boot sweep should always run regardless of
                    # in-process cooldown — this is a one-shot per
                    # boot, after sleeping 30s, so it's not a
                    # rate-limit concern.
                    _LAZY_RECONCILE_LAST.clear()

                    activated_total = 0
                    for tid in tenant_ids:
                        try:
                            activated_any, results = await lazy_reconcile_tenant_pending_subs(
                                db, tid, source="boot_sweep",
                            )
                            n = sum(1 for r in results if r.get("activated"))
                            activated_total += n
                            if activated_any:
                                logger.info(
                                    "[BOOT/billing-sweep] tenant=%s activated_subs=%d",
                                    tid, n,
                                )
                        except Exception as exc:
                            logger.warning(
                                "[BOOT/billing-sweep] tenant=%s failed: %r",
                                tid, exc,
                            )
                    logger.info(
                        "[BOOT/billing-sweep] complete tenants=%d activated_subs=%d",
                        len(tenant_ids), activated_total,
                    )
                finally:
                    db.close()
            except Exception as exc:
                logger.warning("[BOOT/billing-sweep] aborted: %r", exc)

        asyncio.create_task(_reconcile_pending_moyasar_sweep())
    except Exception as exc:
        logger.warning("[BOOT/billing-sweep] dispatch skipped: %s", exc)

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

    # Wave / Batch scheduler — runs alongside the campaign dispatcher
    # but on its own loop so a misbehaving wave can never break the
    # immediate-campaign rescue path. See ``run_campaign_wave_scheduler``
    # docstring for the full design rationale.
    def _f_campaign_wave_scheduler():
        from core.scheduler import run_campaign_wave_scheduler  # noqa: PLC0415
        return run_campaign_wave_scheduler()
    _start("campaign_wave_scheduler", _f_campaign_wave_scheduler, 14)

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

    # Meta phone-tier sync — keeps WhatsAppConnection.meta_messaging_limit
    # fresh for every connected tenant on a fixed cadence (default 6h via
    # NAHLA_META_TIER_SYNC_INTERVAL_SEC) so the dashboard's "حد Meta" card
    # never shows a stale tier just because nobody happened to open the
    # usage page recently.
    def _f_meta_tier_sync():
        from core.scheduler import run_meta_tier_sync_scheduler  # noqa: PLC0415
        return run_meta_tier_sync_scheduler()
    _start("meta_tier_sync", _f_meta_tier_sync, 45)

    def _f_wa_refresh():
        from core.scheduler import run_wa_token_refresh_scheduler  # noqa: PLC0415
        return run_wa_token_refresh_scheduler()
    _start("wa_token_refresh", _f_wa_refresh, 45)

    def _f_wa_health():
        from core.scheduler import run_wa_token_health_scheduler  # noqa: PLC0415
        return run_wa_token_health_scheduler()
    _start("wa_token_health", _f_wa_health, 48)

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

    def _f_ai_quality_monitor():
        from core.scheduler import run_ai_quality_scheduler  # noqa: PLC0415
        return run_ai_quality_scheduler()
    _start("ai_quality_monitor", _f_ai_quality_monitor, 65)

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
# Phase 1A: the heavy lifting moved to two layers that run BEFORE this
# module imports:
#
#   1. ``scripts/preflight_check.py`` — invoked from start.sh before
#      uvicorn binds. Refuses to start the worker if any critical
#      secret is missing or matches a known placeholder.
#   2. ``core/config.py`` — raises ``RuntimeError`` at import time in
#      production when JWT_SECRET / ADMIN_PASSWORD / WHATSAPP_VERIFY_TOKEN
#      are unsafe.
#
# By the time we reach this line in production, the secrets are known
# good. We keep a single soft warning here for non-blocking hygiene
# checks (e.g. ENABLE_ADMIN_DEBUG flagged on, REDIS_URL missing).
if IS_PRODUCTION:
    if (os.environ.get("ENABLE_ADMIN_DEBUG", "") or "").strip().lower() == "true":
        logger.warning(
            "SECURITY — ENABLE_ADMIN_DEBUG is TRUE in production. "
            "Turn it off as soon as the recovery action is complete."
        )
    if not (os.environ.get("REDIS_URL", "") or "").strip():
        logger.warning(
            "OBSERVABILITY — REDIS_URL is not set in production. "
            "Rate limiting and JWT revocation fall back to per-worker "
            "in-process counters (not shared across workers)."
        )
    if not (os.environ.get("SENTRY_DSN", "") or "").strip():
        logger.warning(
            "OBSERVABILITY — SENTRY_DSN is not set in production. "
            "Backend errors will not be reported to Sentry."
        )
    logger.info("Production startup hygiene checks complete.")
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
