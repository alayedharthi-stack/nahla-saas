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
from typing import Any, Dict, List, Optional

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
from services.whatsapp_platform.service import graph_get_with_context, graph_post_with_context
from services.whatsapp_platform.token_manager import (
    get_oauth_session_state as _shared_get_oauth_session_state,
    get_token_candidates,
    get_token_for_operation,
    persist_token_context,
    update_token_state as _shared_update_token_state,
)
from routers.whatsapp_connect import (
    WHATSAPP_PROVIDER_META,
    _merchant_channel_label,
    _provider_label,
    _wa_provider,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/whatsapp/embedded", tags=["whatsapp-embedded"])

GRAPH = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
PHONE_FIELDS = (
    "id,display_phone_number,verified_name,code_verification_status,"
    "name_status,status,quality_rating"
)
_DEFAULT_PIN = "000000"


def _resolve_register_pin(conn: "WhatsAppConnection") -> str:
    """Return the tenant's two-step verification PIN for Cloud API register.

    Priority:
      1. PIN already stored in extra_metadata (set during previous register).
      2. Generate a random 6-digit PIN, store it, and return it.

    The PIN is persisted so re-register after display-name changes uses the
    same value (Meta requires consistency until the tenant resets it).
    """
    import secrets  # noqa: PLC0415
    meta = dict(conn.extra_metadata or {})
    existing = meta.get("wa_register_pin")
    if existing and len(str(existing)) == 6 and str(existing).isdigit():
        return str(existing)
    pin = f"{secrets.randbelow(1_000_000):06d}"
    meta["wa_register_pin"] = pin
    conn.extra_metadata = meta
    return pin


# ── helpers ───────────────────────────────────────────────────────────────────

def resolve_tenant_id(request: Request) -> int:
    """Extract tenant_id from the authenticated session (JWT middleware).

    Priority: request.state (set by auth middleware) > X-Tenant-ID header.
    Never reads from query params, cookies, or callback data.
    """
    tid = request.state.__dict__.get("tenant_id") or request.headers.get("X-Tenant-ID")
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


async def _exchange_for_long_lived_token(short_token: str) -> dict:
    """Exchange a short-lived user token for a long-lived token when possible."""
    if not META_APP_ID or not META_APP_SECRET or not short_token:
        return {"access_token": short_token, "token_type": "short_lived"}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{GRAPH}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": META_APP_ID,
                "client_secret": META_APP_SECRET,
                "fb_exchange_token": short_token,
            },
        )
        data = resp.json()
    if "error" in data:
        logger.warning("[EmbeddedSignup] long-lived exchange failed: %s", data)
        return {"access_token": short_token, "token_type": "short_lived"}
    return {
        "access_token": data.get("access_token", short_token),
        "token_type": "long_lived",
        "expires_in": data.get("expires_in", 5183944),
    }


def _token_expiry_from_debug(debug_info: Dict[str, Any]) -> Optional[datetime]:
    raw_expires = debug_info.get("expires_at")
    try:
        if raw_expires:
            return datetime.fromtimestamp(int(raw_expires), tz=timezone.utc)
    except Exception:
        pass
    return None


def _update_oauth_state(
    conn: "WhatsAppConnection",
    *,
    status: str,
    message: Optional[str] = None,
    token_source: Optional[str] = None,
    debug_info: Optional[Dict[str, Any]] = None,
    expires_at: Optional[datetime] = None,
) -> None:
    _shared_update_token_state(
        conn,
        token_source=token_source,
        token_status="healthy" if token_source == "platform" else None,
        token_expires_at=expires_at,
        oauth_session_status=status,
        oauth_session_message=message,
        debug_info=debug_info,
    )


def _get_oauth_session_state(conn: Optional["WhatsAppConnection"]) -> tuple[str, Optional[str]]:
    return _shared_get_oauth_session_state(conn)


def _candidate_graph_tokens(
    conn: "WhatsAppConnection",
    *,
    prefer_platform: bool,
):
    return get_token_candidates(conn, prefer_platform=prefer_platform)


