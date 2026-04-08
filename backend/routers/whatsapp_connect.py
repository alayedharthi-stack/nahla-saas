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

from core.config import META_APP_ID, META_APP_SECRET, META_GRAPH_API_VERSION, META_WA_CONFIG_ID
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

    resp: dict = {
        "status":        "pending",
        "meta_app_id":   META_APP_ID or "CONFIGURE_META_APP_ID",
        "graph_version": META_GRAPH_API_VERSION,
        "scope":         "whatsapp_business_management,whatsapp_business_messaging",
        "extras": {
            "feature": "whatsapp_embedded_signup",
            "setup":   {},
        },
    }
    # Include config_id only when set — avoids Meta rejecting an empty string
    if META_WA_CONFIG_ID:
        resp["config_id"] = META_WA_CONFIG_ID
    return resp


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

        # Notify merchant — WhatsApp connected
        try:
            import asyncio as _asyncio  # noqa: PLC0415
            from core.wa_notify import notify_whatsapp_connected  # noqa: PLC0415
            from core.tenant import get_or_create_settings, merge_defaults, DEFAULT_WHATSAPP, DEFAULT_STORE  # noqa: PLC0415
            _s      = get_or_create_settings(db, tenant_id)
            _wa     = merge_defaults(_s.whatsapp_settings or {}, DEFAULT_WHATSAPP)
            _st     = merge_defaults(_s.store_settings    or {}, DEFAULT_STORE)
            _phone  = _wa.get("owner_whatsapp_number", "") or conn.phone_number or ""
            _sname  = _st.get("store_name", "") or f"متجر #{tenant_id}"
            if _phone:
                _asyncio.ensure_future(notify_whatsapp_connected(_phone, _sname))
        except Exception as _exc:
            logger.warning("tenant=%s WhatsApp-connected notification error: %s", tenant_id, _exc)

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


@router.get("/usage")
async def get_usage(
    request:  Request,
    db:       Session = Depends(get_db),
    breakdown: bool   = False,
):
    """
    Return this month's WhatsApp conversation usage for the tenant.

    Query params
    ------------
    breakdown=true  — also include daily_breakdown list (for detail page chart)

    Response always includes
    ------------------------
    service_conversations_used, marketing_conversations_used,
    conversations_used (sum), conversations_limit, usage_pct,
    exceeded, near_limit, hard_stop, unlimited, month, year, reset_date

    With breakdown=true also includes
    ----------------------------------
    daily_breakdown: [{day, service, marketing, total}, ...]
    """
    from core.wa_usage import get_usage_this_month, get_daily_breakdown  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    data      = get_usage_this_month(db, tenant_id)

    if breakdown:
        data["daily_breakdown"] = get_daily_breakdown(
            db, tenant_id, data["year"], data["month"]
        )

    return data


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


# ══════════════════════════════════════════════════════════════════════════════
# Shared-WABA Direct Registration Flow
# ─────────────────────────────────────
# Merchant enters phone number in their dashboard → Nahla calls Meta API to
# register the number under Nahla's WABA → Meta sends OTP to merchant's phone
# → Merchant enters OTP → number is verified and saved.
#
# Endpoints:
#   POST /whatsapp/direct/request-otp   — register number + send OTP
#   POST /whatsapp/direct/verify-otp    — verify OTP + save connection
#   GET  /whatsapp/direct/status        — registration progress
# ══════════════════════════════════════════════════════════════════════════════

class DirectOTPRequest(BaseModel):
    phone_number:  str   # e.g. "+966501234567" or "0501234567"
    display_name:  str   # merchant's store display name on WhatsApp
    method:        str = "SMS"   # "SMS" or "VOICE"


class DirectVerifyRequest(BaseModel):
    phone_number_id: str   # returned from request-otp step
    code:            str   # 6-digit OTP from Meta


