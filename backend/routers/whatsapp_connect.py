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
from services.whatsapp_platform.token_manager import (
    get_oauth_session_state,
    get_token_context,
    get_token_for_operation,
    update_token_state,
)

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
    meta = dict(conn.extra_metadata or {})
    token_ctx = get_token_context(conn)
    return {
        "status":                       conn.status,
        "connection_status":            conn.status,
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
        "oauth_session_status":         token_ctx.oauth_session_status,
        "oauth_session_message":        token_ctx.oauth_session_message,
        "oauth_session_needs_reauth":   token_ctx.oauth_session_status in {"expired", "invalid", "missing"},
        "active_graph_token_source":    meta.get("active_graph_token_source", token_ctx.source),
        "token_status":                 meta.get("token_status", token_ctx.token_status),
        "token_health":                 meta.get("token_health", token_ctx.token_status),
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
    if conn and conn.connection_type == "embedded":
        if not conn.phone_number_id:
            return {"verified": False, "reason": "no_connection"}
        try:
            from routers.whatsapp_embedded import sync_embedded_connection_from_meta  # noqa: PLC0415
            payload = await sync_embedded_connection_from_meta(conn, db, attempt_register=True)
            return {
                "verified": bool(payload.get("connected")),
                "sending_enabled": bool(payload.get("sending_enabled")),
                "status": payload.get("status"),
                "reason": payload.get("message") or payload.get("last_error"),
            }
        except Exception as exc:
            conn.last_error = str(exc)[:500]
            db.commit()
            return {"verified": False, "reason": str(exc)}

    if not conn or conn.status not in ("connected", "pending", "activation_pending", "review_pending"):
        return {"verified": False, "reason": "no_connection"}

    token_ctx = get_token_context(conn)
    update_token_state(
        conn,
        token_source=token_ctx.source,
        token_status=token_ctx.token_status,
        oauth_session_status=token_ctx.oauth_session_status,
        oauth_session_message=token_ctx.oauth_session_message,
    )
    if not token_ctx.token:
        db.commit()
        return {"verified": False, "reason": "missing_token"}

    try:
        # Ping the phone number ID to verify token is still valid
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{GRAPH_BASE}/{conn.phone_number_id or 'me'}",
                params={
                    "fields":       "id,display_phone_number,code_verification_status",
                    "access_token": token_ctx.token,
                },
            )

        if resp.status_code == 401:
            update_token_state(
                conn,
                token_source=token_ctx.source,
                token_status="expired",
                oauth_session_status=token_ctx.oauth_session_status,
                oauth_session_message=token_ctx.oauth_session_message,
            )
            conn.last_error = "Token expired or revoked"
            db.commit()
            return {"verified": False, "reason": "token_expired"}

        if resp.status_code == 200:
            data = resp.json()
            from routers.whatsapp_embedded import _build_phone_sync_state  # noqa: PLC0415
            sync_state = _build_phone_sync_state(data if isinstance(data, dict) else {})
            conn.sending_enabled  = bool(sync_state.get("sending_enabled"))
            conn.status           = sync_state.get("db_status") or "activation_pending"
            conn.last_verified_at = datetime.now(timezone.utc)
            conn.last_error       = None if conn.sending_enabled else sync_state.get("message")
            conn.extra_metadata   = {
                **(conn.extra_metadata or {}),
                "meta_code_verification_status": sync_state.get("verification_status"),
                "meta_name_status": sync_state.get("name_status"),
                "meta_phone_status": sync_state.get("meta_phone_status"),
                "meta_quality_rating": sync_state.get("quality_rating"),
                "embedded_status_message": sync_state.get("message"),
            }
            db.commit()
            return {
                "verified": bool(conn.sending_enabled),
                "sending_enabled": conn.sending_enabled,
                "status": conn.status,
            }

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

    token_ctx = get_token_context(conn)
    token_present = bool(token_ctx.token)
    token_valid = token_ctx.token_status in {"healthy", "expiring_soon"}

    checks = {
        "has_connection":   conn.status in ("connected", "pending", "activation_pending", "review_pending"),
        "token_present":    token_present,
        "token_valid":      token_valid,
        "webhook_verified": bool(conn.webhook_verified),
        "sending_enabled":  bool(conn.sending_enabled),
    }
    healthy = all(checks.values())

    return {
        "healthy":       healthy,
        "status":        conn.status,
        "connection_status": conn.status,
        "token_status":  token_ctx.token_status,
        "oauth_session_status": token_ctx.oauth_session_status,
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
    from core.config import WA_BUSINESS_ACCOUNT_ID, META_GRAPH_API_VERSION  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    existing_conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    try:
        token_ctx = await get_token_for_operation(
            db,
            existing_conn,
            tenant_id=tenant_id,
            operation="phone_register",
            prefer_platform=True,
        )
    except Exception:
        token_ctx = None

    if not token_ctx or not token_ctx.token or not WA_BUSINESS_ACCOUNT_ID:
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
        "Authorization": f"Bearer {token_ctx.token}",
        "Content-Type":  "application/json",
    }

    logger.info(
        "[WA Direct] META_REQUEST | tenant=%s WABA=%s cc=%s national=%s display_name=%r method=%s",
        tenant_id, WA_BUSINESS_ACCOUNT_ID, cc, national, body.display_name, body.method,
    )

    # ── Check DB: if already pending for same number, validate ID then skip add ──
    full_phone    = f"+{cc}{national}"
    if (
        existing_conn
        and existing_conn.status == "pending"
        and existing_conn.phone_number_id
        and existing_conn.phone_number == full_phone
    ):
        stored_phone_id = existing_conn.phone_number_id
        logger.info(
            "[WA Direct] Pending resume candidate tenant=%s phone=%s id=%s — validating with Meta",
            tenant_id, full_phone, stored_phone_id,
        )
        # ── Validate the stored phone_number_id is still alive on Meta ────────
        id_valid = False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                chk = await client.get(
                    f"{graph}/{stored_phone_id}",
                    headers=headers,
                    params={"fields": "id,display_phone_number,code_verification_status"},
                )
                chk_data = chk.json()
            if "error" in chk_data:
                logger.warning(
                    "[WA Direct] Stored phone_number_id=%s is STALE (Meta error=%s) — will re-add",
                    stored_phone_id, chk_data["error"],
                )
                # Clear stale ID so the add-step runs below
                existing_conn.phone_number_id = None
                db.commit()
            else:
                id_valid = True
                logger.info("[WA Direct] phone_number_id=%s still valid — resuming", stored_phone_id)
        except Exception as exc:
            logger.warning("[WA Direct] Validation check failed (%s) — will re-add", exc)

        if id_valid:
            phone_number_id = stored_phone_id
            # Jump directly to OTP request — skip add step
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    otp_resp = await client.post(
                        f"{graph}/{phone_number_id}/request_code",
                        headers=headers,
                        json={"code_method": body.method.upper(), "language": "ar"},
                    )
                    otp_data = otp_resp.json()
                if "error" in otp_data:
                    err = otp_data["error"]
                    err_code    = err.get("code", 0)
                    err_subcode = err.get("error_subcode", 0)
                    user_msg    = err.get("error_user_msg", "")
                    logger.warning("[WA Direct] Resend OTP error (pending resume): %s", otp_data)

                    # Rate-limited / too many failed attempts — surface to user
                    RATE_CODES = {136024, 131056, 131042, 368, 4, 17, 80007, 2388091}
                    is_rate = (
                        err_code in RATE_CODES
                        or err_subcode in RATE_CODES
                        or "rate" in err.get("message", "").lower()
                        or "انتظار" in user_msg
                        or "wait" in err.get("message", "").lower()
                    )
                    if is_rate:
                        arabic_msg = user_msg or (
                            "لقد حاولت عدة مرات — يُرجى الانتظار بضع ساعات قبل طلب رمز جديد."
                        )
                        raise HTTPException(
                            status_code=429,
                            detail=arabic_msg,
                            headers={"X-Nahla-Error-Code": "OTP_RATE_LIMITED"},
                        )
            except HTTPException:
                raise
            except Exception as exc:
                logger.warning("[WA Direct] Resend OTP exception (pending resume): %s", exc)
            # OTP sent successfully — proceed to Step 2
            return {
                "status":          META_CODE_SENT,
                "code":            META_CODE_SENT,
                "phone_number_id": phone_number_id,
                "message":         "تم إرسال رمز التحقق — أدخل الرمز الذي وصلك.",
                "already_sent":    True,
            }
        # else: fall through to re-add the phone number below

    # ── Step A: Check if phone already exists in WABA ───────────────────────
    phone_number_id = ""
    bare_number = f"{cc}{national}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            list_resp = await client.get(
                f"{graph}/{WA_BUSINESS_ACCOUNT_ID}/phone_numbers",
                headers=headers,
                params={"fields": "id,display_phone_number,code_verification_status"},
            )
            list_data = list_resp.json()
        for entry in list_data.get("data", []):
            dp = entry.get("display_phone_number", "").replace(" ", "").replace("-", "").replace("+", "")
            if bare_number in dp or dp in bare_number:
                phone_number_id = entry["id"]
                logger.info(
                    "[WA Direct] Phone already in WABA id=%s dp=%s status=%s",
                    phone_number_id, dp, entry.get("code_verification_status"),
                )
                break
    except Exception as lookup_exc:
        logger.warning("[WA Direct] WABA phone list lookup failed: %s", lookup_exc)

    # ── Step B: Add phone number to WABA only if not already there ───────────
    if not phone_number_id:
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
            logger.warning(
                "[WA Direct] Add phone raw_error code=%s subcode=%s msg=%s full=%s",
                code, subcode, msg, add_data,
            )
            internal_code, ux_message = _normalize_meta_error(code, msg, subcode, user_msg)
            # Try to extract phone_number_id from error_data
            phone_number_id = err.get("error_data", {}).get("id", "") or ""
            if not phone_number_id and internal_code != META_ALREADY_REGISTERED:
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

    # ── Step B: Save pending state BEFORE requesting OTP ────────────────────
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if conn is None:
        conn = WhatsAppConnection(tenant_id=tenant_id)
        db.add(conn)
    conn.status          = "pending"
    conn.phone_number_id = phone_number_id
    conn.phone_number    = f"+{cc}{national}"
    conn.last_attempt_at = datetime.now(timezone.utc)
    conn.last_error      = None
    db.commit()

    # ── Step C: Request OTP ──────────────────────────────────────────────────
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
        err      = otp_data["error"]
        err_code = err.get("code", 0)
        err_sub  = err.get("error_subcode", 0)
        err_msg  = err.get("message", "")
        logger.warning(
            "[WA Direct] OTP request raw_error code=%s subcode=%s msg=%r fbtrace=%s full=%s",
            err_code, err_sub, err_msg, err.get("fbtrace_id"), otp_data,
        )
        # Rate-limited or code already sent → tell user to use the previous code
        RATE_LIMIT_CODES = {131056, 131042, 368, 4, 17}
        OTP_SENT_SUBCODES = {2388016, 2388021}
        if err_code in RATE_LIMIT_CODES or err_sub in OTP_SENT_SUBCODES or (
            "rate" in err_msg.lower() or "too many" in err_msg.lower() or
            "wait" in err_msg.lower() or "cooldown" in err_msg.lower()
        ):
            # Code was already sent — still return success so frontend goes to Step 2
            logger.info("[WA Direct] OTP rate-limited/already-sent, resuming pending state")
            return {
                "status":          META_CODE_SENT,
                "code":            META_CODE_SENT,
                "phone_number_id": phone_number_id,
                "message":         "تم إرسال رمز التحقق مسبقاً — أدخل الرمز الذي وصلك أو انتظر قليلاً قبل طلب رمز جديد.",
                "already_sent":    True,
            }
        ic, ux = _normalize_meta_error(err_code, err_msg, err_sub, err.get("error_user_msg", ""))
        conn.last_error = f"OTP_REQUEST_FAILED code={err_code} sub={err_sub} msg={err_msg}"
        db.commit()
        # If phone is already in WABA (phone_number_id known), proceed to Step 2
        # regardless of OTP error — user may already have the code
        if phone_number_id:
            logger.info(
                "[WA Direct] OTP step failed but phone_number_id=%s known → resuming Step 2",
                phone_number_id,
            )
            return {
                "status":          META_CODE_SENT,
                "code":            META_CODE_SENT,
                "phone_number_id": phone_number_id,
                "message":         "أدخل رمز التحقق الذي وصلك، أو انتظر دقائق قبل طلب رمز جديد.",
                "already_sent":    True,
            }
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


