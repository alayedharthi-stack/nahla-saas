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
    # For FB JS SDK codes, redirect_uri must match what was used during login.
    # If not provided, we try without it first, then with the app URL.
    params: dict = {
        "client_id":     META_APP_ID,
        "client_secret": META_APP_SECRET,
        "code":          code,
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
    Extract the WhatsApp Business Account ID from the token using multiple strategies:
    1. debug_token granular_scopes (works when config_id triggers full WA signup)
    2. GET /me/businesses → list WABAs per business (fallback without config_id)
    """
    # Strategy 1: debug_token granular_scopes
    info = await _debug_token(token)
    for scope in info.get("granular_scopes", []):
        if scope.get("scope") == "whatsapp_business_management":
            ids = scope.get("target_ids", [])
            if ids:
                logger.info("[EmbeddedSignup] WABA found via granular_scopes: %s", ids[0])
                return str(ids[0])

    # Strategy 2: list businesses, then their WABAs
    logger.info("[EmbeddedSignup] Falling back to /me/businesses lookup")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            biz_resp = await client.get(
                f"{GRAPH}/me/businesses",
                headers={"Authorization": f"Bearer {token}"},
                params={"fields": "id,name"},
            )
            biz_data = biz_resp.json()
        logger.info("[EmbeddedSignup] /me/businesses: %s", biz_data)
        for biz in biz_data.get("data", []):
            biz_id = biz["id"]
            async with httpx.AsyncClient(timeout=15) as client:
                wa_resp = await client.get(
                    f"{GRAPH}/{biz_id}/whatsapp_business_accounts",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"fields": "id,name"},
                )
                wa_data = wa_resp.json()
            logger.info("[EmbeddedSignup] WABAs for biz %s: %s", biz_id, wa_data)
            for waba in wa_data.get("data", []):
                logger.info("[EmbeddedSignup] WABA found via businesses: %s", waba["id"])
                return str(waba["id"])
    except Exception as e:
        logger.warning("[EmbeddedSignup] Business lookup failed: %s", e)

    raise HTTPException(
        status_code=400,
        detail=(
            "لم يُعثر على حساب واتساب للأعمال مرتبط بهذا الحساب. "
            "تأكد من أن لديك حساب واتساب للأعمال في Meta Business Manager."
        ),
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
    # Accept either a raw access_token (from JS SDK) or a code (legacy)
    access_token: Optional[str] = None
    code: Optional[str] = None
    redirect_uri: Optional[str] = None


class PhoneSelectRequest(BaseModel):
    phone_number_id: str


class AddPhoneRequest(BaseModel):
    country_code: str          # e.g. "966"
    phone_number: str          # without country code, e.g. "512345678"
    verified_name: str         # display name
    code_method: str = "SMS"   # SMS or VOICE


class VerifyPhoneRequest(BaseModel):
    phone_number_id: str
    code: str


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

    # 1 — Get user token: either passed directly from JS SDK or exchanged from code
    token_data: dict = {}
    if body.access_token:
        user_token = body.access_token
        logger.info("[EmbeddedSignup] using access_token from JS SDK tenant=%s", tenant_id)
    elif body.code:
        token_data = await _exchange_code_for_token(body.code, body.redirect_uri or "")
        user_token = token_data["access_token"]
    else:
        raise HTTPException(status_code=400, detail="يجب إرسال access_token أو code")

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

    is_verified = phone_data.get("code_verification_status") == "VERIFIED"

    conn.phone_number_id       = body.phone_number_id
    conn.phone_number          = phone_data.get("display_phone_number", "")
    conn.business_display_name = phone_data.get("verified_name", "")
    conn.connection_type       = "embedded"

    if is_verified:
        conn.status           = "connected"
        conn.sending_enabled  = True
        conn.connected_at     = datetime.now(timezone.utc)
        conn.last_verified_at = datetime.now(timezone.utc)
    else:
        # Phone exists in WABA but not yet verified — request OTP now
        conn.status          = "otp_pending"
        conn.sending_enabled = False
        async with httpx.AsyncClient(timeout=15) as client:
            otp_resp = await client.post(
                f"{GRAPH}/{body.phone_number_id}/request_code",
                headers={"Authorization": f"Bearer {token}"},
                json={"code_method": "SMS", "language": "ar"},
            )
            otp_data = otp_resp.json()
        logger.info("[EmbeddedSignup] select-phone OTP request: %s", otp_data)

    db.commit()

    logger.info(
        "[EmbeddedSignup] select-phone OK tenant=%s phone_id=%s number=%s verified=%s",
        tenant_id, body.phone_number_id, conn.phone_number, is_verified,
    )

    if is_verified:
        return {
            "status":       "connected",
            "phone_number": conn.phone_number,
            "display_name": conn.business_display_name,
            "verified":     True,
            "message":      "تم ربط رقم الواتساب بنجاح!",
        }
    else:
        return {
            "status":          "otp_required",
            "phone_number_id": body.phone_number_id,
            "phone_number":    conn.phone_number,
            "display_name":    conn.business_display_name,
            "verified":        False,
            "message":         "تم إرسال رمز التحقق عبر SMS — أدخله لإكمال الربط.",
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


@router.post("/add-phone")
async def add_phone(
    body: AddPhoneRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Add a new phone number to the merchant's WABA and send OTP.
    Called when the merchant's WABA has no phone numbers yet.
    """
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn or not conn.whatsapp_business_account_id:
        raise HTTPException(status_code=400, detail="لا يوجد WABA مرتبط. أكمل خطوة الربط أولاً.")

    waba_id = conn.whatsapp_business_account_id
    token   = conn.access_token

    # Step 1 — Register the phone number with the WABA
    async with httpx.AsyncClient(timeout=20) as client:
        reg_resp = await client.post(
            f"{GRAPH}/{waba_id}/phone_numbers",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "cc":            body.country_code,
                "phone_number":  body.phone_number,
                "migrate_phone_number": False,
                "verified_name": body.verified_name,
            },
        )
        reg_data = reg_resp.json()

    logger.info("[EmbeddedSignup] add-phone register: %s", reg_data)

    if "error" in reg_data:
        err = reg_data["error"]
        raise HTTPException(
            status_code=400,
            detail=f"فشل إضافة الرقم: {err.get('message', '')} (code={err.get('code')}, subcode={err.get('error_subcode')})",
        )

    phone_number_id = reg_data.get("id")
    if not phone_number_id:
        raise HTTPException(status_code=500, detail="لم يُعاد phone_number_id من Meta")

    # Step 2 — Request OTP
    async with httpx.AsyncClient(timeout=15) as client:
        otp_resp = await client.post(
            f"{GRAPH}/{phone_number_id}/request_code",
            headers={"Authorization": f"Bearer {token}"},
            json={"code_method": body.code_method, "language": "ar"},
        )
        otp_data = otp_resp.json()

    logger.info("[EmbeddedSignup] add-phone request_code: %s", otp_data)

    if "error" in otp_data:
        err = otp_data["error"]
        raise HTTPException(
            status_code=400,
            detail=f"فشل إرسال رمز التحقق: {err.get('message', '')} (code={err.get('code')}, subcode={err.get('error_subcode')})",
        )

    # Save phone_number_id temporarily for verification step
    conn.phone_number_id = phone_number_id
    conn.status          = "otp_pending"
    db.commit()

    return {
        "status":          "otp_sent",
        "phone_number_id": phone_number_id,
        "message":         f"تم إرسال رمز التحقق عبر {body.code_method}",
    }


