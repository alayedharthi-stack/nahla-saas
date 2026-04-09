"""
WhatsApp Embedded Signup — per-merchant WABA flow.

Each merchant goes through Meta's official Embedded Signup, which grants
Nahla access to the merchant's OWN WhatsApp Business Account (WABA).
This avoids the need for platform-level BSP permissions.

Flow:
  1. Frontend loads FB SDK and shows "Connect WhatsApp" button.
  2. Merchant clicks → FB.login() popup opens.
  3. Merchant logs in, creates/picks WABA and phone number.
  4. Popup closes → callback returns a short-lived `code`.
  5. Frontend POSTs code to  POST /whatsapp/embedded/exchange.
  6. Backend exchanges code → user token → lists WABA → subscribes app.
  7. Merchant's WABA ID, phone_number_id, token stored in DB.
  8. All future messaging uses merchant's own token & WABA.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.config import (
    META_APP_ID,
    META_APP_SECRET,
    META_GRAPH_API_VERSION,
    META_WA_CONFIG_ID,
)
from core.database import get_db
from database.models import WhatsAppConnection

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/whatsapp/embedded", tags=["whatsapp-embedded"])

GRAPH = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"


# ── helpers ───────────────────────────────────────────────────────────────────

def resolve_tenant_id(request: Request) -> int:
    tid = request.headers.get("X-Tenant-ID") or request.state.__dict__.get("tenant_id")
    if not tid:
        raise HTTPException(status_code=401, detail="tenant_id مفقود")
    return int(tid)


async def _exchange_code_for_token(code: str, redirect_uri: str = "") -> dict:
    """Exchange a short-lived code for a user access token."""
    params: dict = {
        "client_id": META_APP_ID,
        "client_secret": META_APP_SECRET,
        "code": code,
    }
    if redirect_uri:
        params["redirect_uri"] = redirect_uri

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{GRAPH}/oauth/access_token", params=params)
        data = resp.json()

    logger.info("[EmbeddedSignup] token exchange: http=%s body=%s", resp.status_code, data)
    if "error" in data:
        raise HTTPException(
            status_code=400,
            detail=f"فشل تبادل الكود مع Meta: {data['error'].get('message', '')}",
        )
    return data   # {access_token, token_type, expires_in?}


async def _debug_token(token: str) -> dict:
    """Inspect token metadata including granular scopes (WABA IDs)."""
    app_token = f"{META_APP_ID}|{META_APP_SECRET}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{GRAPH}/debug_token",
            params={"input_token": token, "access_token": app_token},
        )
        data = resp.json()
    logger.info("[EmbeddedSignup] debug_token: %s", data)
    return data.get("data", {})


async def _get_waba_id_from_token(token: str) -> str:
    """
    Extract the first WhatsApp Business Account ID the token has access to
    by inspecting granular_scopes returned from debug_token.
    """
    info = await _debug_token(token)
    for scope in info.get("granular_scopes", []):
        if scope.get("scope") == "whatsapp_business_management":
            ids = scope.get("target_ids", [])
            if ids:
                return str(ids[0])
    raise HTTPException(
        status_code=400,
        detail="لم يتم منح صلاحية الوصول لحساب واتساب للأعمال. تأكد من إتمام خطوات الربط.",
    )


async def _subscribe_app_to_waba(waba_id: str, token: str) -> None:
    """Subscribe Nahla app to the merchant's WABA to receive webhooks."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{GRAPH}/{waba_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
    logger.info("[EmbeddedSignup] subscribed_apps WABA=%s result=%s", waba_id, data)


