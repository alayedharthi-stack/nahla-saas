"""
routers/salla_oauth.py
───────────────────────
Salla OAuth 2.0 flow and store data endpoints.

Routes (public — no JWT required on OAuth paths)
  GET  /api/salla/authorize         ← returns auth URL to frontend
  GET  /oauth/salla/callback        ← Salla redirects here with ?code=
  GET  /integrations/salla/success  ← success landing (public, shown inside iframe)
  GET  /integrations/salla/error    ← error landing  (public, shown inside iframe)

Routes (protected — JWT required)
  GET  /api/salla/store
  GET  /api/salla/products
  POST /api/salla/test-coupon

OAuth Flow:
  1. Merchant clicks "Connect Salla" in dashboard
  2. Frontend calls GET /api/salla/authorize → gets authorize_url
  3. Browser opens authorize_url → Salla asks merchant to approve
  4. Salla redirects to /oauth/salla/callback?code=XXX&state=TENANT_ID
  5. Backend exchanges code → tokens, saves to DB
  6. Redirect to SALLA_EMBEDDED_URL?status=connected  (success)
       or SALLA_EMBEDDED_URL?status=error&reason=XXX  (failure)
"""
from __future__ import annotations

import logging
import os
import secrets as _secrets
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from models import Integration, Product, SmartAutomation, Tenant, User, WhatsAppConnection

from core.audit import audit
from core.auth import create_token, get_jwt_tenant_id, hash_password
from core.config import (
    DASHBOARD_URL,
    SALLA_CLIENT_ID,
    SALLA_CLIENT_SECRET,
    SALLA_EMBEDDED_URL,
    SALLA_REDIRECT_URI,
    SALLA_TEST_CLIENT_ID,
    SALLA_TEST_CLIENT_SECRET,
    SALLA_TEST_REDIRECT_URI,
)
from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id

logger = logging.getLogger("nahla-backend")

router = APIRouter(tags=["Salla OAuth"])

# ── URL helpers ────────────────────────────────────────────────────────────────

# Dashboard URL for internal redirects (e.g. after store settings save)
_DASHBOARD = DASHBOARD_URL.split("=", 1)[-1] if "=" in DASHBOARD_URL else DASHBOARD_URL
_DASHBOARD = _DASHBOARD.rstrip("/") or "https://app.nahlah.ai"

# Salla embedded app landing page — where to redirect after OAuth
# This must be the iframe URL registered in Salla partner portal
_SALLA_APP  = SALLA_EMBEDDED_URL.rstrip("/")

# Dashboard origin (always app.nahlah.ai in prod) — used to build the
# post-OAuth redirect URL.  We deliberately use DASHBOARD_URL (origin only)
# instead of SALLA_EMBEDDED_URL because the latter may include a path like
# "/app/salla" which would break the rsplit("/") logic that used to derive
# the callback base.  Explicit origin → predictable URLs.
_DASHBOARD_ORIGIN = _DASHBOARD or "https://app.nahlah.ai"

# Always land on the Salla mini-dashboard after OAuth — never on /landing
# (which is the public marketing/registration page) and never on the iframe
# embedded URL (which only works inside Salla's iframe context).
_SALLA_POST_OAUTH_PATH = "/app/entry"

# The Salla callback page on the dashboard (for new merchants auto-logged in
# via Salla — it stores the JWT in localStorage then routes to /app/entry).
_SALLA_CALLBACK_BASE = _DASHBOARD_ORIGIN

# Prefix used in state param to identify new-merchant installs from Salla
_NEW_MERCHANT_PREFIX = "salla_new_"


def _success_url(store_id: str = "", store_name: str = "") -> str:
    """Build the post-OAuth success redirect URL.

    Always lands on /app/entry (the Salla mini-dashboard), NEVER on /landing
    or on the iframe URL.  The merchant has just authorised the app, so they
    have a valid JWT in localStorage already (set during the earlier
    salla_token_login call) and can enter the dashboard directly.
    """
    params = urllib.parse.urlencode({
        "status": "connected",
        "store":  store_id,
        "name":   store_name,
    })
    return f"{_DASHBOARD_ORIGIN}{_SALLA_POST_OAUTH_PATH}?{params}"


def _error_url(reason: str, detail: str = "") -> str:
    """Build the post-OAuth error redirect URL — also lands on /app/entry
    so the merchant can retry from inside the mini-dashboard instead of
    being dumped on the marketing landing page."""
    params: dict = {"status": "error", "reason": reason}
    if detail:
        params["detail"] = detail[:200]   # truncate to avoid oversized URLs
    return f"{_DASHBOARD_ORIGIN}{_SALLA_POST_OAUTH_PATH}?{urllib.parse.urlencode(params)}"


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC ROUTES — OAuth flow (no JWT required)
# ═══════════════════════════════════════════════════════════════════════════════


# ── Salla Embedded Token Login ─────────────────────────────────────────────────