@router.post("/verify-phone")
async def verify_phone(
    body: VerifyPhoneRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Verify OTP for a newly added phone number."""
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn or not conn.access_token:
        raise HTTPException(status_code=400, detail="لا يوجد اتصال نشط")

    token = conn.access_token

    async with httpx.AsyncClient(timeout=15) as client:
        verify_resp = await client.post(
            f"{GRAPH}/{body.phone_number_id}/verify_code",
            headers={"Authorization": f"Bearer {token}"},
            json={"code": body.code},
        )
        verify_data = verify_resp.json()

    logger.info("[EmbeddedSignup] verify-phone: %s", verify_data)

    if "error" in verify_data:
        raise HTTPException(
            status_code=400,
            detail=f"رمز التحقق غير صحيح: {verify_data['error'].get('message', '')}",
        )

    # Fetch phone details to update the connection
    async with httpx.AsyncClient(timeout=15) as client:
        ph_resp = await client.get(
            f"{GRAPH}/{body.phone_number_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "id,display_phone_number,verified_name,code_verification_status"},
        )
        ph_data = ph_resp.json()

    conn.phone_number_id          = body.phone_number_id
    conn.phone_number             = ph_data.get("display_phone_number", "")
    conn.business_display_name    = ph_data.get("verified_name", "")
    conn.status                   = "connected"
    conn.sending_enabled          = True
    conn.connected_at             = datetime.now(timezone.utc)
    conn.last_verified_at         = datetime.now(timezone.utc)
    db.commit()

    return {
        "status":       "connected",
        "phone_number": conn.phone_number,
        "display_name": conn.business_display_name,
        "message":      "تم التحقق وربط الرقم بنجاح!",
    }