async def _get_waba_id_from_token(token: str) -> str:
    """
    Extract the WhatsApp Business Account ID from the token using multiple strategies:
    1. debug_token granular_scopes (works when config_id triggers full WA signup)
    2. GET /me/businesses → list WABAs per business
    3. GET /me/whatsapp_business_accounts (direct query)
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

    # Strategy 3: direct query (some token types expose this edge)
    logger.info("[EmbeddedSignup] Trying /me/whatsapp_business_accounts direct query")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            direct_resp = await client.get(
                f"{GRAPH}/me/whatsapp_business_accounts",
                headers={"Authorization": f"Bearer {token}"},
                params={"fields": "id,name"},
            )
            direct_data = direct_resp.json()
        logger.info("[EmbeddedSignup] /me/whatsapp_business_accounts: %s", direct_data)
        for waba in direct_data.get("data", []):
            logger.info("[EmbeddedSignup] WABA found via direct query: %s", waba["id"])
            return str(waba["id"])
    except Exception as e:
        logger.warning("[EmbeddedSignup] Direct WABA query failed: %s", e)

    raise HTTPException(
        status_code=400,
        detail=(
            "تعذّر العثور على حساب واتساب للأعمال. "
            "أعد المحاولة واختر «إنشاء حساب واتساب جديد» في نافذة Meta أثناء الربط."
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


def _meta_flag(value: Any) -> str:
    """Normalize Meta enum-like values for resilient comparisons."""
    return str(value or "").strip().upper().replace(" ", "_").replace("-", "_")


def _meta_has_token(value: str, *tokens: str) -> bool:
    return any(token in value for token in tokens)


def _meta_embedded_error_message(error: Dict[str, Any], fallback: str) -> str:
    """Map raw Meta embedded-signup errors to merchant-friendly Arabic text."""
    code = int(error.get("code") or 0)
    subcode = int(error.get("error_subcode") or 0)
    message = str(error.get("message") or "")
    raw = f"{code}:{subcode}:{message}".lower()

    if code == 131000 or "something went wrong" in raw:
        return (
            "Meta واجهت خللًا مؤقتًا أثناء مزامنة حالة الرقم. "
            "إذا وصلك رمز التحقق أو تم التحقق منه بنجاح، انتظر قليلًا ثم اضغط تحديث الآن."
        )
    if code == 190:
        return (
            "انتهت جلسة Meta الإدارية في نحلة. إذا كان الرقم ما زال ظاهرًا في Meta فالاتصال نفسه "
            "غالبًا مستمر، لكن قد تحتاج إعادة التفويض لإدارة واتساب من داخل نحلة."
        )
    if "permission" in raw or code in (10, 200):
        return "تعذر إكمال العملية بسبب صلاحيات Meta. تأكد من ربط الحساب الصحيح ثم أعد المحاولة."
    return fallback


def _serialize_phones(phones: List[dict]) -> List[dict]:
    return [
        {
            "id": p["id"],
            "number": p.get("display_phone_number", ""),
            "name": p.get("verified_name", ""),
            "verified": _meta_flag(p.get("code_verification_status")) == "VERIFIED",
        }
        for p in phones
    ]


async def _get_phone_details(phone_number_id: str, token: str) -> Dict[str, Any]:
    """Fetch live phone state from Meta for a single phone number."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{GRAPH}/{phone_number_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": PHONE_FIELDS},
        )
        data = resp.json()
    logger.info("[EmbeddedSignup] phone_details phone_id=%s result=%s", phone_number_id, data)
    return data