@router.post("/salla/token-login")
async def salla_token_login(request: Request, db: Session = Depends(get_db)):
    """
    PUBLIC — no JWT required.

    Official entry point for every merchant who opens Nahla from inside Salla.

    ─────────────────────────────────────────────────────────────
    MULTI-TENANT GUARANTEES:
      • Each Salla store gets its own Tenant row (tenant_id is unique per store)
      • Each JWT contains tenant_id in claims — cannot be spoofed
      • Middleware enforces tenant_id from JWT on every API call
      • Admin account is NEVER returned here — only role=merchant tokens
    ─────────────────────────────────────────────────────────────

    Flow:
      1. Receive Salla embedded token (v4.public.*) + app_id
      2. Introspect via Salla API  →  get merchant/store identity
      3. Look up or create isolated Tenant + User for this store
      4. Issue Nahla JWT { sub, role, tenant_id }
      5. Return JWT so /salla/app can build link to /salla-callback
    """
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "?"
    )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    salla_token = (body.get("token") or "").strip()
    app_id      = str(body.get("app_id") or SALLA_CLIENT_ID or "")

    if not salla_token:
        raise HTTPException(status_code=400, detail="token required")

    # Mask token for logs — show only first 20 chars
    token_preview = salla_token[:20] + "…"
    logger.info(
        "[SallaLogin] ▶ STEP 1 — Salla token received | ip=%s app_id=%s token=%s",
        client_ip, app_id, token_preview,
    )

    # ══════════════════════════════════════════════════════════════
    # STEP 2 — Introspect the Salla embedded token
    # ══════════════════════════════════════════════════════════════
    merchant_id_str = ""
    store_name      = ""
    owner_email     = ""
    introspect_ok   = False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.salla.dev/exchange-authority/v1/introspect",
                json={
                    "env":     "prod",
                    "token":   salla_token,
                    "iss":     "merchant-dashboard",
                    "subject": "embedded-page",
                },
                headers={
                    "S-Source":     app_id,
                    "Content-Type": "application/json",
                    "Accept":       "application/json",
                },
            )

            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    introspect_ok   = True
                    payload_data    = data.get("data") or {}
                    merchant        = payload_data.get("merchant") or {}

                    # Handle multiple possible Salla response shapes
                    merchant_id_str = str(
                        merchant.get("id")              or
                        payload_data.get("merchant_id") or
                        payload_data.get("store_id")    or
                        ""
                    )
                    store_name  = (
                        merchant.get("name")       or
                        payload_data.get("store_name") or
                        ""
                    )
                    owner_email = (
                        merchant.get("email")      or
                        payload_data.get("email")  or
                        merchant.get("mobile")     or
                        ""
                    ).strip().lower()

                    logger.info(
                        "[SallaLogin] ✅ STEP 2 — Introspect SUCCESS | "
                        "merchant_id=%s store=%r email=%s",
                        merchant_id_str, store_name, owner_email,
                    )
                else:
                    logger.warning(
                        "[SallaLogin] ⚠️  STEP 2 — Introspect returned success=false | body=%.300s",
                        resp.text,
                    )
            else:
                logger.warning(
                    "[SallaLogin] ⚠️  STEP 2 — Introspect HTTP %s | body=%.200s",
                    resp.status_code, resp.text,
                )
    except Exception as exc:
        logger.error("[SallaLogin] ❌ STEP 2 — Introspect call raised: %s", exc)

    # ══════════════════════════════════════════════════════════════
    # STEP 3 — Derive identity (fallback if introspect gave no email)
    # ══════════════════════════════════════════════════════════════
    email_is_derived = False
    if not owner_email and merchant_id_str:
        safe_name    = "".join(c for c in store_name if c.isalnum() or c in "-_").lower()[:30]
        owner_email  = f"{safe_name or 'store'}-{merchant_id_str}@salla-merchant.nahlah.ai"
        email_is_derived = True   # derived — cannot trust user-email lookup
        logger.info(
            "[SallaLogin] ℹ️  STEP 3 — No email from Salla, using derived: %s",
            owner_email,
        )

    if not owner_email:
        logger.error(
            "[SallaLogin] ❌ Cannot identify merchant — introspect_ok=%s merchant_id=%s",
            introspect_ok, merchant_id_str,
        )
        raise HTTPException(
            status_code=401,
            detail="Could not identify merchant from Salla token. "
                   "Please install the app via Salla store to link your account.",
        )

    logger.info(
        "[SallaLogin] ▶ STEP 4 — Resolving Nahla account | email=%s merchant_id=%s",
        owner_email, merchant_id_str,
    )

    # ══════════════════════════════════════════════════════════════
    # STEP 4 — Find or create isolated Tenant + User
    # ══════════════════════════════════════════════════════════════
    #
    # RULE: salla_store_id (merchant_id_str) is the AUTHORITATIVE key
    # for tenant resolution.  Email is a secondary fallback ONLY when
    # no store_id is available.  Two stores that share the same partner
    # email MUST get separate tenants.
    #
    # Priority:
    #   1. Integration by external_store_id  → tenant_id
    #   2. Integration by config JSONB       → tenant_id  (legacy repair)
    #   3. Email lookup                      → tenant_id  (ONLY if no store_id)
    #   4. Create new Tenant                 → new tenant_id
    try:
        existing_integration = None

        # ── Priority 1-3: store_id-based Integration lookup ──────────────
        if merchant_id_str:
            existing_integration = db.query(Integration).filter(
                Integration.provider == "salla",
                Integration.external_store_id == str(merchant_id_str),
            ).first()

            if not existing_integration:
                existing_integration = db.query(Integration).filter(
                    Integration.provider == "salla",
                    Integration.config["store_id"].as_string() == str(merchant_id_str),
                ).first()
                if existing_integration:
                    existing_integration.external_store_id = str(merchant_id_str)
                    logger.info(
                        "[SallaLogin]    Repaired external_store_id for integration id=%s "
                        "tenant=%s store=%s",
                        existing_integration.id, existing_integration.tenant_id, merchant_id_str,
                    )

            if not existing_integration:
                existing_integration = db.query(Integration).filter(
                    Integration.provider == "salla",
                    Integration.config["salla_merchant_id_alt"].as_string() == str(merchant_id_str),
                ).first()
                if existing_integration:
                    logger.info(
                        "[SallaLogin]    Matched via salla_merchant_id_alt=%s "
                        "tenant=%s integration=%s",
                        merchant_id_str, existing_integration.tenant_id, existing_integration.id,
                    )

        if existing_integration:
            # ── Returning store — tenant already exists ─────────────────────
            tenant_id = existing_integration.tenant_id
            role      = "merchant"
            is_new    = False

            stored_email = (
                (existing_integration.config or {}).get("salla_owner_email")
            )
            canonical_email = stored_email or owner_email

            linked_user = (
                db.query(User)
                .filter(
                    User.tenant_id == tenant_id,
                    User.email     == canonical_email,
                )
                .first()
            )
            if linked_user:
                owner_email = linked_user.email
                logger.info(
                    "[SallaLogin] ✅ STEP 4 — Linked user found by store email | "
                    "store_id=%s tenant_id=%s user=%s",
                    merchant_id_str, tenant_id, owner_email,
                )
            else:
                owner_email = canonical_email
                logger.info(
                    "[SallaLogin] ✅ STEP 4 — TENANT FOUND, creating user | "
                    "store_id=%s tenant_id=%s email=%s",
                    merchant_id_str, tenant_id, owner_email,
                )

        elif not merchant_id_str and not email_is_derived:
            # ── Fallback: no store_id available, try email ─────────────────
            # This path only fires when Salla introspect failed to return
            # a merchant/store ID.  Email is the last resort.
            existing_user = db.query(User).filter(User.email == owner_email).first()
            if existing_user:
                tenant_id = existing_user.tenant_id
                role      = existing_user.role or "merchant"
                is_new    = False
                logger.info(
                    "[SallaLogin] ✅ STEP 4 — TENANT FOUND (email fallback, no store_id) | "
                    "email=%s tenant_id=%s",
                    owner_email, tenant_id,
                )
            else:
                # No store_id AND no user — create new tenant
                unique_name = store_name or "متجر سلة"
                new_tenant  = Tenant(name=unique_name)
                db.add(new_tenant)
                db.flush()
                tenant_id = new_tenant.id
                role      = "merchant"
                is_new    = True

                db.add(User(
                    username      = owner_email.split("@")[0],
                    email         = owner_email,
                    password_hash = hash_password(_secrets.token_urlsafe(16)),
                    role          = role,
                    tenant_id     = tenant_id,
                    is_active     = True,
                ))
                db.flush()
                logger.info(
                    "[SallaLogin] ✅ STEP 4 — TENANT CREATED (no store_id, email-only) | "
                    "email=%s tenant_id=%s",
                    owner_email, tenant_id,
                )

        else:
            # ── First-time store: create isolated Tenant + User ────────────
            unique_name = f"{store_name or 'متجر سلة'}-{merchant_id_str}" if merchant_id_str else (store_name or "متجر سلة")
            new_tenant = Tenant(name=unique_name)
            db.add(new_tenant)
            db.flush()
            tenant_id = new_tenant.id
            role      = "merchant"
            is_new    = True

            # Ensure unique email per tenant — if a user with this email
            # already exists in another tenant, derive a store-scoped email.
            if db.query(User).filter(User.email == owner_email).first():
                safe_name   = "".join(c for c in store_name if c.isalnum() or c in "-_").lower()[:30]
                owner_email = f"{safe_name or 'store'}-{merchant_id_str}@salla-merchant.nahlah.ai"
                logger.info(
                    "[SallaLogin]    Email already exists in another tenant — "
                    "using store-scoped email: %s",
                    owner_email,
                )

            new_user = User(
                username      = owner_email.split("@")[0],
                email         = owner_email,
                password_hash = hash_password(_secrets.token_urlsafe(16)),
                role          = role,
                tenant_id     = tenant_id,
                is_active     = True,
            )
            db.add(new_user)
            db.flush()

            logger.info(
                "[SallaLogin] ✅ STEP 4 — TENANT CREATED (new store) | "
                "email=%s tenant_id=%s store=%r store_id=%s",
                owner_email, tenant_id, store_name, merchant_id_str,
            )

        # ── Save / update Salla integration record ────────────────────────────
        if merchant_id_str:
            integration = db.query(Integration).filter(
                Integration.provider == "salla",
                Integration.external_store_id == str(merchant_id_str),
            ).first()

            now_iso = datetime.now(timezone.utc).isoformat()

            # The embedded Salla token CAN be used as a temporary api_key for
            # most read operations (products, orders, customers, store info).
            # We save it so the merchant sees the integration as "connected"
            # immediately on first login, even before the full OAuth refresh
            # token arrives via the app.store.authorize webhook.
            embedded_api_key = salla_token  # the v4.public.* token from the iframe

            if integration:
                cfg = dict(integration.config or {})
                # Keep existing long-lived api_key if present, else use embedded
                existing_key = cfg.get("api_key", "")
                cfg.update({
                    "store_id":          merchant_id_str,
                    "store_name":        store_name,
                    "last_seen":         now_iso,
                    "salla_owner_email": owner_email,
                })
                if not existing_key and embedded_api_key:
                    cfg["api_key"] = embedded_api_key
                    cfg["api_key_source"] = "embedded_token"
                    cfg["api_key_received_at"] = now_iso
                integration.config = cfg
                integration.external_store_id = merchant_id_str
                # Mark as enabled if we have ANY usable api_key
                if cfg.get("api_key"):
                    integration.enabled = True
                logger.info(
                    "[SallaLogin]    Integration UPDATED | tenant=%s store_id=%s enabled=%s api_key_source=%s",
                    tenant_id, merchant_id_str, integration.enabled,
                    cfg.get("api_key_source", "oauth"),
                )
            else:
                db.add(Integration(
                    tenant_id = tenant_id,
                    provider  = "salla",
                    external_store_id = merchant_id_str,
                    config    = {
                        "store_id":             merchant_id_str,
                        "store_name":           store_name,
                        "salla_token_login":    True,
                        "connected_at":         now_iso,
                        "salla_owner_email":    owner_email,
                        "api_key":              embedded_api_key,
                        "api_key_source":       "embedded_token",
                        "api_key_received_at":  now_iso,
                    },
                    enabled = bool(embedded_api_key),
                ))
                logger.info(
                    "[SallaLogin]    Integration CREATED | tenant=%s store_id=%s "
                    "enabled=%s api_key_source=embedded_token",
                    tenant_id, merchant_id_str, bool(embedded_api_key),
                )

        db.commit()

    except Exception as exc:
        logger.exception("[SallaLogin] ❌ STEP 4 — DB error: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Database error during account setup")

    # ══════════════════════════════════════════════════════════════
    # STEP 5 — Issue Nahla JWT (must carry user_id for tenant isolation)
    # ══════════════════════════════════════════════════════════════
    db_user   = db.query(User).filter(User.email == owner_email).first()
    db_user_id = db_user.id if db_user else None
    nahla_jwt = create_token(
        email=owner_email, role=role, tenant_id=tenant_id, user_id=db_user_id
    )

    # Check WhatsApp connection status for smart redirect
    wa_conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    wa_connected = bool(wa_conn and wa_conn.status == "connected" and wa_conn.sending_enabled)

    # All Salla merchants land on the mini-dashboard (/app/entry).
    # FUTURE: new merchants will first see /app/pricing for plan selection.
    redirect_target = "/app/entry"

    logger.info(
        "[SallaLogin] ✅ STEP 5 — JWT ISSUED | "
        "tenant_id=%s role=%s is_new=%s wa_connected=%s redirect=%s",
        tenant_id, role, is_new, wa_connected, redirect_target,
    )
    logger.info(
        "[SallaLogin] ══ COMPLETE ═══ merchant=%s tenant=%s wa=%s → %s",
        owner_email, tenant_id, wa_connected, redirect_target,
    )

    # ── Detect if OAuth tokens are missing (embedded-only) ───────────────────
    # The embedded token (v4.public.*) cannot call Salla's /admin/v2/* APIs.
    # Without a real OAuth refresh_token, the sync of products/orders/customers
    # will fail. We tell the frontend to redirect to OAuth flow.
    needs_oauth = False
    oauth_url   = ""
    try:
        check_integ = db.query(Integration).filter(
            Integration.tenant_id == tenant_id,
            Integration.provider == "salla",
        ).first()
        if check_integ:
            cfg = check_integ.config or {}
            has_refresh   = bool(cfg.get("refresh_token"))
            api_key_src   = cfg.get("api_key_source", "")
            # OAuth needed if no refresh_token OR api_key is just the embedded one
            if not has_refresh or api_key_src == "embedded_token":
                needs_oauth = True
                # Build OAuth authorize URL with state pointing to this tenant
                if SALLA_CLIENT_ID and SALLA_REDIRECT_URI:
                    # Normalize redirect_uri: strip trailing slash and whitespace
                    # to match Salla Partner Portal's exact-match requirement.
                    normalized_redirect = SALLA_REDIRECT_URI.strip().rstrip("/")
                    state = f"t{tenant_id}_{_secrets.token_urlsafe(6)}"
                    params = urllib.parse.urlencode({
                        "client_id":     SALLA_CLIENT_ID,
                        "redirect_uri":  normalized_redirect,
                        "response_type": "code",
                        "scope":         "offline_access",
                        "state":         state,
                    })
                    oauth_url = f"https://accounts.salla.sa/oauth2/auth?{params}"
                    logger.info(
                        "[SallaLogin] needs_oauth=True | tenant=%s | "
                        "client_id=%s | redirect_uri=%r (raw env=%r) | "
                        "has_refresh=%s api_key_source=%s",
                        tenant_id,
                        (SALLA_CLIENT_ID[:8] + "***") if SALLA_CLIENT_ID else "EMPTY",
                        normalized_redirect, SALLA_REDIRECT_URI,
                        has_refresh, api_key_src,
                    )
                    logger.info("[SallaLogin] FULL oauth_url=%s", oauth_url)
                else:
                    logger.error(
                        "[SallaLogin] needs_oauth=True BUT cannot build oauth_url | "
                        "SALLA_CLIENT_ID=%s SALLA_REDIRECT_URI=%r",
                        bool(SALLA_CLIENT_ID), SALLA_REDIRECT_URI,
                    )
    except Exception as _e:
        logger.warning("[SallaLogin] needs_oauth check failed: %s", _e)

    # ── Trigger initial Salla sync (fire-and-forget) ONLY if OAuth tokens present ─
    # Without OAuth tokens, sync will fail (embedded token can't call /admin/v2/*).
    if is_new and tenant_id and not needs_oauth:
        try:
            import asyncio as _asyncio  # noqa: PLC0415

            async def _initial_sync_token_login(tid: int):
                await _asyncio.sleep(2)
                from core.database import get_db as _gdb  # noqa: PLC0415
                from services.store_sync import StoreSyncService  # noqa: PLC0415
                _db = next(_gdb())
                try:
                    svc = StoreSyncService(_db, tid)
                    result = await svc.full_sync(triggered_by="salla_token_login_first_install")
                    logger.info(
                        "[SallaLogin] Initial sync done | tenant=%s status=%s",
                        tid, result.get("status"),
                    )
                except Exception as exc:
                    logger.error(
                        "[SallaLogin] Initial sync failed | tenant=%s: %s", tid, exc,
                    )
                finally:
                    _db.close()

            _asyncio.ensure_future(_initial_sync_token_login(tenant_id))
            logger.info("[SallaLogin] Initial sync queued | tenant=%s", tenant_id)
        except Exception as _exc:
            logger.warning("[SallaLogin] Could not queue initial sync: %s", _exc)

    return {
        "access_token":   nahla_jwt,
        "role":           role,
        "tenant_id":      tenant_id,
        "store_name":     store_name,
        "store_id":       merchant_id_str,
        "email":          owner_email,
        "needs_oauth":    needs_oauth,
        "oauth_url":      oauth_url,
        "is_new":         is_new,
        "wa_connected":   wa_connected,
        "redirect_to":    redirect_target,
    }


