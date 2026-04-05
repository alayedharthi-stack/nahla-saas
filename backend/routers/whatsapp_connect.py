"""
routers/whatsapp_connect.py
────────────────────────────
WhatsApp / Meta Embedded Signup flow and connection management.

State machine
  not_connected → pending → connected
                         ↘ error
  connected → needs_reauth (token expired)
  connected → disconnected  (merchant manually disconnects)

Routes
  GET  /whatsapp/connection          — current connection status (safe fields only)
  POST /whatsapp/connection/start    — mark connection as pending, return Meta auth URL
  POST /whatsapp/connection/callback — receive embedded signup data, exchange with Meta
  POST /whatsapp/connection/verify   — re-verify active connection prerequisites
  POST /whatsapp/connection/disconnect
  POST /whatsapp/connection/reconnect
  GET  /whatsapp/connection/health
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import WhatsAppConnection  # noqa: E402

from core.config import META_APP_ID, META_APP_SECRET, META_GRAPH_API_VERSION
from core.database import get_db
from core.tenant import get_or_create_tenant, resolve_tenant_id

logger = logging.getLogger("nahla-backend")

router = APIRouter(prefix="/whatsapp", tags=["WhatsApp Connection"])

GRAPH_BASE = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"

# ── Pydantic schemas ──────────────────────────────────────────────────────────

class EmbeddedSignupCallbackIn(BaseModel):
    """Payload sent from frontend after the Facebook SDK embedded-signup flow."""
    code:  str
    state: Optional[str] = None
    # Optional fields the SDK may return directly
    waba_id:          Optional[str] = None
    phone_number_id:  Optional[str] = None
    business_id:      Optional[str] = None


class ConnectionStatusOut(BaseModel):
    """Safe public view of the connection — no tokens."""
    status:                       str
    phone_number:                 Optional[str]  = None
    business_display_name:        Optional[str]  = None
    whatsapp_business_account_id: Optional[str]  = None
    phone_number_id:              Optional[str]  = None
    meta_business_account_id:     Optional[str]  = None
    connected_at:                 Optional[str]  = None
    last_verified_at:             Optional[str]  = None
    last_attempt_at:              Optional[str]  = None
    last_error:                   Optional[str]  = None
    webhook_verified:             bool           = False
    sending_enabled:              bool           = False
    token_expires_at:             Optional[str]  = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_or_create_connection(db: Session, tenant_id: int) -> WhatsAppConnection:
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn:
        conn = WhatsAppConnection(tenant_id=tenant_id, status="not_connected")
        db.add(conn)
        db.flush()
    return conn


def _safe_view(conn: WhatsAppConnection) -> dict:
    """Return a connection dict safe for the frontend (no access_token)."""
    return {
        "status":                       conn.status,
        "phone_number":                 conn.phone_number,
        "business_display_name":        conn.business_display_name,
        "whatsapp_business_account_id": conn.whatsapp_business_account_id,
        "phone_number_id":              conn.phone_number_id,
        "meta_business_account_id":     conn.meta_business_account_id,
        "connected_at":                 conn.connected_at.isoformat() if conn.connected_at else None,
        "last_verified_at":             conn.last_verified_at.isoformat() if conn.last_verified_at else None,
        "last_attempt_at":              conn.last_attempt_at.isoformat() if conn.last_attempt_at else None,
        "last_error":                   conn.last_error,
        "webhook_verified":             bool(conn.webhook_verified),
        "sending_enabled":              bool(conn.sending_enabled),
        "token_expires_at":             conn.token_expires_at.isoformat() if conn.token_expires_at else None,
    }


async def _exchange_code_for_token(code: str) -> dict:
    """
    Exchange the short-lived code returned by Meta Embedded Signup for a
    system-user token or a long-lived page token.
    Returns a dict with at minimum {'access_token': ..., 'token_type': ...}
    """
    if not META_APP_ID or not META_APP_SECRET:
        raise HTTPException(
            status_code=503,
            detail="META_APP_ID / META_APP_SECRET are not configured on this server.",
        )
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "client_id":     META_APP_ID,
                "client_secret": META_APP_SECRET,
                "code":          code,
                "redirect_uri":  "",  # Embedded signup uses empty redirect_uri
            },
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Meta token exchange failed: {resp.text[:400]}",
        )
    return resp.json()


async def _fetch_waba_info(token: str, waba_id: str) -> dict:
    """Fetch WABA details from Graph API."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/{waba_id}",
            params={
                "fields": "id,name,currency,message_template_namespace,on_behalf_of_business_info",
                "access_token": token,
            },
        )
    if resp.status_code != 200:
        return {}
    return resp.json()