async def _get_phone_numbers(waba_id: str, token: str) -> List[dict]:
    """List phone numbers under the merchant's WABA."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{GRAPH}/{waba_id}/phone_numbers",
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "id,display_phone_number,verified_name,code_verification_status"},
        )
        data = resp.json()
    logger.info("[EmbeddedSignup] phone_numbers WABA=%s result=%s", waba_id, data)
    return data.get("data", [])


# ── schemas ───────────────────────────────────────────────────────────────────

class ExchangeRequest(BaseModel):
    code: str
    redirect_uri: Optional[str] = None


class PhoneSelectRequest(BaseModel):
    phone_number_id: str


# ── endpoints ────────────────────────────────────────────────────────────────

@router.get("/config")
async def get_config():
    """Return public config needed by the frontend FB SDK."""
    if not META_APP_ID:
        raise HTTPException(status_code=503, detail="META_APP_ID غير مُهيَّأ في البيئة.")
    return {
        "app_id": META_APP_ID,
        "config_id": META_WA_CONFIG_ID,
        "graph_version": META_GRAPH_API_VERSION,
    }


@router.post("/exchange")
async def exchange_code(
    body: ExchangeRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Step 1 — Exchange the code returned by FB SDK for a token,
    discover the merchant's WABA, subscribe the app, list phone numbers.
    """
    tenant_id = resolve_tenant_id(request)

    if not META_APP_ID or not META_APP_SECRET:
        raise HTTPException(
            status_code=503,
            detail="إعدادات تطبيق Meta غير مكتملة. تواصل مع الدعم.",
        )

    logger.info("[EmbeddedSignup] exchange START tenant=%s", tenant_id)

    # 1 — Exchange code → user token
    token_data = await _exchange_code_for_token(body.code, body.redirect_uri or "")
    user_token = token_data["access_token"]

    # 2 — Discover WABA ID from token scopes
    waba_id = await _get_waba_id_from_token(user_token)
    logger.info("[EmbeddedSignup] waba_id=%s tenant=%s", waba_id, tenant_id)

    # 3 — Subscribe app to WABA
    await _subscribe_app_to_waba(waba_id, user_token)

    # 4 — List phone numbers
    phones = await _get_phone_numbers(waba_id, user_token)

    # 5 — Upsert WhatsAppConnection
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn:
        conn = WhatsAppConnection(tenant_id=tenant_id)
        db.add(conn)

    conn.whatsapp_business_account_id = waba_id
    conn.access_token                 = user_token
    conn.token_type                   = token_data.get("token_type", "user")
    conn.connection_type              = "embedded"
    conn.status                       = "pending"   # waiting for phone selection

    # Token expiry (Meta user tokens expire in ~60 days)
    expires_in = token_data.get("expires_in")
    if expires_in:
        conn.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    db.commit()

    logger.info(
        "[EmbeddedSignup] exchange OK tenant=%s waba=%s phones=%d",
        tenant_id, waba_id, len(phones),
    )

    return {
        "status":   "waba_connected",
        "waba_id":  waba_id,
        "phones":   [
            {
                "id":     p["id"],
                "number": p.get("display_phone_number", ""),
                "name":   p.get("verified_name", ""),
                "verified": p.get("code_verification_status", "") == "VERIFIED",
            }
            for p in phones
        ],
        "message":  "تم ربط حساب واتساب للأعمال بنجاح. اختر رقم الهاتف.",
    }


@router.post("/select-phone")
async def select_phone(
    body: PhoneSelectRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Step 2 — Merchant picks a phone number from the list returned in /exchange.
    Fetches the phone details and marks connection as active.
    """
    tenant_id = resolve_tenant_id(request)

    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn or not conn.access_token:
        raise HTTPException(status_code=400, detail="أكمل خطوة ربط حساب واتساب أولاً.")

    token = conn.access_token

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{GRAPH}/{body.phone_number_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "id,display_phone_number,verified_name,code_verification_status,quality_rating"},
        )
        phone_data = resp.json()

    if "error" in phone_data:
        raise HTTPException(
            status_code=400,
            detail=f"تعذر جلب بيانات الرقم: {phone_data['error'].get('message','')}",
        )

    conn.phone_number_id          = body.phone_number_id
    conn.phone_number             = phone_data.get("display_phone_number", "")
    conn.business_display_name    = phone_data.get("verified_name", "")
    conn.status                   = "connected"
    conn.sending_enabled          = True
    conn.connected_at             = datetime.now(timezone.utc)
    conn.last_verified_at         = datetime.now(timezone.utc)
    conn.connection_type          = "embedded"
    db.commit()

    logger.info(
        "[EmbeddedSignup] select-phone OK tenant=%s phone_id=%s number=%s",
        tenant_id, body.phone_number_id, conn.phone_number,
    )

    return {
        "status":       "connected",
        "phone_number": conn.phone_number,
        "display_name": conn.business_display_name,
        "verified":     phone_data.get("code_verification_status") == "VERIFIED",
        "message":      "تم ربط رقم الواتساب بنجاح!",
    }


@router.get("/status")
async def get_status(request: Request, db: Session = Depends(get_db)):
    """Return current embedded signup connection status for this tenant."""
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn or conn.connection_type != "embedded":
        return {"connected": False}
    return {
        "connected":    conn.status == "connected",
        "status":       conn.status,
        "phone_number": conn.phone_number,
        "waba_id":      conn.whatsapp_business_account_id,
        "display_name": conn.business_display_name,
    }
