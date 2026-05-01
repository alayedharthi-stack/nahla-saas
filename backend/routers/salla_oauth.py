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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from models import Integration, Product, SmartAutomation, Tenant, User, WhatsAppConnection

from core.audit import audit
from core.auth import create_token, decode_token, get_jwt_tenant_id, hash_password
from core.config import (
    DASHBOARD_URL,
    SALLA_OAUTH_CLIENT_ID,
    SALLA_OAUTH_CLIENT_SECRET,
    SALLA_OAUTH_REDIRECT_URI,
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

# NOTE: We deliberately do NOT define a post-install redirect target.
# After OAuth completes, /oauth/salla/callback returns a neutral 200 OK
# with a single Arabic line and stops.  Salla's embedded-app policy
# requires merchants to enter the app via the 'استخدام التطبيق' button
# only — any redirect we initiate (even to a Salla URL) counts as us
# overriding Salla's flow, which is not allowed.

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
    introspect_access_token  = ""
    introspect_refresh_token = ""
    introspect_expires_in    = None

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
                # Diagnostic: log the SHAPE of the response (keys only,
                # no token values) so we can tell whether Salla returned
                # a refresh_token for this app/flow.  Embedded "Communication
                # App" introspect typically returns access_token only;
                # full OAuth returns access_token + refresh_token.
                _payload_data = data.get("data") if isinstance(data.get("data"), dict) else {}
                _merchant     = _payload_data.get("merchant") if isinstance(_payload_data.get("merchant"), dict) else {}
                logger.info(
                    "[SallaLogin] STEP 2 — introspect response shape | "
                    "top_keys=%s data_keys=%s merchant_keys=%s "
                    "has_access_token=%s has_refresh_token=%s",
                    list(data.keys()),
                    list(_payload_data.keys()),
                    list(_merchant.keys()) if _merchant else [],
                    bool(_payload_data.get("access_token") or data.get("access_token")),
                    bool(_payload_data.get("refresh_token") or data.get("refresh_token")),
                )
                if data.get("success"):
                    introspect_ok   = True
                    payload_data    = data.get("data") or {}
                    merchant        = payload_data.get("merchant") or {}
                    # Defensive: if Salla ever returns a refresh_token in
                    # introspect (some app types do), capture it so the
                    # integration save below can persist it.
                    introspect_access_token  = str(payload_data.get("access_token")  or data.get("access_token")  or "")
                    introspect_refresh_token = str(payload_data.get("refresh_token") or data.get("refresh_token") or "")
                    introspect_expires_in    = payload_data.get("expires_in") or data.get("expires_in")

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
            #
            # If introspect ALSO returned an access_token/refresh_token (some
            # Salla app types do — Easy mode, OAuth-completed apps), we
            # prefer those over the embedded iframe token because they're
            # valid Admin API tokens.
            embedded_api_key   = salla_token  # the v4.public.* token from the iframe
            api_key_to_persist = introspect_access_token or embedded_api_key
            api_key_source     = "introspect" if introspect_access_token else "embedded_token"

            if integration:
                cfg = dict(integration.config or {})
                # If the existing row has a real refresh_token, keep its
                # access_token (don't downgrade to embedded).  Otherwise
                # overwrite with whatever introspect/embedded gave us so
                # the row reflects the active session.
                existing_refresh = cfg.get("refresh_token", "")
                cfg.update({
                    "store_id":          merchant_id_str,
                    "store_name":        store_name,
                    "last_seen":         now_iso,
                    "salla_owner_email": owner_email,
                })
                if not existing_refresh:
                    # No prior OAuth state — refresh from introspect/embedded
                    cfg["api_key"]             = api_key_to_persist
                    cfg["api_key_source"]      = api_key_source
                    cfg["api_key_received_at"] = now_iso
                if introspect_refresh_token:
                    cfg["refresh_token"] = introspect_refresh_token
                    cfg["api_key_source"] = "introspect"
                    if introspect_expires_in:
                        cfg["expires_in"] = introspect_expires_in
                # When the merchant actively logs in, clear any stale reauth /
                # no_auto_refresh / cleanup flags so a pending app.store.authorize
                # webhook (which may arrive shortly after login) — or this very
                # introspect — can restore full sync.
                cfg.pop("needs_reauth",                None)
                cfg.pop("needs_reauth_at",             None)
                cfg.pop("needs_reauth_reason",         None)
                cfg.pop("no_auto_refresh",             None)
                cfg.pop("no_auto_refresh_reason",      None)
                cfg.pop("no_auto_refresh_at",          None)
                cfg.pop("soft_disabled",               None)
                cfg.pop("uninstalled_at",              None)
                cfg.pop("superseded_by_oauth_reconnect", None)
                cfg.pop("disabled_reason",             None)
                cfg.pop("disabled_at",                 None)
                integration.config = cfg
                integration.external_store_id = merchant_id_str
                # Mark as enabled if we have ANY usable api_key
                if cfg.get("api_key"):
                    integration.enabled = True
                from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
                flag_modified(integration, "config")
                logger.info(
                    "[SallaLogin]    Integration UPDATED | tenant=%s store_id=%s "
                    "enabled=%s api_key_source=%s has_refresh=%s",
                    tenant_id, merchant_id_str, integration.enabled,
                    cfg.get("api_key_source") or "?",
                    bool(cfg.get("refresh_token")),
                )
            else:
                new_cfg = {
                    "store_id":             merchant_id_str,
                    "store_name":           store_name,
                    "salla_token_login":    True,
                    "connected_at":         now_iso,
                    "salla_owner_email":    owner_email,
                    "api_key":              api_key_to_persist,
                    "api_key_source":       api_key_source,
                    "api_key_received_at":  now_iso,
                }
                if introspect_refresh_token:
                    new_cfg["refresh_token"] = introspect_refresh_token
                    new_cfg["api_key_source"] = "introspect"
                    if introspect_expires_in:
                        new_cfg["expires_in"] = introspect_expires_in
                db.add(Integration(
                    tenant_id = tenant_id,
                    provider  = "salla",
                    external_store_id = merchant_id_str,
                    config    = new_cfg,
                    enabled   = bool(api_key_to_persist),
                ))
                logger.info(
                    "[SallaLogin]    Integration CREATED | tenant=%s store_id=%s "
                    "enabled=%s api_key_source=%s has_refresh=%s",
                    tenant_id, merchant_id_str, bool(api_key_to_persist),
                    new_cfg.get("api_key_source"),
                    bool(new_cfg.get("refresh_token")),
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

    # ── Detect integration shape (Dual Architecture) ─────────────────────────
    #
    # We surface TWO independent flags to the dashboard:
    #
    #   • needs_oauth   — legacy Custom OAuth path on the SAME (Communication)
    #                     app.  Kept for backwards compatibility only — under
    #                     the Dual Architecture we never expect this to fire
    #                     for new merchants because Communication Apps cannot
    #                     complete OAuth on themselves.
    #
    #   • needs_api_sync — TRUE whenever the active integration row does NOT
    #                      yet hold a refresh_token from the dedicated Sync
    #                      OAuth app (SALLA_OAUTH_CLIENT_ID).  The dashboard
    #                      renders a "Connect API" CTA in /app/entry when this
    #                      is true.
    #
    # IMPORTANT: For Easy Mode merchants, OAuth authorize flow MUST NEVER be
    # triggered.  Easy Mode apps have no registered redirect_uri in Salla
    # Partner Portal — pointing the browser at accounts.salla.sa/oauth2/auth
    # produces a 404 "redirect_uri does not match".  Tokens for these
    # merchants arrive via the app.store.authorize webhook a few seconds
    # after install, so the correct behaviour when refresh_token is missing
    # is to wait (or ask the merchant to re-install), NOT to redirect.
    needs_oauth    = False
    needs_api_sync = True   # default ON until we prove otherwise
    oauth_url      = ""
    try:
        check_integ = db.query(Integration).filter(
            Integration.tenant_id == tenant_id,
            Integration.provider == "salla",
        ).first()
        if check_integ:
            cfg = check_integ.config or {}
            has_refresh   = bool(cfg.get("refresh_token"))
            api_key_src   = (cfg.get("api_key_source") or "").lower()
            app_type      = (cfg.get("app_type")       or "").lower()
            is_easy_mode  = (
                app_type == "easy"
                or api_key_src == "easy_mode_webhook"
            )
            api_sync_done = (
                bool(cfg.get("api_sync_enabled"))
                and has_refresh
                and bool(check_integ.enabled)
            )

            # api_sync only counts the dedicated Sync OAuth row — Easy Mode
            # tokens via webhook also qualify as "full Admin API access" so
            # we don't nag those merchants either.
            if api_sync_done or is_easy_mode:
                needs_api_sync = False

            if is_easy_mode:
                # Easy Mode: never OAuth.  If refresh_token is somehow
                # missing here, the app.store.authorize webhook will land
                # within seconds (or the merchant must reinstall from
                # s.salla.sa/apps).  Either way the embedded session is
                # valid for the dashboard and the orders poller will pick
                # up tokens once they arrive.
                logger.info(
                    "[SallaLogin] easy_mode tenant=%s — needs_oauth=false "
                    "(has_refresh=%s api_key_source=%s app_type=%s)",
                    tenant_id, has_refresh, api_key_src or "?", app_type or "?",
                )
            elif not has_refresh or api_key_src == "embedded_token":
                # Legacy Custom OAuth path — only here do we still flip
                # needs_oauth.  Easy Mode merchants will not reach this
                # branch.
                needs_oauth = True
                if SALLA_CLIENT_ID and SALLA_REDIRECT_URI:
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
                        "[SallaLogin] (custom-oauth) needs_oauth=True | tenant=%s | "
                        "client_id=%s | redirect_uri=%r | has_refresh=%s api_key_source=%s",
                        tenant_id,
                        (SALLA_CLIENT_ID[:8] + "***") if SALLA_CLIENT_ID else "EMPTY",
                        normalized_redirect, has_refresh, api_key_src,
                    )
                else:
                    logger.error(
                        "[SallaLogin] (custom-oauth) needs_oauth=True BUT cannot build oauth_url | "
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
        "needs_api_sync": needs_api_sync,
        "api_sync_start_url": "/api/salla/oauth/start",
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
    # ── Outer guard so any unhandled error returns clean JSON, not 500 ──────
    # (The previous version let exceptions propagate which surfaced as a
    # CORS-wrapped 500 — useless for diagnosis.  Now every failure is
    # logged with the full traceback and returned as a structured body
    # the frontend can act on.)
    import traceback as _tb  # noqa: PLC0415
    requested_store = str(request.query_params.get("store_id") or "").strip()
    tenant_id = 0
    integration_id: Optional[int] = None
    api_key_source = ""
    has_access_token = False
    has_refresh_token = False
    try:
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

        # ── Store-isolation guard ──────────────────────────────────────────
        # If the caller passed ?store_id=, verify the JWT's tenant actually
        # owns that Salla store.  A mismatch means the old token is for a
        # DIFFERENT store → reject so the frontend does a fresh token-login.
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
            integration_id    = matching_integ.id
            _cfg              = matching_integ.config or {}
            api_key_source    = (_cfg.get("api_key_source") or "").lower()
            has_access_token  = bool(_cfg.get("api_key"))
            has_refresh_token = bool(_cfg.get("refresh_token"))

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

        # ── Merchant readiness probes (cheap point-queries) ───────────────
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
            "[SallaSession] OK tenant=%s store=%s integration_id=%s "
            "api_key_source=%s has_access=%s has_refresh=%s "
            "wa=%s autos=%s products=%s",
            tenant_id, requested_store or "-", integration_id,
            api_key_source or "?", has_access_token, has_refresh_token,
            wa_connected, has_automations, has_products,
        )

        return {
            "connected":          True,
            "tenant_id":          tenant_id,
            "token":              fresh_token,
            "whatsapp_connected": wa_connected,
            "has_automations":    has_automations,
            "has_products":       has_products,
            "integration": {
                "id":                integration_id,
                "api_key_source":    api_key_source or None,
                "has_access_token":  has_access_token,
                "has_refresh_token": has_refresh_token,
            },
        }

    except HTTPException:
        raise
    except Exception as exc:
        # Full diagnostics — type, message, traceback, plus the most
        # useful integration context (no token values).
        tb = _tb.format_exc()
        logger.error(
            "[SallaSession] FAILED store=%s tenant_id=%s integration_id=%s "
            "api_key_source=%s has_access=%s has_refresh=%s "
            "exc_type=%s exc_msg=%s\n%s",
            requested_store or "-", tenant_id, integration_id,
            api_key_source or "?", has_access_token, has_refresh_token,
            type(exc).__name__, str(exc)[:300], tb[-2000:],
        )
        # Return a clean JSON 200 so the dashboard can show a useful UI
        # instead of a CORS-wrapped 500.  When refresh_token is missing
        # we surface needs_oauth so the frontend can prompt reconnect.
        if not has_refresh_token:
            return JSONResponse(
                status_code=200,
                content={
                    "connected":    False,
                    "needs_oauth":  True,
                    "reason":       "missing_refresh_token",
                    "tenant_id":    tenant_id or None,
                    "integration_id": integration_id,
                    "store_id":     requested_store or None,
                    "reconnect_url": "/api/salla/reconnect",
                    "error_type":   type(exc).__name__,
                    "error":        str(exc)[:200],
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "connected":      False,
                "reason":         "session_check_failed",
                "tenant_id":      tenant_id or None,
                "integration_id": integration_id,
                "store_id":       requested_store or None,
                "error_type":     type(exc).__name__,
                "error":          str(exc)[:200],
            },
        )


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
    Build a Salla OAuth authorize URL for the **basic / Communication App**
    (``SALLA_CLIENT_ID``).  This is the LEGACY flow and is intentionally
    kept untouched so any caller still relying on it (the Communication
    App install path, Easy/Embedded reconnects, internal scripts, etc.)
    keeps working.

    ─────────────────────────────────────────────────────────────────────
    Dual Integration Architecture — DO NOT MIX THESE TWO ENDPOINTS:

      • /api/salla/authorize   → LEGACY basic flow.
                                 Uses SALLA_CLIENT_ID + SALLA_REDIRECT_URI
                                 (the Communication App's callback at
                                  /oauth/salla/callback).
                                 Returns JSON: {url, redirect_uri}.

      • /api/salla/oauth/start → NEW Sync OAuth API flow (added later).
                                 Uses SALLA_OAUTH_CLIENT_ID +
                                 SALLA_OAUTH_REDIRECT_URI (the dedicated Sync
                                 app's callback at /api/salla/oauth/callback).
                                 Returns 302 directly to Salla.
    ─────────────────────────────────────────────────────────────────────

    The dashboard's "ربط المتجر عبر سلة (OAuth)" button on
    /integrations was migrated to the new endpoint to avoid a
    redirect_uri mismatch error in Partner Portal — but this endpoint
    itself remains live for callers that target the basic app.
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
        "Salla authorize URL generated (LEGACY basic-app flow) | "
        "tenant=%s redirect_uri=%r (raw=%r)",
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


# ═══════════════════════════════════════════════════════════════════════════════
# DUAL INTEGRATION ARCHITECTURE — second "Sync" Custom OAuth app
# ───────────────────────────────────────────────────────────────────────────────
# These endpoints use SALLA_OAUTH_* credentials (a SEPARATE General/Custom
# OAuth app registered in Partner Portal) whose ONLY purpose is to obtain a
# real refresh_token for long-lived Admin API access (product/order/customer
# sync, automations, background pollers).
#
#   /api/salla/oauth/start          = NEW general-OAuth API-sync flow.
#                                      Public, accepts ?token=<jwt>, 302 →
#                                      accounts.salla.sa/oauth2/auth using
#                                      SALLA_OAUTH_CLIENT_ID +
#                                      SALLA_OAUTH_REDIRECT_URI.
#   /api/salla/oauth/callback       = NEW general-OAuth callback.
#                                      Public, Salla redirects here with ?code=,
#                                      exchanges via SALLA_OAUTH_CLIENT_SECRET,
#                                      then saves api_sync_enabled=True on the row.
#   /api/salla/integration-status   = JWT-protected, returns granular state so
#                                      /app/entry can render the "Connect API"
#                                      banner.
#
# Strict isolation contract:
#   • These endpoints MUST never read SALLA_CLIENT_ID / SALLA_CLIENT_SECRET /
#     SALLA_REDIRECT_URI / SALLA_WEBHOOK_SECRET at runtime.
#   • The legacy basic flow (/api/salla/authorize + /oauth/salla/callback) and
#     the Communication App webhook (/webhook/salla) MUST never read
#     SALLA_OAUTH_*.  Each app's secrets stay completely separate.
# ═══════════════════════════════════════════════════════════════════════════════


_API_SYNC_STATE_SUFFIX = "_apisync"

# Cookie used as a fallback when Salla strips the `state` query param.
# Set on /api/salla/oauth/start and read on /api/salla/oauth/callback.
# SameSite=Lax allows it to be sent on the top-level GET redirect from
# accounts.salla.sa back to api.nahlah.ai.  HttpOnly + Secure since it
# encodes the tenant_id and only the server should ever read it.
_OAUTH_STATE_COOKIE = "nahla_oauth_state"
_OAUTH_STATE_COOKIE_TTL_SECONDS = 600  # 10 minutes — OAuth flows complete in <60s


def _api_oauth_redirect_url(status: str, **extras: str) -> str:
    """Build the post-callback redirect URL on the dashboard origin.

    ``status`` is one of: ``success`` | ``error``.
    Extra kwargs are appended as query params (e.g. ``reason=<code>``).
    The dashboard reads ``?salla_oauth=<status>`` on /app/entry to refresh
    the integration status banner / show a toast.
    """
    qs = {"salla_oauth": status}
    qs.update({k: v for k, v in extras.items() if v})
    return f"{_DASHBOARD_ORIGIN}{_SALLA_POST_OAUTH_PATH}?{urllib.parse.urlencode(qs)}"


def _api_oauth_success_html(store_id: str, store_name: str) -> str:
    """Tiny HTML page shown after the Sync OAuth completes.

    Posts a message to the parent/opener window and tries to close the popup.
    Falls back to a plain Arabic confirmation line if no opener is available.
    """
    safe_store = (store_id or "").replace("<", "&lt;").replace(">", "&gt;")
    safe_name  = (store_name or "").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>تم تفعيل المزامنة الكاملة</title>
  <style>
    html, body {{
      margin: 0; padding: 0;
      background: #ffffff; color: #1f2937;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
                   Tahoma, Arial, sans-serif;
    }}
    body {{
      min-height: 100dvh;
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      padding: 24px; text-align: center; gap: 12px;
    }}
    h1 {{ font-size: 18px; margin: 0; color: #15803d; }}
    p  {{ font-size: 14px; line-height: 1.7; max-width: 420px; margin: 0; color: #374151; }}
    button {{
      margin-top: 14px; padding: 10px 22px;
      background: #f59e0b; color: #0f172a; border: none;
      border-radius: 10px; font-weight: 700; cursor: pointer;
      font-family: inherit; font-size: 14px;
    }}
  </style>
</head>
<body>
  <h1>تم تفعيل المزامنة الكاملة</h1>
  <p>تم ربط متجرك بنحلة عبر OAuth بنجاح. يمكنك إغلاق هذه النافذة والعودة إلى لوحة التحكم.</p>
  <button onclick="window.close()">إغلاق النافذة</button>
  <script>
    (function () {{
      var payload = {{
        type: 'salla_api_connected',
        store_id: '{safe_store}',
        store_name: '{safe_name}'
      }};
      try {{ if (window.opener) window.opener.postMessage(payload, '*'); }} catch (e) {{}}
      try {{ if (window.parent && window.parent !== window) window.parent.postMessage(payload, '*'); }} catch (e) {{}}
      setTimeout(function () {{ try {{ window.close(); }} catch (e) {{}} }}, 1500);
    }})();
  </script>
</body>
</html>"""


def _resolve_tenant_from_query_token(token: str) -> int:
    """Decode a JWT carried as a query-string parameter and return tenant_id.

    Used by ``/api/salla/oauth/start`` because the OAuth flow opens at the
    top window (escaping the Salla iframe) — the standard ``Authorization``
    header is not delivered by browser top-level navigation, so the dashboard
    embeds the JWT in the URL.  Only the signature and tenant_id claim
    matter here; the resulting code is bound to the tenant via ``state``.
    """
    if not token:
        raise HTTPException(status_code=401, detail="token query param required")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    tid = payload.get("tenant_id")
    if tid is None:
        raise HTTPException(status_code=401, detail="token missing tenant_id claim")
    try:
        return int(tid)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="token tenant_id is not an integer")


@router.get("/api/salla/oauth/start")
async def salla_api_oauth_start(request: Request, token: Optional[str] = None):
    """
    Start the Custom OAuth flow for the SECOND ("Sync") Salla app.

    Public endpoint — accepts the merchant's JWT as ``?token=`` because this
    URL is opened in ``window.top`` (the OAuth provider rejects iframes).
    The JWT is decoded server-side to extract ``tenant_id`` which is then
    embedded in the OAuth ``state`` so the callback can recover it without
    re-authenticating the user.

    Returns: 302 redirect to ``https://accounts.salla.sa/oauth2/auth``.
    """
    if not SALLA_OAUTH_CLIENT_ID:
        logger.error("[Salla API OAuth] SALLA_OAUTH_CLIENT_ID not configured!")
        raise HTTPException(
            status_code=503,
            detail="Salla OAuth (Sync) app not configured. Set SALLA_OAUTH_CLIENT_ID, "
                   "SALLA_OAUTH_CLIENT_SECRET, and SALLA_OAUTH_REDIRECT_URI.",
        )

    tenant_id = _resolve_tenant_from_query_token(token or "")

    normalized_redirect = (SALLA_OAUTH_REDIRECT_URI or "").strip().rstrip("/")
    scope_value = "offline_access"
    state  = f"t{tenant_id}_{_secrets.token_urlsafe(6)}{_API_SYNC_STATE_SUFFIX}"
    oauth_params = {
        "client_id":     SALLA_OAUTH_CLIENT_ID,
        "redirect_uri":  normalized_redirect,
        "response_type": "code",
        "scope":         scope_value,
        "state":         state,
    }
    params = urllib.parse.urlencode(oauth_params)
    auth_url = f"https://accounts.salla.sa/oauth2/auth?{params}"

    # Verbose logging of the EXACT OAuth URL being generated. The full
    # client_id is logged here intentionally — it's a public identifier
    # (Salla treats it as non-secret); only client_secret must be hidden.
    # This matches the diagnostic style of /api/salla/diag/oauth-config.
    client_id_masked = (SALLA_OAUTH_CLIENT_ID[:8] + "***") if SALLA_OAUTH_CLIENT_ID else "EMPTY"
    logger.info(
        "[Salla API OAuth] USING NEW FLOW\n"
        "  endpoint     = /api/salla/oauth/start\n"
        "  redirect_uri = %s",
        normalized_redirect,
    )
    logger.info(
        "[Salla API OAuth] BUTTON CLICKED — generating authorization URL | "
        "tenant_id=%s",
        tenant_id,
    )
    logger.info("[Salla API OAuth]   client_id (full)   = %s", SALLA_OAUTH_CLIENT_ID or "EMPTY")
    logger.info("[Salla API OAuth]   client_id (masked) = %s", client_id_masked)
    logger.info("[Salla API OAuth]   redirect_uri       = %r", normalized_redirect)
    logger.info("[Salla API OAuth]   scope              = %r", scope_value)
    logger.info("[Salla API OAuth]   response_type      = %r", oauth_params["response_type"])
    logger.info("[Salla API OAuth]   state              = %r", state)
    logger.info("[Salla API OAuth]   FULL_AUTH_URL      = %s", auth_url)
    # Defensive: confirm state IS in the URL we're about to redirect to.
    if "&state=" not in auth_url and "?state=" not in auth_url:
        logger.error("[Salla API OAuth] FULL_AUTH_URL is missing &state= — refusing to redirect")
        raise HTTPException(status_code=500, detail="oauth_state_missing_from_url")

    # Set a fallback cookie so the callback can still recover tenant_id even
    # if Salla's redirect strips the `state` query param.  SameSite=Lax is
    # critical: it allows the cookie to be sent on the top-level GET that
    # accounts.salla.sa issues back to api.nahlah.ai.
    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie(
        key=_OAUTH_STATE_COOKIE,
        value=state,
        max_age=_OAUTH_STATE_COOKIE_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/api/salla/oauth/",
    )
    logger.info("[Salla API OAuth]   state cookie set   = %s (max_age=%ss, samesite=lax)", _OAUTH_STATE_COOKIE, _OAUTH_STATE_COOKIE_TTL_SECONDS)
    return response


@router.get("/api/salla/oauth/callback")
async def salla_api_oauth_callback(
    request: Request,
    code:  Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db:    Session = Depends(get_db),
):
    """
    Callback for the SECOND ("Sync") Salla Custom OAuth app.

    Distinct from ``/oauth/salla/callback`` (the legacy Communication-App
    callback) because:
      • Uses SALLA_OAUTH_CLIENT_ID / SALLA_OAUTH_CLIENT_SECRET / SALLA_OAUTH_REDIRECT_URI.
      • Resolves tenant from ``state`` (which encodes ``t{tenant_id}_..._apisync``)
        OR from the ``nahla_oauth_state`` cookie set on /api/salla/oauth/start
        as a fallback when Salla strips the state query param.
      • On success, marks the integration row with ``api_sync_enabled=True``
        + ``api_canonical=True`` so ``pick_active_salla_integration`` will
        treat it as the canonical source of refresh_token going forward.
      • Redirects (302) to ``{DASHBOARD_URL}/app/entry?salla_oauth=success``
        on success or ``?salla_oauth=error&reason=<code>`` on failure.
        This endpoint NEVER requires a session token — tenant is resolved
        entirely from state/cookie.
    """
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )

    # ── CALLBACK HIT log (very first thing) ─────────────────────────────────
    cookie_state = request.cookies.get(_OAUTH_STATE_COOKIE) or ""
    logger.info(
        "[Salla API OAuth] CALLBACK HIT\n"
        "  code_present  = %s\n"
        "  state_present = %s\n"
        "  state_in_query= %r\n"
        "  state_in_cookie=%r\n"
        "  error         = %r\n"
        "  ip            = %s",
        bool(code), bool(state), state, cookie_state, error, client_ip,
    )

    if error:
        logger.warning("[Salla API OAuth] provider error: %s", error)
        return RedirectResponse(
            url=_api_oauth_redirect_url("error", reason=str(error)[:60]),
            status_code=302,
        )

    if not code:
        return RedirectResponse(
            url=_api_oauth_redirect_url("error", reason="missing_code"),
            status_code=302,
        )

    if not SALLA_OAUTH_CLIENT_ID or not SALLA_OAUTH_CLIENT_SECRET:
        logger.error("[Salla API OAuth] credentials not configured")
        return RedirectResponse(
            url=_api_oauth_redirect_url("error", reason="app_not_configured"),
            status_code=302,
        )

    # ── Resolve tenant: query state first, cookie as fallback ───────────────
    raw_state = (state or "").strip() or cookie_state.strip()
    state_source = "query" if state else ("cookie" if cookie_state else "missing")

    tenant_id = 0
    if raw_state.startswith("t") and "_" in raw_state:
        try:
            tenant_id = int(raw_state.split("_", 1)[0][1:])
        except ValueError:
            tenant_id = 0

    logger.info(
        "[Salla API OAuth] state resolution | source=%s tenant_resolved=%s raw_state=%r",
        state_source, tenant_id, raw_state,
    )

    if tenant_id <= 0:
        logger.error(
            "[Salla API OAuth] no tenant resolvable | query_state=%r cookie_state=%r",
            state, cookie_state,
        )
        return RedirectResponse(
            url=_api_oauth_redirect_url("error", reason="invalid_state"),
            status_code=302,
        )

    if not raw_state.endswith(_API_SYNC_STATE_SUFFIX):
        # Defensive: this callback is dedicated to the API Sync app — refuse
        # any state that wasn't produced by /api/salla/oauth/start.
        logger.warning(
            "[Salla API OAuth] state without expected suffix — possible cross-app mix-up | state=%s",
            raw_state,
        )
        return RedirectResponse(
            url=_api_oauth_redirect_url("error", reason="wrong_callback"),
            status_code=302,
        )

    # ── Token exchange ──────────────────────────────────────────────────────
    normalized_redirect = (SALLA_OAUTH_REDIRECT_URI or "").strip().rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token_resp = await client.post(
                "https://accounts.salla.sa/oauth2/token",
                data={
                    "grant_type":    "authorization_code",
                    "client_id":     SALLA_OAUTH_CLIENT_ID,
                    "client_secret": SALLA_OAUTH_CLIENT_SECRET,
                    "code":          code,
                    "redirect_uri":  normalized_redirect,
                },
                headers={
                    "Accept":       "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

            if token_resp.status_code != 200:
                try:
                    err_json = token_resp.json()
                    salla_err = err_json.get("error", "")
                    salla_msg = err_json.get("error_description", token_resp.text[:200])
                except Exception:
                    salla_err = "http_error"
                    salla_msg = token_resp.text[:200]
                logger.error(
                    "[Salla API OAuth] token exchange FAILED | http=%s err=%s desc=%s",
                    token_resp.status_code, salla_err, salla_msg,
                )
                return RedirectResponse(
                    url=_api_oauth_redirect_url("error", reason=f"token_exchange_failed:{(salla_err or 'http')[:30]}"),
                    status_code=302,
                )

            token_data    = token_resp.json()
            access_token  = token_data.get("access_token", "")
            refresh_token = token_data.get("refresh_token", "")
            expires_in    = token_data.get("expires_in", 0)
            token_type    = token_data.get("token_type", "Bearer")

            logger.info(
                "[Salla API OAuth] token exchange OK | access_token_present=%s refresh_token_present=%s expires_in=%s",
                bool(access_token), bool(refresh_token), expires_in,
            )

            if not refresh_token:
                # The whole point of this app is to obtain refresh_token —
                # if Salla didn't return one, the app config is wrong
                # (most likely scope is missing offline_access).
                logger.error(
                    "[Salla API OAuth] no refresh_token in response — check scope=offline_access "
                    "and that Partner Portal app is General/Custom OAuth (not Communication)"
                )
                return RedirectResponse(
                    url=_api_oauth_redirect_url("error", reason="no_refresh_token"),
                    status_code=302,
                )

            # ── Fetch store info to get the external store_id ───────────────
            salla_store_id = ""
            store_name     = ""
            store_resp = await client.get(
                "https://api.salla.dev/admin/v2/store/info",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept":        "application/json",
                },
            )
            if store_resp.status_code == 200:
                store_data = (store_resp.json() or {}).get("data", {}) or {}
                salla_store_id = str(store_data.get("id") or store_data.get("store_id") or "")
                store_name     = store_data.get("name") or store_data.get("store_name") or ""
            else:
                logger.warning(
                    "[Salla API OAuth] store/info fetch failed: %s — will fall back to existing store_id",
                    store_resp.status_code,
                )

    except httpx.TimeoutException as exc:
        logger.error("[Salla API OAuth] token exchange timed out: %s", exc)
        return RedirectResponse(
            url=_api_oauth_redirect_url("error", reason="timeout"),
            status_code=302,
        )
    except Exception as exc:
        logger.exception("[Salla API OAuth] unexpected error: %s", exc)
        return RedirectResponse(
            url=_api_oauth_redirect_url("error", reason="network_error"),
            status_code=302,
        )

    # ── Persist as the canonical Sync integration ───────────────────────────
    try:
        from services.salla_guard import claim_store_for_tenant  # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        existing = db.query(Integration).filter(
            Integration.tenant_id == tenant_id,
            Integration.provider  == "salla",
        ).first()

        # Fall back to whatever store_id we already have on file when Salla's
        # store/info call did not resolve one in this exchange.
        if not salla_store_id and existing:
            salla_store_id = (existing.config or {}).get("store_id", "") or (existing.external_store_id or "")
        if not store_name and existing:
            store_name = (existing.config or {}).get("store_name", "") or store_name

        now_iso = datetime.now(timezone.utc).isoformat()
        # Merge with existing config so we don't blow away embedded session
        # data (store_name, salla_owner_email, subscription metadata, etc.).
        merged_config: dict = dict((existing.config or {}) if existing else {})
        merged_config.update({
            "api_key":             access_token,
            "refresh_token":       refresh_token,
            "token_type":          token_type,
            "expires_in":          expires_in,
            "store_id":            salla_store_id or merged_config.get("store_id", ""),
            "store_name":          store_name or merged_config.get("store_name", ""),
            "api_sync_enabled":    True,
            "api_canonical":       True,
            "is_canonical":        True,
            "app_type":            "custom_oauth_sync",
            "api_key_source":      "custom_oauth_sync",
            "api_client_id":       SALLA_OAUTH_CLIENT_ID,
            "api_redirect_uri":    SALLA_OAUTH_REDIRECT_URI,
            "api_connected_at":    now_iso,
            "api_key_received_at": now_iso,
        })
        # Clear stale reauth flags — we just refreshed.
        for k in (
            "needs_reauth", "needs_reauth_at", "needs_reauth_reason",
            "no_auto_refresh", "no_auto_refresh_reason", "no_auto_refresh_at",
            "soft_disabled", "uninstalled_at", "disabled_reason", "disabled_at",
        ):
            merged_config.pop(k, None)

        if salla_store_id:
            integration = claim_store_for_tenant(
                db,
                store_id=salla_store_id,
                tenant_id=tenant_id,
                new_config=merged_config,
            )
            flag_modified(integration, "config")
        elif existing:
            existing.config  = merged_config
            existing.enabled = True
            flag_modified(existing, "config")
        else:
            db.add(Integration(
                tenant_id=tenant_id,
                provider="salla",
                config=merged_config,
                enabled=True,
            ))

        db.commit()
        logger.info(
            "[Salla API OAuth] OK | tenant=%s store_id=%s store=%r api_sync_enabled=True",
            tenant_id, salla_store_id, store_name,
        )
    except Exception as exc:
        logger.exception("[Salla API OAuth] DB save FAILED: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return RedirectResponse(
            url=_api_oauth_redirect_url("error", reason="db_save_failed"),
            status_code=302,
        )

    success_url = _api_oauth_redirect_url("success", store=salla_store_id or "")
    logger.info(
        "[Salla API OAuth] CALLBACK COMPLETE | tenant=%s store_id=%s -> %s",
        tenant_id, salla_store_id, success_url,
    )
    response = RedirectResponse(url=success_url, status_code=302)
    # Clear the state cookie — flow is complete.
    response.delete_cookie(_OAUTH_STATE_COOKIE, path="/api/salla/oauth/")
    return response


@router.get("/api/salla/integration-status")
async def salla_integration_status(request: Request, db: Session = Depends(get_db)):
    """
    Granular Salla integration state for the dashboard.

    Returns the data needed to render two distinct status cards in
    /app/entry: one for the Communication App (embedded session) and one
    for the Sync OAuth app (refresh_token + Admin API).

    Also returns ``oauth_start_url`` ready for the dashboard to open in
    ``window.top`` when ``api_sync_enabled`` is False.
    """
    from store_integration.registry import pick_active_salla_integration  # noqa: PLC0415

    tenant_id = get_jwt_tenant_id(request)
    integration = pick_active_salla_integration(db, tenant_id)

    embedded_connected   = False
    embedded_store_name  = ""
    embedded_store_id    = ""
    api_sync_enabled     = False
    api_connected_at     = ""
    api_canonical        = False
    is_easy_mode         = False
    has_refresh_token    = False
    has_any_api_key      = False

    if integration:
        cfg = integration.config or {}
        embedded_connected   = True
        embedded_store_name  = cfg.get("store_name", "") or ""
        embedded_store_id    = cfg.get("store_id", "") or (integration.external_store_id or "")
        has_refresh_token    = bool(cfg.get("refresh_token"))
        has_any_api_key      = bool(cfg.get("api_key"))
        api_sync_enabled     = bool(cfg.get("api_sync_enabled")) and has_refresh_token and bool(integration.enabled)
        api_canonical        = bool(cfg.get("api_canonical"))
        api_connected_at     = cfg.get("api_connected_at", "") or ""
        is_easy_mode         = (
            (cfg.get("app_type") or "").lower() == "easy"
            or (cfg.get("api_key_source") or "").lower() == "easy_mode_webhook"
        )

    # WhatsApp connection
    wa_connected = False
    try:
        wa_conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
        wa_connected = bool(
            wa_conn and wa_conn.status == "connected" and wa_conn.sending_enabled
        )
    except Exception as _exc:
        logger.warning("[integration-status] WA lookup failed tenant=%s: %s", tenant_id, _exc)

    sync_app_configured = bool(SALLA_OAUTH_CLIENT_ID and SALLA_OAUTH_CLIENT_SECRET)

    return {
        "embedded_connected":  embedded_connected,
        "embedded_store_id":   embedded_store_id,
        "embedded_store_name": embedded_store_name,
        "api_sync_enabled":    api_sync_enabled,
        "api_canonical":       api_canonical,
        "api_connected_at":    api_connected_at,
        "easy_mode":           is_easy_mode,
        "has_refresh_token":   has_refresh_token,
        "has_api_key":         has_any_api_key,
        "whatsapp_connected":  wa_connected,
        "sync_app_configured": sync_app_configured,
        "oauth_start_url":     "/api/salla/oauth/start",
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
    # All error states render inline HTML on api.nahlah.ai — we never bounce
    # the merchant to a Nahla page, per Salla embedded-app policy.
    if error:
        logger.warning("[Salla OAuth] Provider error: %s", error)
        return HTMLResponse(content=_install_error_html(error), status_code=400)

    if not code:
        logger.warning("[Salla OAuth] Missing code in callback")
        return HTMLResponse(content=_install_error_html("missing_code"), status_code=400)

    if not SALLA_CLIENT_ID or not SALLA_CLIENT_SECRET:
        logger.error("[Salla OAuth] SALLA_CLIENT_ID or SALLA_CLIENT_SECRET not configured")
        return HTMLResponse(content=_install_error_html("app_not_configured"), status_code=503)

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
                return HTMLResponse(
                    content=_install_error_html(f"token_exchange_failed: {salla_err or salla_msg}"),
                    status_code=400,
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
        return HTMLResponse(content=_install_error_html("timeout"), status_code=504)
    except Exception as exc:
        logger.exception("[Salla OAuth] Unexpected error during token exchange: %s", exc)
        return HTMLResponse(content=_install_error_html("network_error"), status_code=502)

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
            return HTMLResponse(content=_install_error_html("registration_failed"), status_code=500)

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
                return HTMLResponse(
                    content=_install_error_html("tenant_resolution_failed"),
                    status_code=500,
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
        return HTMLResponse(content=_install_error_html("db_save_failed"), status_code=500)

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

    # ── Step 5: Return a neutral 200 OK — DO NOT redirect anywhere ────────────
    # Salla's embedded-app contract is:
    #   • OAuth callback is a server-side step.
    #   • The merchant must enter the app via Salla's 'استخدام التطبيق'
    #     button — never via any auto-opened/redirected page from us.
    #
    # So we MUST NOT redirect to:
    #   ❌ app.nahlah.ai (any path)
    #   ❌ s.salla.sa/apps/nahla (still a navigation we initiate)
    #   ❌ salla.sa/dashboard (often shows 'المتجر مغلق' for setup-mode stores)
    #
    # We return a minimal, brand-free 200 OK HTML with one Arabic line
    # and stop.  The merchant returns to Salla on their own; Salla shows
    # the app + 'استخدام التطبيق' button naturally; the iframe loads
    # /app/salla on that click and creates the Nahla session via
    # /salla/token-login at that point.
    logger.info(
        "[SallaOAuth] install complete | tenant=%s store=%s is_new=%s "
        "has_jwt=%s — returning neutral 200 OK, NO redirect, NO Nahla UI",
        tenant_id, salla_store_id, is_new_merchant, bool(auto_jwt),
    )
    return HTMLResponse(content=_install_complete_html(), status_code=200)


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


def _install_complete_html() -> str:
    """
    Truly neutral post-OAuth response — minimal text only.

    Salla embedded-app policy: nothing from us should run, render, or
    redirect after OAuth completes.  The merchant must return to Salla
    on their own and press 'استخدام التطبيق'.  This page therefore:

      • Has no Nahla logo, no brand colour, no icon.
      • Does NOT call window.close().
      • Does NOT redirect anywhere (no meta refresh, no JS navigation).
      • Does NOT load any external resources.
      • Just shows one short Arabic line on a white background and stops.

    Salla's OAuth flow does not require any specific HTTP body or
    redirect target after token exchange — returning a plain 200 OK is
    sufficient.  We return minimal HTML purely so a merchant who sees
    this tab understands what happened, instead of a blank page.
    """
    return """<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>تم الربط</title>
  <style>
    html, body {
      margin: 0; padding: 0;
      background: #ffffff; color: #1f2937;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
                   Tahoma, Arial, sans-serif;
    }
    body {
      min-height: 100dvh;
      display: flex; align-items: center; justify-content: center;
      padding: 24px; text-align: center;
    }
    p { font-size: 15px; line-height: 1.8; max-width: 420px; margin: 0; }
  </style>
</head>
<body>
  <p>تم الربط بنجاح. يمكنك إغلاق هذه الصفحة والعودة إلى سلة.</p>
</body>
</html>"""


def _install_error_html(reason: str) -> str:
    """
    Neutral error response — same minimal style as the success page.

    No Nahla branding, no auto-close, no redirect.  Includes the raw
    error reason as a small code line so a merchant or support agent
    can report it back to us.
    """
    safe_reason = (reason or "unknown_error").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>تعذّر إتمام الربط</title>
  <style>
    html, body {{
      margin: 0; padding: 0;
      background: #ffffff; color: #1f2937;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
                   Tahoma, Arial, sans-serif;
    }}
    body {{
      min-height: 100dvh;
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      padding: 24px; text-align: center;
    }}
    p {{ font-size: 15px; line-height: 1.8; max-width: 420px; margin: 0; }}
    code {{
      display: inline-block; margin-top: 16px;
      background: #f3f4f6; color: #6b7280;
      padding: 4px 10px; border-radius: 6px; font-size: 12px;
    }}
  </style>
</head>
<body>
  <p>تعذّر إتمام الربط. يمكنك إغلاق هذه الصفحة وإعادة المحاولة من سلة.</p>
  <code>{safe_reason}</code>
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

    0. Easy Mode merchants — if the integration was installed via the
       Salla App Store (app_type='easy' or api_key_source='easy_mode_webhook'),
       OAuth authorize WILL fail with redirect_uri mismatch because Easy
       Mode apps don't have a registered redirect_uri.  Return clear
       reinstall instructions instead.
    1. Silent refresh  — use refresh_token if present (Custom OAuth flow).
    2. Reactivate      — if no refresh_token but api_key exists, re-enable the
                         integration as-is (manual/long-lived token mode).
    3. OAuth redirect  — last resort for Custom OAuth apps only.

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
    app_type      = (cfg.get("app_type")       or "").lower()
    api_key_src   = (cfg.get("api_key_source") or "").lower()
    client_id     = SALLA_CLIENT_ID     or ""
    client_secret = SALLA_CLIENT_SECRET or ""

    is_easy_mode = (
        app_type == "easy"
        or api_key_src == "easy_mode_webhook"
    )

    # ── Path 0: Easy Mode merchants — reinstall instructions, never OAuth ────
    # Custom OAuth authorize requires a registered redirect_uri; Easy Mode
    # apps do not have one so the redirect would 400.  The proper flow is
    # to uninstall + reinstall from Salla → My Apps, which triggers an
    # app.store.authorize webhook that drops fresh tokens into the
    # Integration row and clears needs_reauth.
    if is_easy_mode:
        # If a silent refresh might still work (refresh_token + client creds
        # exist), try it once before bothering the merchant — Easy Mode does
        # provide refresh_tokens via the webhook.
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
                        cfg.pop("needs_reauth",          None)
                        cfg.pop("needs_reauth_reason",   None)
                        cfg.pop("needs_reauth_at",       None)
                        cfg.pop("no_auto_refresh",       None)
                        cfg.pop("no_auto_refresh_reason",None)
                        intg.config  = cfg
                        intg.enabled = True
                        db.commit()
                        logger.info(
                            "[SallaReconnect/Easy] silent refresh succeeded | tenant=%s",
                            tenant_id,
                        )
                        return {
                            "action":  "refreshed",
                            "message": "تم تجديد التوكن تلقائياً — الربط فعّال الآن",
                        }
            except Exception as exc:
                logger.warning(
                    "[SallaReconnect/Easy] silent refresh attempt failed | "
                    "tenant=%s: %s — falling back to reinstall instructions",
                    tenant_id, exc,
                )

        logger.info(
            "[SallaReconnect/Easy] returning reinstall instructions | "
            "tenant=%s app_type=%s api_key_source=%s",
            tenant_id, app_type or "?", api_key_src or "?",
        )
        return {
            "action":         "easy_reinstall_required",
            "message": (
                "لإعادة ربط سلة، احذف تطبيق نحلة من «تطبيقاتي» في سلة "
                "ثم أعد تثبيته. بعد التثبيت سيصل الربط تلقائياً عبر "
                "Webhook خلال ثوانٍ."
            ),
            "salla_apps_url": "https://s.salla.sa/apps",
            "steps": [
                "افتح حسابك في سلة → «تطبيقاتي»",
                "ابحث عن «نحلة» واضغط «إلغاء التثبيت»",
                "ثم اضغط «تثبيت» مرة أخرى من نفس الصفحة",
                "ستعود إلى نحلة تلقائياً خلال 5–10 ثوانٍ بربط جديد",
            ],
            "note": (
                "أنت تستخدم تطبيق سلة Easy Mode، لذلك إعادة الربط تتم "
                "من داخل سلة وليس عبر OAuth خارجي."
            ),
        }

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