def _normalize_phone(raw: str) -> tuple[str, str]:
    """
    Normalize any common Saudi/international phone format and return
    (country_code, national_number) ready for the Meta phone_numbers API.

    Accepted inputs → all produce ("966", "5XXXXXXXX"):
        +966542878717   966542878717   0542878717   542878717
        ٠٥٤٢٨٧٨٧١٧  (Arabic digits)   966 54-287 8717 (spaces/dashes)

    Validation after normalization:
        Saudi mobile: ^9665\\d{8}$   (total 12 digits: 966 + 5 + 8 digits)
    """
    # ── 1. Convert Arabic-Indic digits to ASCII ──────────────────────────────
    arabic_map = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    cleaned = raw.translate(arabic_map)

    # ── 2. Strip whitespace, dashes, dots, parentheses ──────────────────────
    import re as _re  # noqa: PLC0415
    cleaned = _re.sub(r"[\s\-\.\(\)]+", "", cleaned)

    # ── 3. Remove leading + or 00 ────────────────────────────────────────────
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    if cleaned.startswith("00"):
        cleaned = cleaned[2:]

    # ── 4. Determine country code & national number ──────────────────────────
    if cleaned.startswith("966"):
        cc       = "966"
        national = cleaned[3:]
    elif cleaned.startswith("0"):
        # Local Saudi format: 05XXXXXXXX → remove leading 0
        cc       = "966"
        national = cleaned[1:]
    elif len(cleaned) == 9 and cleaned.startswith("5"):
        # Bare 9-digit Saudi number: 5XXXXXXXX
        cc       = "966"
        national = cleaned
    else:
        # Unknown → pass as-is, let Meta decide
        cc       = "966"
        national = cleaned

    logger.info(
        "[PhoneNorm] original=%r  cleaned=%r  cc=%s  national=%s  valid=%s",
        raw, cleaned, cc, national,
        bool(_re.match(r"^5\d{8}$", national)),
    )

    return cc, national


def _validate_phone(cc: str, national: str) -> str | None:
    """
    Return an Arabic error message if the phone is invalid, else None.
    Currently enforces Saudi mobile format only (9-digit national starting with 5).
    """
    import re as _re  # noqa: PLC0415
    if cc == "966" and not _re.match(r"^5\d{8}$", national):
        return (
            "صيغة رقم الهاتف غير صحيحة. "
            "أدخل رقماً سعودياً صحيحاً مثل: +966542878717 أو 0542878717 أو 542878717"
        )
    return None


