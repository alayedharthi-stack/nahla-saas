"""
core/middleware.py
──────────────────
All FastAPI middleware functions and the rate_limit() helper used by route handlers.
Register these in main.py — never import from here in routers.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time as _time
from typing import Awaitable, Callable

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

from core.auth import JWT_AVAILABLE, PLATFORM_ADMIN_ROLES, decode_token
from core.audit import audit
from core.config import API_SECRET_KEY

logger = logging.getLogger("nahla-backend")


# ── Cross-middleware safety contract ────────────────────────────────────────
# Paths that must never be touched by a heavy middleware (DB session,
# JWT decode, rate-limit Redis lookup, support-session DB read, etc.).
# Each middleware below checks this set EARLY and short-circuits to
# ``call_next`` when the path matches, so a congested chain cannot
# starve liveness probes, the dashboard's connectivity check, or the
# webhook ack-first path.
#
# Webhook paths are matched by prefix (``/webhook/``) further below;
# only exact-match liveness/login paths live here.
ULTRA_LIGHT_PATHS = frozenset({
    "/",
    "/alive",
    "/healthz",
    "/auth/ping",
    "/auth/login",        # JSON login — same DB/hot path as login-form
    "/auth/login-form",   # form-encoded login (no preflight); must
                          # stay snappy when the JSON path is blocked
})


def _is_bypass_path(path: str) -> bool:
    """True for paths that should skip every non-essential middleware.

    Exact match on ULTRA_LIGHT_PATHS, plus prefix match on /webhook/
    so providers (Meta / 360dialog) always get a fast 200 even when
    the rest of the chain is congested.
    """
    if path in ULTRA_LIGHT_PATHS:
        return True
    if path.startswith("/webhook/"):
        return True
    return False


# Optional anyio import — used by the safety wrapper below to
# recognise an EndOfStream that was raised through call_next when a
# client disconnected mid-request.
try:
    from anyio import EndOfStream as _AnyioEndOfStream  # type: ignore
except Exception:  # pragma: no cover — anyio is a Starlette dep
    _AnyioEndOfStream = ()  # type: ignore[assignment]


async def _safe_call_next(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
    *,
    name: str,
) -> Response:
    """
    Wrap ``await call_next(request)`` so this middleware can NEVER
    exit without returning a Response.

    Without this wrapper, the BaseHTTPMiddleware machinery raises
    ``RuntimeError: No response returned`` whenever the inner ASGI
    task ends without sending — which happens routinely when the
    client disconnects (``asyncio.CancelledError``,
    ``anyio.EndOfStream``) or when an inner middleware itself
    misbehaves. The exception unwinds through every outer middleware
    and surfaces in the ASGI server, congesting the worker because
    Starlette retries the send.

    Behaviour:
    * Successful response               → returned unchanged.
    * ``CancelledError``                → log + 499-shaped fallback.
    * ``anyio.EndOfStream``             → log + 499-shaped fallback.
    * ``RuntimeError("No response …")`` → log + 500-shaped fallback.
    * Any other ``Exception``           → log + 500-shaped fallback.

    Webhook paths (``/webhook/*``) get a 200 ``ok=false`` body
    instead of 499/500 so 360dialog / Meta do NOT enter retry-storm
    mode on a transient cancellation. See ``_safe_fallback_response``.

    Note that we re-import ``anyio.EndOfStream`` lazily here in case
    the symbol gets relocated in a future anyio release; production
    must keep working even if the type check fails.
    """
    try:
        return await call_next(request)
    except asyncio.CancelledError:
        logger.warning(
            "[%s] CancelledError on %s %s — client disconnected",
            name, request.method, request.url.path,
        )
        return _safe_fallback_response(request, 499)
    except RuntimeError as exc:
        if "No response returned" in str(exc):
            logger.error(
                "[%s] BaseHTTPMiddleware end-of-chain w/o response on %s %s: %s",
                name, request.method, request.url.path, exc,
            )
            return _safe_fallback_response(request, 500)
        # Other RuntimeErrors are real bugs — keep the stack trace.
        logger.exception(
            "[%s] RuntimeError on %s %s",
            name, request.method, request.url.path,
        )
        return _safe_fallback_response(request, 500)
    except BaseException as exc:  # noqa: BLE001
        # Catch BaseException so anyio.EndOfStream (which subclasses
        # Exception today but may evolve) and other low-level cancel
        # markers cannot escape. Re-raise SystemExit/KeyboardInterrupt.
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        is_eos = bool(_AnyioEndOfStream) and isinstance(exc, _AnyioEndOfStream)
        if is_eos:
            logger.warning(
                "[%s] anyio.EndOfStream on %s %s — client gone",
                name, request.method, request.url.path,
            )
            return _safe_fallback_response(request, 499)
        logger.exception(
            "[%s] downstream raised %s on %s %s",
            name, type(exc).__name__, request.method, request.url.path,
        )
        return _safe_fallback_response(request, 500)

# Public path prefixes that never require a JWT token.
# Keep these as specific as possible — broad prefixes can accidentally
# expose protected endpoints under the same prefix.
JWT_PUBLIC_PREFIXES = (
    "/health",
    "/healthz",                         # alias for upstream proxies
    "/alive",                           # ultra-light liveness probe
    "/version",                         # public deploy-identity probe
    "/api/version",                     # alias of /version
    "/debug/",                          # TEMPORARY: token-gated debug surface
                                        # (gated inside the handler via DEBUG_ADMIN_TOKEN)
    "/admin/debug/",                    # TEMPORARY: env-flag-gated admin recovery
                                        # (gated inside the handler via ENABLE_ADMIN_DEBUG
                                        # + optional ADMIN_DEBUG_SECRET)
    "/webhook",
    "/auth",
    "/oauth",                           # Salla/WhatsApp OAuth callbacks
    "/integrations/salla/",             # Salla success/error landing HTML pages (public)
    "/salla",                           # /salla/start (new merchant install entry point)
    "/api/salla/test/authorize",        # Salla TEST app OAuth start — public redirect
    "/api/salla/diag/",                 # public diagnostic endpoints (no secrets exposed)
    "/api/salla/oauth/start",           # Sync (OAuth) app: 302 → accounts.salla.sa
                                        # JWT validated inside handler from ?token=
                                        # (Authorization header is stripped on top-level navigation)
    "/api/salla/oauth/callback",        # Sync (OAuth) app: Salla redirects here with ?code=
                                        # tenant resolved from signed state, no JWT needed
    "/zid",                             # /zid/app, /zid/redirect, /zid/token-login
    "/settings/validate",               # Salla Partner Portal validation probe
    "/snippet.js",
    "/track",
    # ── Public store scripts (loaded by external stores — no JWT) ──────────────
    "/merchant/addons/widget/",         # legacy widget embed.js
    "/merchant/widgets/salla-auto.js",  # universal Salla snippet
    "/merchant/widgets/salla/",         # by-salla-store widgets
    "/merchant/widgets/",               # all widget JS/JSON endpoints
    "/salla-auto.js",                   # short alias (configured in Salla Partner Portal)
    "/static/salla-auto.js",            # legacy path (configured in Salla Partner Portal)
)
# NOTE: /integrations/whatsapp/status and /integrations/debug are PROTECTED — JWT required.


# ── Middleware functions ───────────────────────────────────────────────────────

async def multi_tenant_middleware(request: Request, call_next):
    """Read X-Tenant-ID header and attach to request.state (dev routing only)."""
    # Belt-and-suspenders: never inspect headers / state for OPTIONS — CORS
    # preflight responses must always pass through cleanly. Same logic in
    # every inner middleware below.
    if request.method == "OPTIONS":
        return await _safe_call_next(request, call_next, name="multi_tenant")
    if _is_bypass_path(request.url.path):
        return await _safe_call_next(request, call_next, name="multi_tenant")
    raw = request.headers.get("X-Tenant-ID")
    try:
        tenant_id = str(int(raw)) if raw is not None else None
    except (ValueError, TypeError):
        tenant_id = None
    request.state.tenant_id = tenant_id
    return await _safe_call_next(request, call_next, name="multi_tenant")


async def api_key_middleware(request: Request, call_next):
    """Reject unauthenticated service calls without X-Nahla-Key when configured."""
    if request.method == "OPTIONS":
        # Preflight requests carry no Authorization header by design — they
        # MUST be allowed through so CORSMiddleware can answer them. A 401
        # here would surface to the browser as a CORS failure with no
        # actionable message.
        return await _safe_call_next(request, call_next, name="api_key")
    if _is_bypass_path(request.url.path):
        return await _safe_call_next(request, call_next, name="api_key")
    if API_SECRET_KEY:
        path = request.url.path
        if not (
            path.startswith("/health")
            or path.startswith("/alive")
            or path.startswith("/healthz")
            or path.startswith("/version")
            or path.startswith("/api/version")
            or path.startswith("/debug/")    # TEMPORARY: token-gated debug surface
            or path.startswith("/admin/debug/")  # TEMPORARY: env-flag-gated admin recovery
            or path.startswith("/webhook")
            or path.startswith("/auth")
            or path.startswith("/api/salla/diag/")  # public diagnostic
        ):
            auth_header = request.headers.get("Authorization", "")
            has_bearer_token = auth_header.startswith("Bearer ")
            if not has_bearer_token and request.headers.get("X-Nahla-Key", "") != API_SECRET_KEY:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized"},
                    headers=_cors_error_headers(request),
                )
    return await _safe_call_next(request, call_next, name="api_key")


async def global_rate_limit_middleware(request: Request, call_next):
    """300 requests per minute per IP — exempts /health and /auth."""
    if request.method == "OPTIONS":
        return await _safe_call_next(request, call_next, name="global_rate_limit")
    # Webhooks + liveness paths are exempted explicitly — providers
    # ALWAYS exceed any per-IP threshold during a burst, and the
    # ack-first contract requires a sub-100 ms response.
    if _is_bypass_path(request.url.path):
        return await _safe_call_next(request, call_next, name="global_rate_limit")
    if not (
        request.url.path.startswith("/health")
        or request.url.path.startswith("/alive")
        or request.url.path.startswith("/healthz")
        or request.url.path.startswith("/auth")
    ):
        sys.path.insert(
            0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../observability"))
        )
        from rate_limiter import check_rate_limit as _check  # noqa: PLC0415
        client_ip = request.headers.get("X-Real-IP") or (
            request.client.host if request.client else "unknown"
        )
        if not _check(f"global:{client_ip}", max_count=300, window_seconds=60):
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers=_cors_error_headers(request),
            )
    return await _safe_call_next(request, call_next, name="global_rate_limit")


async def request_logging_middleware(request: Request, call_next):
    """Log HTTP method, path, status code, and latency for every request.

    Routes can voluntarily increment ``request.state.db_ms`` /
    ``request.state.ai_ms`` / ``request.state.lock_wait_ms`` to give the
    operator a breakdown in the per-request log line. When unset they
    default to 0 — we never crash a request because a route forgot to
    seed the counters.

    Slow requests (>1500 ms) are logged at WARNING with a
    ``[SLOW REQUEST]`` prefix and forwarded to ``core.runtime_perf`` so
    they show up in ``GET /admin/runtime/perf`` for live diagnostics.

    Every CORS preflight is also logged at INFO with a ``[CORS]`` prefix
    so a browser-side CORS failure can be matched line-by-line against
    the API logs:

        [CORS] OPTIONS /auth/login origin=https://app.nahlah.ai
               acr_method=POST acr_headers=content-type
    """
    if request.method == "OPTIONS":
        # Surface preflight requests at INFO so an ops watcher can spot
        # missing/mistaken Origin or Access-Control-Request-* headers in
        # one log line. We intentionally log BEFORE passing to call_next
        # so the line appears even if a deeper handler hangs.
        logger.info(
            "[CORS] OPTIONS %s origin=%s acr_method=%s acr_headers=%s",
            request.url.path,
            request.headers.get("origin", "-"),
            request.headers.get("access-control-request-method", "-"),
            request.headers.get("access-control-request-headers", "-"),
        )

    start = _time.monotonic()
    request.state.db_ms = 0
    request.state.ai_ms = 0
    request.state.lock_wait_ms = 0

    # Track concurrent in-flight HTTP requests so the runtime heartbeat
    # and /admin/runtime/perf can answer "how many requests is the
    # worker servicing right now?". Always paired in a try/finally so
    # the counter cannot leak if the route raises.
    try:
        from core.runtime_perf import (  # noqa: PLC0415
            incr_active_request, decr_active_request,
        )
        incr_active_request()
        _decr_active = decr_active_request
    except Exception:
        _decr_active = None

    # Hardened: _safe_call_next NEVER lets an exception escape — it
    # returns a fallback Response for CancelledError, EndOfStream,
    # RuntimeError("No response returned"), or any other exception.
    # This is the most important safety property of this middleware.
    response: Response
    try:
        response = await _safe_call_next(request, call_next, name="request_logging")
    finally:
        if _decr_active is not None:
            try:
                _decr_active()
            except Exception:
                pass

    duration_ms = round((_time.monotonic() - start) * 1000)

    db_ms        = int(getattr(request.state, "db_ms",        0) or 0)
    ai_ms        = int(getattr(request.state, "ai_ms",        0) or 0)
    lock_wait_ms = int(getattr(request.state, "lock_wait_ms", 0) or 0)
    tenant_id    = getattr(request.state, "tenant_id", "-")
    client_ip    = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )
    status_code  = getattr(response, "status_code", 500)

    breakdown = ""
    if db_ms or ai_ms or lock_wait_ms:
        breakdown = f" db={db_ms}ms ai={ai_ms}ms lock_wait={lock_wait_ms}ms"

    if duration_ms >= 1500:
        logger.warning(
            "[SLOW REQUEST] %s %s %d %dms%s tenant=%s ip=%s",
            request.method,
            request.url.path,
            status_code,
            duration_ms,
            breakdown,
            tenant_id,
            client_ip,
        )
    else:
        logger.info(
            "%s %s %d %dms%s tenant=%s ip=%s",
            request.method,
            request.url.path,
            status_code,
            duration_ms,
            breakdown,
            tenant_id,
            client_ip,
        )

    try:
        from core.runtime_perf import record_request  # noqa: PLC0415
        record_request(
            method=request.method,
            path=request.url.path,
            status=status_code,
            total_ms=duration_ms,
            db_ms=db_ms,
            ai_ms=ai_ms,
            lock_wait_ms=lock_wait_ms,
            tenant_id=str(tenant_id),
        )
    except Exception:
        # Telemetry must never affect the response path.
        pass

    return response


async def salla_iframe_middleware(request: Request, call_next):
    """
    Allow app.nahlah.ai to be embedded in Salla's iframe viewer (s.salla.sa).
    Sets Content-Security-Policy frame-ancestors instead of X-Frame-Options
    so Salla can load the app inside their embedded app viewer.

    Uses ``_safe_call_next`` so this middleware NEVER exits without a
    Response, even on CancelledError / EndOfStream / RuntimeError("No
    response returned"). This is the precondition for not surfacing
    ``RuntimeError`` from BaseHTTPMiddleware on a client disconnect,
    and the safety contract every middleware in the chain shares.

    The frame-ancestors CSP only matters for HTML page responses, so
    we skip the header tweak entirely for /webhook/* and ultra-light
    paths to keep the hot path allocation-free.
    """
    if _is_bypass_path(request.url.path):
        return await _safe_call_next(request, call_next, name="salla_iframe")

    response = await _safe_call_next(request, call_next, name="salla_iframe")
    try:
        response.headers["Content-Security-Policy"] = (
            "frame-ancestors 'self' https://s.salla.sa https://*.salla.sa "
            "https://store.salla.sa https://app.nahlah.ai https://apps.salla.sa"
        )
        if "x-frame-options" in response.headers:
            del response.headers["x-frame-options"]
    except Exception as exc:  # noqa: BLE001
        # Header tweaks are nice-to-have. Never fail a successful
        # downstream response over a header mutation.
        logger.warning("[salla_iframe] header tweak failed: %s", exc)
    return response


def _safe_fallback_response(request: Request, status_code: int) -> JSONResponse:
    """
    Build a JSONResponse with CORS headers as a last-resort fallback
    when a middleware would otherwise exit without a response.

    For webhook paths we override the requested ``status_code`` with
    200 so upstream providers (360dialog, Meta) do not enter their
    retry loop on what is almost always a transient cancellation.
    The actual processing error (if any) has already been logged.
    """
    path = request.url.path or ""
    if path.startswith("/webhook/"):
        return JSONResponse(
            status_code=200,
            content={"ok": False, "error": "webhook_processing_error"},
            headers=_cors_error_headers(request),
        )
    return JSONResponse(
        status_code=status_code,
        content={"detail": "Internal server error", "code": "internal_error"},
        headers=_cors_error_headers(request),
    )


def _cors_error_headers(request: Request) -> dict:
    """
    Return CORS headers for error responses emitted directly by this middleware.

    Although CORSMiddleware is registered as the outermost layer (so it adds
    headers to all responses that pass through it), any JSONResponse returned
    directly from *inner* middleware bypasses CORSMiddleware entirely.
    This helper adds the minimum required headers so browsers don't mask the
    real error with a misleading CORS failure message.
    """
    from core.config import CORS_ORIGINS as _origins, CORS_ORIGIN_REGEX as _origin_regex  # noqa: PLC0415
    import re as _re  # noqa: PLC0415
    origin = request.headers.get("origin", "")
    if origin and (
        origin in _origins
        or "*" in _origins
        or (_origin_regex and _re.fullmatch(_origin_regex, origin))
    ):
        return {
            "Access-Control-Allow-Origin":      origin,
            "Access-Control-Allow-Credentials": "true",
            "X-Nahla-Error-Type":              "cors-compatible-error",
        }
    if origin:
        logger.warning("[CORS] Origin not allowed for error response | origin=%s path=%s", origin, request.url.path)
    return {}


async def jwt_enforcement_middleware(request: Request, call_next):
    """
    Require a valid JWT for all non-public routes.
    On success: attaches the decoded payload to request.state.jwt_payload and
    overrides request.state.tenant_id from the token claim (prevents header spoofing).
    """
    path = request.url.path

    # Always pass through CORS preflight without a token
    if request.method == "OPTIONS":
        from fastapi.responses import Response as _Resp  # noqa: PLC0415
        # For public widget preflight — reply immediately with wildcard CORS
        if path.startswith("/merchant/widgets/") or path.startswith("/merchant/addons/widget/"):
            return _Resp(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization",
                    "Access-Control-Max-Age": "86400",
                },
            )
        return await _safe_call_next(request, call_next, name="jwt_enforcement")

    # Ultra-light + webhook paths skip JWT entirely. Without this,
    # /alive and /healthz would 401 because they're not under the
    # public-prefix tree, defeating the "no blocking I/O on liveness"
    # promise.
    if _is_bypass_path(path):
        return await _safe_call_next(request, call_next, name="jwt_enforcement")

    if any(path.startswith(p) for p in JWT_PUBLIC_PREFIXES):
        return await _safe_call_next(request, call_next, name="jwt_enforcement")

    # Public store scripts + store-facing widget APIs — no JWT possible from external stores
    # Pattern: /merchant/widgets/{id}/*.js | *.json | /create-coupon
    if path.startswith("/merchant/widgets/") and (
        path.endswith(".js")
        or path.endswith(".json")
        or path.endswith("/create-coupon")
    ):
        response = await _safe_call_next(request, call_next, name="jwt_enforcement")
        # Allow ANY store domain to call these public endpoints (CORS wildcard)
        try:
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        except Exception:
            pass
        return response

    # Legacy addon embed scripts
    if path.startswith("/merchant/addons/widget/") and path.endswith(".js"):
        response = await _safe_call_next(request, call_next, name="jwt_enforcement")
        try:
            response.headers["Access-Control-Allow-Origin"] = "*"
        except Exception:
            pass
        return response

    if not JWT_AVAILABLE:
        logger.critical(
            "SECURITY HALT: python-jose is not installed. "
            "JWT enforcement cannot be applied. Refusing all protected requests."
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Auth service unavailable — server misconfiguration.",
                "code": "jwt_library_missing",
            },
            headers=_cors_error_headers(request),
        )

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required", "code": "missing_token"},
            headers=_cors_error_headers(request),
        )

    payload = decode_token(auth_header[7:])
    if not payload:
        return JSONResponse(
            status_code=401,
            content={"detail": "Token expired or invalid", "code": "invalid_token"},
            headers=_cors_error_headers(request),
        )

    # Attach the full payload so route handlers can read any claim
    request.state.jwt_payload = payload

    # Tenant ID comes strictly from the JWT — never from headers or defaults.
    # Admin tokens carry tenant_id=1 by convention; all merchant tokens carry
    # the actual tenant that was assigned at registration time.
    tid = payload.get("tenant_id")
    if tid is None:
        logger.warning(
            "[JWT] Token has no tenant_id claim — path=%s sub=%s role=%s",
            request.url.path, payload.get("sub"), payload.get("role"),
        )
        # Refuse to proceed without a tenant scope; old tokens must be refreshed.
        return JSONResponse(
            status_code=401,
            content={"detail": "Token missing tenant_id — please log in again", "code": "no_tenant_claim"},
            headers=_cors_error_headers(request),
        )

    request.state.tenant_id = str(int(tid))
    return await _safe_call_next(request, call_next, name="jwt_enforcement")


# ── Owner ↔ merchant scope isolation middleware ────────────────────────────────

# Authenticated paths a platform-admin token MAY call without explicitly
# impersonating a specific merchant.
#
# Anything outside this allowlist is treated as a merchant-scoped endpoint:
# admin/owner tokens reaching such an endpoint without an ``impersonation``
# JWT claim are refused with HTTP 403, because admin tokens carry
# ``tenant_id = 1`` by convention (see ``jwt_enforcement_middleware``) and
# would otherwise leak that one tenant's data into the owner UI.
#
# Order rules:
# * Keep the prefix as specific as possible — broad prefixes can accidentally
#   expose protected merchant routes that happen to share a prefix.
# * Public routes (``JWT_PUBLIC_PREFIXES``) are skipped automatically because
#   the JWT middleware lets them through without a payload, so this middleware
#   never reaches the role check for them.
OWNER_ALLOWED_PREFIXES = (
    "/admin",                # all owner/admin APIs (revenue, tenants, features, …)
    "/tenants/",             # GET /tenants/{id} alias gated by require_admin
    "/whatsapp/admin/",      # admin-only WhatsApp coexistence ops
    "/system/health",        # platform health (admin dashboards)
    "/system/events",        # platform event log (admin dashboards)
    "/auth",                 # login/refresh/logout (also public, but be explicit)
    "/health",               # health probes
)

_owner_scope_audit = logging.getLogger("nahla.tenant_isolation_audit")


async def owner_merchant_scope_middleware(request: Request, call_next):
    """
    Defense-in-depth tenant-isolation guard for the platform-admin role.

    Runs AFTER :func:`jwt_enforcement_middleware` so ``request.state.jwt_payload``
    is already decoded and validated. Skips public paths automatically (no
    payload attached), skips merchant tokens, skips support-impersonation
    tokens (which have already chosen an explicit, audited tenant scope).

    OPTIONS preflight requests are passed through unconditionally so a
    browser CORS check is never blocked by an authorization rule.

    For every other request, if the role is in :data:`PLATFORM_ADMIN_ROLES`
    and the path is NOT in :data:`OWNER_ALLOWED_PREFIXES`, the request is
    refused with HTTP 403 and an audit event is emitted. This blocks the
    entire class of "owner-token-with-tenant_id=1 leaks merchant data into
    the owner UI" bugs at the framework level instead of relying on every
    router to remember the per-endpoint dependency.
    """
    if request.method == "OPTIONS":
        return await _safe_call_next(request, call_next, name="owner_scope")
    if _is_bypass_path(request.url.path):
        return await _safe_call_next(request, call_next, name="owner_scope")
    payload = getattr(request.state, "jwt_payload", None)
    if not payload:
        return await _safe_call_next(request, call_next, name="owner_scope")

    role = str(payload.get("role") or "").strip()
    if role not in PLATFORM_ADMIN_ROLES:
        return await _safe_call_next(request, call_next, name="owner_scope")

    if payload.get("impersonation"):
        return await _safe_call_next(request, call_next, name="owner_scope")

    path = request.url.path
    if any(path.startswith(p) for p in OWNER_ALLOWED_PREFIXES):
        return await _safe_call_next(request, call_next, name="owner_scope")

    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )
    _owner_scope_audit.warning(
        "MERCHANT_SCOPE_DENIED_FOR_ADMIN role=%s sub=%s tenant=%s path=%s ip=%s",
        role, payload.get("sub"), payload.get("tenant_id"), path, client_ip,
    )
    try:
        audit(
            "merchant_scope_denied_for_admin",
            path=path,
            method=request.method,
            role=role,
            sub=payload.get("sub"),
            tenant_id=payload.get("tenant_id"),
            ip=client_ip,
            source="middleware",
        )
    except Exception as _e:  # never fail the rejection on an audit error
        logger.error("[owner_scope] audit emission failed: %s", _e)

    return JSONResponse(
        status_code=403,
        content={
            "detail": (
                "هذه الواجهة مخصصة لبيانات تاجر محدد. "
                "حسابات المنصة لا تستطيع قراءة بيانات متجر مباشرة دون "
                "تفعيل وضع الدعم/التشخيص لمتجر محدد."
            ),
            "code": "merchant_scope_required",
        },
        headers=_cors_error_headers(request),
    )


# ── Support-session middleware ─────────────────────────────────────────────────

# Paths that support-impersonation sessions are NEVER allowed to call.
# Keep this list conservative; add paths as new sensitive features are built.
#
# Matching: prefix-based. Any incoming request path starting with one of
# these strings is rejected with 403 + audit log. Read-only exceptions
# under these prefixes go into ``_SUPPORT_ALLOWED_READS`` below.
_SUPPORT_BLOCKED_PATHS = (
    # Password / credential changes
    "/auth/change-password",
    "/auth/change-email",
    "/auth/reset-password",
    # Billing and payment — write paths must never be reachable from
    # a support session. Read paths are allow-listed below so support
    # can diagnose subscription state without the merchant on the call.
    "/billing",
    "/payment",
    "/subscription",
    "/checkout",
    # Secrets and integration tokens
    "/settings/integrations",
    "/settings/secrets",
    "/integrations/zid/token",
    "/integrations/salla/token",
    "/whatsapp/direct/connect",
    "/whatsapp/direct/verify",
    # Tenant / account destruction
    "/tenant/delete",
    "/account/delete",
    "/admin/delete-tenant",
)

# Read-only endpoints under blocked prefixes that ARE safe to allow
# during a support session. Each entry is ``(METHOD, exact_path)``.
#
# Why exact-match (not prefix): every entry here is an attestation
# that *this exact path* returns no mutation side-effects AND no
# bearer-token / card-PAN-grade secret. Adding a new entry should
# require auditing what the handler returns. A future write endpoint
# that happens to share a prefix (e.g. `/billing/status/cancel`)
# would NOT be allowed unless explicitly added.
#
# Audit trail: the support session middleware still records every
# allowed read at INFO level, so the merchant has a full log of what
# the admin looked at.
_SUPPORT_ALLOWED_READS: frozenset[tuple[str, str]] = frozenset({
    # Subscription state — needed to diagnose "why are outbound sends
    # being rejected?" without forcing the merchant onto a call.
    ("GET", "/billing/status"),
    # Plan catalog — public-ish info, no merchant-specific data.
    ("GET", "/billing/plans"),
    # Entitlements snapshot — what features the plan unlocks.
    ("GET", "/billing/entitlements"),
    # Result of a returning payment redirect — read-only screen.
    # The handler reads provider state; it does not mutate billing.
    ("GET", "/billing/payment-result"),
    # Read-only debug snapshot — same intent as the admin one.
    ("GET", "/billing/debug/current"),
})

_support_middleware_log = logging.getLogger("nahla.support_audit")


async def support_session_middleware(request: Request, call_next):
    """
    Enforces the security model for support-impersonation JWTs.

    Runs AFTER jwt_enforcement_middleware so request.state.jwt_payload is
    already decoded and validated.

    What this middleware does
    ─────────────────────────
    1. If role != "support_impersonation" → pass through unchanged.

    2. Verify session_version matches the DB value for this tenant.
       If the merchant revoked access (version bumped), reject immediately
       with 403 even though the JWT itself has not expired.

    3. Block any request to a sensitive path — return 403 + audit log.

    4. Log every request made during a support session (actor, path, tenant, IP).

    Note: localStorage is irrelevant here — all decisions are made from
    JWT claims decoded server-side. The frontend can show whatever it likes;
    only the backend enforces access control.
    """
    payload = getattr(request.state, "jwt_payload", None)

    # OPTIONS preflight always passes through cleanly so a CORS check is
    # never blocked by the support-session sensitive-paths rule.
    if request.method == "OPTIONS":
        return await _safe_call_next(request, call_next, name="support_session")

    # Bypass for ultra-light + webhook paths so a heavy DB session
    # check never blocks a liveness probe or a 360dialog ack.
    if _is_bypass_path(request.url.path):
        return await _safe_call_next(request, call_next, name="support_session")

    # Not a support session — skip
    if not payload or not payload.get("impersonation"):
        return await _safe_call_next(request, call_next, name="support_session")

    role = payload.get("role", "")
    if role != "support_impersonation":
        # Impersonation=True but wrong role — treat as tampered token
        return JSONResponse(
            status_code=403,
            content={"detail": "رمز الجلسة غير صالح", "code": "invalid_support_token"},
            headers=_cors_error_headers(request),
        )

    tenant_id   = payload.get("tenant_id")
    actor_email = payload.get("actor_sub", "unknown")
    token_sv    = int(payload.get("session_version", -1))
    path        = request.url.path
    ip          = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )

    # ── 1. Verify session_version against DB (revocation check) ────────────────
    try:
        import sys, os  # noqa: E401
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        from core.database import SessionLocal  # noqa: PLC0415
        from core.tenant import get_or_create_settings  # noqa: PLC0415

        with SessionLocal() as db:
            settings = get_or_create_settings(db, int(tenant_id))
            db.commit()
            meta = dict(settings.extra_metadata or {})
            sa   = meta.get("support_access", {})
            db_sv = int(sa.get("session_version", 0))

        if token_sv < db_sv:
            _support_middleware_log.warning(
                "SUPPORT_TOKEN_REVOKED actor=%s tenant=%s sv_token=%d sv_db=%d path=%s ip=%s",
                actor_email, tenant_id, token_sv, db_sv, path, ip,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "تم إلغاء وصول الدعم الفني من قِبَل التاجر. الجلسة منتهية.",
                    "code":   "support_access_revoked",
                },
                headers=_cors_error_headers(request),
            )
    except Exception as _e:
        logger.error("[support_middleware] session_version check failed: %s", _e)
        # Fail-open with a warning rather than blocking the request on a DB error
        _support_middleware_log.warning(
            "SUPPORT_SV_CHECK_FAILED actor=%s tenant=%s path=%s err=%s",
            actor_email, tenant_id, path, _e,
        )

    # ── 2. Block sensitive paths + record attempt ───────────────────────────────
    #
    # Two-tier check:
    #   (a) If the path starts with a blocked prefix AND the
    #       (method, path) tuple is NOT on the explicit read allow-list,
    #       reject with 403.
    #   (b) If it IS on the allow-list (e.g. GET /billing/status),
    #       record an INFO audit line and let the request through.
    #
    # The allow-list is intentionally narrow — only paths whose
    # handlers have been audited to return no mutation side-effects
    # and no card-grade secrets.
    is_blocked_prefix = any(
        path.startswith(blocked) for blocked in _SUPPORT_BLOCKED_PATHS
    )
    is_allowed_read = (request.method.upper(), path) in _SUPPORT_ALLOWED_READS
    if is_blocked_prefix and not is_allowed_read:
        _support_middleware_log.warning(
            "SUPPORT_BLOCKED_SENSITIVE actor=%s tenant=%s path=%s ip=%s",
            actor_email, tenant_id, path, ip,
        )
        # Map path to semantic action name for audit log
        _blocked_action = _path_to_action(path, request.method, blocked=True)
        _write_support_audit_db(
            tenant_id=int(tenant_id),
            action=_blocked_action,
            actor=actor_email,
            status="blocked",
            details={"path": path, "method": request.method, "ip": ip},
        )
        return JSONResponse(
            status_code=403,
            content={
                "detail": (
                    "هذه العملية محظورة خلال جلسة الدعم الفني. "
                    "يجب على التاجر إجراء هذا التغيير بنفسه."
                ),
                "code": "support_sensitive_blocked",
            },
            headers=_cors_error_headers(request),
        )

    # ── 3. Audit log every support request (logger + DB) ───────────────────────
    if is_blocked_prefix and is_allowed_read:
        # Distinct log line for allow-listed reads so audits can grep
        # "SUPPORT_BLOCKED_PREFIX_READ_OK" to enumerate which
        # otherwise-sensitive paths support hit during a session.
        _support_middleware_log.info(
            "SUPPORT_BLOCKED_PREFIX_READ_OK actor=%s tenant=%s "
            "path=%s method=%s ip=%s sv=%d",
            actor_email, tenant_id, path, request.method, ip, token_sv,
        )
    _support_middleware_log.info(
        "SUPPORT_ACCESS actor=%s tenant=%s path=%s method=%s ip=%s sv=%d",
        actor_email, tenant_id, path, request.method, ip, token_sv,
    )
    _action = _path_to_action(path, request.method, blocked=False)
    _write_support_audit_db(
        tenant_id=int(tenant_id),
        action=_action,
        actor=actor_email,
        status="success",
        details={"path": path, "method": request.method, "ip": ip, "sv": token_sv},
    )

    return await _safe_call_next(request, call_next, name="support_session")


# ── Activity Tracking helpers ──────────────────────────────────────────────────

# Maps (path_prefix, method) → semantic action name
_PATH_ACTION_MAP = [
    ("/orders",                    "GET",    "support_view_orders"),
    ("/customers",                 "GET",    "support_view_customers"),
    ("/smart-automations",         "GET",    "support_view_automations"),
    ("/autopilot",                 "GET",    "support_view_automations"),
    ("/settings",                  "GET",    "support_view_settings"),
    ("/templates",                 "GET",    "support_view_templates"),
    ("/campaigns",                 "GET",    "support_view_campaigns"),
    ("/conversations",             "GET",    "support_view_conversations"),
    ("/merchant/customers",        "PATCH",  "support_edit_customer"),
    ("/merchant/customers",        "PUT",    "support_edit_customer"),
    ("/merchant/orders",           "PATCH",  "support_edit_order"),
    ("/merchant/orders",           "PUT",    "support_edit_order"),
    ("/smart-automations",         "PATCH",  "support_edit_automation"),
    ("/smart-automations",         "PUT",    "support_edit_automation"),
    # Blocked paths (will be appended in blocked=True mode)
    ("/campaigns",                 "POST",   "support_attempt_send_campaign"),
    ("/merchant/customers",        "DELETE", "support_attempt_delete_customer"),
    ("/merchant/orders",           "DELETE", "support_attempt_delete_order"),
    ("/billing",                   "POST",   "support_attempt_change_billing"),
    ("/billing",                   "PATCH",  "support_attempt_change_billing"),
    ("/merchant/support-access",   "POST",   "support_view_settings"),  # accessing own grant
]


def _path_to_action(path: str, method: str, *, blocked: bool) -> str:
    """Map a request path + method to a semantic audit action name."""
    for prefix, m, action in _PATH_ACTION_MAP:
        if path.startswith(prefix) and (m == method or m == "*"):
            if blocked and not action.startswith("support_attempt"):
                return f"support_attempt_{path.split('/')[1]}"
            return action
    return "support_view_page" if method == "GET" else "support_action"


def _write_support_audit_db(
    *,
    tenant_id: int,
    action: str,
    actor: str,
    status: str,
    details: dict,
) -> None:
    """
    Write a row to AuditLog for support session activity.
    Non-fatal — any DB error is logged and swallowed so the main request continues.
    Only writes for meaningful actions (skips health checks, static assets, etc.).
    """
    # Skip high-frequency noise paths that aren't meaningful for auditing
    _skip_prefixes = ("/health", "/version", "/merchant/support-access", "/merchant/notifications")
    path = details.get("path", "")
    if any(path.startswith(s) for s in _skip_prefixes):
        return

    try:
        import os as _os, sys as _sys
        _sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))
        from core.database import SessionLocal  # noqa: PLC0415
        from models import AuditLog  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415

        with SessionLocal() as db:
            row = AuditLog(
                tenant_id=tenant_id,
                category="support_activity",
                resource_type="page",
                resource_id=path,
                action=action,
                details={**details, "actor": actor, "status": status},
                created_at=datetime.now(timezone.utc),
            )
            db.add(row)
            db.commit()
    except Exception as exc:
        logger.warning("[support_activity] AuditLog write failed: %s", exc)


# ── Per-route rate limit helper ────────────────────────────────────────────────

def rate_limit(key: str, max_count: int, window_seconds: int) -> None:
    """Raise HTTP 429 if the per-key rate limit is exceeded."""
    sys.path.insert(
        0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../observability"))
    )
    from rate_limiter import check_rate_limit  # noqa: PLC0415
    if not check_rate_limit(key, max_count, window_seconds):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Max {max_count} requests per {window_seconds}s.",
        )
