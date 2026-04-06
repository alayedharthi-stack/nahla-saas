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
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from models import Integration

from core.audit import audit
from core.auth import get_jwt_tenant_id
from core.config import (
    DASHBOARD_URL,
    SALLA_CLIENT_ID,
    SALLA_CLIENT_SECRET,
    SALLA_EMBEDDED_URL,
    SALLA_REDIRECT_URI,
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


def _success_url(store_id: str = "", store_name: str = "") -> str:
    """Build the post-OAuth success redirect URL."""
    params = urllib.parse.urlencode({
        "status": "connected",
        "store":  store_id,
        "name":   store_name,
    })
    return f"{_SALLA_APP}?{params}"


def _error_url(reason: str, detail: str = "") -> str:
    """Build the post-OAuth error redirect URL."""
    params: dict = {"status": "error", "reason": reason}
    if detail:
        params["detail"] = detail[:200]   # truncate to avoid oversized URLs
    return f"{_SALLA_APP}?{urllib.parse.urlencode(params)}"


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC ROUTES — OAuth flow (no JWT required)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/api/salla/authorize")
async def salla_authorize(request: Request):
    """
    Returns the Salla OAuth authorization URL.
    Frontend opens this URL to start the OAuth flow.
    """
    tenant_id = resolve_tenant_id(request)
    if not SALLA_CLIENT_ID:
        raise HTTPException(status_code=503, detail="SALLA_CLIENT_ID not configured")

    params = urllib.parse.urlencode({
        "client_id":     SALLA_CLIENT_ID,
        "redirect_uri":  SALLA_REDIRECT_URI,
        "response_type": "code",
        "scope":         "offline_access",
        "state":         str(tenant_id),
    })
    auth_url = f"https://accounts.salla.sa/oauth2/auth?{params}"
    logger.info(
        "Salla authorize URL generated | tenant=%s redirect_uri=%s",
        tenant_id, SALLA_REDIRECT_URI,
    )
    return {"url": auth_url, "redirect_uri": SALLA_REDIRECT_URI}


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
    try:
        tenant_id = int(state) if state else 1
    except (ValueError, TypeError):
        tenant_id = 1
    logger.info("[Salla OAuth] tenant_id=%s", tenant_id)

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
    logger.info("[Salla OAuth] Starting token exchange...")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token_resp = await client.post(
                "https://accounts.salla.sa/oauth2/token",
                data={
                    "grant_type":    "authorization_code",
                    "client_id":     SALLA_CLIENT_ID,
                    "client_secret": SALLA_CLIENT_SECRET,
                    "code":          code,
                    "redirect_uri":  SALLA_REDIRECT_URI,
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
                salla_store_id = str(store_data.get("id", ""))
                store_name     = store_data.get("name", "")
                merchant_id    = str(store_data.get("merchant", {}).get("id", "")) if isinstance(
                    store_data.get("merchant"), dict
                ) else str(store_data.get("merchant", ""))
                logger.info(
                    "[Salla OAuth] Store info: id=%s name=%s merchant_id=%s",
                    salla_store_id, store_name, merchant_id,
                )
            else:
                logger.warning(
                    "[Salla OAuth] Store info fetch failed: %s %.200s",
                    store_resp.status_code, store_resp.text,
                )

    except httpx.TimeoutException as exc:
        logger.error("[Salla OAuth] Token exchange timed out: %s", exc)
        return RedirectResponse(url=_error_url("timeout"), status_code=302)
    except Exception as exc:
        logger.exception("[Salla OAuth] Unexpected error during token exchange: %s", exc)
        return RedirectResponse(url=_error_url("network_error"), status_code=302)

    # ── Step 4: Save integration to DB ─────────────────────────────────────────
    logger.info("[Salla OAuth] Saving integration to DB | tenant=%s", tenant_id)
    try:
        get_or_create_tenant(db, tenant_id)
        integration = db.query(Integration).filter(
            Integration.tenant_id == tenant_id,
            Integration.provider  == "salla",
        ).first()

        new_config = {
            "api_key":       access_token,
            "refresh_token": refresh_token,
            "token_type":    token_type,
            "expires_in":    expires_in,
            "store_id":      salla_store_id,
            "store_name":    store_name,
            "merchant_id":   merchant_id,
            "redirect_uri":  SALLA_REDIRECT_URI,   # stored for future refresh calls
            "connected_at":  datetime.now(timezone.utc).isoformat(),
        }

        if integration:
            integration.config  = new_config
            integration.enabled = True
            logger.info("[Salla OAuth] Updated existing integration for tenant=%s", tenant_id)
        else:
            db.add(Integration(
                tenant_id=tenant_id,
                provider="salla",
                config=new_config,
                enabled=True,
            ))
            logger.info("[Salla OAuth] Created new integration for tenant=%s", tenant_id)

        db.commit()
        logger.info(
            "[Salla OAuth] DB save SUCCESS | tenant=%s store_id=%s store_name=%s",
            tenant_id, salla_store_id, store_name,
        )
    except Exception as exc:
        logger.exception("[Salla OAuth] DB save FAILED: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return RedirectResponse(url=_error_url("db_save_failed"), status_code=302)

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

    # ── Step 5: Redirect to embedded app ───────────────────────────────────────
    success_url = _success_url(salla_store_id, store_name)
    logger.info("[Salla OAuth] Flow complete — redirecting to %s", success_url)
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
    dest  = f"{_SALLA_APP}?status=connected&store={store}&name={name}"
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
    dest   = f"{_SALLA_APP}?status=error&reason={reason}"
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