@router.post("/salla/session/launch-dashboard")
async def launch_dashboard(request: Request, db: Session = Depends(get_db)):
    """
    PUBLIC — accepts the merchant's existing Nahla JWT in the request body
    (NOT the Authorization header) so it works from inside the Salla iframe
    even when the embedded session uses different cookies / CORS rules.

    Issues a one-time short-lived launch token (120 s) that the frontend can
    embed in a URL opened via target="_top".  The launch page exchanges it for
    a full-lifetime session token without showing a login screen.

    Body: { "token": "<merchant's JWT>", "next": "/overview" }
    OR    Authorization: Bearer ... (legacy path)

    Response:
      { "launch_url": "https://app.nahlah.ai/app/salla/launch?token=...&next=..." }
    """
    from core.config import JWT_SECRET, JWT_ALGORITHM  # noqa: PLC0415

    # Try body first, then Authorization header (backward compatible)
    body_token = ""
    body_next  = ""
    try:
        body = await request.json()
        body_token = str(body.get("token") or "").strip()
        body_next  = str(body.get("next")  or "").strip()
    except Exception:
        body = {}

    auth_header = request.headers.get("Authorization", "")
    header_token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""

    nahla_jwt_input = body_token or header_token
    next_path       = body_next or str(request.query_params.get("next", "/overview"))

    if not nahla_jwt_input:
        logger.warning("[LaunchDashboard] No JWT provided (neither body nor Authorization)")
        raise HTTPException(status_code=401, detail="JWT required")

    # Validate the JWT manually (independent of middleware)
    try:
        import jose.jwt as _jose_jwt  # noqa: PLC0415
        decoded = _jose_jwt.decode(nahla_jwt_input, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception as _e:
        logger.warning("[LaunchDashboard] JWT decode failed: %s", _e)
        raise HTTPException(status_code=401, detail="invalid_or_expired_token")

    tenant_id = int(decoded.get("tenant_id", 0))
    email     = str(decoded.get("sub", ""))
    role      = str(decoded.get("role", "merchant"))
    user_id   = decoded.get("user_id")

    if not tenant_id:
        logger.warning("[LaunchDashboard] JWT missing tenant_id")
        raise HTTPException(status_code=401, detail="token_missing_tenant")

    # Fetch user_id if missing
    if not user_id:
        from models import User  # noqa: PLC0415
        u = db.query(User).filter(User.email == email).first()
        user_id = u.id if u else None

    # Build SHORT-LIVED launch token (120 s) with marker
    from datetime import timedelta, timezone  # noqa: PLC0415
    launch_payload = {
        "sub":          email,
        "role":         role,
        "tenant_id":    tenant_id,
        "user_id":      user_id,
        "launch_token": True,
        "exp":          int((datetime.now(timezone.utc) + timedelta(seconds=120)).timestamp()),
    }
    try:
        import jose.jwt as _jose_jwt  # noqa: PLC0415
        launch_jwt = _jose_jwt.encode(launch_payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    except Exception as _e:
        logger.error("[LaunchDashboard] Failed to encode launch token: %s", _e)
        raise HTTPException(status_code=500, detail="token_encoding_failed")

    import urllib.parse as _up  # noqa: PLC0415
    params = _up.urlencode({"token": launch_jwt, "next": next_path})
    launch_url = f"{_DASHBOARD.rstrip('/')}/app/salla/launch?{params}"

    logger.info(
        "[LaunchDashboard] launch_url issued | tenant=%s email=%s next=%s",
        tenant_id, email, next_path,
    )
    return {"launch_url": launch_url}


@router.post("/salla/session/resolve-launch")
async def resolve_launch(request: Request, db: Session = Depends(get_db)):
    """
    PUBLIC — no auth header required.

    Accepts a short-lived launch token from the request body, validates it,
    and returns a full-lifetime Nahla JWT together with user metadata.

    The frontend launch page (/app/salla/launch) calls this endpoint,
    stores the returned token in localStorage, then replaces the URL
    (hiding the token) and navigates to the `next` path.

    Request body: { "token": "<launch_jwt>" }
    Response:     { "access_token": "...", "tenant_id": ..., "email": "...",
                    "role": "...", "store_name": "..." }
    """
    from core.config import JWT_SECRET, JWT_ALGORITHM  # noqa: PLC0415
    from models import User, Integration  # noqa: PLC0415

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="token required")

    # Validate the launch JWT
    try:
        import jose.jwt as _jose_jwt  # noqa: PLC0415
        decoded = _jose_jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception as _e:
        logger.warning("[ResolveLaunch] Invalid or expired token: %s", _e)
        raise HTTPException(
            status_code=401,
            detail="تعذر تسجيل الدخول من سلة، حاول فتح التطبيق مرة أخرى.",
        )

    # Extra safety: the token MUST carry the launch_token marker so that
    # normal session tokens cannot be submitted here to mint a second session.
    if not decoded.get("launch_token"):
        raise HTTPException(
            status_code=401,
            detail="تعذر تسجيل الدخول من سلة، حاول فتح التطبيق مرة أخرى.",
        )

    email     = str(decoded.get("sub", ""))
    role      = str(decoded.get("role", "merchant"))
    tenant_id = int(decoded.get("tenant_id", 0))

    if not tenant_id or not email:
        raise HTTPException(status_code=401, detail="بيانات الجلسة غير مكتملة.")

    # Fetch user_id (may be absent from token if legacy)
    db_user = db.query(User).filter(User.email == email).first()
    user_id = db_user.id if db_user else decoded.get("user_id")

    # Fetch store name from integration config for localStorage persistence
    integ = (
        db.query(Integration)
        .filter(Integration.tenant_id == tenant_id, Integration.provider == "salla")
        .first()
    )
    store_name = ((integ.config or {}).get("store_name") or "") if integ else ""

    # Issue a FULL-LIFETIME session token (no launch_token marker)
    full_jwt = create_token(
        email=email,
        role=role,
        tenant_id=tenant_id,
        user_id=user_id,
    )

    logger.info(
        "[ResolveLaunch] Full session issued | tenant=%s email=%s",
        tenant_id, email,
    )
    return {
        "access_token": full_jwt,
        "tenant_id":    tenant_id,
        "email":        email,
        "role":         role,
        "store_name":   store_name,
    }


@router.get("/salla/whoami")
async def salla_whoami(request: Request, db: Session = Depends(get_db)):
    """
    PROTECTED — requires valid Nahla JWT.

    Returns the identity and isolation proof for the currently-authenticated merchant.
    Use this to verify multi-tenant isolation:

      curl -H "Authorization: Bearer <JWT>" https://api.nahlah.ai/salla/whoami

    Two different merchants MUST see different tenant_id values.
    """
    from core.auth import require_authenticated  # noqa: PLC0415

    payload = require_authenticated(request)
    tenant_id  = int(payload.get("tenant_id", 0))
    email      = payload.get("sub", "")
    role       = payload.get("role", "")

    # Fetch tenant name
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    tenant_name = tenant.name if tenant else "?"

    # Fetch Salla integration for this tenant only
    salla_int = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
        Integration.provider  == "salla",
    ).first()
    salla_cfg = (salla_int.config or {}) if salla_int else {}
    salla_store_id   = salla_cfg.get("store_id", "?") if salla_int else "not_connected"
    salla_store_name = salla_cfg.get("store_name", "?") if salla_int else "not_connected"
    salla_enabled    = bool(salla_int.enabled) if salla_int else False
    salla_has_api    = bool(salla_cfg.get("api_key")) if salla_int else False
    salla_needs_reauth = bool(salla_cfg.get("needs_reauth")) if salla_int else False

    return {
        "isolation_check": "OK — you see ONLY your tenant data",
        "jwt_claims": {
            "email":     email,
            "role":      role,
            "tenant_id": tenant_id,
        },
        "tenant": {
            "id":   tenant_id,
            "name": tenant_name,
        },
        "salla_integration": {
            "store_id":    salla_store_id,
            "store_name":  salla_store_name,
            "enabled":     salla_enabled,
            "has_api_key": salla_has_api,
            "needs_reauth": salla_needs_reauth,
            "connected":   salla_enabled and salla_has_api and salla_store_id not in ("?", "not_connected", "") and not salla_needs_reauth,
        },
        "security_note": (
            "tenant_id comes from the JWT claims only — "
            "cannot be changed by the client or request headers."
        ),
    }


# ── Session Check (Zero-Friction Entry) ───────────────────────────────────────

@router.get("/api/salla/session")
async def salla_check_session(request: Request, db: Session = Depends(get_db)):
    """
    PROTECTED — requires valid Nahla JWT in Authorization header.

    Used by /app/salla at startup: if a live session exists the merchant is
    routed directly to the dashboard without re-running the token-exchange.

    Also used by /app/entry to fetch merchant readiness state in one call.

    Optional query param ?store_id= — when present, the endpoint verifies
    that the JWT's tenant actually owns this Salla store. If not, 401 is
    returned so the frontend can force a fresh token-login, preventing
    cross-tenant data leakage when a user switches between stores.

    Returns 200 {
      connected, tenant_id, token,
      whatsapp_connected, has_automations, has_products, store_name
    }
    Returns 401 if expired / missing / store mismatch.
    """
    try:
        payload = require_authenticated(request)
    except HTTPException:
        raise HTTPException(status_code=401, detail="session_expired")

    tenant_id = int(payload.get("tenant_id", 0))
    user_id   = payload.get("user_id")
    email     = str(payload.get("sub", ""))
    role      = str(payload.get("role", "merchant"))

    if not tenant_id:
        raise HTTPException(status_code=401, detail="invalid_tenant")

    # ── Store-isolation guard ────────────────────────────────────────────────
    # If the caller passed ?store_id=, verify the JWT's tenant actually
    # owns that Salla store.  A mismatch means the old token is for a
    # DIFFERENT store → reject so the frontend does a fresh token-login.
    requested_store = str(request.query_params.get("store_id") or "").strip()
    if requested_store:
        matching_integ = db.query(Integration).filter(
            Integration.tenant_id == tenant_id,
            Integration.provider  == "salla",
            Integration.external_store_id == requested_store,
        ).first()
        if not matching_integ:
            logger.warning(
                "[SallaSession] store_id mismatch — JWT tenant=%s does not own store=%s",
                tenant_id, requested_store,
            )
            raise HTTPException(status_code=401, detail="store_mismatch")

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=401, detail="tenant_not_found")

    # Rolling session — re-issue a fresh JWT
    fresh_token = create_token(
        email=email,
        role=role,
        tenant_id=tenant_id,
        user_id=int(user_id) if user_id is not None else None,
    )

    # ── Merchant readiness probes (cheap point-queries) ────────────────────
    wa_conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == tenant_id)
        .first()
    )
    wa_connected = bool(wa_conn and wa_conn.status == "connected")

    has_automations = (
        db.query(SmartAutomation.id)
        .filter(
            SmartAutomation.tenant_id == tenant_id,
            SmartAutomation.enabled.is_(True),
        )
        .first()
    ) is not None

    has_products = (
        db.query(Product.id)
        .filter(Product.tenant_id == tenant_id)
        .limit(1)
        .first()
    ) is not None

    logger.info(
        "[SallaSession] ✅ tenant=%s wa=%s autos=%s products=%s",
        tenant_id, wa_connected, has_automations, has_products,
    )

    return {
        "connected":          True,
        "tenant_id":          tenant_id,
        "token":              fresh_token,
        "whatsapp_connected": wa_connected,
        "has_automations":    has_automations,
        "has_products":       has_products,
    }