async def _register_phone(phone_number_id: str, token: str, pin: str) -> Dict[str, Any]:
    """Activate the phone on WhatsApp Cloud API after OTP verification."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{GRAPH}/{phone_number_id}/register",
            headers={"Authorization": f"Bearer {token}"},
            json={"messaging_product": "whatsapp", "pin": pin},
        )
        data = resp.json()
    logger.info("[EmbeddedSignup] register phone_id=%s result=%s", phone_number_id, data)
    return data


async def _get_phone_details_with_fallback(
    conn: "WhatsAppConnection",
    db: Session,
    phone_number_id: Optional[str] = None,
) -> tuple[Dict[str, Any], Optional[str]]:
    """Fetch live phone state using OAuth token first, then platform token if needed."""
    last_data: Dict[str, Any] = {}
    last_source: Optional[str] = None
    tenant_id = getattr(conn, "tenant_id", None)
    resolved_phone_id = phone_number_id or conn.phone_number_id or ""
    for ctx in _candidate_graph_tokens(conn, prefer_platform=False):
        last_source = ctx.source
        persist_token_context(db, conn, tenant_id=tenant_id, operation="embedded.status_sync", ctx=ctx)
        data = await graph_get_with_context(
            ctx,
            tenant_id=tenant_id,
            operation="embedded.status_sync",
            path=resolved_phone_id,
            params={"fields": PHONE_FIELDS},
            timeout=15,
        )
        if "error" not in data:
            _update_oauth_state(conn, status="healthy", token_source=ctx.source)
            return data, ctx.source
        last_data = data
        err = data.get("error") or {}
        if ctx.source == "merchant_oauth" and int(err.get("code") or 0) == 190:
            _update_oauth_state(
                conn,
                status="expired",
                message=_meta_embedded_error_message(err, "انتهت صلاحية جلسة Meta."),
                token_source=ctx.source,
            )
    return last_data, last_source


async def _register_phone_with_fallback(
    conn: "WhatsAppConnection",
    db: Session,
    pin: str,
) -> tuple[Dict[str, Any], Optional[str]]:
    """Register the phone with the most stable token available."""
    last_data: Dict[str, Any] = {}
    last_source: Optional[str] = None
    tenant_id = getattr(conn, "tenant_id", None)
    for ctx in _candidate_graph_tokens(conn, prefer_platform=True):
        last_source = ctx.source
        persist_token_context(db, conn, tenant_id=tenant_id, operation="embedded.phone_register", ctx=ctx)
        data = await graph_post_with_context(
            ctx,
            tenant_id=tenant_id,
            operation="embedded.phone_register",
            path=f"{conn.phone_number_id or ''}/register",
            json={"messaging_product": "whatsapp", "pin": pin},
            timeout=20,
        )
        if "error" not in data:
            return data, ctx.source
        last_data = data
        err = data.get("error") or {}
        if ctx.source == "merchant_oauth" and int(err.get("code") or 0) == 190:
            _update_oauth_state(
                conn,
                status="expired",
                message=_meta_embedded_error_message(err, "انتهت صلاحية جلسة Meta."),
                token_source=ctx.source,
            )
    return last_data, last_source


def _build_phone_sync_state(phone_data: Dict[str, Any]) -> Dict[str, Any]:
    """Map raw Meta phone state to Nahla's connection state machine."""
    code_status = _meta_flag(phone_data.get("code_verification_status"))
    name_status = _meta_flag(phone_data.get("name_status"))
    phone_status = _meta_flag(phone_data.get("status"))
    quality_rating = phone_data.get("quality_rating")

    is_name_rejected = _meta_has_token(
        name_status, "REJECT", "DECLIN", "DISAPPROV", "DENY", "BLOCK",
    )
    is_name_pending = _meta_has_token(name_status, "PENDING", "REVIEW")
    is_phone_rejected = _meta_has_token(
        phone_status, "RESTRICT", "DISABLE", "BLOCK", "DELETE", "FLAG",
    )
    is_phone_pending = _meta_has_token(
        phone_status, "PENDING", "MIGRAT", "OFFLINE", "IN_PROGRESS",
    )
    is_phone_connected = _meta_has_token(phone_status, "CONNECTED", "ONLINE", "ACTIVE")
    is_verified = code_status == "VERIFIED"

    if is_name_rejected:
        return {
            "connected": False,
            "sending_enabled": False,
            "db_status": "error",
            "verification_status": code_status or None,
            "name_status": name_status or None,
            "meta_phone_status": phone_status or None,
            "quality_rating": quality_rating,
            "message": (
                "تم التحقق من الرقم لكن Meta رفضت اسم العرض. "
                "عدّل الاسم التجاري ليطابق نشاط التاجر ثم أعد المحاولة."
            ),
        }

    if is_phone_rejected:
        return {
            "connected": False,
            "sending_enabled": False,
            "db_status": "error",
            "verification_status": code_status or None,
            "name_status": name_status or None,
            "meta_phone_status": phone_status or None,
            "quality_rating": quality_rating,
            "message": "Meta أوقفت هذا الرقم أو قيّدته، لذلك لا يمكن تفعيله حاليًا.",
        }

    if not is_verified:
        return {
            "connected": False,
            "sending_enabled": False,
            "db_status": "otp_pending",
            "verification_status": code_status or None,
            "name_status": name_status or None,
            "meta_phone_status": phone_status or None,
            "quality_rating": quality_rating,
            "message": "يلزم إدخال رمز التحقق الذي أرسلته Meta لإكمال ربط الرقم.",
        }

    # If Meta already says the phone is connected and the OTP is verified,
    # treat it as ready even if the display name is still under review.
    if is_phone_connected:
        return {
            "connected": True,
            "sending_enabled": True,
            "db_status": "connected",
            "verification_status": code_status or None,
            "name_status": name_status or None,
            "meta_phone_status": phone_status or None,
            "quality_rating": quality_rating,
            "message": (
                "الرقم مفعّل وجاهز للإرسال. اسم العرض ما زال تحت مراجعة Meta، "
                "لكن ذلك لا يمنع تفعيل الرقم."
                if is_name_pending else
                "الرقم مفعّل ومتزامن مع Meta وجاهز للإرسال."
            ),
        }

    if is_name_pending:
        return {
            "connected": False,
            "sending_enabled": False,
            "db_status": "review_pending",
            "verification_status": code_status or None,
            "name_status": name_status or None,
            "meta_phone_status": phone_status or None,
            "quality_rating": quality_rating,
            "message": (
                "تم التحقق من الرقم، لكن اسم العرض ما زال تحت مراجعة Meta. "
                "سيظهر الرقم كـمعلّق إلى أن تنتهي المراجعة."
            ),
        }

    if is_phone_pending:
        return {
            "connected": False,
            "sending_enabled": False,
            "db_status": "activation_pending",
            "verification_status": code_status or None,
            "name_status": name_status or None,
            "meta_phone_status": phone_status or None,
            "quality_rating": quality_rating,
            "message": (
                "تم التحقق من الرقم، لكن Meta ما زالت تُكمل تفعيله على Cloud API. "
                "سننتظر حتى تصبح الحالة جاهزة فعليًا."
            ),
        }

    return {
        "connected": True,
        "sending_enabled": True,
        "db_status": "connected",
        "verification_status": code_status or None,
        "name_status": name_status or None,
        "meta_phone_status": phone_status or None,
        "quality_rating": quality_rating,
        "message": "الرقم مفعّل ومتزامن مع Meta وجاهز للإرسال.",
    }