@router.post("/direct/resend-otp")
async def direct_resend_otp(
    body: DirectVerifyRequest,   # reuse — only phone_number_id is needed
    request: Request,
    db: Session = Depends(get_db),
):
    """Resend OTP to an already-registered phone number (uses saved phone_number_id)."""
    from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415
    tenant_id = resolve_tenant_id(request)
    graph   = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"

    # Prefer phone_number_id from DB to avoid spoofing
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    token_ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation="request_code",
        prefer_platform=True,
    )
    headers = {"Authorization": f"Bearer {token_ctx.token}", "Content-Type": "application/json"}
    phone_number_id = (conn.phone_number_id if conn else None) or body.phone_number_id

    if not phone_number_id:
        raise HTTPException(status_code=400, detail="لا يوجد رقم هاتف مرتبط. ابدأ من الخطوة الأولى.")

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{graph}/{phone_number_id}/request_code",
                headers=headers,
                json={"code_method": "SMS", "language": "ar"},
            )
            data = r.json()
    except Exception as exc:
        logger.error("[WA Resend] API error: %s", exc)
        raise HTTPException(status_code=503, detail="خطأ في الاتصال بـ Meta")

    if "error" in data:
        err = data["error"]
        err_code    = err.get("code", 0)
        err_subcode = err.get("error_subcode", 0)
        logger.warning("[WA Resend] raw_error code=%s subcode=%s full=%s", err_code, err_subcode, data)

        # ── Stale / invalid phone_number_id (Meta error 100, subcode 33) ──────
        # The stored ID no longer exists on Meta — try to find the fresh one or reset.
        if err_code == 100 or err_subcode == 33:
            from core.config import WA_BUSINESS_ACCOUNT_ID  # noqa: PLC0415
            fresh_id = ""
            try:
                stored_phone = (conn.phone_number or "").replace("+","").replace(" ","").replace("-","")
                logger.info("[WA Resend] ID stale — searching WABA list for phone=%s", stored_phone)
                async with httpx.AsyncClient(timeout=15) as client:
                    lst = await client.get(
                        f"{graph}/{WA_BUSINESS_ACCOUNT_ID}/phone_numbers",
                        headers=headers,
                        params={"fields": "id,display_phone_number,code_verification_status"},
                    )
                    lst_data = lst.json()
                for entry in lst_data.get("data", []):
                    dp = entry.get("display_phone_number","").replace(" ","").replace("-","").replace("+","")
                    if stored_phone and (stored_phone in dp or dp in stored_phone):
                        fresh_id = entry["id"]
                        logger.info("[WA Resend] fresh ID found: %s for phone=%s", fresh_id, stored_phone)
                        break
            except Exception as lookup_exc:
                logger.error("[WA Resend] WABA lookup failed: %s", lookup_exc)

            if fresh_id:
                # Update DB and retry request_code with fresh ID
                if conn:
                    conn.phone_number_id = fresh_id
                    db.commit()
                try:
                    async with httpx.AsyncClient(timeout=20) as client:
                        r2 = await client.post(
                            f"{graph}/{fresh_id}/request_code",
                            headers=headers,
                            json={"code_method": "SMS", "language": "ar"},
                        )
                        d2 = r2.json()
                    logger.info("[WA Resend] retry with fresh_id=%s result=%s", fresh_id, d2)
                    if "error" not in d2:
                        if conn:
                            conn.last_attempt_at = datetime.now(timezone.utc)
                            db.commit()
                        return {
                            "status":          META_CODE_SENT,
                            "phone_number_id": fresh_id,
                            "message":         "تم إرسال رمز تحقق جديد إلى رقمك.",
                        }
                except Exception as retry_exc:
                    logger.error("[WA Resend] retry with fresh_id failed: %s", retry_exc)

            # Fresh ID not found — clear stale DB record and force restart
            logger.warning("[WA Resend] Cannot recover stale ID for tenant=%s — clearing DB", tenant_id)
            if conn:
                conn.phone_number_id = None
                conn.status          = "disconnected"
                db.commit()
            raise HTTPException(
                status_code=400,
                detail="رقم الهاتف لم يعد موجوداً في النظام. يرجى العودة للخطوة الأولى وإعادة إدخال الرقم.",
                headers={"X-Nahla-Error-Code": "STALE_PHONE_ID"},
            )

        # Rate limited or cooldown — tell user to wait
        RATE_CODES = {131056, 131042, 368, 4, 17, 80007}
        if err_code in RATE_CODES or "rate" in err.get("message","").lower():
            return {
                "status":          META_CODE_SENT,
                "phone_number_id": phone_number_id,
                "message":         "انتظر بضع دقائق قبل طلب رمز جديد — الرمز السابق لا يزال صالحاً.",
                "rate_limited":    True,
            }
        ic, ux = _normalize_meta_error(err_code, err.get("message",""), err_subcode)
        raise HTTPException(status_code=400, detail=ux, headers={"X-Nahla-Error-Code": ic})

    # Update last_attempt_at
    if conn:
        conn.last_attempt_at = datetime.now(timezone.utc)
        db.commit()

    return {
        "status":          META_CODE_SENT,
        "phone_number_id": phone_number_id,
        "message":         "تم إرسال رمز تحقق جديد إلى رقمك.",
    }