async def _fetch_phone_number_info(token: str, phone_number_id: str) -> dict:
    """Fetch phone number details from Graph API."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/{phone_number_id}",
            params={
                "fields": "id,display_phone_number,verified_name,code_verification_status",
                "access_token": token,
            },
        )
    if resp.status_code != 200:
        return {}
    return resp.json()


async def _exchange_for_long_lived_token(short_token: str) -> dict:
    """Exchange a short-lived user token for a 60-day long-lived token."""
    if not META_APP_ID or not META_APP_SECRET:
        return {"access_token": short_token, "token_type": "short_lived"}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "grant_type":        "fb_exchange_token",
                "client_id":         META_APP_ID,
                "client_secret":     META_APP_SECRET,
                "fb_exchange_token": short_token,
            },
        )
    if resp.status_code != 200:
        return {"access_token": short_token, "token_type": "short_lived"}
    data = resp.json()
    return {
        "access_token": data.get("access_token", short_token),
        "token_type":   "long_lived",
        "expires_in":   data.get("expires_in", 5183944),   # ~60 days
    }


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/connection")
async def get_connection_status(request: Request, db: Session = Depends(get_db)):
    """Return the current WhatsApp connection status for this tenant."""
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    conn = _get_or_create_connection(db, tenant_id)
    db.commit()

    # Surface needs_reauth if token has expired
    if conn.status == "connected" and conn.token_expires_at:
        if datetime.now(timezone.utc) > conn.token_expires_at:
            conn.status = "needs_reauth"
            db.commit()

    return _safe_view(conn)


@router.post("/connection/start")
async def start_connection(request: Request, db: Session = Depends(get_db)):
    """
    Mark the connection as pending and return the Meta Embedded Signup
    configuration the frontend needs to open the FB.login() popup.
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    conn = _get_or_create_connection(db, tenant_id)

    conn.status           = "pending"
    conn.last_attempt_at  = datetime.now(timezone.utc)
    conn.last_error       = None
    db.commit()

    return {
        "status":      "pending",
        "meta_app_id": META_APP_ID or "CONFIGURE_META_APP_ID",
        "graph_version": META_GRAPH_API_VERSION,
        "scope":       "whatsapp_business_management,whatsapp_business_messaging",
        "extras": {
            "feature":        "whatsapp_embedded_signup",
            "setup": {
                "solutionID": "",   # optional: your Meta Solution ID
            },
        },
    }


