"""
core/middleware.py
──────────────────
All FastAPI middleware functions and the rate_limit() helper used by route handlers.
Register these in main.py — never import from here in routers.
"""
from __future__ import annotations

import logging
import os
import sys
import time as _time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from core.auth import JWT_AVAILABLE, decode_token
from core.config import API_SECRET_KEY

logger = logging.getLogger("nahla-backend")

# Public path prefixes that never require a JWT token.
# Keep these as specific as possible — broad prefixes can accidentally
# expose protected endpoints under the same prefix.
JWT_PUBLIC_PREFIXES = (
    "/health",
    "/webhook",
    "/auth",
    "/oauth",                           # Salla/WhatsApp OAuth callbacks
    "/integrations/salla/",             # Salla success/error landing HTML pages (public)
    "/salla",                           # /salla/start (new merchant install entry point)
    "/api/salla/test/authorize",        # Salla TEST app OAuth start — public redirect
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
    # ── TEMP DIAG (revert after RCA): one-shot phone_number_id lookup ──────────
    "/admin/troubleshooting/whatsapp/lookup",
)
# NOTE: /integrations/whatsapp/status and /integrations/debug are PROTECTED — JWT required.


# ── Middleware functions ───────────────────────────────────────────────────────

async def multi_tenant_middleware(request: Request, call_next):
    """Read X-Tenant-ID header and attach to request.state (dev routing only)."""
    raw = request.headers.get("X-Tenant-ID")
    try:
        tenant_id = str(int(raw)) if raw is not None else None
    except (ValueError, TypeError):
        tenant_id = None
    request.state.tenant_id = tenant_id
    return await call_next(request)


async def api_key_middleware(request: Request, call_next):
    """Reject unauthenticated service calls without X-Nahla-Key when configured."""
    if API_SECRET_KEY:
        path = request.url.path
        if not (
            path.startswith("/health")
            or path.startswith("/webhook")
            or path.startswith("/auth")
        ):
            auth_header = request.headers.get("Authorization", "")
            has_bearer_token = auth_header.startswith("Bearer ")
            if not has_bearer_token and request.headers.get("X-Nahla-Key", "") != API_SECRET_KEY:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized"},
                    headers=_cors_error_headers(request),
                )
    return await call_next(request)


async def global_rate_limit_middleware(request: Request, call_next):
    """300 requests per minute per IP — exempts /health and /auth."""
    if not (
        request.url.path.startswith("/health")
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
    return await call_next(request)


async def request_logging_middleware(request: Request, call_next):
    """Log HTTP method, path, status code, and latency for every request."""
    start = _time.monotonic()
    response = await call_next(request)
    duration_ms = round((_time.monotonic() - start) * 1000)
    tenant_id = getattr(request.state, "tenant_id", "-")
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )
    logger.info(
        "%s %s %d %dms tenant=%s ip=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        tenant_id,
        client_ip,
    )
    return response


async def salla_iframe_middleware(request: Request, call_next):
    """
    Allow app.nahlah.ai to be embedded in Salla's iframe viewer (s.salla.sa).
    Sets Content-Security-Policy frame-ancestors instead of X-Frame-Options
    so Salla can load the app inside their embedded app viewer.
    """
    response = await call_next(request)
    # Allow embedding only from trusted Salla domains
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors 'self' https://s.salla.sa https://*.salla.sa "
        "https://store.salla.sa https://app.nahlah.ai https://apps.salla.sa"
    )
    # Remove restrictive X-Frame-Options if it was set
    if "x-frame-options" in response.headers:
        del response.headers["x-frame-options"]
    return response


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
        return await call_next(request)

    if any(path.startswith(p) for p in JWT_PUBLIC_PREFIXES):
        return await call_next(request)

    # Public store scripts + store-facing widget APIs — no JWT possible from external stores
    # Pattern: /merchant/widgets/{id}/*.js | *.json | /create-coupon
    if path.startswith("/merchant/widgets/") and (
        path.endswith(".js")
        or path.endswith(".json")
        or path.endswith("/create-coupon")
    ):
        response = await call_next(request)
        # Allow ANY store domain to call these public endpoints (CORS wildcard)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return response

    # Legacy addon embed scripts
    if path.startswith("/merchant/addons/widget/") and path.endswith(".js"):
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
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
    return await call_next(request)


# ── Support-session middleware ─────────────────────────────────────────────────

# Paths that support-impersonation sessions are NEVER allowed to call.
# Keep this list conservative; add paths as new sensitive features are built.
_SUPPORT_BLOCKED_PATHS = (
    # Password / credential changes
    "/auth/change-password",
    "/auth/change-email",
    "/auth/reset-password",
    # Billing and payment — must never be reachable from a support session
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

    # Not a support session — skip
    if not payload or not payload.get("impersonation"):
        return await call_next(request)

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

    # ── 2. Block sensitive paths ────────────────────────────────────────────────
    if any(path.startswith(blocked) for blocked in _SUPPORT_BLOCKED_PATHS):
        _support_middleware_log.warning(
            "SUPPORT_BLOCKED_SENSITIVE actor=%s tenant=%s path=%s ip=%s",
            actor_email, tenant_id, path, ip,
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

    # ── 3. Audit log every support request ─────────────────────────────────────
    _support_middleware_log.info(
        "SUPPORT_ACCESS actor=%s tenant=%s path=%s method=%s ip=%s sv=%d",
        actor_email, tenant_id, path, request.method, ip, token_sv,
    )

    return await call_next(request)


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