@router.post("/direct/request-otp")
async def direct_request_otp(
    body: DirectOTPRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Step 1 — Register the merchant's phone number under Nahla's WABA and
    send an OTP to that number via SMS or voice call.
    """
    from core.config import WA_BUSINESS_ACCOUNT_ID, WA_TOKEN, META_GRAPH_API_VERSION  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)

    if not WA_TOKEN or not WA_BUSINESS_ACCOUNT_ID:
        raise HTTPException(
            status_code=503,
            detail="خدمة واتساب غير مُهيَّأة. تواصل مع الدعم.",
        )

    cc, national = _normalize_phone(body.phone_number)

    # ── Full trace log ───────────────────────────────────────────────────────
    logger.info(
        "[WA Direct] request-otp TRACE | tenant=%s "
        "original_input=%r  cc=%s  national=%s  waba=%s",
        tenant_id, body.phone_number, cc, national, WA_BUSINESS_ACCOUNT_ID,
    )

    # Validate after normalization — reject early with a clear Arabic message
    phone_err = _validate_phone(cc, national)
    if phone_err:
        logger.warning(
            "[WA Direct] PHONE_VALIDATION_ERROR | tenant=%s input=%r cc=%s national=%s reason=%s",
            tenant_id, body.phone_number, cc, national, phone_err,
        )
        raise HTTPException(
            status_code=400,
            detail=phone_err,
            headers={"X-Nahla-Error-Code": "PHONE_VALIDATION_ERROR"},
        )

    graph        = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
    headers      = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type":  "application/json",
    }

    logger.info(
        "[WA Direct] META_REQUEST | tenant=%s WABA=%s cc=%s national=%s display_name=%r method=%s",
        tenant_id, WA_BUSINESS_ACCOUNT_ID, cc, national, body.display_name, body.method,
    )

    # ── Step A: Add phone number to WABA ────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            add_resp = await client.post(
                f"{graph}/{WA_BUSINESS_ACCOUNT_ID}/phone_numbers",
                headers=headers,
                json={
                    "cc":            cc,
                    "phone_number":  national,
                    "verified_name": body.display_name,
                    "migrate_phone_number": False,
                },
            )
            add_data = add_resp.json()
    except Exception as exc:
        logger.error("[WA Direct] Add phone API error: %s", exc)
        raise HTTPException(status_code=503, detail="خطأ في الاتصال بـ Meta")

    if "error" in add_data:
        err      = add_data["error"]
        code     = err.get("code", 0)
        subcode  = err.get("error_subcode", 0)
        msg      = err.get("message", "")
        user_msg = err.get("error_user_msg", "") or err.get("error_user_title", "")
        # Log full raw Meta response for debugging — never forwarded to UI
        logger.warning(
            "[WA Direct] Add phone raw_error code=%s subcode=%s msg=%s user_msg=%s full=%s",
            code, subcode, msg, user_msg, add_data,
        )

        internal_code, ux_message = _normalize_meta_error(code, msg, subcode, user_msg)

        # Already registered → proceed to OTP step
        if internal_code == META_ALREADY_REGISTERED:
            phone_number_id = add_data.get("error", {}).get("error_data", {}).get("id", "") or ""
        else:
            raise HTTPException(
                status_code=400,
                detail=ux_message,
                headers={"X-Nahla-Error-Code": internal_code},
            )
    else:
        phone_number_id = add_data.get("id", "")

    if not phone_number_id:
        raise HTTPException(
            status_code=400,
            detail=_UX_MESSAGES[META_INVALID_NUMBER],
            headers={"X-Nahla-Error-Code": META_INVALID_NUMBER},
        )

    # ── Step B: Request OTP ──────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            otp_resp = await client.post(
                f"{graph}/{phone_number_id}/request_code",
                headers=headers,
                json={
                    "code_method": body.method.upper(),
                    "language":    "ar",
                },
            )
            otp_data = otp_resp.json()
    except Exception as exc:
        logger.error("[WA Direct] Request OTP API error: %s", exc)
        raise HTTPException(status_code=503, detail=_UX_MESSAGES[META_UNKNOWN_ERROR])

    if "error" in otp_data:
        err = otp_data["error"]
        ic, ux = _normalize_meta_error(
            err.get("code", 0), err.get("message", ""),
            err.get("error_subcode", 0), err.get("error_user_msg", ""),
        )
        logger.warning("[WA Direct] OTP request raw_error code=%s full=%s", err.get("code"), otp_data)
        raise HTTPException(status_code=400, detail=ux, headers={"X-Nahla-Error-Code": ic})

    logger.info(
        "[WA Direct] OTP sent | tenant=%s phone_number_id=%s",
        tenant_id, phone_number_id,
    )

    return {
        "status":          META_CODE_SENT,
        "code":            META_CODE_SENT,
        "phone_number_id": phone_number_id,
        "message":         f"تم إرسال رمز التحقق إلى +{cc}{national}",
    }


@router.post("/direct/verify-otp")
async def direct_verify_otp(
    body: DirectVerifyRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Step 2 — Verify the OTP code and save the WhatsApp connection.
    """
    from core.config import WA_TOKEN, META_GRAPH_API_VERSION  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    graph     = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
    headers   = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type":  "application/json",
    }

    logger.info(
        "[WA Direct] verify-otp | tenant=%s phone_number_id=%s",
        tenant_id, body.phone_number_id,
    )

    # ── Step A: Verify code with Meta ────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            verify_resp = await client.post(
                f"{graph}/{body.phone_number_id}/verify_code",
                headers=headers,
                json={"code": body.code},
            )
            verify_data = verify_resp.json()
    except Exception as exc:
        logger.error("[WA Direct] Verify code API error: %s", exc)
        raise HTTPException(status_code=503, detail=_UX_MESSAGES[META_UNKNOWN_ERROR])

    if "error" in verify_data:
        err = verify_data["error"]
        code = err.get("code", 0)
        logger.warning("[WA Direct] Verify OTP raw_error code=%s full=%s", code, verify_data)
        if code in (136012, 136013):
            raise HTTPException(
                status_code=400,
                detail="رمز التحقق غير صحيح أو منتهي الصلاحية. اطلب رمزاً جديداً.",
                headers={"X-Nahla-Error-Code": "META_INVALID_CODE"},
            )
        ic, ux = _normalize_meta_error(code, err.get("message", ""), err.get("error_subcode", 0))
        raise HTTPException(status_code=400, detail=ux, headers={"X-Nahla-Error-Code": ic})

    # ── Step B: Fetch phone number details ───────────────────────────────────
    phone_number   = ""
    display_name   = ""
    waba_id        = ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            info_resp = await client.get(
                f"{graph}/{body.phone_number_id}",
                headers=headers,
                params={"fields": "display_phone_number,verified_name,status"},
            )
            info = info_resp.json()
            phone_number = info.get("display_phone_number", "")
            display_name = info.get("verified_name", "")
    except Exception:
        pass

    # ── Step C: Save connection to DB ────────────────────────────────────────
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if conn is None:
        conn = WhatsAppConnection(tenant_id=tenant_id)
        db.add(conn)

    conn.status                       = "connected"
    conn.phone_number_id              = body.phone_number_id
    conn.phone_number                 = phone_number
    conn.business_display_name        = display_name
    conn.whatsapp_business_account_id = waba_id or ""
    conn.access_token                 = WA_TOKEN   # shared WABA uses platform token
    conn.webhook_verified             = True
    conn.sending_enabled              = True
    conn.connected_at                 = datetime.now(timezone.utc)
    conn.last_error                   = None

    # Also update tenant-level fields for quick access
    tenant = get_or_create_tenant(db, tenant_id)
    tenant.whatsapp_phone_id = body.phone_number_id
    tenant.whatsapp_token    = WA_TOKEN

    db.commit()

    logger.info(
        "[WA Direct] ✅ Connected | tenant=%s phone=%s name=%s",
        tenant_id, phone_number, display_name,
    )

    return {
        "status":       "connected",
        "phone_number": phone_number,
        "display_name": display_name,
        "message":      "تم ربط واتساب بنجاح! 🎉",
    }