@router.post("/connection/callback")
async def embedded_signup_callback(
    body: EmbeddedSignupCallbackIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Called by the frontend after the Meta SDK returns control.
    Exchanges the code for tokens, fetches WABA/phone info, persists everything.
    """
    tenant_id = resolve_tenant_id(request)
    get_or_create_tenant(db, tenant_id)
    conn = _get_or_create_connection(db, tenant_id)
    conn.last_attempt_at = datetime.now(timezone.utc)

    try:
        # 1. Exchange code → short-lived user token
        token_data = await _exchange_code_for_token(body.code)
        short_token = token_data.get("access_token", "")

        # 2. Upgrade to long-lived token (~60 days)
        long_data   = await _exchange_for_long_lived_token(short_token)
        token       = long_data["access_token"]
        token_type  = long_data.get("token_type", "long_lived")
        expires_in  = long_data.get("expires_in", 5183944)

        # 3. Store WABA identifier — prefer value from callback body, else derive
        waba_id = body.waba_id or ""
        phone_id = body.phone_number_id or ""

        # 4. Fetch WABA details if we have an ID
        waba_info  = await _fetch_waba_info(token, waba_id) if waba_id else {}
        phone_info = await _fetch_phone_number_info(token, phone_id) if phone_id else {}

        # 5. Persist
        conn.status                       = "connected"
        conn.access_token                 = token
        conn.token_type                   = token_type
        conn.token_expires_at             = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        conn.whatsapp_business_account_id = waba_id or waba_info.get("id")
        conn.phone_number_id              = phone_id or phone_info.get("id")
        conn.phone_number                 = phone_info.get("display_phone_number")
        conn.business_display_name        = (
            phone_info.get("verified_name")
            or waba_info.get("name")
        )
        conn.meta_business_account_id     = (
            body.business_id
            or (waba_info.get("on_behalf_of_business_info") or {}).get("id")
        )
        conn.connected_at                 = datetime.now(timezone.utc)
        conn.last_error                   = None
        conn.webhook_verified             = False  # must be checked separately
        conn.sending_enabled              = bool(conn.phone_number_id and conn.whatsapp_business_account_id)

        db.commit()
        logger.info(
            "tenant=%s WhatsApp connected — WABA=%s phone=%s",
            tenant_id, conn.whatsapp_business_account_id, conn.phone_number,
        )
        return {"status": "connected", **_safe_view(conn)}

    except HTTPException:
        raise
    except Exception as exc:
        conn.status     = "error"
        conn.last_error = str(exc)[:1000]
        db.commit()
        logger.error("tenant=%s WhatsApp callback error: %s", tenant_id, exc)
        raise HTTPException(status_code=502, detail=f"Meta callback failed: {exc}") from exc


@router.post("/connection/verify")
async def verify_connection(request: Request, db: Session = Depends(get_db)):
    """
    Re-verify that the stored connection is still valid by pinging Meta.
    Updates webhook_verified and sending_enabled flags.
    """
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn or conn.status not in ("connected", "needs_reauth"):
        return {"verified": False, "reason": "no_connection"}

    if not conn.access_token:
        conn.status = "needs_reauth"
        db.commit()
        return {"verified": False, "reason": "missing_token"}

    try:
        # Ping the phone number ID to verify token is still valid
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{GRAPH_BASE}/{conn.phone_number_id or 'me'}",
                params={
                    "fields":       "id,display_phone_number,code_verification_status",
                    "access_token": conn.access_token,
                },
            )

        if resp.status_code == 401:
            conn.status     = "needs_reauth"
            conn.last_error = "Token expired or revoked"
            db.commit()
            return {"verified": False, "reason": "token_expired"}

        if resp.status_code == 200:
            data = resp.json()
            conn.status           = "connected"
            conn.last_verified_at = datetime.now(timezone.utc)
            conn.sending_enabled  = data.get("code_verification_status") == "VERIFIED"
            conn.last_error       = None
            db.commit()
            return {"verified": True, "sending_enabled": conn.sending_enabled}

        conn.last_error = f"Meta returned {resp.status_code}"
        db.commit()
        return {"verified": False, "reason": conn.last_error}

    except Exception as exc:
        conn.last_error = str(exc)[:500]
        db.commit()
        return {"verified": False, "reason": str(exc)}


@router.post("/connection/disconnect")
async def disconnect(request: Request, db: Session = Depends(get_db)):
    """Merchant disconnects WhatsApp — wipes token, preserves identifiers for re-connect."""
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn:
        return {"status": "not_connected"}

    conn.status          = "disconnected"
    conn.access_token    = None
    conn.token_type      = None
    conn.token_expires_at = None
    conn.sending_enabled = False
    conn.last_error      = None
    db.commit()
    logger.info("tenant=%s WhatsApp disconnected", tenant_id)
    return {"status": "disconnected"}


@router.post("/connection/reconnect")
async def reconnect(request: Request, db: Session = Depends(get_db)):
    """Reset the connection to 'pending' so the merchant can run Embedded Signup again."""
    tenant_id = resolve_tenant_id(request)
    conn = _get_or_create_connection(db, tenant_id)

    conn.status          = "pending"
    conn.last_attempt_at = datetime.now(timezone.utc)
    conn.last_error      = None
    db.commit()
    return {
        "status":      "pending",
        "meta_app_id": META_APP_ID or "CONFIGURE_META_APP_ID",
        "graph_version": META_GRAPH_API_VERSION,
        "scope":       "whatsapp_business_management,whatsapp_business_messaging",
        "extras": {
            "feature": "whatsapp_embedded_signup",
        },
    }


@router.get("/connection/health")
async def connection_health(request: Request, db: Session = Depends(get_db)):
    """Quick health-check endpoint for the merchant troubleshooting panel."""
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn or conn.status == "not_connected":
        return {
            "healthy": False,
            "status":  "not_connected",
            "checks": {
                "has_connection":     False,
                "token_present":      False,
                "token_valid":        False,
                "webhook_verified":   False,
                "sending_enabled":    False,
            },
        }

    token_present = bool(conn.access_token)
    token_valid   = (
        token_present
        and (not conn.token_expires_at or datetime.now(timezone.utc) < conn.token_expires_at)
    )

    checks = {
        "has_connection":   conn.status in ("connected", "pending"),
        "token_present":    token_present,
        "token_valid":      token_valid,
        "webhook_verified": bool(conn.webhook_verified),
        "sending_enabled":  bool(conn.sending_enabled),
    }
    healthy = all(checks.values())

    return {
        "healthy":       healthy,
        "status":        conn.status,
        "phone_number":  conn.phone_number,
        "checks":        checks,
        "last_verified": conn.last_verified_at.isoformat() if conn.last_verified_at else None,
        "last_error":    conn.last_error,
    }
