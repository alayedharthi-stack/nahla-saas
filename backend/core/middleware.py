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

# Public path prefixes that never require a JWT token
JWT_PUBLIC_PREFIXES = (
    "/health",
    "/webhook",
    "/auth",
    "/oauth",             # Salla/WhatsApp OAuth callbacks
    "/integrations",      # Salla success/error landing pages
    "/salla",             # /salla/start (new merchant install entry point)
    "/settings/validate", # Salla Partner Portal validation probe
    "/snippet.js",
    "/track",
)


# ── Middleware functions ───────────────────────────────────────────────────────

async def multi_tenant_middleware(request: Request, call_next):
    """Read X-Tenant-ID header and attach to request.state (dev routing only)."""
    raw = request.headers.get("X-Tenant-ID", "1")
    try:
        tenant_id = str(int(raw))
    except (ValueError, TypeError):
        tenant_id = "1"
    request.state.tenant_id = tenant_id
    return await call_next(request)


async def api_key_middleware(request: Request, call_next):
    """Reject requests without X-Nahla-Key header when API_SECRET_KEY is configured."""
    if API_SECRET_KEY:
        path = request.url.path
        if not (
            path.startswith("/health")
            or path.startswith("/webhook")
            or path.startswith("/auth")
        ):
            if request.headers.get("X-Nahla-Key", "") != API_SECRET_KEY:
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
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
            return JSONResponse(status_code=429, content={"detail": "Too many requests"})
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


async def jwt_enforcement_middleware(request: Request, call_next):
    """
    Require a valid JWT for all non-public routes.
    On success: attaches the decoded payload to request.state.jwt_payload and
    overrides request.state.tenant_id from the token claim (prevents header spoofing).
    """
    path = request.url.path

    # Always pass through CORS preflight without a token
    if request.method == "OPTIONS":
        return await call_next(request)

    if any(path.startswith(p) for p in JWT_PUBLIC_PREFIXES):
        return await call_next(request)

    if not JWT_AVAILABLE:
        logger.warning("JWT enforcement skipped — python-jose not installed")
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required", "code": "missing_token"},
        )

    payload = decode_token(auth_header[7:])
    if not payload:
        return JSONResponse(
            status_code=401,
            content={"detail": "Token expired or invalid", "code": "invalid_token"},
        )

    request.state.jwt_payload = payload
    request.state.tenant_id = str(payload.get("tenant_id", 1))
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