@router.post("/api/salla/activate-from-email")
async def salla_activate_from_email(request: Request, db: Session = Depends(get_db)):
    """
    PUBLIC — no JWT required.

    Salla's process: when a merchant installs the app, Salla sends the
    merchant's embedded token + info to the partner's email.  The admin (or
    an email-link clicked by the merchant) calls this endpoint to activate
    the account without requiring the merchant to go through the full OAuth
    flow again.

    Body:
        token         — Salla embedded / access token
        merchant_email — hint used when introspection fails
        store_id       — optional Salla store ID hint

    Returns the same payload as /salla/token-login so the frontend can
    persist the session and navigate to /app/entry.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    salla_token    = (body.get("token")          or "").strip()
    merchant_email = (body.get("merchant_email") or "").strip().lower()
    store_id_hint  = str(body.get("store_id") or "")

    if not salla_token:
        raise HTTPException(status_code=400, detail="token required")

    logger.info(
        "[SallaActivate] ▶ activate-from-email | email=%s store=%s",
        merchant_email, store_id_hint,
    )

    # ── Try Salla token introspection (same path as token-login) ──────────
    merchant_id_str = store_id_hint
    store_name      = ""
    owner_email     = merchant_email
    introspect_ok   = False

    try:
        app_id = str(body.get("app_id") or SALLA_CLIENT_ID or "")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.salla.dev/exchange-authority/v1/introspect",
                json={"token": salla_token, "app_id": app_id},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        if resp.status_code == 200:
            idata = resp.json()
            data  = idata.get("data", idata)
            merchant_id_str = str(
                data.get("store", {}).get("id")
                or data.get("merchant", {}).get("id")
                or data.get("id")
                or store_id_hint
            )
            store_name  = data.get("store", {}).get("name", "") or store_name
            owner_email = (
                data.get("merchant", {}).get("email")
                or data.get("email")
                or merchant_email
            )
            introspect_ok = True
            logger.info(
                "[SallaActivate] Introspect OK | merchant=%s store=%s",
                merchant_id_str, store_name,
            )
        else:
            logger.warning(
                "[SallaActivate] Introspect %d — using email hint | email=%s",
                resp.status_code, merchant_email,
            )
    except Exception as exc:
        logger.warning("[SallaActivate] Introspect error: %s — using hint", exc)

    # ── Find existing tenant (by integration record or email) ─────────────
    tenant_id: Optional[int] = None
    is_new = False

    if merchant_id_str:
        existing = db.query(Integration).filter(
            Integration.provider == "salla",
            Integration.external_store_id == merchant_id_str,
        ).first()
        if existing:
            tenant_id = existing.tenant_id

    if not tenant_id and owner_email:
        user_row = db.query(User).filter(User.email == owner_email).first()
        if user_row:
            tenant_id = user_row.tenant_id

    # ── Create tenant if still not found ─────────────────────────────────
    if not tenant_id:
        unique_name = f"{store_name or 'متجر سلة'}-{merchant_id_str}" if merchant_id_str else (store_name or "متجر سلة")
        new_tenant = Tenant(name=unique_name)
        db.add(new_tenant)
        db.flush()
        tenant_id = new_tenant.id
        is_new    = True

        if owner_email:
            new_user = User(
                username      = owner_email.split("@")[0],
                email         = owner_email,
                password_hash = hash_password(_secrets.token_urlsafe(16)),
                role          = "merchant",
                tenant_id     = tenant_id,
                is_active     = True,
            )
            db.add(new_user)
            db.flush()

    # ── Ensure Salla integration row exists ───────────────────────────────
    if merchant_id_str:
        intg = db.query(Integration).filter(
            Integration.provider == "salla",
            Integration.external_store_id == merchant_id_str,
        ).first()
        now_iso = datetime.now(timezone.utc).isoformat()
        if intg:
            cfg = dict(intg.config or {})
            cfg.update({"store_name": store_name, "last_seen": now_iso,
                        "activated_from_email": True})
            intg.config  = cfg
            intg.enabled = True
        else:
            db.add(Integration(
                provider          = "salla",
                external_store_id = merchant_id_str,
                tenant_id         = tenant_id,
                enabled           = True,
                config            = {
                    "store_id":             merchant_id_str,
                    "store_name":           store_name,
                    "salla_owner_email":    owner_email,
                    "activated_from_email": True,
                    "activated_at":         now_iso,
                },
            ))

    db.commit()

    # ── Issue JWT ─────────────────────────────────────────────────────────
    user_row2 = db.query(User).filter(User.tenant_id == tenant_id).first()
    jwt_token = create_token(
        email     = owner_email or (user_row2.email if user_row2 else ""),
        role      = "merchant",
        tenant_id = tenant_id,
        user_id   = user_row2.id if user_row2 else None,
    )

    audit("salla_activate_from_email", tenant_id=tenant_id)
    logger.info(
        "[SallaActivate] ✅ Done | tenant=%s is_new=%s introspect_ok=%s",
        tenant_id, is_new, introspect_ok,
    )

    return {
        "access_token": jwt_token,
        "tenant_id":    tenant_id,
        "store_name":   store_name,
        "email":        owner_email,
        "is_new":       is_new,
        "introspect_ok": introspect_ok,
        "status":       "activated",
    }


@router.get("/admin/salla-activations")
async def admin_salla_activations(request: Request, db: Session = Depends(get_db)):
    """
    ADMIN — returns all Salla integrations for the admin activations dashboard.

    Shows store name, tenant, status and when it was last activated.
    """
    from core.auth import require_authenticated  # noqa: PLC0415
    payload = require_authenticated(request)
    if payload.get("role") not in ("admin", "owner", "staff"):
        raise HTTPException(status_code=403, detail="Admin access required")

    rows = (
        db.query(Integration)
        .filter(Integration.provider == "salla")
        .order_by(Integration.id.desc())
        .limit(200)
        .all()
    )

    result = []
    for r in rows:
        cfg = r.config or {}
        tenant = db.query(Tenant).filter(Tenant.id == r.tenant_id).first()
        result.append({
            "integration_id":       r.id,
            "tenant_id":            r.tenant_id,
            "tenant_name":          tenant.name if tenant else "—",
            "store_id":             r.external_store_id or cfg.get("store_id", ""),
            "store_name":           cfg.get("store_name", ""),
            "email":                cfg.get("salla_owner_email", ""),
            "enabled":              r.enabled,
            "activated_from_email": cfg.get("activated_from_email", False),
            "activated_at":         cfg.get("activated_at", ""),
            "last_seen":            cfg.get("last_seen", ""),
        })

    return {"activations": result, "total": len(result)}


# ── Salla Embedded App Page ────────────────────────────────────────────────────

@router.get("/salla/app", response_class=HTMLResponse)
async def salla_embedded_app(request: Request):
    """
    *** SET THIS AS THE IFRAME URL IN SALLA PARTNER PORTAL ***

    Nahla-branded page served inside Salla's embedded app iframe.
    - Matches Nahla platform visual identity exactly
    - Uses the official Nahla logo and color system
    - Handles Salla SDK handshake to dismiss skeleton loaders
    - Opens Nahla dashboard in a new tab on CTA click
    """
    dashboard_url = "https://app.nahlah.ai"
    logo_url = "https://app.nahlah.ai/logo.png"
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>نحلة AI — مساعد مبيعات واتساب</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;500;600;700;900&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --bg-900:  #0f172a;
      --bg-800:  #1e293b;
      --bg-700:  #334155;
      --amber:   #f59e0b;
      --amber-d: #d97706;
      --amber-l: rgba(245,158,11,0.15);
      --amber-b: rgba(245,158,11,0.35);
      --text:    #f1f5f9;
      --muted:   #94a3b8;
      --border:  rgba(245,158,11,0.2);
    }}

    html, body {{ height: 100%; }}

    body {{
      font-family: 'Cairo', system-ui, sans-serif;
      background: var(--bg-900);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 24px 16px;
      background-image:
        radial-gradient(ellipse 80% 60% at 50% -10%, rgba(245,158,11,0.08) 0%, transparent 70%);
    }}

    /* ── Header / Logo ── */
    .logo-wrap {{
      display: flex;
      flex-direction: column;
      align-items: center;
      margin-bottom: 28px;
    }}
    .logo-img {{
      width: 80px;
      height: 80px;
      object-fit: contain;
      margin-bottom: 12px;
      filter: drop-shadow(0 0 18px rgba(245,158,11,0.4));
    }}
    .logo-name {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .logo-name h1 {{
      font-size: 26px;
      font-weight: 900;
      color: var(--text);
      letter-spacing: -0.5px;
    }}
    .ai-badge {{
      display: inline-flex;
      align-items: center;
      padding: 2px 8px;
      border-radius: 6px;
      background: var(--amber-l);
      border: 1px solid var(--amber-b);
      box-shadow: 0 0 10px rgba(245,158,11,0.3);
      font-size: 11px;
      font-weight: 900;
      color: var(--amber);
      letter-spacing: 0.5px;
    }}
    .tagline {{
      font-size: 13px;
      color: var(--muted);
      margin-top: 6px;
      text-align: center;
      line-height: 1.6;
    }}

    /* ── Card ── */
    .card {{
      width: 100%;
      max-width: 400px;
      background: rgba(255,255,255,0.03);
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 28px 24px;
      backdrop-filter: blur(16px);
    }}

    /* ── Features ── */
    .features {{
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-bottom: 24px;
    }}
    .feature {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 10px 12px;
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 12px;
    }}
    .feature-icon {{
      width: 34px;
      height: 34px;
      border-radius: 10px;
      background: var(--amber-l);
      border: 1px solid var(--amber-b);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 16px;
      flex-shrink: 0;
    }}
    .feature span {{
      font-size: 13px;
      color: #cbd5e1;
      line-height: 1.4;
    }}

    /* ── CTA Button ── */
    .btn {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      width: 100%;
      background: var(--amber);
      color: var(--bg-900);
      font-family: 'Cairo', system-ui, sans-serif;
      font-weight: 700;
      font-size: 15px;
      padding: 14px 24px;
      border-radius: 12px;
      text-decoration: none;
      border: none;
      cursor: pointer;
      transition: background 0.2s, transform 0.1s, box-shadow 0.2s;
      box-shadow: 0 4px 20px rgba(245,158,11,0.35);
    }}
    .btn:hover {{
      background: var(--amber-d);
      transform: translateY(-1px);
      box-shadow: 0 6px 24px rgba(245,158,11,0.5);
    }}
    .btn:active {{ transform: translateY(0); }}

    .trial-note {{
      text-align: center;
      margin-top: 10px;
      font-size: 12px;
      color: #475569;
    }}
    .trial-note b {{ color: var(--amber); font-weight: 600; }}

    /* ── Footer ── */
    .footer {{
      margin-top: 20px;
      font-size: 11px;
      color: #334155;
      text-align: center;
    }}

    /* ── Loading state ── */
    #status-msg {{
      font-size: 12px;
      color: var(--muted);
      text-align: center;
      margin-top: 8px;
      min-height: 18px;
    }}
  </style>
</head>
<body>

  <!-- Logo & Brand -->
  <div class="logo-wrap">
    <img
      src="{logo_url}"
      alt="نحلة"
      class="logo-img"
      onerror="this.style.display='none'; document.getElementById('fallback-emoji').style.display='block'"
    />
    <span id="fallback-emoji" style="display:none;font-size:56px;margin-bottom:8px;">🐝</span>
    <div class="logo-name">
      <h1>نحلة</h1>
      <span class="ai-badge">AI</span>
    </div>
    <p class="tagline">مساعد مبيعات ذكي يرد على عملاء متجرك عبر واتساب<br>على مدار الساعة — بدون تدخل منك</p>
  </div>

  <!-- Features Card -->
  <div class="card">
    <div class="features">
      <div class="feature">
        <div class="feature-icon">💬</div>
        <span>يرد تلقائياً على كل سؤال عن المنتجات والطلبات</span>
      </div>
      <div class="feature">
        <div class="feature-icon">🚀</div>
        <span>الطيار الآلي — يُدير محادثات المبيعات بدون تدخل</span>
      </div>
      <div class="feature">
        <div class="feature-icon">📦</div>
        <span>يتابع الطلبات ويرسل تحديثات الشحن للعملاء</span>
      </div>
      <div class="feature">
        <div class="feature-icon">🎯</div>
        <span>يرسل عروض وكوبونات للعملاء في الوقت المناسب</span>
      </div>
    </div>

    <a href="{dashboard_url}/register" target="_blank" class="btn" id="cta-btn">
      ابدأ تجربتك المجانية 14 يوم ←
    </a>
    <p class="trial-note">مجاناً <b>14 يوماً</b> — لا يلزم بطاقة ائتمانية</p>
    <p id="status-msg"></p>
  </div>

  <div class="footer">بأيدي سعودية 100% 🇸🇦 · Nahla AI</div>

  <!--
    SDK loaded synchronously so embedded.init() → embedded.ready() can fire immediately.
    embedded.ready() requires init() to complete first — the SDK enforces this.
  -->
  <script src="https://cdn.jsdelivr.net/npm/@salla.sa/embedded-sdk@0.2.4/dist/umd/index.js"></script>
  <script>
    var APP_URL    = '{dashboard_url}';
    var API_URL    = 'https://api.nahlah.ai';
    var statusEl   = document.getElementById('status-msg');
    var ctaBtn     = document.getElementById('cta-btn');

    console.log('[Nahla] /salla/app mounted', {{
      sdk: !!(window.Salla && window.Salla.embedded),
      hasToken: !!new URLSearchParams(location.search).get('token'),
    }});

    // ── 1. Salla SDK handshake — dismisses skeleton loaders ─────────────────
    function sendRawReady() {{
      var msg = {{ event: 'embedded::ready', payload: {{}}, timestamp: Date.now(), source: 'embedded-app', metadata: {{ version: '0.2.4' }} }};
      try {{ window.parent.postMessage(msg, '*'); }} catch(_) {{}}
    }}

    function runSDK() {{
      var sdk = window.Salla && window.Salla.embedded;
      if (!sdk) {{ sendRawReady(); return; }}
      sdk.init({{ debug: false }})
        .then(function() {{ sdk.ready(); sendRawReady(); }})
        .catch(function() {{ sendRawReady(); }});
    }}
    runSDK();
    setTimeout(sendRawReady, 3000);  // safety fallback

    // ── 2. Merchant auto-login via Salla embedded token ──────────────────────
    //
    //  The Salla token in the URL identifies WHICH merchant opened the app.
    //  We call /salla/token-login (backend) → introspects token with Salla API
    //  → finds/creates Nahla account → returns a Nahla JWT.
    //
    //  We then build a link to app.nahlah.ai/salla-callback?token=JWT
    //  Because localStorage is domain-scoped, the JWT must be stored on
    //  app.nahlah.ai (not api.nahlah.ai). SallaCallback.tsx handles this.

    var params    = new URLSearchParams(location.search);
    var sallaToken = params.get('token');
    var appId      = params.get('app_id');

    // ── Auto-redirect helper ────────────────────────────────────────────────
    //  Opens the Nahla dashboard in the PARENT frame (top-level Salla window).
    //  Falls back to a new tab if top-frame navigation is blocked by the browser.
    function goToDashboard(link) {{
      ctaBtn.href   = link;
      ctaBtn.target = '_blank';

      // Try navigating the top-level Salla frame first so the merchant
      // doesn't have to click anything
      try {{
        window.top.location.href = link;
      }} catch(e) {{
        // Cross-origin policy blocked top-frame navigation — open new tab
        window.open(link, '_blank');
      }}
    }}

    if (sallaToken) {{
      if (statusEl) statusEl.textContent = 'جاري التحقق من هويتك...';
      ctaBtn.textContent = 'جاري التحميل…';
      ctaBtn.style.opacity = '0.7';
      ctaBtn.style.pointerEvents = 'none';

      fetch(API_URL + '/salla/token-login', {{
        method:  'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body:    JSON.stringify({{ token: sallaToken, app_id: appId }}),
      }})
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        if (data.access_token) {{
          var cbParams = new URLSearchParams({{
            token:        data.access_token,
            status:       'connected',
            new:          data.is_new ? '1' : '0',
            wa_connected: data.wa_connected ? '1' : '0',
          }});
          var dashLink = APP_URL + '/salla-callback?' + cbParams.toString();

          var greeting = data.is_new
            ? 'مرحباً! جاري إعداد حسابك...'
            : 'مرحباً بعودتك ' + (data.store_name || '') + ' ✓';
          if (statusEl) statusEl.textContent = greeting;

          ctaBtn.textContent    = data.is_new ? 'أكمل إعداد متجرك ←' : 'افتح لوحة التحكم ←';
          ctaBtn.style.opacity  = '1';
          ctaBtn.style.pointerEvents = 'auto';

          console.log('[Nahla] token-login OK', {{
            is_new: data.is_new, tenant: data.tenant_id,
          }});

          // ── Auto-redirect after 1.2 s ────────────────────────────────────
          setTimeout(function() {{ goToDashboard(dashLink); }}, 1200);

        }} else {{
          // token-login returned an error payload
          var errMsg = data.detail || data.error || 'تعذّر التحقق';
          if (statusEl) statusEl.textContent = errMsg;
          ctaBtn.textContent = 'سجّل متجرك يدوياً ←';
          ctaBtn.href   = APP_URL + '/register';
          ctaBtn.target = '_blank';
          ctaBtn.style.opacity = '1';
          ctaBtn.style.pointerEvents = 'auto';
          console.warn('[Nahla] token-login: no access_token', data);
        }}
      }})
      .catch(function(err) {{
        if (statusEl) statusEl.textContent = '';
        ctaBtn.textContent = 'افتح نحلة ←';
        ctaBtn.href   = APP_URL + '/register';
        ctaBtn.target = '_blank';
        ctaBtn.style.opacity = '1';
        ctaBtn.style.pointerEvents = 'auto';
        console.error('[Nahla] token-login error:', err);
      }});
    }} else {{
      // No Salla token — show default register CTA
      console.log('[Nahla] No Salla token in URL — showing default CTA');
    }}
  </script>
</body>
</html>""")