@router.post("/direct/verify-otp")
async def direct_verify_otp(
    body: DirectVerifyRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Step 2/3/4 of WhatsApp registration:
      A) verify_code  — confirm OTP
      B) register     — activate phone on WhatsApp (fixes Pending/معلق in Meta)
      C) GET status   — fetch real Meta verification status
      D) Save to DB   — mark as connected only after Meta confirms
    """
    from core.config import WA_BUSINESS_ACCOUNT_ID, META_GRAPH_API_VERSION  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    graph     = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
    phone_id  = body.phone_number_id.strip()
    conn_for_token = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    token_ctx = await get_token_for_operation(
        db,
        conn_for_token,
        tenant_id=tenant_id,
        operation="phone_verify",
        prefer_platform=True,
    )
    token_tail = token_ctx.token[-8:] if token_ctx.token else "EMPTY"
    headers   = {
        "Authorization": f"Bearer {token_ctx.token}",
        "Content-Type":  "application/json",
    }

    logger.info(
        "[WA verify] ▶ START | tenant=%s phone_number_id=%s waba=%s token_tail=...%s",
        tenant_id, phone_id, WA_BUSINESS_ACCOUNT_ID, token_tail,
    )

    # ── Pre-check: confirm phone_number_id belongs to our WABA & token ────────
    # If the stored ID is stale/inaccessible, try to find the fresh ID from WABA.
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            chk_resp = await client.get(
                f"{graph}/{phone_id}",
                headers=headers,
                params={"fields": "id,display_phone_number,code_verification_status"},
            )
            chk_data   = chk_resp.json()
            chk_status = chk_resp.status_code
        logger.info(
            "[WA verify] pre-check GET /%s | http=%s body=%s",
            phone_id, chk_status, chk_data,
        )
        if "error" in chk_data:
            chk_err = chk_data["error"]
            logger.warning(
                "[WA verify] pre-check FAILED for id=%s (code=%s msg=%s) — "
                "trying fresh WABA lookup to find correct phone_number_id",
                phone_id, chk_err.get("code"), chk_err.get("message"),
            )
            # ── Fallback: find correct phone_number_id from WABA phone list ──
            fresh_id = ""
            try:
                conn_rec = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
                stored_phone = (conn_rec.phone_number or "").replace("+", "").replace(" ", "").replace("-", "")
                logger.info(
                    "[WA verify] fallback lookup — stored_phone=%s WABA=%s",
                    stored_phone, WA_BUSINESS_ACCOUNT_ID,
                )
                async with httpx.AsyncClient(timeout=15) as client:
                    list_resp = await client.get(
                        f"{graph}/{WA_BUSINESS_ACCOUNT_ID}/phone_numbers",
                        headers=headers,
                        params={"fields": "id,display_phone_number,code_verification_status"},
                    )
                    list_data = list_resp.json()
                logger.info("[WA verify] WABA phone list: %s", list_data)
                for entry in list_data.get("data", []):
                    dp = entry.get("display_phone_number", "").replace(" ", "").replace("-", "").replace("+", "")
                    if stored_phone and (stored_phone in dp or dp in stored_phone):
                        fresh_id = entry["id"]
                        logger.info(
                            "[WA verify] fallback found fresh id=%s for phone=%s",
                            fresh_id, stored_phone,
                        )
                        break
            except Exception as lookup_exc:
                logger.error("[WA verify] fallback lookup error: %s", lookup_exc)

            if fresh_id:
                logger.info(
                    "[WA verify] replacing stale phone_number_id=%s → %s",
                    phone_id, fresh_id,
                )
                phone_id = fresh_id
                # Update DB with fresh ID
                conn_u = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
                if conn_u:
                    conn_u.phone_number_id = fresh_id
                    db.commit()
            else:
                logger.error(
                    "[WA verify] phone_number_id=%s not accessible and no fresh ID found | "
                    "WABA=%s token_tail=...%s",
                    phone_id, WA_BUSINESS_ACCOUNT_ID, token_tail,
                )
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "رقم الهاتف غير متاح للتحقق. "
                        "يبدو أن الرقم لم يُضف بعد لحساب WhatsApp Business أو انتهت صلاحية الجلسة. "
                        "يرجى البدء من الخطوة الأولى مجدداً."
                    ),
                )
    except HTTPException:
        raise
    except Exception as pre_exc:
        logger.warning("[WA verify] pre-check network error (non-fatal): %s", pre_exc)

    # ── Step A: verify_code ───────────────────────────────────────────────────
    verify_endpoint = f"{graph}/{phone_id}/verify_code"
    verify_payload  = {"code": body.code}
    logger.info(
        "[WA verify] ▶ verify_code | method=POST endpoint=%s payload=%s",
        verify_endpoint, verify_payload,
    )
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            verify_resp = await client.post(
                verify_endpoint,
                headers=headers,
                json=verify_payload,
            )
            verify_status = verify_resp.status_code
            verify_data   = verify_resp.json()
    except Exception as exc:
        logger.error("[WA verify] verify_code network error: %s", exc)
        raise HTTPException(status_code=503, detail=_UX_MESSAGES[META_UNKNOWN_ERROR])

    logger.info(
        "[WA verify] verify_code response | http=%s body=%s",
        verify_status, verify_data,
    )

    if "error" in verify_data:
        err  = verify_data["error"]
        code = err.get("code", 0)
        logger.warning("[WA Direct] submit_otp Meta error | code=%s full=%s", code, verify_data)
        if code in (136012, 136013):
            raise HTTPException(
                status_code=400,
                detail="رمز التحقق غير صحيح أو منتهي الصلاحية. اطلب رمزاً جديداً.",
                headers={"X-Nahla-Error-Code": "META_INVALID_CODE"},
            )
        ic, ux = _normalize_meta_error(code, err.get("message", ""), err.get("error_subcode", 0))
        raise HTTPException(status_code=400, detail=ux, headers={"X-Nahla-Error-Code": ic})

    # ── Step B: register — activates the phone on WhatsApp Cloud API ─────────
    # Without this call the phone stays "Pending/معلق" in Meta Business Manager.
    from routers.whatsapp_embedded import _resolve_register_pin  # noqa: PLC0415
    _conn_for_pin = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    _reg_pin = _resolve_register_pin(_conn_for_pin) if _conn_for_pin else "000000"
    if _conn_for_pin:
        db.commit()

    logger.info(
        "[WA Direct] ▶ register | endpoint=POST %s/%s/register",
        graph, phone_id,
    )
    register_data: dict = {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            reg_resp  = await client.post(
                f"{graph}/{phone_id}/register",
                headers=headers,
                json={"messaging_product": "whatsapp", "pin": _reg_pin},
            )
            reg_status = reg_resp.status_code
            register_data = reg_resp.json()
    except Exception as exc:
        logger.error("[WA Direct] register network error: %s", exc)
        register_data = {}
        reg_status    = 0

    logger.info(
        "[WA Direct] register response | status=%s body=%s",
        reg_status, register_data,
    )

    if "error" in register_data:
        reg_err = register_data["error"]
        reg_code = reg_err.get("code", 0)
        # Error 80007 = already registered — that is acceptable
        if reg_code != 80007:
            logger.warning(
                "[WA Direct] register failed | code=%s full=%s",
                reg_code, register_data,
            )
            ic, ux = _normalize_meta_error(reg_code, reg_err.get("message", ""), reg_err.get("error_subcode", 0))
            raise HTTPException(
                status_code=400,
                detail=f"فشل تفعيل الرقم في Meta: {ux}",
                headers={"X-Nahla-Error-Code": ic},
            )

    # ── Step C: fetch real phone status from Meta ─────────────────────────────
    logger.info(
        "[WA Direct] ▶ fetch_phone_status | endpoint=GET %s/%s",
        graph, phone_id,
    )
    phone_number  = ""
    display_name  = ""
    meta_status   = ""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            info_resp = await client.get(
                f"{graph}/{phone_id}",
                headers=headers,
                params={"fields": "id,display_phone_number,verified_name,code_verification_status,quality_rating"},
            )
            info_status = info_resp.status_code
            info        = info_resp.json()

        logger.info(
            "[WA Direct] fetch_phone_status response | status=%s body=%s",
            info_status, info,
        )

        phone_number = info.get("display_phone_number", "")
        display_name = info.get("verified_name", "")
        meta_status  = info.get("code_verification_status", "")
    except Exception as exc:
        logger.error("[WA Direct] fetch_phone_status error: %s", exc)

    # ── Step D: persist to DB using live Meta state ──────────────────────────
    # IMPORTANT:
    #   Never mark connected immediately after verify_code/register.
    #   The number is only "connected" when Meta confirms it is actually ready.
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if conn is None:
        conn = WhatsAppConnection(tenant_id=tenant_id)
        db.add(conn)

    from routers.whatsapp_embedded import _build_phone_sync_state  # noqa: PLC0415
    sync_state = _build_phone_sync_state(info if isinstance(info, dict) else {})

    conn.status                       = sync_state.get("db_status") or "activation_pending"
    conn.phone_number_id              = phone_id
    conn.phone_number                 = phone_number
    conn.business_display_name        = display_name
    conn.whatsapp_business_account_id = WA_BUSINESS_ACCOUNT_ID
    conn.connection_type              = "direct"
    conn.access_token                 = None
    conn.token_type                   = None
    conn.token_expires_at             = None
    conn.webhook_verified             = bool(sync_state.get("connected"))
    conn.sending_enabled              = bool(sync_state.get("sending_enabled"))
    conn.connected_at                 = datetime.now(timezone.utc) if sync_state.get("connected") else None
    conn.last_verified_at             = datetime.now(timezone.utc) if meta_status == "VERIFIED" else conn.last_verified_at
    conn.last_error                   = None if conn.sending_enabled else sync_state.get("message")
    conn.extra_metadata               = {
        **(conn.extra_metadata or {}),
        "meta_code_verification_status": sync_state.get("verification_status"),
        "meta_name_status": sync_state.get("name_status"),
        "meta_phone_status": sync_state.get("meta_phone_status"),
        "meta_quality_rating": sync_state.get("quality_rating"),
        "embedded_status_message": sync_state.get("message"),
        "meta_register_response": register_data,
    }
    update_token_state(
        conn,
        token_source=token_ctx.source,
        token_status=token_ctx.token_status,
        oauth_session_status="not_applicable",
        oauth_session_message=None,
    )

    db.commit()

    logger.info(
        "[WA Direct] ✅ Finalized | tenant=%s phone=%s name=%s meta_verification=%s db_status=%s sending_enabled=%s",
        tenant_id, phone_number, display_name, meta_status, conn.status, conn.sending_enabled,
    )

    return {
        "status":              conn.status,
        "phone_number":        phone_number,
        "display_name":        display_name,
        "meta_status":         meta_status,
        "register_response":   register_data,
        "sending_enabled":     bool(conn.sending_enabled),
        "message":             (
            "تم ربط واتساب بنجاح! 🎉"
            if conn.sending_enabled
            else (sync_state.get("message") or "تم التحقق من الرقم، لكن Meta ما زالت تُكمل التفعيل.")
        ),
    }



def _build_wa_status(conn: Optional[WhatsAppConnection]) -> dict:
    """Build the unified WhatsApp status response from a DB record (or None)."""
    if not conn or conn.status == "not_connected":
        return {
            "connected": False,
            "status": "not_connected",
            "connection_status": "not_connected",
            "oauth_session_status": "missing",
            "oauth_session_message": None,
            "oauth_session_needs_reauth": False,
            "token_status": "missing",
            "token_health": "missing",
        }

    meta = dict(conn.extra_metadata or {})
    oauth_status, oauth_message = get_oauth_session_state(conn)
    token_ctx = get_token_context(conn)
    resp: dict = {
        "connected":              bool(conn.status == "connected" and conn.sending_enabled),
        "status":                 conn.status,
        "connection_status":      conn.status,
        "phone_number":           conn.phone_number,
        "display_phone_number":   conn.phone_number,
        "business_display_name":  conn.business_display_name,
        "display_name":           conn.business_display_name,
        "phone_number_id":        conn.phone_number_id,
        "waba_id":                conn.whatsapp_business_account_id,
        "verification_status":    (
            ((conn.extra_metadata or {}).get("meta_code_verification_status"))
            or ("verified" if conn.status == "connected" else conn.status)
        ),
        "connected_at":           conn.connected_at.isoformat() if conn.connected_at else None,
        "last_verified_at":       conn.last_verified_at.isoformat() if conn.last_verified_at else None,
        "last_error":             conn.last_error,
        "sending_enabled":        bool(conn.sending_enabled),
        "webhook_verified":       bool(conn.webhook_verified),
        "token_expires_at":       conn.token_expires_at.isoformat() if conn.token_expires_at else None,
        "meta_business_account_id": conn.meta_business_account_id,
        "oauth_session_status":   oauth_status,
        "oauth_session_message":  oauth_message,
        "oauth_session_needs_reauth": oauth_status in {"expired", "invalid", "missing"},
        "active_graph_token_source": meta.get("active_graph_token_source", token_ctx.source),
        "token_status":           meta.get("token_status", token_ctx.token_status),
        "token_health":           meta.get("token_health", token_ctx.token_status),
    }
    if meta.get("meta_name_status") is not None:
        resp["name_status"] = meta.get("meta_name_status")
    if meta.get("meta_phone_status") is not None:
        resp["meta_phone_status"] = meta.get("meta_phone_status")
    if meta.get("embedded_status_message") is not None:
        resp["message"] = meta.get("embedded_status_message")
    if meta.get("meta_quality_rating") is not None:
        resp["quality_rating"] = meta.get("meta_quality_rating")

    if conn.status in ("pending", "otp_pending", "activation_pending", "review_pending") and conn.phone_number_id:
        resp["last_attempt_at"] = (
            conn.last_attempt_at.isoformat() if conn.last_attempt_at else None
        )
    return resp


# ── Unified WhatsApp status (single source of truth) ─────────────────────────

@router.get("/status")
async def whatsapp_status(request: Request, db: Session = Depends(get_db)):
    """
    Unified WhatsApp connection status — used by ALL pages.
    Single source of truth for connected state, phone number, etc.
    """
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if conn and conn.connection_type == "embedded" and conn.phone_number_id:
        try:
            from routers.whatsapp_embedded import sync_embedded_connection_from_meta  # noqa: PLC0415
            return await sync_embedded_connection_from_meta(conn, db, attempt_register=True)
        except Exception as exc:
            logger.warning("[whatsapp/status] embedded sync failed tenant=%s: %s", tenant_id, exc)
    return _build_wa_status(conn)


@router.get("/direct/status")
async def direct_status(request: Request, db: Session = Depends(get_db)):
    """Return the current direct-registration connection status (delegates to unified status)."""
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    return _build_wa_status(conn)


@router.post("/direct/refresh-from-meta")
async def refresh_status_from_meta(request: Request, db: Session = Depends(get_db)):
    """
    Re-check the phone number status in Meta and update the DB if verified.

    Useful when:
    - The number was verified in Meta but the DB still shows 'pending'
    - The merchant already registered the number manually in Meta Business Manager
    - The OTP was received and verified but the Nahla UI still shows the OTP step
    """
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()

    if not conn or not conn.phone_number_id:
        return {"updated": False, "message": "لا يوجد رقم هاتف مسجّل. أكمل ربط الرقم أولاً."}

    if conn.status == "connected":
        return {"updated": False, "already_connected": True, **_build_wa_status(conn)}

    from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415
    graph   = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
    phone_id = conn.phone_number_id
    token_ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation="status_sync",
        prefer_platform=True,
    )
    headers = {
        "Authorization": f"Bearer {token_ctx.token}",
        "Content-Type": "application/json",
    }

    logger.info(
        "[WA refresh] ▶ fetch_phone_status | tenant=%s phone_id=%s endpoint=GET %s/%s",
        tenant_id, phone_id, graph, phone_id,
    )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{graph}/{phone_id}",
                headers=headers,
                params={"fields": "id,display_phone_number,verified_name,code_verification_status,name_status,status,quality_rating"},
            )
            resp_status = resp.status_code
            data = resp.json()
    except Exception as exc:
        logger.error("[WA refresh] Meta API error: %s", exc)
        return {"updated": False, "message": "تعذّر الاتصال بـ Meta. حاول مرة أخرى."}

    logger.info(
        "[WA refresh] fetch_phone_status response | status=%s body=%s",
        resp_status, data,
    )

    if "error" in data:
        err = data["error"]
        logger.warning("[WA refresh] Meta error: %s", err)
        return {
            "updated": False,
            "meta_response": data,
            "message": f"Meta: {err.get('message', 'خطأ غير معروف')}",
        }

    verification_status = data.get("code_verification_status", "")
    display_phone       = data.get("display_phone_number", conn.phone_number or "")
    verified_name       = data.get("verified_name", conn.business_display_name or "")

    logger.info(
        "[WA refresh] tenant=%s phone_id=%s meta_verification=%s",
        tenant_id, phone_id, verification_status,
    )

    # If NOT_VERIFIED: attempt register to re-activate
    if verification_status != "VERIFIED":
        from routers.whatsapp_embedded import _resolve_register_pin as _rrp  # noqa: PLC0415
        _refresh_pin = _rrp(conn) if conn else "000000"
        if conn:
            db.commit()

        logger.info(
            "[WA refresh] ▶ re-register | endpoint=POST %s/%s/register",
            graph, phone_id,
        )
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                reg = await client.post(
                    f"{graph}/{phone_id}/register",
                    headers=headers,
                    json={"messaging_product": "whatsapp", "pin": _refresh_pin},
                )
                reg_data = reg.json()
            logger.info("[WA refresh] re-register response | status=%s body=%s", reg.status_code, reg_data)
        except Exception as exc:
            logger.error("[WA refresh] re-register error: %s", exc)
            reg_data = {}

    from routers.whatsapp_embedded import _build_phone_sync_state as _bps  # noqa: PLC0415
    sync_state = _bps(data if isinstance(data, dict) else {})

    if sync_state.get("connected"):
        from datetime import datetime, timezone as _tz  # noqa: PLC0415
        conn.status                = "connected"
        conn.sending_enabled       = True
        conn.phone_number          = display_phone
        conn.business_display_name = verified_name or conn.business_display_name
        conn.connected_at          = datetime.now(_tz.utc)
        conn.last_error            = None
        conn.extra_metadata        = {
            **(conn.extra_metadata or {}),
            "meta_code_verification_status": sync_state.get("verification_status"),
            "meta_name_status": sync_state.get("name_status"),
            "meta_phone_status": sync_state.get("meta_phone_status"),
            "meta_quality_rating": sync_state.get("quality_rating"),
            "embedded_status_message": sync_state.get("message"),
        }
        db.commit()
        logger.info("[WA refresh] CONNECTED tenant=%s", tenant_id)
        return {
            "updated": True,
            "meta_response": data,
            "message": "✅ تم التحقق من الرقم في Meta وتم تحديث حالة الاتصال.",
            **_build_wa_status(conn),
        }

    # Not verified yet — return full Meta response for diagnosis
    conn.status          = sync_state.get("db_status") or "activation_pending"
    conn.sending_enabled = bool(sync_state.get("sending_enabled"))
    conn.last_error      = sync_state.get("message")
    conn.extra_metadata  = {
        **(conn.extra_metadata or {}),
        "meta_code_verification_status": sync_state.get("verification_status"),
        "meta_name_status": sync_state.get("name_status"),
        "meta_phone_status": sync_state.get("meta_phone_status"),
        "meta_quality_rating": sync_state.get("quality_rating"),
        "embedded_status_message": sync_state.get("message"),
    }
    db.commit()

    return {
        "updated":      False,
        "meta_status":  verification_status,
        "meta_response": data,
        "status": conn.status,
        "sending_enabled": conn.sending_enabled,
        "message": sync_state.get("message") or (
            f"الرقم لم يُفعَّل بعد في Meta. حالته: {verification_status or 'غير معروف'}."
        ),
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
    from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    graph     = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    token_ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation="save_profile",
        prefer_platform=True,
    )
    headers   = {
        "Authorization": f"Bearer {token_ctx.token}",
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
