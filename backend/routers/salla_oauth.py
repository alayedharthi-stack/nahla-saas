"""
routers/salla_oauth.py
───────────────────────
Salla OAuth 2.0 flow and store data endpoints.

Routes
  GET  /api/salla/authorize
  GET  /oauth/salla/callback
  GET  /api/salla/store
  GET  /api/salla/products
  POST /api/salla/test-coupon
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from models import Integration  # noqa: E402

from core.audit import audit
from core.auth import get_jwt_tenant_id
from core.config import DASHBOARD_URL, SALLA_CLIENT_ID, SALLA_CLIENT_SECRET, SALLA_REDIRECT_URI
from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id

# Strip accidental "KEY=value" prefix if DASHBOARD_URL was set incorrectly
_DASHBOARD = DASHBOARD_URL.split("=", 1)[-1] if "=" in DASHBOARD_URL else DASHBOARD_URL
_DASHBOARD = _DASHBOARD.rstrip("/") or "https://app.nahlah.ai"

logger = logging.getLogger("nahla-backend")

router = APIRouter(tags=["Salla OAuth"])


@router.get("/api/salla/authorize")
async def salla_authorize(request: Request):
    """Return the Salla OAuth authorization URL for this tenant."""
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
    return {"url": f"https://accounts.salla.sa/oauth2/auth?{params}"}


@router.get("/oauth/salla/callback")
async def salla_oauth_callback(
    request: Request,
    code:  str = None,
    state: str = None,
    error: str = None,
    db:    Session = Depends(get_db),
):
    """
    Salla OAuth 2.0 callback — full token exchange flow.
    Salla redirects here after the merchant authorises the app.
    Exchanges the code for tokens, fetches store info, saves to DB,
    then redirects to the dashboard success or error page.
    """
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )
    logger.info(
        "Salla OAuth callback received | code=%s state=%s error=%s ip=%s",
        bool(code), state, error, client_ip,
    )

    try:
        tenant_id = int(state) if state else 1
    except (ValueError, TypeError):
        tenant_id = 1
    logger.info("Salla OAuth: resolved tenant_id=%s", tenant_id)

    if error:
        logger.warning("Salla OAuth provider error: %s", error)
        return RedirectResponse(
            url=f"{_DASHBOARD}/store-integration?salla_error={error}", status_code=302,
        )

    if not code:
        logger.warning("Salla OAuth callback: missing code")
        return RedirectResponse(
            url=f"{_DASHBOARD}/store-integration?salla_error=missing_code", status_code=302,
        )

    if not SALLA_CLIENT_ID or not SALLA_CLIENT_SECRET:
        logger.error("Salla OAuth: SALLA_CLIENT_ID or SALLA_CLIENT_SECRET not set")
        return RedirectResponse(
            url=f"{_DASHBOARD}/store-integration?salla_error=app_not_configured", status_code=302,
        )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            logger.info("Salla OAuth: exchanging code for token …")
            token_resp = await client.post(
                "https://accounts.salla.sa/oauth2/token",
                data={
                    "grant_type":    "authorization_code",
                    "client_id":     SALLA_CLIENT_ID,
                    "client_secret": SALLA_CLIENT_SECRET,
                    "code":          code,
                    "redirect_uri":  SALLA_REDIRECT_URI,
                },
                headers={"Accept": "application/json"},
            )
            logger.info("Salla token endpoint response: status=%s", token_resp.status_code)
            if token_resp.status_code != 200:
                logger.error(
                    "Salla token exchange failed: %s %s",
                    token_resp.status_code, token_resp.text[:500],
                )
                return RedirectResponse(
                    url="/integrations/salla/error?reason=token_exchange_failed", status_code=302,
                )

            token_data    = token_resp.json()
            access_token  = token_data.get("access_token", "")
            refresh_token = token_data.get("refresh_token", "")
            expires_in    = token_data.get("expires_in", 0)
            logger.info("Salla OAuth: token exchange succeeded | expires_in=%s", expires_in)

            logger.info("Salla OAuth: fetching store info …")
            store_resp = await client.get(
                "https://api.salla.dev/admin/v2/store",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            logger.info("Salla store info response: status=%s", store_resp.status_code)
            salla_store_id = ""
            store_name     = ""
            if store_resp.status_code == 200:
                store_json     = store_resp.json()
                store_data     = store_json.get("data", {})
                salla_store_id = str(store_data.get("id", ""))
                store_name     = store_data.get("name", "")
                logger.info("Salla store info: id=%s name=%s", salla_store_id, store_name)
            else:
                logger.warning("Salla store info fetch failed: %s", store_resp.status_code)

    except Exception as exc:
        logger.exception("Salla OAuth: unexpected error during token exchange: %s", exc)
        return RedirectResponse(
            url=f"{_DASHBOARD}/store-integration?salla_error=network_error", status_code=302,
        )

    try:
        get_or_create_tenant(db, tenant_id)
        integration = db.query(Integration).filter(
            Integration.tenant_id == tenant_id,
            Integration.provider  == "salla",
        ).first()

        new_config = {
            "api_key":       access_token,
            "store_id":      salla_store_id,
            "refresh_token": refresh_token,
            "store_name":    store_name,
            "expires_in":    expires_in,
            "connected_at":  datetime.now(timezone.utc).isoformat(),
        }

        if integration:
            integration.config  = new_config
            integration.enabled = True
            logger.info("Salla OAuth: updated existing integration for tenant %s", tenant_id)
        else:
            integration = Integration(
                tenant_id=tenant_id,
                provider="salla",
                config=new_config,
                enabled=True,
            )
            db.add(integration)
            logger.info("Salla OAuth: created new integration for tenant %s", tenant_id)

        db.commit()
        logger.info(
            "Salla OAuth: integration saved | tenant=%s store_id=%s store_name=%s",
            tenant_id, salla_store_id, store_name,
        )
    except Exception as exc:
        logger.exception("Salla OAuth: failed to save integration: %s", exc)
        return RedirectResponse(
            url=f"{_DASHBOARD}/store-integration?salla_error=db_save_failed", status_code=302,
        )

    logger.info("Salla OAuth: flow complete — redirecting to success page")
    store_name_enc = urllib.parse.quote(store_name)
    return RedirectResponse(
        url=f"{_DASHBOARD}/store-integration?salla_connected=true&store={salla_store_id}&name={store_name_enc}",
        status_code=302,
    )


@router.get("/api/salla/store")
async def get_salla_store(
    request:   Request,
    db:        Session = Depends(get_db),
    tenant_id: int     = Depends(get_jwt_tenant_id),
):
    """Return saved Salla store info for this tenant (JWT tenant_id only)."""
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
        "connected_at": cfg.get("connected_at"),
        "api_key_hint": ("***" + cfg.get("api_key", "")[-4:]) if cfg.get("api_key") else "",
    }


@router.get("/api/salla/products")
async def get_salla_products(
    request:   Request,
    tenant_id: int = Depends(get_jwt_tenant_id),
):
    """Fetch live products from the tenant's Salla store (JWT tenant_id only)."""
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
    """Validate a coupon code against the tenant's Salla store (JWT tenant_id only)."""
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