@router.get("/settings/validate")
async def salla_settings_validate(request: Request):
    """
    Salla Partner Portal — "رابط التحقق من الإعدادات"
    Salla calls this endpoint to confirm the app is live and reachable.
    Must be public (no JWT) and always return HTTP 200.
    """
    return {
        "status":  "ok",
        "app":     "nahla-ai",
        "version": "2.0",
    }


@router.get("/salla/start")
async def salla_start(request: Request):
    """
    *** SET THIS AS THE APP URL IN SALLA PARTNER PORTAL ***

    Direct browser redirect to Salla OAuth authorization page.
    Used when a merchant opens the Nahla app from their Salla store for the first time.
    No JSON response — only a 302 redirect so Salla/browsers follow it immediately.

    State is marked with prefix so the callback knows this is a NEW merchant install.
    """
    if not SALLA_CLIENT_ID:
        logger.error("[Salla Start] SALLA_CLIENT_ID not configured!")
        return RedirectResponse(
            url=_error_url("app_not_configured", "SALLA_CLIENT_ID missing"),
            status_code=302,
        )

    # Unique state to detect new-merchant flow in the callback
    state = _NEW_MERCHANT_PREFIX + _secrets.token_urlsafe(12)
    params = urllib.parse.urlencode({
        "client_id":     SALLA_CLIENT_ID,
        "redirect_uri":  SALLA_REDIRECT_URI,
        "response_type": "code",
        "scope":         "offline_access",
        "state":         state,
    })
    auth_url = f"https://accounts.salla.sa/oauth2/auth?{params}"
    logger.info("[Salla Start] Redirecting new merchant to OAuth | state=%s", state)
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/api/salla/authorize")
async def salla_authorize(request: Request):
    """
    Returns the Salla OAuth authorization URL.
    Frontend opens this URL to start the OAuth flow.
    """
    tenant_id = resolve_tenant_id(request)
    if not SALLA_CLIENT_ID:
        raise HTTPException(status_code=503, detail="SALLA_CLIENT_ID not configured")

    normalized_redirect = (SALLA_REDIRECT_URI or "").strip().rstrip("/")
    state = f"t{tenant_id}_{_secrets.token_urlsafe(6)}"
    params = urllib.parse.urlencode({
        "client_id":     SALLA_CLIENT_ID,
        "redirect_uri":  normalized_redirect,
        "response_type": "code",
        "scope":         "offline_access",
        "state":         state,
    })
    auth_url = f"https://accounts.salla.sa/oauth2/auth?{params}"
    logger.info(
        "Salla authorize URL generated | tenant=%s redirect_uri=%r (raw=%r)",
        tenant_id, normalized_redirect, SALLA_REDIRECT_URI,
    )
    return {"url": auth_url, "redirect_uri": normalized_redirect}


@router.get("/api/salla/diag/oauth-config")
async def salla_diag_oauth_config():
    """
    PUBLIC — diagnostic endpoint that returns the exact OAuth config the
    backend is using. Safe because it only exposes redirect_uri + a masked
    client_id (NOT the client_secret).

    Use this to verify Railway env vars match Salla Partner Portal:
      curl https://api.nahlah.ai/api/salla/diag/oauth-config
    """
    raw_redirect       = SALLA_REDIRECT_URI or ""
    normalized_redirect = raw_redirect.strip().rstrip("/")
    sample_state       = "tDIAG_xxxxxx"
    sample_params      = urllib.parse.urlencode({
        "client_id":     SALLA_CLIENT_ID or "",
        "redirect_uri":  normalized_redirect,
        "response_type": "code",
        "scope":         "offline_access",
        "state":         sample_state,
    })
    sample_oauth_url   = f"https://accounts.salla.sa/oauth2/auth?{sample_params}"

    return {
        "client_id_set":          bool(SALLA_CLIENT_ID),
        "client_id_prefix":       (SALLA_CLIENT_ID[:8] + "***") if SALLA_CLIENT_ID else None,
        "client_secret_set":      bool(SALLA_CLIENT_SECRET),
        "redirect_uri_raw":       raw_redirect,
        "redirect_uri_normalized": normalized_redirect,
        "redirect_uri_has_trailing_slash": raw_redirect.endswith("/"),
        "redirect_uri_has_whitespace":     raw_redirect != raw_redirect.strip(),
        "expected_value":         "https://api.nahlah.ai/oauth/salla/callback",
        "matches_expected":       normalized_redirect == "https://api.nahlah.ai/oauth/salla/callback",
        "sample_oauth_url":       sample_oauth_url,
        "instructions": (
            "Set Railway env var: "
            "SALLA_REDIRECT_URI=https://api.nahlah.ai/oauth/salla/callback "
            "(NO trailing slash, MUST be https, MUST match Salla Partner Portal Callback URL exactly)"
        ),
    }