@router.get("/direct/debug-token")
async def direct_debug_token():
    """
    Diagnostic endpoint — checks WHATSAPP_TOKEN permissions and WABA access.
    TEMPORARY: Remove after debugging is done.
    """
    from core.config import WA_BUSINESS_ACCOUNT_ID, WA_TOKEN, META_GRAPH_API_VERSION  # noqa: PLC0415
    graph   = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
    headers = {"Authorization": f"Bearer {WA_TOKEN}"}
    result: dict = {
        "waba_id":        WA_BUSINESS_ACCOUNT_ID,
        "token_prefix":   (WA_TOKEN or "")[:12] + "...",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Check token info + debug_token
        try:
            r = await client.get(f"{graph}/me", headers=headers,
                                 params={"fields": "id,name"})
            result["me"] = r.json()
        except Exception as e:
            result["me_error"] = str(e)

        # 2. Get system user's own business
        user_id = result.get("me", {}).get("id", "me")
        try:
            r = await client.get(
                f"{graph}/{user_id}",
                headers=headers,
                params={"fields": "id,name,business"},
            )
            me_full = r.json()
            result["me_business"] = me_full.get("business")
        except Exception as e:
            result["me_business_error"] = str(e)

        # 3. Check if WABA_ID is actually a Phone Number ID (wrong type)
        try:
            r = await client.get(
                f"{graph}/{WA_BUSINESS_ACCOUNT_ID}",
                headers=headers,
                params={"fields": "id,display_phone_number,verified_name,status"},
            )
            result["id_as_phone_number"] = r.json()
        except Exception as e:
            result["id_as_phone_number_error"] = str(e)

        # 4. Try getting System User fields with business field expanded
        try:
            r = await client.get(
                f"{graph}/{user_id}",
                headers=headers,
                params={"fields": "id,name,business,businesses{id,name}"},
            )
            result["me_extended"] = r.json()
        except Exception as e:
            result["me_extended_error"] = str(e)

        # 5. Try app-level business lookup
        try:
            r = await client.get(
                f"{graph}/me/businesses",
                headers=headers,
                params={"fields": "id,name,whatsapp_business_accounts{id,name}"},
            )
            result["me_businesses"] = r.json()
        except Exception as e:
            result["me_businesses_error"] = str(e)

        # 3. Direct WABA access attempt
        try:
            r = await client.get(
                f"{graph}/{WA_BUSINESS_ACCOUNT_ID}",
                headers=headers,
                params={"fields": "id,name,currency,timezone_id"},
            )
            result["waba_direct"] = r.json()
        except Exception as e:
            result["waba_direct_error"] = str(e)

        # 4. List phone numbers under WABA
        try:
            r = await client.get(
                f"{graph}/{WA_BUSINESS_ACCOUNT_ID}/phone_numbers",
                headers=headers,
                params={"fields": "id,display_phone_number,verified_name,status"},
            )
            result["phone_numbers"] = r.json()
        except Exception as e:
            result["phone_numbers_error"] = str(e)

        # 5. Try token debug info
        try:
            r = await client.get(
                f"{graph}/debug_token",
                headers=headers,
                params={"input_token": WA_TOKEN},
            )
            data = r.json().get("data", {})
            result["token_debug"] = {
                "app_id":      data.get("app_id"),
                "expires_at":  data.get("expires_at"),
                "is_valid":    data.get("is_valid"),
                "scopes":      data.get("scopes", []),
            }
        except Exception as e:
            result["token_debug_error"] = str(e)

    logger.info("[WA Debug] Token diagnostic result: %s", result)
    return result


@router.get("/direct/status")
async def direct_status(request: Request, db: Session = Depends(get_db)):
    """Return the current direct-registration connection status."""
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn or conn.status == "not_connected":
        return {"connected": False, "status": "not_connected"}
    return {
        "connected":    conn.status == "connected",
        "status":       conn.status,
        "phone_number": conn.phone_number,
        "display_name": conn.business_display_name,
        "connected_at": conn.connected_at.isoformat() if conn.connected_at else None,
    }


class SaveProfileRequest(BaseModel):
    phone_number_id: str
    vertical:  Optional[str] = "OTHER"
    about:     Optional[str] = None
    address:   Optional[str] = None
    email:     Optional[str] = None
    websites:  Optional[str] = None


@router.post("/direct/save-profile")
async def direct_save_profile(
    body: SaveProfileRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Step 3 — Update the WhatsApp Business Profile for the registered number.
    Calls POST /{phone_number_id}/whatsapp_business_profile
    """
    from core.config import WA_TOKEN, META_GRAPH_API_VERSION  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    graph     = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
    headers   = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type":  "application/json",
    }

    profile_payload: dict = {"messaging_product": "whatsapp"}
    if body.vertical: profile_payload["vertical"]    = body.vertical
    if body.about:    profile_payload["about"]       = body.about
    if body.address:  profile_payload["address"]     = body.address
    if body.email:    profile_payload["email"]       = body.email
    if body.websites: profile_payload["websites"]    = [body.websites]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{graph}/{body.phone_number_id}/whatsapp_business_profile",
                headers=headers,
                json=profile_payload,
            )
            data = resp.json()
    except Exception as exc:
        logger.warning("[WA Direct] save-profile API error: %s", exc)
        raise HTTPException(status_code=503, detail="خطأ في حفظ الملف التجاري")

    if "error" in data:
        err = data["error"]
        logger.warning("[WA Direct] save-profile raw_error code=%s full=%s", err.get("code"), data)
        ic, ux = _normalize_meta_error(err.get("code", 0), err.get("message", ""), err.get("error_subcode", 0))
        raise HTTPException(status_code=400, detail=ux, headers={"X-Nahla-Error-Code": ic})

    logger.info("[WA Direct] ✅ Business profile saved | tenant=%s", tenant_id)
    return {"status": META_PROFILE_SAVED, "code": META_PROFILE_SAVED, "message": _UX_MESSAGES[META_PROFILE_SAVED]}


# ═══════════════════════════════════════════════════════════════════════════════
# META RESPONSE NORMALIZER
# All Meta API responses MUST pass through this layer before reaching the UI.
# Raw provider messages are NEVER sent to the frontend.
# ═══════════════════════════════════════════════════════════════════════════════

# Internal status codes exposed to the frontend
META_CODE_SENT           = "META_CODE_SENT"
META_ALREADY_REGISTERED  = "META_ALREADY_REGISTERED"
META_INVALID_NUMBER      = "META_INVALID_NUMBER"
META_INVALID_NAME        = "META_INVALID_NAME"
META_PERSONAL_NUMBER     = "META_PERSONAL_NUMBER"
META_LIMIT_EXCEEDED      = "META_LIMIT_EXCEEDED"
META_PERMISSION_ERROR    = "META_PERMISSION_ERROR"
META_TOKEN_EXPIRED       = "META_TOKEN_EXPIRED"
META_VERIFIED            = "META_VERIFIED"
META_PROFILE_SAVED       = "META_PROFILE_SAVED"
META_UNKNOWN_ERROR       = "META_UNKNOWN_ERROR"

# Arabic UX messages — defined internally, never derived from raw Meta text
_UX_MESSAGES: dict[str, str] = {
    META_CODE_SENT:          "تم إرسال رمز التحقق إلى رقم الهاتف.",
    META_ALREADY_REGISTERED: "الرقم مسجَّل بالفعل في هذا الحساب.",
    META_INVALID_NUMBER:     "صيغة رقم الهاتف غير صحيحة. تأكد من إدخال الرقم كاملاً مع رمز الدولة.",
    META_INVALID_NAME:       "اسم العرض غير مقبول. استخدم الاسم الرسمي لنشاطك التجاري.",
    META_PERSONAL_NUMBER:    "هذا الرقم مسجَّل على واتساب الشخصي. احذف الحساب الشخصي أولاً ثم أعد المحاولة بعد 24 ساعة.",
    META_LIMIT_EXCEEDED:     "تجاوزت الحد الأقصى لعدد الأرقام المسموح بها. يمكنك حذف أحد الأرقام الحالية أو التواصل مع الدعم لرفع الحد.",
    META_PERMISSION_ERROR:   "تعذر إكمال الربط بسبب إعدادات الصلاحيات في Meta. يرجى التواصل مع الدعم.",
    META_TOKEN_EXPIRED:      "انتهت صلاحية رمز الوصول. يرجى التواصل مع الدعم لتجديده.",
    META_VERIFIED:           "تم التحقق من الرقم بنجاح وتم ربطه بواتساب للأعمال.",
    META_PROFILE_SAVED:      "تم حفظ بيانات الملف التجاري بنجاح.",
    META_UNKNOWN_ERROR:      "حدث خطأ أثناء ربط واتساب. حاول مرة أخرى بعد قليل.",
}

_FALLBACK_MESSAGE = "تمت معالجة الطلب، ولكن تعذر عرض تفاصيل الرسالة بشكل صحيح."


def _normalize_meta_error(
    code: int,
    message: str,
    subcode: int = 0,
    user_msg: str = "",
) -> tuple[str, str]:
    """
    Map a raw Meta error to an (internal_code, arabic_ux_message) tuple.
    The raw message is LOGGED but never returned to the UI.
    """
    logger.debug(
        "[MetaNormalizer] raw error code=%s subcode=%s msg=%s user_msg=%s",
        code, subcode, message, user_msg,
    )

    # ── Subcode mapping (most specific) ─────────────────────────────────────
    subcode_map: dict[int, str] = {
        2388053: META_ALREADY_REGISTERED,
        2361002: META_INVALID_NUMBER,
        2388001: META_INVALID_NAME,
        2388055: META_PERSONAL_NUMBER,
        2388049: META_LIMIT_EXCEEDED,
    }
    if subcode and subcode in subcode_map:
        ic = subcode_map[subcode]
        return ic, _UX_MESSAGES[ic]

    # ── Code mapping ─────────────────────────────────────────────────────────
    code_map: dict[int, str] = {
        136023: META_PERSONAL_NUMBER,
        136031: META_LIMIT_EXCEEDED,
        190:    META_TOKEN_EXPIRED,
        10:     META_PERMISSION_ERROR,
        200:    META_PERMISSION_ERROR,
    }
    if code in code_map:
        ic = code_map[code]
        return ic, _UX_MESSAGES[ic]

    # ── Heuristic: scan raw message for known patterns ───────────────────────
    # IMPORTANT: keep these narrow to avoid misclassifying unrelated errors.
    # Never match "invalid" alone — too broad (e.g. "invalid access token").
    raw = (message + " " + user_msg).lower()
    if "already" in raw and ("register" in raw or "exist" in raw):
        return META_ALREADY_REGISTERED, _UX_MESSAGES[META_ALREADY_REGISTERED]
    if "personal" in raw or "consumer" in raw:
        return META_PERSONAL_NUMBER, _UX_MESSAGES[META_PERSONAL_NUMBER]
    if "count exceeded" in raw or ("limit" in raw and "phone" in raw) or "exceeded" in raw:
        return META_LIMIT_EXCEEDED, _UX_MESSAGES[META_LIMIT_EXCEEDED]
    if "missing permissions" in raw or "does not exist" in raw:
        return META_PERMISSION_ERROR, _UX_MESSAGES[META_PERMISSION_ERROR]
    if "permission" in raw and ("insufficient" in raw or "required" in raw):
        return META_PERMISSION_ERROR, _UX_MESSAGES[META_PERMISSION_ERROR]

    # Fallback — log and return generic
    logger.info(
        "[MetaNormalizer] unmapped error code=%s raw=%r → META_UNKNOWN_ERROR",
        code, raw[:200],
    )
    return META_UNKNOWN_ERROR, _UX_MESSAGES[META_UNKNOWN_ERROR]


def _meta_error_to_arabic(code: int, message: str, subcode: int = 0, user_msg: str = "") -> str:
    """Thin wrapper kept for backwards compatibility — returns only the UX message."""
    _, msg = _normalize_meta_error(code, message, subcode, user_msg)
    return msg