def _apply_embedded_state(
    conn: WhatsAppConnection,
    phone_data: Dict[str, Any],
    sync_state: Dict[str, Any],
    register_data: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist the normalized Meta state into the WhatsAppConnection row."""
    now = datetime.now(timezone.utc)
    meta = dict(conn.extra_metadata or {})
    meta.update({
        "meta_code_verification_status": sync_state.get("verification_status"),
        "meta_name_status": sync_state.get("name_status"),
        "meta_phone_status": sync_state.get("meta_phone_status"),
        "meta_quality_rating": sync_state.get("quality_rating"),
        "embedded_status_message": sync_state.get("message"),
        "last_meta_sync_at": now.isoformat(),
    })
    if register_data is not None:
        meta["meta_register_response"] = register_data

    conn.extra_metadata = meta
    conn.connection_type = "embedded"
    conn.provider = WHATSAPP_PROVIDER_META
    conn.phone_number = phone_data.get("display_phone_number") or conn.phone_number
    conn.business_display_name = phone_data.get("verified_name") or conn.business_display_name
    conn.status = sync_state["db_status"]
    conn.sending_enabled = bool(sync_state["sending_enabled"])

    if sync_state.get("verification_status") == "VERIFIED":
        conn.last_verified_at = now

    if sync_state["connected"]:
        conn.connected_at = conn.connected_at or now
        conn.last_error = None
    elif conn.status == "error":
        conn.last_error = sync_state["message"]
    else:
        conn.last_error = None


def _build_embedded_status_payload(
    conn: Optional[WhatsAppConnection],
    phones: Optional[List[dict]] = None,
) -> Dict[str, Any]:
    if not conn or conn.connection_type != "embedded":
        return {"connected": False, "status": "not_connected", "phones": phones or []}

    meta = dict(conn.extra_metadata or {})
    oauth_status, oauth_message = _get_oauth_session_state(conn)
    payload: Dict[str, Any] = {
        "connected": bool(conn.status == "connected" and conn.sending_enabled),
        "status": conn.status,
        "connection_status": conn.status,
        "connection_type": conn.connection_type,
        "provider": _wa_provider(conn),
        "provider_label": _provider_label(conn),
        "merchant_channel_label": _merchant_channel_label(conn),
        "meta_business_account_id": conn.meta_business_account_id,
        "phone_number_id": conn.phone_number_id,
        "phone_number": conn.phone_number,
        "display_phone_number": conn.phone_number,
        "waba_id": conn.whatsapp_business_account_id,
        "business_display_name": conn.business_display_name,
        "display_name": conn.business_display_name,
        "sending_enabled": bool(conn.sending_enabled),
        "verification_status": meta.get("meta_code_verification_status"),
        "name_status": meta.get("meta_name_status"),
        "meta_phone_status": meta.get("meta_phone_status"),
        "quality_rating": meta.get("meta_quality_rating"),
        "message": meta.get("embedded_status_message"),
        "connected_at": conn.connected_at.isoformat() if conn.connected_at else None,
        "last_verified_at": conn.last_verified_at.isoformat() if conn.last_verified_at else None,
        "last_attempt_at": conn.last_attempt_at.isoformat() if conn.last_attempt_at else None,
        "webhook_verified": bool(conn.webhook_verified),
        "token_expires_at": conn.token_expires_at.isoformat() if conn.token_expires_at else None,
        "last_error": conn.last_error,
        "oauth_session_status": oauth_status,
        "oauth_session_message": oauth_message,
        "oauth_session_needs_reauth": oauth_status in {"expired", "invalid", "missing"},
        "active_graph_token_source": meta.get("active_graph_token_source"),
        "token_status": meta.get("token_status", "healthy" if meta.get("active_graph_token_source") == "platform" else None),
        "token_health": meta.get("token_health", meta.get("token_status")),
    }
    if phones is not None:
        payload["phones"] = phones
    return payload


async def sync_embedded_connection_from_meta(
    conn: WhatsAppConnection,
    db: Session,
    *,
    attempt_register: bool = True,
) -> Dict[str, Any]:
    """
    Pull the live phone state from Meta and persist it.
    When the OTP is already verified, also attempts the final Cloud API register step.
    """
    if not conn.phone_number_id:
        return _build_embedded_status_payload(conn)

    phone_data, token_source = await _get_phone_details_with_fallback(conn, db)
    if "error" in phone_data:
        err = phone_data["error"]
        meta = dict(conn.extra_metadata or {})
        prev_name_status = _meta_flag(meta.get("meta_name_status"))
        was_verified = bool(conn.last_verified_at or meta.get("meta_code_verification_status") == "VERIFIED")
        transient_msg = _meta_embedded_error_message(
            err,
            f"تعذر مزامنة حالة الرقم مع Meta: {err.get('message', '')}",
        )

        err_code = int(err.get("code") or 0)
        if err_code == 190:
            _update_oauth_state(
                conn,
                status="expired",
                message=transient_msg,
                token_source=token_source,
            )
            meta["embedded_status_message"] = (
                "الرقم ما زال مربوطًا في Meta ونحلة، لكن جلسة Meta الإدارية انتهت. "
                "قد تحتاج فقط إلى إعادة التفويض لإدارة الحساب من داخل نحلة."
            )
            meta["last_meta_sync_error"] = err
            meta["last_meta_sync_at"] = datetime.now(timezone.utc).isoformat()
            conn.extra_metadata = meta
            conn.last_error = None
            db.commit()
            return _build_embedded_status_payload(conn)

        if was_verified and err_code == 131000:
            conn.status = "review_pending" if _meta_has_token(prev_name_status, "PENDING", "REVIEW") else (
                conn.status if conn.status in ("activation_pending", "review_pending") else "activation_pending"
            )
            conn.sending_enabled = False
            conn.last_error = None
            meta["embedded_status_message"] = transient_msg
            meta["last_meta_sync_error"] = err
        else:
            conn.status = "error"
            conn.sending_enabled = False
            conn.last_error = transient_msg
            meta["embedded_status_message"] = transient_msg
            meta["last_meta_sync_error"] = err

        meta["last_meta_sync_at"] = datetime.now(timezone.utc).isoformat()
        conn.extra_metadata = meta
        db.commit()
        return _build_embedded_status_payload(conn)

    sync_state = _build_phone_sync_state(phone_data)
    register_data: Optional[Dict[str, Any]] = None

    should_register = (
        attempt_register
        and sync_state.get("verification_status") == "VERIFIED"
        and not sync_state["connected"]
    )
    if should_register:
        pin = _resolve_register_pin(conn)
        register_data, _ = await _register_phone_with_fallback(conn, db, pin)
        reg_error = register_data.get("error")
        if reg_error and reg_error.get("code") != 80007:
            reg_msg = reg_error.get("message", "تعذر إكمال التفعيل")
            reg_flag = _meta_flag(reg_msg)
            if _meta_has_token(reg_flag, "PENDING", "REVIEW"):
                sync_state = {
                    **sync_state,
                    "connected": False,
                    "sending_enabled": False,
                    "db_status": "review_pending",
                    "message": (
                        "تم التحقق من الرقم، لكن Meta ما زالت تراجع الاسم التجاري "
                        "أو بيانات الحساب قبل تفعيل الإرسال."
                    ),
                }
            else:
                sync_state = {
                    **sync_state,
                    "connected": False,
                    "sending_enabled": False,
                    "db_status": "error",
                    "message": f"فشل تفعيل الرقم في Meta: {reg_msg}",
                }
            _apply_embedded_state(conn, phone_data, sync_state, register_data)
            db.commit()
            return _build_embedded_status_payload(conn)

        refreshed, _ = await _get_phone_details_with_fallback(conn, db)
        if "error" not in refreshed:
            phone_data = refreshed
            sync_state = _build_phone_sync_state(phone_data)

    _apply_embedded_state(conn, phone_data, sync_state, register_data)

    # When the connection finalises (status=connected), attempt webhook subscription
    # if it hasn't been done yet, and persist the verified flag explicitly.
    if sync_state.get("connected") and not conn.webhook_verified:
        from services.whatsapp_connection_service import subscribe_waba_webhook  # noqa: PLC0415
        wh_ok, wh_err = subscribe_waba_webhook(
            conn.whatsapp_business_account_id or "",
            conn.access_token or "",
            conn.tenant_id,
        )
        if wh_ok:
            conn.webhook_verified = True
            logger.info(
                "[EmbeddedSignup] webhook subscribed on finalise — tenant=%s waba=%s",
                conn.tenant_id, conn.whatsapp_business_account_id,
            )
        else:
            logger.warning(
                "[EmbeddedSignup] webhook subscription FAILED on finalise — "
                "tenant=%s waba=%s err=%r",
                conn.tenant_id, conn.whatsapp_business_account_id, wh_err,
            )
        meta = dict(conn.extra_metadata or {})
        meta["webhook_subscription_error"] = wh_err
        conn.extra_metadata = meta

    db.commit()
    return _build_embedded_status_payload(conn)


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

    # 0 — Verify the tenant row exists (no ghost-tenant writes)
    from database.models import Tenant as _Tenant  # noqa: PLC0415
    if not db.query(_Tenant).filter(_Tenant.id == tenant_id).first():
        logger.error(
            "[EmbeddedSignup] exchange REJECTED — tenant_id=%s has no DB row", tenant_id
        )
        raise HTTPException(
            status_code=403,
            detail=f"المتجر رقم {tenant_id} غير موجود. يرجى تسجيل الدخول مرة أخرى.",
        )

    # 1 — Get user token: either passed directly from JS SDK or exchanged from code
    token_data: dict = {}
    if body.access_token:
        short_token = body.access_token
        logger.info("[EmbeddedSignup] using access_token from JS SDK tenant=%s", tenant_id)
    elif body.code:
        token_data = await _exchange_code_for_token(body.code, body.redirect_uri or "")
        short_token = token_data["access_token"]
    else:
        raise HTTPException(status_code=400, detail="يجب إرسال access_token أو code")

    long_data = await _exchange_for_long_lived_token(short_token)
    user_token = long_data.get("access_token") or short_token
    debug_info = await _debug_token(user_token)

    # 2 — Discover WABA ID from token scopes
    waba_id = await _get_waba_id_from_token(user_token)
    logger.info("[EmbeddedSignup] waba_id=%s tenant=%s", waba_id, tenant_id)

    # 3 — Enforce WABA uniqueness (fatal if claimed by another tenant) and
    #     store intermediate credentials via the canonical service.
    #     This also evicts stale disconnected rows that reference this WABA.
    from services.whatsapp_connection_service import (  # noqa: PLC0415
        begin_waba_session,
        subscribe_waba_webhook,
        WhatsAppConnectionConflict,
        WhatsAppConnectionError,
    )
    try:
        begin_waba_session(
            db,
            tenant_id       = tenant_id,
            waba_id         = waba_id,
            access_token    = user_token,
            connection_type = "embedded",
            actor           = "embedded_exchange",
        )
    except WhatsAppConnectionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WhatsAppConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Re-fetch conn after service write so we can update token metadata
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn:
        raise HTTPException(status_code=500, detail="Internal error: connection row missing after write.")

    conn.token_type = long_data.get("token_type") or token_data.get("token_type", "user")
    conn.last_error = None
    conn.extra_metadata = {}

    # Token expiry (Meta user tokens expire in ~60 days)
    expires_in = long_data.get("expires_in") or token_data.get("expires_in")
    expires_at = None
    if expires_in:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    else:
        expires_at = _token_expiry_from_debug(debug_info)
    _update_oauth_state(
        conn,
        status="healthy" if debug_info.get("is_valid", True) else "invalid",
        token_source="merchant_oauth",
        debug_info=debug_info,
        expires_at=expires_at,
    )
    db.commit()

    # 4 — Subscribe app to WABA (result surfaces clearly in response, not swallowed)
    webhook_ok, webhook_err = subscribe_waba_webhook(waba_id, user_token, tenant_id)
    if webhook_ok:
        conn.webhook_verified = True
        db.commit()
        logger.info("[EmbeddedSignup] WABA webhook subscribed — tenant=%s waba=%s", tenant_id, waba_id)
    else:
        logger.warning(
            "[EmbeddedSignup] WABA webhook subscription FAILED — tenant=%s waba=%s err=%r",
            tenant_id, waba_id, webhook_err,
        )

    # 5 — List phone numbers
    phones = await _get_phone_numbers(waba_id, user_token)

    # ── Auto-select when exactly one phone exists ─────────────────────────
    if len(phones) == 1:
        auto_phone = phones[0]
        auto_pid = auto_phone["id"]

        db.query(WhatsAppConnection).filter(
            WhatsAppConnection.phone_number_id == auto_pid,
            WhatsAppConnection.tenant_id != tenant_id,
        ).update({"phone_number_id": None, "status": "disconnected", "sending_enabled": False})

        conn.phone_number_id       = auto_pid
        conn.phone_number          = auto_phone.get("display_phone_number", "")
        conn.business_display_name = auto_phone.get("verified_name", "")
        conn.status                = "pending"
        db.commit()

        logger.info(
            "[EmbeddedSignup] exchange OK — auto-select tenant=%s waba=%s phone_id=%s",
            tenant_id, waba_id, auto_pid,
        )
        return await sync_embedded_connection_from_meta(conn, db, attempt_register=True)

    # ── Multiple phones or none → return list for manual selection ─────────
    conn.status           = "pending"
    conn.phone_number_id  = None
    conn.phone_number     = None
    conn.business_display_name = None
    conn.connected_at     = None
    conn.last_verified_at = None
    db.commit()

    logger.info(
        "[EmbeddedSignup] exchange OK tenant=%s waba=%s phones=%d webhook_subscribed=%s",
        tenant_id, waba_id, len(phones), webhook_ok,
    )

    return {
        "status":              "waba_connected",
        "waba_id":             waba_id,
        "phones":              _serialize_phones(phones),
        "webhook_subscribed":  webhook_ok,
        "webhook_error":       webhook_err,
        "message":             "تم ربط حساب واتساب للأعمال بنجاح. اختر رقم الهاتف.",
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
    if not conn:
        raise HTTPException(status_code=400, detail="أكمل خطوة ربط حساب واتساب أولاً.")

    phone_data, _ = await _get_phone_details_with_fallback(conn, db, body.phone_number_id)

    if "error" in phone_data:
        raise HTTPException(
            status_code=400,
            detail=_meta_embedded_error_message(
                phone_data["error"],
                f"تعذر جلب بيانات الرقم: {phone_data['error'].get('message','')}",
            ),
        )

    # ── Integrity guard: phone_number_id uniqueness — always fatal ───────────
    from core.tenant_integrity import (  # noqa: PLC0415
        assert_phone_id_not_claimed,
        evict_phone_id_from_other_tenants,
        TenantIntegrityError,
    )
    try:
        assert_phone_id_not_claimed(db, body.phone_number_id, tenant_id)
    except TenantIntegrityError as _tie:
        logger.error(
            "[EmbeddedSignup] select-phone BLOCKED — phone_number_id=%s already "
            "claimed by another tenant. tenant=%s conflict: %s",
            body.phone_number_id, tenant_id, _tie,
        )
        raise HTTPException(status_code=409, detail=str(_tie)) from _tie
    try:
        evict_phone_id_from_other_tenants(db, body.phone_number_id, tenant_id)
    except Exception as _evict_exc:  # noqa: BLE001
        logger.warning("[EmbeddedSignup] select-phone eviction warning (non-fatal): %s", _evict_exc)

    conn.phone_number_id       = body.phone_number_id
    conn.phone_number          = phone_data.get("display_phone_number", "")
    conn.business_display_name = phone_data.get("verified_name", "")
    conn.connection_type       = "embedded"
    conn.provider              = WHATSAPP_PROVIDER_META
    conn.status                = "pending"
    conn.sending_enabled       = False
    db.commit()

    initial_state = _build_phone_sync_state(phone_data)
    if initial_state.get("verification_status") == "VERIFIED":
        return await sync_embedded_connection_from_meta(conn, db, attempt_register=True)

    # Always request OTP on first selection to confirm ownership
    otp_ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation="embedded.request_code",
        prefer_platform=False,
    )
    otp_data = await graph_post_with_context(
        otp_ctx,
        tenant_id=tenant_id,
        operation="embedded.request_code",
        path=f"{body.phone_number_id}/request_code",
        json={"code_method": "SMS", "language": "ar"},
        timeout=15,
    )

    logger.info("[EmbeddedSignup] select-phone OTP request: %s", otp_data)

    if "error" in otp_data:
        err     = otp_data["error"]
        code    = err.get("code")
        subcode = err.get("error_subcode")
        # Rate limit: too many OTP requests for this number
        if code == 136024 or subcode in (2388091, 2388095):
            raise HTTPException(
                status_code=429,
                detail=(
                    "تم تجاوز الحد المسموح به لطلبات التحقق لهذا الرقم. "
                    "يرجى الانتظار بضع ساعات والمحاولة مرة واحدة فقط."
                ),
            )
        raise HTTPException(
            status_code=400,
            detail=_meta_embedded_error_message(
                err,
                f"فشل إرسال رمز التحقق: {err.get('message','')} (code={code})",
            ),
        )

    logger.info(
        "[EmbeddedSignup] select-phone OTP sent tenant=%s phone_id=%s number=%s",
        tenant_id, body.phone_number_id, conn.phone_number,
    )

    conn.last_attempt_at = datetime.now(timezone.utc)
    _apply_embedded_state(conn, phone_data, {
        **initial_state,
        "db_status": "otp_pending",
        "connected": False,
        "sending_enabled": False,
        "message": "تم إرسال رمز التحقق عبر SMS. أدخله لإكمال الربط مع Meta.",
    })
    db.commit()

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
    logger.info("[EmbeddedSignup] status START tenant=%s origin=%s", tenant_id, request.headers.get("origin", ""))
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn or conn.connection_type != "embedded":
        return {"connected": False, "status": "not_connected", "phones": []}

    phones: List[dict] = []
    if conn.whatsapp_business_account_id and not conn.phone_number_id:
        try:
            list_ctx = await get_token_for_operation(
                db,
                conn,
                tenant_id=tenant_id,
                operation="embedded.list_phones",
                prefer_platform=False,
            )
            phones = _serialize_phones(
                await _get_phone_numbers(conn.whatsapp_business_account_id, list_ctx.token),
            )
        except Exception as exc:
            logger.warning("[EmbeddedSignup] status phone list fetch failed: %s", exc)

    if conn.phone_number_id:
        try:
            return await sync_embedded_connection_from_meta(conn, db, attempt_register=True)
        except Exception as exc:
            logger.warning("[EmbeddedSignup] status sync failed tenant=%s: %s", tenant_id, exc)
            conn.last_error = str(exc)[:500]
            db.commit()

    return _build_embedded_status_payload(conn, phones)


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
    logger.info(
        "[EmbeddedSignup] add-phone START tenant=%s origin=%s cc=%s phone=%s",
        tenant_id,
        request.headers.get("origin", ""),
        body.country_code,
        body.phone_number,
    )
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn or not conn.whatsapp_business_account_id:
        raise HTTPException(status_code=400, detail="لا يوجد WABA مرتبط. أكمل خطوة الربط أولاً.")

    waba_id = conn.whatsapp_business_account_id

    reg_data: dict = {}
    for ctx in _candidate_graph_tokens(conn, prefer_platform=False):
        persist_token_context(db, conn, tenant_id=tenant_id, operation="embedded.add_phone", ctx=ctx)
        reg_data = await graph_post_with_context(
            ctx,
            tenant_id=tenant_id,
            operation="embedded.add_phone",
            path=f"{waba_id}/phone_numbers",
            json={
                "cc":            body.country_code,
                "phone_number":  body.phone_number,
                "migrate_phone_number": False,
                "verified_name": body.verified_name,
            },
            timeout=20,
        )
        if "error" not in reg_data:
            break
        err = reg_data.get("error") or {}
        if ctx.source == "merchant_oauth" and int(err.get("code") or 0) == 190:
            _update_oauth_state(conn, status="expired",
                message="انتهت صلاحية جلسة Meta — جارٍ المحاولة بالتوكن البديل.",
                token_source=ctx.source)
            logger.warning("[EmbeddedSignup] add-phone 190 on merchant token — retrying with next candidate")
            continue
        break

    logger.info("[EmbeddedSignup] add-phone register: %s", reg_data)

    if "error" in reg_data:
        err = reg_data["error"]
        raise HTTPException(
            status_code=400,
            detail=_meta_embedded_error_message(
                err,
                f"فشل إضافة الرقم: {err.get('message', '')} (code={err.get('code')}, subcode={err.get('error_subcode')})",
            ),
        )

    phone_number_id = reg_data.get("id")
    if not phone_number_id:
        raise HTTPException(status_code=500, detail="لم يُعاد phone_number_id من Meta")

    # Step 2 — Request OTP (with same fallback pattern)
    otp_data: dict = {}
    for ctx in _candidate_graph_tokens(conn, prefer_platform=False):
        otp_data = await graph_post_with_context(
            ctx,
            tenant_id=tenant_id,
            operation="embedded.add_phone.otp",
            path=f"{phone_number_id}/request_code",
            json={"code_method": body.code_method, "language": "ar"},
            timeout=15,
        )
        if "error" not in otp_data:
            break
        otp_err = otp_data.get("error") or {}
        if ctx.source == "merchant_oauth" and int(otp_err.get("code") or 0) == 190:
            logger.warning("[EmbeddedSignup] add-phone OTP 190 — retrying with next candidate")
            continue
        break

    logger.info("[EmbeddedSignup] add-phone request_code: %s", otp_data)

    if "error" in otp_data:
        err = otp_data["error"]
        raise HTTPException(
            status_code=400,
            detail=_meta_embedded_error_message(
                err,
                f"فشل إرسال رمز التحقق: {err.get('message', '')} (code={err.get('code')}, subcode={err.get('error_subcode')})",
            ),
        )

    # Remove stale connections for this phone from other tenants
    db.query(WhatsAppConnection).filter(
        WhatsAppConnection.phone_number_id == phone_number_id,
        WhatsAppConnection.tenant_id != tenant_id,
    ).update({"phone_number_id": None, "status": "disconnected", "sending_enabled": False})

    conn.phone_number_id = phone_number_id
    conn.status          = "otp_pending"
    conn.connection_type = "embedded"
    conn.provider        = WHATSAPP_PROVIDER_META
    conn.last_attempt_at = datetime.now(timezone.utc)
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
    logger.info(
        "[EmbeddedSignup] verify-phone START tenant=%s origin=%s phone_id=%s",
        tenant_id,
        request.headers.get("origin", ""),
        body.phone_number_id,
    )
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn:
        raise HTTPException(status_code=400, detail="لا يوجد اتصال نشط")
    token_ctx = await get_token_for_operation(
        db,
        conn,
        tenant_id=tenant_id,
        operation="embedded.verify_phone",
        prefer_platform=False,
    )

    async with httpx.AsyncClient(timeout=15) as client:
        verify_resp = await client.post(
            f"{GRAPH}/{body.phone_number_id}/verify_code",
            headers={"Authorization": f"Bearer {token_ctx.token}"},
            json={"code": body.code},
        )
        verify_data = verify_resp.json()

    logger.info("[EmbeddedSignup] verify-phone: %s", verify_data)

    if "error" in verify_data:
        raise HTTPException(
            status_code=400,
            detail=f"رمز التحقق غير صحيح: {verify_data['error'].get('message', '')}",
        )

    conn.phone_number_id = body.phone_number_id
    conn.connection_type = "embedded"
    conn.provider = WHATSAPP_PROVIDER_META
    db.commit()

    synced = await sync_embedded_connection_from_meta(conn, db, attempt_register=True)
    return synced