@router.get("/oauth/salla/callback")
async def salla_oauth_callback(
    request: Request,
    code:  Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db:    Session = Depends(get_db),
):
    """
    Salla OAuth 2.0 callback — public endpoint (no JWT).
    Salla redirects here after the merchant authorises the app.

    Steps:
      1. Validate code/state
      2. Exchange code → access_token + refresh_token
      3. Fetch store info from Salla API
      4. Save integration to DB
      5. Redirect to embedded app landing page
    """
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )
    logger.info(
        "[Salla OAuth] Callback received | code=%s state=%s error=%s ip=%s",
        bool(code), state, error, client_ip,
    )
    logger.info(
        "[Salla OAuth] Using redirect_uri=%s client_id=%s",
        SALLA_REDIRECT_URI,
        (SALLA_CLIENT_ID[:6] + "***") if SALLA_CLIENT_ID else "NOT SET",
    )

    # ── Resolve tenant from state param ────────────────────────────────────────
    # state = "t<id>_<rand>"  → existing merchant (new format, >=8 chars for Salla)
    # state = integer         → existing merchant (legacy, may be short)
    # state = "salla_new_*"   → brand-new merchant installing from Salla
    is_new_merchant = (state or "").startswith(_NEW_MERCHANT_PREFIX)
    try:
        raw = state or ""
        if raw.startswith("t") and "_" in raw:
            tenant_id = int(raw.split("_")[0][1:])
        elif not is_new_merchant:
            tenant_id = int(raw) if raw else 0
        else:
            tenant_id = 0
    except (ValueError, TypeError):
        tenant_id = 0
    logger.info(
        "[Salla OAuth] tenant_id=%s is_new_merchant=%s",
        tenant_id, is_new_merchant,
    )

    # ── Handle provider-side errors ────────────────────────────────────────────
    if error:
        logger.warning("[Salla OAuth] Provider error: %s", error)
        return RedirectResponse(url=_error_url(error), status_code=302)

    if not code:
        logger.warning("[Salla OAuth] Missing code in callback")
        return RedirectResponse(url=_error_url("missing_code"), status_code=302)

    if not SALLA_CLIENT_ID or not SALLA_CLIENT_SECRET:
        logger.error("[Salla OAuth] SALLA_CLIENT_ID or SALLA_CLIENT_SECRET not configured")
        return RedirectResponse(url=_error_url("app_not_configured"), status_code=302)

    # ── Step 2: Token exchange ─────────────────────────────────────────────────
    # Normalize redirect_uri so it matches EXACTLY the authorize-URL value
    # (Salla compares them character-for-character; trailing slash will fail).
    normalized_redirect_cb = (SALLA_REDIRECT_URI or "").strip().rstrip("/")
    logger.info("[Salla OAuth] Starting token exchange | redirect_uri=%r", normalized_redirect_cb)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token_resp = await client.post(
                "https://accounts.salla.sa/oauth2/token",
                data={
                    "grant_type":    "authorization_code",
                    "client_id":     SALLA_CLIENT_ID,
                    "client_secret": SALLA_CLIENT_SECRET,
                    "code":          code,
                    "redirect_uri":  normalized_redirect_cb,
                },
                headers={
                    "Accept":       "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

            logger.info(
                "[Salla OAuth] Token endpoint response: status=%s body_preview=%.300s",
                token_resp.status_code,
                token_resp.text,
            )

            if token_resp.status_code != 200:
                # Parse Salla's error for better diagnostics
                try:
                    err_json  = token_resp.json()
                    salla_err = err_json.get("error", "")
                    salla_msg = err_json.get("error_description", token_resp.text[:200])
                except Exception:
                    salla_err = "http_error"
                    salla_msg = token_resp.text[:200]

                logger.error(
                    "[Salla OAuth] Token exchange FAILED | http=%s salla_error=%s desc=%s",
                    token_resp.status_code, salla_err, salla_msg,
                )
                return RedirectResponse(
                    url=_error_url("token_exchange_failed", salla_err or salla_msg),
                    status_code=302,
                )

            token_data    = token_resp.json()
            access_token  = token_data.get("access_token", "")
            refresh_token = token_data.get("refresh_token", "")
            expires_in    = token_data.get("expires_in", 0)
            token_type    = token_data.get("token_type", "Bearer")
            logger.info(
                "[Salla OAuth] Token exchange SUCCESS | expires_in=%s token_type=%s",
                expires_in, token_type,
            )

            # ── Step 3: Fetch store info ───────────────────────────────────────
            logger.info("[Salla OAuth] Fetching store info...")
            salla_store_id = ""
            store_name     = ""
            merchant_id    = ""

            store_resp = await client.get(
                "https://api.salla.dev/admin/v2/store/info",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept":        "application/json",
                },
            )
            logger.info("[Salla OAuth] Store info response: status=%s", store_resp.status_code)

            if store_resp.status_code == 200:
                store_json     = store_resp.json()
                store_data     = store_json.get("data", {})
                salla_store_id = str(store_data.get("id", "") or store_data.get("store_id", ""))
                store_name     = store_data.get("name", "") or store_data.get("store_name", "")
                merchant_id    = str(store_data.get("merchant", {}).get("id", "")) if isinstance(
                    store_data.get("merchant"), dict
                ) else str(store_data.get("merchant", ""))
                logger.info(
                    "[Salla OAuth] ✅ Store info: id=%s name=%r merchant_id=%s full_keys=%s",
                    salla_store_id, store_name, merchant_id, list(store_data.keys()),
                )
            else:
                logger.warning(
                    "[Salla OAuth] ⚠️ Store info fetch failed: %s %.300s",
                    store_resp.status_code, store_resp.text,
                )
                # Attempt fallback: try merchant/info endpoint
                try:
                    fallback_resp = await client.get(
                        "https://api.salla.dev/admin/v2/merchant/info",
                        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                    )
                    if fallback_resp.status_code == 200:
                        fb_data = fallback_resp.json().get("data", {})
                        salla_store_id = str(fb_data.get("id", "") or fb_data.get("store_id", ""))
                        store_name = fb_data.get("name", "") or fb_data.get("store_name", "")
                        logger.info("[Salla OAuth] ✅ Fallback store info: id=%s name=%r", salla_store_id, store_name)
                    else:
                        logger.warning("[Salla OAuth] ⚠️ Fallback also failed: %s", fallback_resp.status_code)
                except Exception as fb_exc:
                    logger.warning("[Salla OAuth] ⚠️ Fallback request error: %s", fb_exc)

    except httpx.TimeoutException as exc:
        logger.error("[Salla OAuth] Token exchange timed out: %s", exc)
        return RedirectResponse(url=_error_url("timeout"), status_code=302)
    except Exception as exc:
        logger.exception("[Salla OAuth] Unexpected error during token exchange: %s", exc)
        return RedirectResponse(url=_error_url("network_error"), status_code=302)

    # ── Step 4: Resolve / create Nahla account for this merchant ───────────────
    auto_jwt: str = ""

    if is_new_merchant:
        # ── Auto-register new merchant from Salla ────────────────────────────
        logger.info("[Salla OAuth] Auto-registering new merchant | store=%s", store_name)
        try:
            # Derive email: use Salla store info or generate a placeholder
            salla_email = ""
            try:
                store_resp2 = await httpx.AsyncClient(timeout=10).get(
                    "https://api.salla.dev/admin/v2/settings/account",
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                )
                if store_resp2.status_code == 200:
                    acc = store_resp2.json().get("data", {})
                    salla_email = (
                        acc.get("email") or
                        acc.get("mobile") or
                        ""
                    )
            except Exception:
                pass

            # Fallback email if Salla didn't return one
            if not salla_email:
                safe_store = "".join(c for c in store_name if c.isalnum() or c in "-_").lower()[:30]
                salla_email = f"{safe_store or 'store'}-{salla_store_id}@salla-merchant.nahlah.ai"

            salla_email = salla_email.strip().lower()
            logger.info("[Salla OAuth] Merchant email resolved: %s", salla_email)

            # ── store_id is the AUTHORITATIVE key for tenant resolution ──
            # Email is a fallback ONLY when no store_id is available.
            existing_integ = None
            if salla_store_id:
                existing_integ = db.query(Integration).filter(
                    Integration.provider == "salla",
                    Integration.external_store_id == str(salla_store_id),
                ).first()
                if not existing_integ:
                    existing_integ = db.query(Integration).filter(
                        Integration.provider == "salla",
                        Integration.config["store_id"].as_string() == str(salla_store_id),
                    ).first()
                    if existing_integ:
                        existing_integ.external_store_id = str(salla_store_id)

            if existing_integ:
                tenant_id = existing_integ.tenant_id
                logger.info("[Salla OAuth] Found existing tenant by store_id=%s → tenant=%s", salla_store_id, tenant_id)
                existing_user2 = db.query(User).filter(User.tenant_id == tenant_id).first()
                if existing_user2:
                    auto_jwt = create_token(
                        email=existing_user2.email,
                        role=existing_user2.role or "merchant",
                        tenant_id=tenant_id,
                        user_id=existing_user2.id,
                    )
                    logger.info(
                        "[Salla OAuth] Reusing existing user for tenant | email=%s tenant=%s",
                        existing_user2.email, tenant_id,
                    )
                else:
                    temp_password = _secrets.token_urlsafe(16)
                    new_user = User(
                        username=salla_email.split("@")[0],
                        email=salla_email,
                        password_hash=hash_password(temp_password),
                        role="merchant",
                        tenant_id=tenant_id,
                        is_active=True,
                    )
                    db.add(new_user)
                    db.flush()
                    auto_jwt = create_token(
                        email=salla_email,
                        role="merchant",
                        tenant_id=tenant_id,
                        user_id=new_user.id,
                    )
                    logger.info(
                        "[Salla OAuth] Created user for existing tenant | email=%s tenant=%s",
                        salla_email, tenant_id,
                    )
            elif not salla_store_id:
                # No store_id available — fall back to email lookup
                existing_user = db.query(User).filter(User.email == salla_email).first()
                if existing_user:
                    tenant_id = existing_user.tenant_id
                    logger.info(
                        "[Salla OAuth] Found existing account (email fallback, no store_id) | "
                        "email=%s tenant=%s",
                        salla_email, tenant_id,
                    )
                    auto_jwt = create_token(
                        email=existing_user.email,
                        role=existing_user.role or "merchant",
                        tenant_id=tenant_id,
                        user_id=existing_user.id,
                    )
                else:
                    # No store_id, no user — create new tenant
                    new_tenant = Tenant(name=store_name or "متجر سلة")
                    db.add(new_tenant)
                    db.flush()
                    tenant_id = new_tenant.id
            else:
                # New store_id not seen before — create new Tenant
                unique_name = f"{store_name or 'متجر سلة'}-{salla_store_id}"
                new_tenant = Tenant(name=unique_name)
                db.add(new_tenant)
                db.flush()
                tenant_id = new_tenant.id

                # Derive a store-scoped email if the email is already taken
                # by a user in a different tenant (shared partner emails).
                if db.query(User).filter(User.email == salla_email).first():
                    safe_store = "".join(c for c in store_name if c.isalnum() or c in "-_").lower()[:30]
                    salla_email = f"{safe_store or 'store'}-{salla_store_id}@salla-merchant.nahlah.ai"
                    logger.info("[Salla OAuth] Email conflict — using store-scoped: %s", salla_email)

                temp_password = _secrets.token_urlsafe(16)
                new_user = User(
                    username=salla_email.split("@")[0],
                    email=salla_email,
                    password_hash=hash_password(temp_password),
                    role="merchant",
                    tenant_id=tenant_id,
                    is_active=True,
                )
                db.add(new_user)
                db.flush()

                auto_jwt = create_token(
                    email=salla_email,
                    role="merchant",
                    tenant_id=tenant_id,
                    user_id=new_user.id,
                )
                logger.info(
                    "[Salla OAuth] Auto-registered new merchant | email=%s tenant=%s user_id=%s",
                    salla_email, tenant_id, new_user.id,
                )

        except Exception as exc:
            logger.exception("[Salla OAuth] Auto-register failed: %s", exc)
            try:
                db.rollback()
            except Exception:
                pass
            return RedirectResponse(url=_error_url("registration_failed"), status_code=302)

    else:
        # Existing merchant — tenant_id came from state
        if tenant_id == 0 and salla_store_id:
            # State was not in our format (Salla may replace it).
            # Search ALL integrations (enabled+disabled) to find the tenant.
            existing_integ = db.query(Integration).filter(
                Integration.provider == "salla",
                Integration.external_store_id == str(salla_store_id),
            ).first()
            if existing_integ:
                tenant_id = existing_integ.tenant_id
                logger.info(
                    "[Salla OAuth] Resolved tenant from store_id | store=%s → tenant=%s enabled=%s",
                    salla_store_id, tenant_id, existing_integ.enabled,
                )
        if tenant_id == 0:
            if not salla_store_id:
                logger.error(
                    "[Salla OAuth] ❌ Cannot resolve tenant — no store_id AND no state. "
                    "Refusing to fall back to tenant_id=1."
                )
                return RedirectResponse(
                    url=_error_url("tenant_resolution_failed"),
                    status_code=302,
                )
            # Create a brand-new tenant for this store instead of
            # silently falling back to tenant_id=1.
            unique_name = (
                f"{store_name or 'متجر سلة'}-{salla_store_id}"
                if salla_store_id
                else (store_name or "متجر سلة")
            )
            new_tenant = Tenant(name=unique_name)
            db.add(new_tenant)
            db.flush()
            tenant_id = new_tenant.id
            logger.info(
                "[Salla OAuth] Created new tenant (existing-merchant-path, "
                "state was lost by Salla) | store_id=%s tenant=%s",
                salla_store_id, tenant_id,
            )
        get_or_create_tenant(db, tenant_id)

    # ── Step 4b: Save Salla integration to DB ──────────────────────────────────
    logger.info(
        "[Salla OAuth] ▶ Saving integration | tenant=%s store_id=%r store_name=%r",
        tenant_id, salla_store_id, store_name,
    )
    if not salla_store_id:
        logger.error(
            "[Salla OAuth] ❌ store_id is EMPTY — widget auto-load will NOT work. "
            "Check store info API response above."
        )

    try:
        from services.salla_guard import claim_store_for_tenant  # noqa: PLC0415

        new_config = {
            "api_key":       access_token,
            "refresh_token": refresh_token,
            "token_type":    token_type,
            "expires_in":    expires_in,
            "store_id":      salla_store_id,
            "store_name":    store_name,
            "merchant_id":   merchant_id,
            "redirect_uri":  SALLA_REDIRECT_URI,
            "connected_at":  datetime.now(timezone.utc).isoformat(),
        }

        if salla_store_id:
            claim_store_for_tenant(
                db,
                store_id=salla_store_id,
                tenant_id=tenant_id,
                new_config=new_config,
            )
        else:
            integration = db.query(Integration).filter(
                Integration.tenant_id == tenant_id,
                Integration.provider  == "salla",
            ).first()
            if integration:
                integration.config  = new_config
                integration.enabled = True
            else:
                db.add(Integration(
                    tenant_id=tenant_id, provider="salla",
                    config=new_config, enabled=True,
                ))

        db.commit()
        logger.info(
            "[Salla OAuth] ✅ DB commit SUCCESS | tenant=%s store_id=%s | "
            "Widget URL: /merchant/widgets/salla/%s/nahla-widgets.js",
            tenant_id, salla_store_id, salla_store_id,
        )
    except Exception as exc:
        logger.exception("[Salla OAuth] ❌ DB save FAILED: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return RedirectResponse(url=_error_url("db_save_failed"), status_code=302)

    # ── Email: welcome (new) or salla_connected (returning) ─────────────────────
    try:
        from services.email_service import enqueue_email  # noqa: PLC0415
        if owner_email and "@" in owner_email:
            if is_new_merchant:
                enqueue_email(
                    to=owner_email,
                    subject="مرحباً بك في نحلة 🐝 — طيار مبيعاتك الآلي جاهز",
                    template="welcome_email",
                    sender_type="welcome",
                    variables={
                        "merchant_name": store_name or owner_email.split("@")[0],
                        "store_name":    store_name,
                    },
                )
            else:
                enqueue_email(
                    to=owner_email,
                    subject="✅ تم ربط متجرك بسلة بنجاح",
                    template="salla_connected",
                    sender_type="system",
                    variables={
                        "merchant_name": store_name or owner_email.split("@")[0],
                        "store_name":    store_name,
                        "connected_at":  __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
                    },
                )
    except Exception as _email_exc:
        logger.warning("[Salla OAuth] Email trigger error: %s", _email_exc)

    # ── Notify merchant (fire-and-forget) ──────────────────────────────────────
    try:
        import asyncio as _asyncio  # noqa: PLC0415
        from core.wa_notify import notify_store_connected  # noqa: PLC0415
        from core.tenant import get_or_create_settings, merge_defaults, DEFAULT_WHATSAPP  # noqa: PLC0415
        _s     = get_or_create_settings(db, tenant_id)
        _wa    = merge_defaults(_s.whatsapp_settings or {}, DEFAULT_WHATSAPP)
        _phone = _wa.get("owner_whatsapp_number", "")
        if _phone:
            _asyncio.ensure_future(notify_store_connected(_phone, store_name, "سلة"))
    except Exception as _exc:
        logger.warning("[Salla OAuth] WA notification error: %s", _exc)

    # ── Trigger initial sync (fire-and-forget) ────────────────────────────────
    try:
        import asyncio as _asyncio2  # noqa: PLC0415

        async def _initial_sync(tid: int):
            await _asyncio2.sleep(3)
            from core.database import get_db as _gdb  # noqa: PLC0415
            from services.store_sync import StoreSyncService  # noqa: PLC0415
            _db = next(_gdb())
            try:
                svc = StoreSyncService(_db, tid)
                result = await svc.full_sync(triggered_by="oauth_connect")
                logger.info("[Salla OAuth] Initial sync done | tenant=%s result=%s", tid, result.get("status"))
            except Exception as exc:
                logger.error("[Salla OAuth] Initial sync failed | tenant=%s: %s", tid, exc)
            finally:
                _db.close()

        _asyncio2.ensure_future(_initial_sync(tenant_id))
        logger.info("[Salla OAuth] Initial sync task queued | tenant=%s", tenant_id)
    except Exception as _exc:
        logger.warning("[Salla OAuth] Could not queue initial sync: %s", _exc)

    # ── Step 5: Redirect ────────────────────────────────────────────────────────
    # ALL Salla merchants land on the mini-dashboard /app/entry after OAuth.
    # Never on /landing (public marketing page) and never on the iframe URL.
    if auto_jwt:
        # New merchant: route through /salla-callback so it can persist the
        # fresh JWT in localStorage on app.nahlah.ai BEFORE entering the
        # mini-dashboard.  The callback page navigates to /app/entry on success.
        params = urllib.parse.urlencode({
            "token":     auto_jwt,
            "status":    "connected",
            "store":     salla_store_id,
            "name":      store_name,
            "new":       "1" if is_new_merchant else "0",
        })
        redirect_url = f"{_DASHBOARD_ORIGIN}/salla-callback?{params}"
        logger.info(
            "[SallaOAuth] callback success redirect_to=%s | path=/salla-callback "
            "tenant=%s store=%s is_new=%s",
            redirect_url, tenant_id, salla_store_id, is_new_merchant,
        )
        return RedirectResponse(url=redirect_url, status_code=302)

    # Existing merchant: they already have a valid JWT in localStorage from the
    # earlier salla_token_login call — go straight to /app/entry.
    success_url = _success_url(salla_store_id, store_name)
    logger.info(
        "[SallaOAuth] callback success redirect_to=%s | path=%s "
        "tenant=%s store=%s",
        success_url, _SALLA_POST_OAUTH_PATH, tenant_id, salla_store_id,
    )
    return RedirectResponse(url=success_url, status_code=302)


@router.get("/integrations/salla/success", response_class=HTMLResponse)
async def salla_integration_success(request: Request):
    """
    Public success landing page (no JWT required).
    Shown if the browser lands here instead of the embedded app.
    Immediately redirects the user to the embedded app.
    """
    store = request.query_params.get("store", "")
    name  = urllib.parse.quote(request.query_params.get("name", ""))
    dest  = f"{_DASHBOARD_ORIGIN}{_SALLA_POST_OAUTH_PATH}?status=connected&store={store}&name={name}"
    return HTMLResponse(content=_redirect_html(dest, "تم ربط المتجر بنجاح ✅", "جاري التحويل..."))


@router.get("/integrations/salla/error", response_class=HTMLResponse)
async def salla_integration_error(request: Request):
    """
    Public error landing page (no JWT required).
    Shown if the browser lands here instead of the embedded app.
    Immediately redirects the user to the embedded app with the error reason.
    """
    reason = request.query_params.get("reason", "unknown_error")
    detail = request.query_params.get("detail", "")
    dest   = f"{_DASHBOARD_ORIGIN}{_SALLA_POST_OAUTH_PATH}?status=error&reason={reason}"
    if detail:
        dest += f"&detail={urllib.parse.quote(detail)}"
    return HTMLResponse(content=_redirect_html(dest, "حدث خطأ أثناء ربط المتجر", f"السبب: {reason}"))


def _redirect_html(dest: str, title: str, subtitle: str) -> str:
    """Return a minimal HTML page that auto-redirects."""
    return f"""<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="2; url={dest}">
  <title>نحلة AI — {title}</title>
  <style>
    body {{ font-family: Arial, sans-serif; text-align: center; padding: 60px 20px; background: #fffbeb; color: #1e293b; }}
    h2 {{ color: #f59e0b; }} p {{ color: #64748b; }}
  </style>
</head>
<body>
  <h2>🐝 نحلة AI</h2>
  <h3>{title}</h3>
  <p>{subtitle}</p>
  <p style="font-size:13px">جاري التحويل التلقائي...</p>
  <script>setTimeout(() => location.href = "{dest}", 1500);</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# SALLA TEST APP — separate routes using SALLA_TEST_* credentials
# Does NOT modify or affect the production OAuth flow above.
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/api/salla/test/authorize", include_in_schema=True)
async def salla_test_authorize(request: Request):
    """
    PUBLIC — no JWT required.
    Redirects directly to Salla OAuth authorization using the TEST app credentials.
    """
    # ── Diagnostic logs — confirms exactly which credentials are used ──────────
    logger.info("[SallaTest][DIAG] ▶ /api/salla/test/authorize called")
    logger.info("[SallaTest][DIAG] SALLA_TEST_CLIENT_ID  = %s",
                (SALLA_TEST_CLIENT_ID[:8] + "***") if SALLA_TEST_CLIENT_ID else "NOT SET")
    logger.info("[SallaTest][DIAG] SALLA_TEST_REDIRECT_URI = %s", SALLA_TEST_REDIRECT_URI)
    logger.info("[SallaTest][DIAG] SALLA_CLIENT_ID (prod) = %s",
                (SALLA_CLIENT_ID[:8] + "***") if SALLA_CLIENT_ID else "NOT SET")

    if not SALLA_TEST_CLIENT_ID:
        logger.error("[SallaTest][DIAG] ✗ SALLA_TEST_CLIENT_ID is empty — cannot redirect")
        raise HTTPException(status_code=503, detail="SALLA_TEST_CLIENT_ID not configured")

    params = urllib.parse.urlencode({
        "client_id":     SALLA_TEST_CLIENT_ID,
        "redirect_uri":  SALLA_TEST_REDIRECT_URI,
        "response_type": "code",
        "scope":         "offline_access",
        "state":         _NEW_MERCHANT_PREFIX + "test",
    })
    auth_url = f"https://accounts.salla.sa/oauth2/auth?{params}"
    logger.info("[SallaTest][DIAG] ✓ Final authorize URL = %s", auth_url)
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/oauth/salla/test/callback")
async def salla_test_oauth_callback(
    request: Request,
    code:    Optional[str] = None,
    state:   Optional[str] = None,
    error:   Optional[str] = None,
    db:      Session = Depends(get_db),
):
    """
    Salla TEST app OAuth callback — uses SALLA_TEST_* credentials.
    Identical logic to /oauth/salla/callback but uses the test app's keys.
    The production /oauth/salla/callback is NOT affected.
    """
    logger.info(
        "[SallaTest] Callback received | code=%s state=%s error=%s",
        bool(code), state, error,
    )

    is_new_merchant = (state or "").startswith(_NEW_MERCHANT_PREFIX)
    try:
        tenant_id = int(state) if (state and not is_new_merchant) else 0
    except (ValueError, TypeError):
        tenant_id = 0

    if error:
        return RedirectResponse(url=_error_url(error), status_code=302)

    if not code:
        return RedirectResponse(url=_error_url("missing_code"), status_code=302)

    if not SALLA_TEST_CLIENT_ID or not SALLA_TEST_CLIENT_SECRET:
        logger.error("[SallaTest] TEST credentials not configured")
        return RedirectResponse(url=_error_url("app_not_configured"), status_code=302)

    # Token exchange using TEST app credentials
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token_resp = await client.post(
                "https://accounts.salla.sa/oauth2/token",
                data={
                    "grant_type":    "authorization_code",
                    "client_id":     SALLA_TEST_CLIENT_ID,
                    "client_secret": SALLA_TEST_CLIENT_SECRET,
                    "code":          code,
                    "redirect_uri":  SALLA_TEST_REDIRECT_URI,
                },
                headers={
                    "Accept":       "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            logger.info("[SallaTest] Token response: status=%s", token_resp.status_code)

            if token_resp.status_code != 200:
                try:
                    err_json  = token_resp.json()
                    salla_err = err_json.get("error", "token_exchange_failed")
                    salla_msg = err_json.get("error_description", token_resp.text[:200])
                except Exception:
                    salla_err, salla_msg = "token_exchange_failed", token_resp.text[:200]
                logger.error("[SallaTest] Token exchange FAILED: %s %s", salla_err, salla_msg)
                return RedirectResponse(url=_error_url("token_exchange_failed", salla_err), status_code=302)

            token_data    = token_resp.json()
            access_token  = token_data.get("access_token", "")
            refresh_token = token_data.get("refresh_token", "")
            expires_in    = int(token_data.get("expires_in", 0))

            # Fetch store info
            salla_store_id = ""
            store_name     = ""
            merchant_id    = ""
            store_resp = await client.get(
                "https://api.salla.dev/admin/v2/store/info",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            if store_resp.status_code == 200:
                store_data     = store_resp.json().get("data", {})
                salla_store_id = str(store_data.get("id", ""))
                store_name     = store_data.get("name", "")
                merchant_id    = str(store_data.get("merchant", {}).get("id", "")) if isinstance(
                    store_data.get("merchant"), dict
                ) else str(store_data.get("merchant", ""))
                logger.info("[SallaTest] Store: id=%s name=%s", salla_store_id, store_name)

    except Exception as exc:
        logger.exception("[SallaTest] Unexpected error: %s", exc)
        return RedirectResponse(url=_error_url("network_error"), status_code=302)

    # Save / update integration in DB — same guard as production callback
    try:
        from datetime import datetime, timezone  # noqa: PLC0415
        from services.salla_guard import claim_store_for_tenant  # noqa: PLC0415

        if tenant_id:
            get_or_create_tenant(db, tenant_id)

        cfg = {
            "api_key":       access_token,
            "refresh_token": refresh_token,
            "store_id":      salla_store_id,
            "store_name":    store_name,
            "merchant_id":   merchant_id,
            "expires_in":    expires_in,
            "connected_at":  datetime.now(timezone.utc).isoformat(),
            "app_type":      "test",
        }

        effective_tenant = tenant_id or 0
        if salla_store_id and effective_tenant:
            claim_store_for_tenant(
                db,
                store_id=salla_store_id,
                tenant_id=effective_tenant,
                new_config=cfg,
            )
        else:
            integration = db.query(Integration).filter(
                Integration.tenant_id == effective_tenant,
                Integration.provider  == "salla",
            ).first()
            if integration:
                integration.config  = cfg
                integration.enabled = True
            else:
                db.add(Integration(
                    tenant_id=effective_tenant, provider="salla",
                    config=cfg, enabled=True,
                ))

        db.commit()
        logger.info("[SallaTest] Integration saved | tenant=%s store=%s", tenant_id, salla_store_id)
    except Exception as exc:
        logger.error("[SallaTest] DB save failed: %s", exc)
        return RedirectResponse(url=_error_url("db_save_failed"), status_code=302)

    # Trigger initial sync for the test store
    if tenant_id:
        try:
            import asyncio as _asyncio3  # noqa: PLC0415

            async def _test_initial_sync(tid: int):
                await _asyncio3.sleep(3)
                from core.database import get_db as _gdb  # noqa: PLC0415
                from services.store_sync import StoreSyncService  # noqa: PLC0415
                _db = next(_gdb())
                try:
                    svc = StoreSyncService(_db, tid)
                    result = await svc.full_sync(triggered_by="oauth_test_connect")
                    logger.info("[SallaTest] Initial sync done | tenant=%s result=%s", tid, result.get("status"))
                except Exception as e:
                    logger.error("[SallaTest] Initial sync failed | tenant=%s: %s", tid, e)
                finally:
                    _db.close()

            _asyncio3.ensure_future(_test_initial_sync(tenant_id))
        except Exception as _e:
            logger.warning("[SallaTest] Could not queue initial sync: %s", _e)

    success_url = (
        f"{_DASHBOARD_ORIGIN}{_SALLA_POST_OAUTH_PATH}"
        f"?salla_connected=true&name={urllib.parse.quote(store_name)}"
    )
    logger.info("[SallaOAuth] callback success redirect_to=%s | path=%s (test app)",
                success_url, _SALLA_POST_OAUTH_PATH)
    return RedirectResponse(url=success_url, status_code=302)


# ═══════════════════════════════════════════════════════════════════════════════
# PROTECTED ROUTES — require JWT
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/api/salla/store")
async def get_salla_store(
    request:   Request,
    db:        Session = Depends(get_db),
    tenant_id: int     = Depends(get_jwt_tenant_id),
):
    """Return saved Salla store info for this tenant."""
    audit("salla_store_read", tenant_id=tenant_id)
    integration = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
        Integration.provider  == "salla",
        Integration.enabled   == True,  # noqa: E712
    ).first()
    if not integration:
        raise HTTPException(status_code=404, detail="Salla integration not configured")
    cfg = integration.config or {}
    return {
        "configured":   True,
        "store_id":     cfg.get("store_id", ""),
        "store_name":   cfg.get("store_name", ""),
        "merchant_id":  cfg.get("merchant_id", ""),
        "connected_at": cfg.get("connected_at"),
        "redirect_uri": cfg.get("redirect_uri", ""),
        "api_key_hint": ("***" + cfg.get("api_key", "")[-4:]) if cfg.get("api_key") else "",
    }


@router.get("/api/salla/products")
async def get_salla_products(
    request:   Request,
    tenant_id: int = Depends(get_jwt_tenant_id),
):
    """Fetch live products from the tenant's Salla store."""
    audit("salla_products_fetched", tenant_id=tenant_id)
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from store_integration.registry import get_adapter  # noqa: PLC0415
    adapter = get_adapter(tenant_id)
    if not adapter:
        raise HTTPException(status_code=404, detail="Salla integration not configured")
    try:
        products = await adapter.get_products()
        return {"products": [p.dict() for p in products], "count": len(products)}
    except Exception as exc:
        logger.error("Salla products fetch error tenant=%s: %s", tenant_id, exc)
        raise HTTPException(status_code=502, detail=f"Salla API error: {exc}")


@router.post("/api/salla/test-coupon")
async def test_salla_coupon(
    request:   Request,
    tenant_id: int = Depends(get_jwt_tenant_id),
):
    """Validate a coupon code against the tenant's Salla store."""
    body = await request.json()
    coupon_code = body.get("coupon_code", "").strip()
    if not coupon_code:
        raise HTTPException(status_code=400, detail="coupon_code is required")
    audit("salla_coupon_test", tenant_id=tenant_id, coupon=coupon_code)
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from store_integration.registry import get_adapter  # noqa: PLC0415
    adapter = get_adapter(tenant_id)
    if not adapter:
        raise HTTPException(status_code=404, detail="Salla integration not configured")
    try:
        offer = await adapter.validate_coupon(coupon_code)
        if offer:
            return {"valid": True, "coupon": offer.dict()}
        return {"valid": False, "reason": "coupon not found or expired"}
    except Exception as exc:
        logger.error("Salla coupon error tenant=%s: %s", tenant_id, exc)
        raise HTTPException(status_code=502, detail=f"Salla API error: {exc}")


@router.post("/api/salla/reconnect")
async def salla_reconnect(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Smart reconnect — tries every available path in priority order:

    1. Silent refresh  — use refresh_token if present.
    2. Reactivate      — if no refresh_token but api_key exists, re-enable the
                         integration as-is (manual/long-lived token mode).
    3. OAuth redirect  — last resort, returns the Salla OAuth URL.

    Always returns HTTP 200; caller inspects `action` to decide next step.
    """
    tenant_id = resolve_tenant_id(request)
    audit("salla_reconnect_attempt", tenant_id=tenant_id)

    intg = db.query(Integration).filter(
        Integration.tenant_id == tenant_id,
        Integration.provider  == "salla",
    ).first()

    cfg           = dict(intg.config or {}) if intg else {}
    api_key       = cfg.get("api_key", "")
    refresh_token = cfg.get("refresh_token", "")
    client_id     = SALLA_CLIENT_ID     or ""
    client_secret = SALLA_CLIENT_SECRET or ""

    # ── Path 1: silent token refresh via refresh_token ────────────────────────
    if refresh_token and client_id and client_secret:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://accounts.salla.sa/oauth2/token",
                    data={
                        "grant_type":    "refresh_token",
                        "client_id":     client_id,
                        "client_secret": client_secret,
                        "refresh_token": refresh_token,
                    },
                    headers={
                        "Accept":       "application/json",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
            if resp.status_code == 200:
                data = resp.json()
                new_access  = data.get("access_token", "")
                new_refresh = data.get("refresh_token", refresh_token)
                if new_access and intg:
                    cfg["api_key"]            = new_access
                    cfg["refresh_token"]      = new_refresh
                    cfg["last_token_refresh"] = datetime.now(timezone.utc).isoformat()
                    cfg.pop("needs_reauth",        None)
                    cfg.pop("no_auto_refresh",     None)
                    cfg.pop("no_auto_refresh_reason", None)
                    intg.config  = cfg
                    intg.enabled = True
                    db.commit()
                    logger.info("[SallaReconnect] Token refreshed silently | tenant=%s", tenant_id)
                    return {
                        "action":  "refreshed",
                        "message": "تم تجديد التوكن بنجاح — الربط فعّال الآن",
                    }
            # If invalid_grant, fall through to Path 2 (keep existing api_key)
            logger.warning(
                "[SallaReconnect] Refresh failed %d | tenant=%s body=%s",
                resp.status_code, tenant_id, resp.text[:200],
            )
        except Exception as exc:
            logger.warning("[SallaReconnect] Refresh error | tenant=%s: %s", tenant_id, exc)

    # ── Path 2: re-activate existing api_key (no refresh_token needed) ────────
    # Works when Salla app is under review / refresh_token was revoked but the
    # access_token (api_key) is still valid or was manually entered.
    if api_key and intg:
        cfg.pop("needs_reauth",           None)
        cfg.pop("needs_reauth_at",        None)
        cfg.pop("needs_reauth_reason",    None)
        cfg.pop("refresh_token",          None)   # remove the revoked token
        cfg["no_auto_refresh"]        = True
        cfg["no_auto_refresh_reason"] = "manual_mode"
        cfg["reactivated_at"]         = datetime.now(timezone.utc).isoformat()
        intg.config  = cfg
        intg.enabled = True
        db.commit()
        logger.info(
            "[SallaReconnect] Re-activated with existing api_key (manual mode) | tenant=%s",
            tenant_id,
        )
        return {
            "action":  "reactivated",
            "message": "تم تفعيل الاتصال بالمفتاح الحالي — الربط نشط الآن",
            "note":    "يعمل في وضع المفتاح الدائم. لتجديد المفتاح ادخل Access Token جديد من لوحة سلة.",
        }

    # ── Path 3: no token at all — OAuth required ──────────────────────────────
    if not SALLA_CLIENT_ID:
        raise HTTPException(status_code=503, detail="SALLA_CLIENT_ID not configured")

    state = f"t{tenant_id}_{_secrets.token_urlsafe(6)}"
    params = urllib.parse.urlencode({
        "client_id":     SALLA_CLIENT_ID,
        "redirect_uri":  SALLA_REDIRECT_URI,
        "response_type": "code",
        "scope":         "offline_access",
        "state":         state,
    })
    oauth_url = f"https://accounts.salla.sa/oauth2/auth?{params}"
    logger.info("[SallaReconnect] No token found — OAuth required | tenant=%s", tenant_id)
    return {
        "action":  "oauth_required",
        "url":     oauth_url,
        "message": "يلزم ربط المتجر أولاً عبر OAuth أو بإدخال مفتاح API يدوياً",
    }
