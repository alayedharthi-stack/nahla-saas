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

import base64 as _b64
import hashlib as _hashlib
import hmac as _hmac
import json
import logging
import os
import secrets as _secrets
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, NamedTuple, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.config import (
    JWT_SECRET,
    META_APP_ID,
    META_APP_SECRET,
    META_EMBEDDED_SIGNUP_CONFIG_ID,
    META_GRAPH_API_VERSION,
    META_REDIRECT_URI,
    META_WA_CONFIG_ID,
    is_meta_embedded_signup_enabled,
    meta_embedded_disabled_reason,
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
from services.meta_oauth_redirect import (
    canonical_meta_redirect_uri,
    graph_oauth_token_params,
    js_sdk_token_exchange_redirect_uri,
    token_exchange_log_fields,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/whatsapp/embedded", tags=["whatsapp-embedded"])

GRAPH = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
PHONE_FIELDS = (
    "id,display_phone_number,verified_name,code_verification_status,"
    "name_status,status,quality_rating"
)
_DEFAULT_PIN = "000000"


def _is_coexistence_conn(conn: Optional["WhatsAppConnection"]) -> bool:
    from services.meta_coexistence import is_coexistence_mode  # noqa: PLC0415
    return bool(conn is not None and is_coexistence_mode(conn))


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


class _OAuthState(NamedTuple):
    tenant_id: int
    redirect_uri: str


async def _exchange_code_for_token(code: str, redirect_uri: Optional[str] = None) -> dict:
    """Exchange a short-lived code for a user access token.

    Server-side OAuth must pass the exact redirect_uri bound into ``state``.
    FB.login JS SDK / Coexistence must pass ``None`` so Graph omits
    ``redirect_uri`` — Meta did not issue that code against a Nahlah URI.
    """
    params = graph_oauth_token_params(code=code, redirect_uri=redirect_uri)
    safe = token_exchange_log_fields(params)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{GRAPH}/oauth/access_token", params=params)
        data = resp.json()

    logger.info(
        "[EmbeddedSignup] token exchange: http=%s keys=%s has_token=%s "
        "redirect_uri_present=%s redirect_uri=%s",
        resp.status_code,
        sorted(data.keys()) if isinstance(data, dict) else type(data).__name__,
        bool(isinstance(data, dict) and data.get("access_token")),
        safe["redirect_uri_present"],
        safe["redirect_uri"],
    )
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
    logger.info(
        "[EmbeddedSignup] debug_token: is_valid=%s type=%s expires_at=%s",
        data.get("data", {}).get("is_valid") if isinstance(data, dict) else None,
        data.get("data", {}).get("type") if isinstance(data, dict) else None,
        data.get("data", {}).get("expires_at") if isinstance(data, dict) else None,
    )
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
        logger.warning(
            "[EmbeddedSignup] long-lived exchange failed: %s",
            (data.get("error") or {}).get("message") if isinstance(data, dict) else "unknown",
        )
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


async def _subscribe_app_to_phone(phone_number_id: str, token: str) -> None:
    """
    Subscribe Nahla app to a specific WhatsApp phone number to receive
    webhooks. Per Meta docs the subscription must happen on the
    PHONE_NUMBER_ID, not on the WABA_ID.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{GRAPH}/{phone_number_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {token}"},
            json={"subscribed_fields": ["messages", "messaging_postbacks", "message_echoes"]},
        )
        data = resp.json()
    logger.info("[EmbeddedSignup] subscribed_apps phone=%s result=%s", phone_number_id, data)


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


def _is_bsp_tp_entitlement_error(error: Dict[str, Any]) -> bool:
    """Return True iff Meta is rejecting our app because it's not
    onboarded as a BSP/Tech Provider with the WhatsApp Embedded
    Signup entitlement.

    The error surfaces as a generic ``"Embedded signup is only
    available for BSPs or TPs"`` string with no stable numeric code,
    so we sniff the message text. We're deliberately lenient: any
    one of the BSP/TP keywords + the phrase "embedded signup" or the
    explicit URL Meta links to in the popup matches.
    """
    message = str(error.get("message") or "").lower()
    if not message:
        return False
    has_bsp_or_tp = (
        " bsp" in message
        or " tp" in message
        or "bsps" in message
        or " tps" in message
        or "tech provider" in message
        or "solution partner" in message
    )
    talks_about_embedded = (
        "embedded signup" in message
        or "embedded sign up" in message
    )
    return has_bsp_or_tp and talks_about_embedded


_BSP_TP_FRIENDLY = (
    "لم يتم تفعيل صلاحية Embedded Signup المباشر بعد على تطبيق نحلة. "
    "استخدم الربط عبر 360dialog حالياً، وسنُعلمك فور اكتمال اعتماد Meta."
)


def _meta_embedded_error_message(error: Dict[str, Any], fallback: str) -> str:
    """Map raw Meta embedded-signup errors to merchant-friendly Arabic text."""
    code = int(error.get("code") or 0)
    subcode = int(error.get("error_subcode") or 0)
    message = str(error.get("message") or "")
    raw = f"{code}:{subcode}:{message}".lower()

    # The BSP/TP entitlement error is the most common cause of the
    # "ربط مع Meta" popup failing in apps that have whatsapp_business_
    # messaging approved but were never onboarded as a Tech Provider.
    # It's NOT actionable by the merchant — surface the 360dialog
    # fallback so they know what to do next.
    if _is_bsp_tp_entitlement_error(error):
        return _BSP_TP_FRIENDLY

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


async def _finalize_coexistence_exchange(
    conn: WhatsAppConnection,
    db: Session,
    *,
    tenant_id: int,
    waba_id: str,
    user_token: str,
    phones: List[dict],
    hinted_phone_id: str,
    finish_event: Optional[str],
) -> Dict[str, Any]:
    from core.tenant_integrity import (  # noqa: PLC0415
        TenantIntegrityError,
        assert_phone_id_not_claimed,
        evict_phone_id_from_other_tenants,
    )
    from services.meta_coexistence import (  # noqa: PLC0415
        apply_smb_sync_results,
        coexistence_webhook_fields,
        initiate_smb_app_data,
        merge_coexistence_metadata,
        smb_syncs_accepted,
        start_coexistence_deadline,
        verify_coexistence_phone,
    )
    from services.whatsapp_connection_service import subscribe_phone_webhook  # noqa: PLC0415

    conn.status = "authorizing"
    start_coexistence_deadline(conn)
    merge_coexistence_metadata(
        conn,
        finish_event=finish_event or None,
        client_phone_hint=hinted_phone_id or None,
    )
    db.commit()

    phone_ids = {str(p.get("id") or "") for p in phones if p.get("id")}
    chosen: Optional[dict] = None
    if hinted_phone_id:
        if hinted_phone_id not in phone_ids:
            conn.status = "failed"
            conn.sending_enabled = False
            conn.last_error = "رقم الهاتف الذي أرجعته Meta لا ينتمي لهذا الحساب."
            merge_coexistence_metadata(conn, failure_code="phone_hint_mismatch")
            db.commit()
            raise HTTPException(status_code=400, detail=conn.last_error)
        chosen = next(p for p in phones if str(p.get("id")) == hinted_phone_id)
    elif len(phones) == 1:
        chosen = phones[0]
    elif not phones:
        conn.status = "failed"
        conn.sending_enabled = False
        conn.last_error = "لم يرجع Meta رقم هاتف لحساب واتساب الأعمال."
        merge_coexistence_metadata(conn, failure_code="missing_phone")
        db.commit()
        raise HTTPException(status_code=400, detail=conn.last_error)
    else:
        conn.status = "configuring"
        conn.sending_enabled = False
        conn.last_error = None
        db.commit()
        payload = _build_embedded_status_payload(conn, _serialize_phones(phones))
        payload["status"] = "configuring"
        payload["connected"] = False
        payload["message"] = "تم ربط حساب واتساب للأعمال. اختر رقم واتساب الأعمال الموجود على الجوال."
        return payload

    phone_id = str(chosen["id"])
    try:
        assert_phone_id_not_claimed(db, phone_id, tenant_id)
    except TenantIntegrityError as exc:
        conn.status = "failed"
        conn.sending_enabled = False
        conn.last_error = str(exc)
        merge_coexistence_metadata(conn, failure_code="phone_claimed")
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        evict_phone_id_from_other_tenants(db, phone_id, tenant_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Coexistence] eviction warning tenant=%s: %s", tenant_id, exc)

    conn.phone_number_id = phone_id
    conn.phone_number = chosen.get("display_phone_number") or conn.phone_number
    conn.business_display_name = chosen.get("verified_name") or conn.business_display_name
    conn.connection_type = "embedded"
    conn.provider = WHATSAPP_PROVIDER_META
    conn.status = "configuring"
    conn.sending_enabled = False
    db.commit()

    eligible, phone_data, eligibility_err = verify_coexistence_phone(phone_id, user_token, tenant_id)
    if phone_data.get("display_phone_number"):
        conn.phone_number = phone_data.get("display_phone_number") or conn.phone_number
    if phone_data.get("verified_name"):
        conn.business_display_name = phone_data.get("verified_name") or conn.business_display_name
    if not eligible:
        conn.status = "failed"
        conn.last_error = eligibility_err
        merge_coexistence_metadata(
            conn,
            failure_code="not_eligible",
            is_on_biz_app=phone_data.get("is_on_biz_app"),
            platform_type=phone_data.get("platform_type"),
        )
        db.commit()
        payload = _build_embedded_status_payload(conn, _serialize_phones(phones))
        payload["connected"] = False
        payload["status"] = "failed"
        payload["message"] = eligibility_err
        return payload

    webhook_ok, webhook_err = subscribe_phone_webhook(
        phone_id,
        user_token,
        tenant_id,
        waba_id=waba_id,
        subscribed_fields=coexistence_webhook_fields(),
    )
    conn.webhook_verified = bool(webhook_ok)
    merge_coexistence_metadata(conn, webhook_subscription_error=webhook_err)
    if not webhook_ok:
        conn.status = "configuring"
        conn.last_error = "تم حفظ الحساب لكن الاشتراك في Webhooks لم يكتمل بعد."
        db.commit()
        payload = _build_embedded_status_payload(conn, _serialize_phones(phones))
        payload["status"] = "configuring"
        payload["connected"] = False
        payload["message"] = conn.last_error
        return payload

    sync_results = initiate_smb_app_data(phone_id, user_token, tenant_id)
    apply_smb_sync_results(conn, sync_results)
    meta = dict(conn.extra_metadata or {})
    if smb_syncs_accepted(meta):
        conn.status = "connected"
        conn.sending_enabled = True
        conn.connected_at = conn.connected_at or datetime.now(timezone.utc)
        conn.last_error = None
        try:
            from core.whatsapp_ai_live import stamp_whatsapp_ai_live_since_if_empty  # noqa: PLC0415
            stamp_whatsapp_ai_live_since_if_empty(conn)
        except Exception:  # noqa: silent-ok — AI-live stamp is best-effort after connect
            pass
        db.commit()
        payload = _build_embedded_status_payload(conn, _serialize_phones(phones))
        payload["message"] = (
            "تم ربط رقم واتساب الأعمال على الجوال مع نحلة. "
            "أبقِ التطبيق مفتوحاً لإكمال المزامنة."
        )
        return payload

    conn.status = "configuring"
    conn.sending_enabled = False
    conn.last_error = "تم الربط جزئياً. جارٍ تهيئة مزامنة تطبيق واتساب الأعمال."
    db.commit()
    payload = _build_embedded_status_payload(conn, _serialize_phones(phones))
    payload["status"] = "configuring"
    payload["connected"] = False
    payload["message"] = conn.last_error
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
        and not _is_coexistence_conn(conn)
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

    if _is_coexistence_conn(conn):
        from services.meta_coexistence import (  # noqa: PLC0415
            maybe_fail_sync_deadline,
            smb_syncs_accepted,
        )
        if maybe_fail_sync_deadline(conn):
            db.commit()
            return _build_embedded_status_payload(conn)
        if not smb_syncs_accepted(dict(conn.extra_metadata or {})):
            sync_state = {
                **sync_state,
                "connected": False,
                "sending_enabled": False,
                "db_status": "configuring",
                "message": (
                    conn.last_error
                    or "تم الربط جزئياً. جارٍ تهيئة مزامنة تطبيق واتساب الأعمال."
                ),
            }

    _apply_embedded_state(conn, phone_data, sync_state, register_data)

    # When the connection finalises (status=connected), attempt webhook subscription
    # if it hasn't been done yet, and persist the verified flag explicitly.
    if sync_state.get("connected") and not conn.webhook_verified:
        from services.whatsapp_connection_service import subscribe_phone_webhook  # noqa: PLC0415
        from services.whatsapp_platform.wa_connection_secrets import read_access_token  # noqa: PLC0415
        from services.meta_coexistence import coexistence_webhook_fields, is_coexistence_mode  # noqa: PLC0415
        fields = coexistence_webhook_fields() if is_coexistence_mode(conn) else None
        wh_ok, wh_err = subscribe_phone_webhook(
            conn.phone_number_id or "",
            read_access_token(conn),
            conn.tenant_id,
            waba_id=conn.whatsapp_business_account_id or None,
            subscribed_fields=fields,
        )
        if wh_ok:
            conn.webhook_verified = True
            logger.info(
                "[EmbeddedSignup] webhook subscribed on finalise — "
                "tenant=%s phone=%s waba=%s",
                conn.tenant_id, conn.phone_number_id, conn.whatsapp_business_account_id,
            )
        else:
            logger.warning(
                "[EmbeddedSignup] webhook subscription FAILED on finalise — "
                "tenant=%s phone=%s waba=%s err=%r",
                conn.tenant_id, conn.phone_number_id,
                conn.whatsapp_business_account_id, wh_err,
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
    # Ignored. JS SDK exchange omits Graph redirect_uri; server-side OAuth
    # reuses the URI bound into ``state``. Never accept a browser URL here.
    redirect_uri: Optional[str] = None
    connection_mode: Optional[str] = None
    finish_event: Optional[str] = None
    waba_id: Optional[str] = None
    phone_number_id: Optional[str] = None


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
    """Return public config needed by the frontend FB SDK.

    Even when the embedded-signup entitlement is missing we return
    200 with ``embedded_signup_enabled=False`` so the dashboard can
    render the "قريباً / قيد التفعيل" state cleanly instead of
    erroring out. A 503 is reserved for the case where ``META_APP_ID``
    itself isn't configured (no Meta integration at all).
    """
    if not META_APP_ID:
        # No Meta integration whatsoever. The dashboard should never
        # try to mount the FB SDK — surface that explicitly.
        return {
            "app_id": "",
            "config_id": "",
            "graph_version": META_GRAPH_API_VERSION,
            "embedded_signup_enabled": False,
            "disabled_reason": (
                "إعدادات تطبيق Meta غير مكتملة على الخادم. "
                "الربط المباشر مع Meta غير مفعّل بعد."
            ),
            "oauth_start_path": None,
            "redirect_uri_configured": bool(META_REDIRECT_URI and "://" in META_REDIRECT_URI),
        }
    enabled = is_meta_embedded_signup_enabled()
    return {
        "app_id": META_APP_ID,
        # We expose the config_id under BOTH names so older dashboard
        # builds (which only read ``config_id``) keep working while
        # newer builds can opt into the explicit name.
        "config_id": META_EMBEDDED_SIGNUP_CONFIG_ID,
        "embedded_signup_config_id": META_EMBEDDED_SIGNUP_CONFIG_ID,
        "graph_version": META_GRAPH_API_VERSION,
        "embedded_signup_enabled": enabled,
        "disabled_reason": "" if enabled else meta_embedded_disabled_reason(),
        # FE can use this to navigate to the server-side OAuth flow
        # (an alternative to the JS SDK popup, useful for headless /
        # embedded environments where window.open is blocked).
        "oauth_start_path": "/whatsapp/embedded/oauth/start" if enabled else None,
        "redirect_uri_configured": bool(META_REDIRECT_URI and "://" in META_REDIRECT_URI),
    }


# ── Server-side OAuth flow (May 2026) ─────────────────────────────────────────
#
# A parallel path to the FB JS SDK popup. The dashboard navigates the
# browser to ``GET /whatsapp/embedded/oauth/start``, we sign an
# ``state`` payload containing the tenant_id, build the proper
# Embedded-Signup OAuth URL with ``client_id`` + ``config_id``, and
# 302-redirect to Meta. Meta sends the merchant back to
# ``GET /whatsapp/embedded/oauth/callback?code=…&state=…``, where we
# verify the state HMAC, exchange the short-lived code for a user
# token, and run the same WABA discovery as the JS-SDK path.
#
# Why have BOTH paths? The FB SDK popup is the official Meta flow,
# but on some merchant browsers (in-app webviews, strict cookie
# policies, hostile ad blockers) the popup is blocked or never
# returns. The server-side flow falls back to a normal top-level
# navigation, which always works.

# OAuth state TTL — the merchant has 10 minutes to complete the
# Meta dialog before we reject the callback. Plenty for the human
# but short enough that a stolen state value isn't useful.
_OAUTH_STATE_TTL_SECONDS = 600


def _sign_oauth_state(
    tenant_id: int,
    nonce: str,
    issued_at: int,
    redirect_uri: str,
) -> str:
    """Return a compact, URL-safe HMAC-signed state token.

    Payload binds tenant + the exact canonical redirect_uri used to start
    the dialog so token exchange cannot reconstruct a different value.
    Format: ``b64(json).b64(hmac_sha256)``. Uses JWT_SECRET as the signing
    key because it's already required in production; rotating it invalidates
    open OAuth sessions (correct security behaviour).
    """
    body = json.dumps(
        {
            "v": 1,
            "t": int(tenant_id),
            "n": nonce,
            "iat": int(issued_at),
            "ru": redirect_uri,
        },
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    sig = _hmac.new(JWT_SECRET.encode("utf-8"), body, _hashlib.sha256).digest()
    return f"{_b64.urlsafe_b64encode(body).rstrip(b'=').decode()}." \
           f"{_b64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"


def _verify_oauth_state(state: str) -> _OAuthState:
    """Verify the signed state and return tenant_id + bound redirect_uri.

    Raises HTTPException(400) on malformed input, tampered signature,
    missing redirect binding, or expired token.
    """
    try:
        body_b64, sig_b64 = state.split(".", 1)
        body = _b64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))
        sig = _b64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        expected = _hmac.new(JWT_SECRET.encode("utf-8"), body, _hashlib.sha256).digest()
        if not _hmac.compare_digest(sig, expected):
            raise ValueError("state signature mismatch")
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("state payload is not an object")
        tenant_id = int(payload["t"])
        issued_at = int(payload["iat"])
        redirect_uri = str(payload.get("ru") or "").strip()
        if not redirect_uri:
            raise ValueError("state missing bound redirect_uri")
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("[EmbeddedSignup] oauth/callback rejected — bad state: %s", exc)
        raise HTTPException(
            status_code=400,
            detail="رابط ربط Meta غير صالح أو منتهي الصلاحية. أعد المحاولة من نحلة.",
        ) from exc

    now = int(datetime.now(timezone.utc).timestamp())
    if now - issued_at > _OAUTH_STATE_TTL_SECONDS:
        logger.info(
            "[EmbeddedSignup] oauth/callback rejected — state expired (age=%ds, max=%ds)",
            now - issued_at, _OAUTH_STATE_TTL_SECONDS,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "انتهت صلاحية رابط الربط مع Meta. "
                "أعد فتح صفحة ربط واتساب في نحلة وأعد المحاولة."
            ),
        )
    return _OAuthState(tenant_id=tenant_id, redirect_uri=redirect_uri)


def _build_meta_oauth_authorize_url(state: str, redirect_uri: str) -> str:
    """Build the canonical FB Login for Business / WhatsApp Embedded
    Signup authorize URL.

    NOTE the param choices — this is the difference between "works"
    and "Embedded signup is only available for BSPs or TPs":
      * ``client_id``  — modern OAuth2 name (Meta also accepts the
                          legacy ``app_id`` but Embedded Signup
                          config_id binding requires the canonical
                          name).
      * ``config_id``  — REQUIRED. This is what tells Meta to run
                          the WhatsApp Embedded Signup flow instead
                          of a generic FB Login dialog. Without it,
                          Meta uses your app's default OAuth scope
                          which doesn't include WABA management →
                          BSP/TP entitlement error.
      * ``response_type=code`` — server-side exchange path.
      * ``redirect_uri`` — exact canonical config value, also bound
                          into ``state`` for token exchange.
      * ``state``      — HMAC-signed tenant_id + redirect_uri.
      * ``scope``      — explicit list; Meta will still apply the
                          config_id's allowed scopes on top.
    """
    from urllib.parse import urlencode  # noqa: PLC0415

    params = {
        "client_id": META_APP_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "config_id": META_EMBEDDED_SIGNUP_CONFIG_ID,
        "scope": ",".join([
            "business_management",
            "whatsapp_business_management",
            "whatsapp_business_messaging",
        ]),
        "state": state,
    }
    base = f"https://www.facebook.com/{META_GRAPH_API_VERSION}/dialog/oauth"
    return f"{base}?{urlencode(params)}"


@router.get("/oauth/start")
async def oauth_start(request: Request):
    """Server-side entry point for the Embedded Signup flow.

    The dashboard navigates the merchant's browser to this URL; we
    verify the tenant session, sign a state token, and 302 to Meta.
    """
    if not is_meta_embedded_signup_enabled():
        # The dashboard should never link here when the FF is off,
        # but a defensive 503 keeps us honest if it does.
        raise HTTPException(
            status_code=503,
            detail=meta_embedded_disabled_reason()
                   or "الربط المباشر مع Meta غير مفعّل بعد.",
        )
    tenant_id = resolve_tenant_id(request)
    redirect_uri = canonical_meta_redirect_uri()
    if "://" not in redirect_uri:
        raise HTTPException(
            status_code=503,
            detail="عنوان رجوع OAuth لـ Meta غير مُعد على الخادم.",
        )
    nonce = _secrets.token_urlsafe(16)
    issued_at = int(datetime.now(timezone.utc).timestamp())
    state = _sign_oauth_state(tenant_id, nonce, issued_at, redirect_uri)
    url = _build_meta_oauth_authorize_url(state, redirect_uri)
    logger.info(
        "[EmbeddedSignup] oauth/start tenant=%s redirect_uri=%s",
        tenant_id, redirect_uri,
    )
    return RedirectResponse(url=url, status_code=302)


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_reason: Optional[str] = None,
    error_description: Optional[str] = None,
):
    """Receive Meta's redirect after the user approves the dialog.

    Steps:
      1. Verify the HMAC state and resolve the tenant_id.
      2. Surface BSP/TP entitlement errors gracefully (no raw Meta
         English copy reaches the merchant).
      3. Exchange ``code`` for a user token using the SAME redirect_uri
         bound into ``state`` at start (Meta requires byte-for-byte match).
      4. Discover the merchant's WABA + persist credentials via the
         shared service path used by the JS-SDK ``/exchange`` endpoint.
      5. Redirect the browser back to the dashboard with a result
         hash (``#meta=ok`` / ``#meta=error&reason=…``) so the SPA
         can render a final-step toast.
    """
    if not state:
        raise HTTPException(status_code=400, detail="رابط الربط مع Meta ناقص (state).")
    oauth_state = _verify_oauth_state(state)
    tenant_id = oauth_state.tenant_id
    redirect_uri = oauth_state.redirect_uri

    # Meta sometimes redirects back with error= when the merchant
    # cancels or our app lacks the right entitlement. Translate
    # before bouncing the merchant to the dashboard.
    if error:
        friendly = _meta_embedded_error_message(
            {"message": error_description or error_reason or error},
            "تم إلغاء الربط مع Meta أو رفضته. يمكنك المحاولة لاحقاً أو استخدام الربط عبر 360dialog.",
        )
        logger.warning(
            "[EmbeddedSignup] oauth/callback Meta returned error tenant=%s "
            "error=%s reason=%s desc=%s",
            tenant_id, error, error_reason, error_description,
        )
        return _oauth_callback_finish(ok=False, reason=friendly)

    if not code:
        return _oauth_callback_finish(
            ok=False,
            reason="لم يرجع Meta رمز التفويض. أعد محاولة الربط من نحلة.",
        )

    try:
        token_data = await _exchange_code_for_token(code, redirect_uri)
    except HTTPException as exc:
        # Catch the BSP/TP variant explicitly so the merchant lands
        # on the "use 360dialog" message instead of a raw Meta error.
        return _oauth_callback_finish(ok=False, reason=str(exc.detail))

    short_token = token_data["access_token"]
    long_data = await _exchange_for_long_lived_token(short_token)
    user_token = long_data.get("access_token") or short_token
    debug_info = await _debug_token(user_token)

    try:
        waba_id = await _get_waba_id_from_token(user_token)
    except HTTPException as exc:
        return _oauth_callback_finish(ok=False, reason=str(exc.detail))

    from database.models import Tenant as _Tenant  # noqa: PLC0415
    if not db.query(_Tenant).filter(_Tenant.id == tenant_id).first():
        logger.error(
            "[EmbeddedSignup] oauth/callback REJECTED — tenant_id=%s has no DB row",
            tenant_id,
        )
        return _oauth_callback_finish(ok=False, reason="المتجر غير موجود — أعد تسجيل الدخول.")

    from services.whatsapp_connection_service import (  # noqa: PLC0415
        begin_waba_session,
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
            actor           = "embedded_oauth_callback",
        )
    except WhatsAppConnectionConflict as exc:
        return _oauth_callback_finish(ok=False, reason=str(exc))
    except WhatsAppConnectionError as exc:
        return _oauth_callback_finish(ok=False, reason=str(exc))

    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if conn is not None:
        conn.token_type = long_data.get("token_type") or token_data.get("token_type", "user")
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

    logger.info(
        "[EmbeddedSignup] oauth/callback OK tenant=%s waba=%s", tenant_id, waba_id,
    )
    return _oauth_callback_finish(ok=True, reason=None)


def _oauth_callback_finish(*, ok: bool, reason: Optional[str]) -> RedirectResponse:
    """Send the merchant back to the dashboard with a result hash.

    We use a fragment (``#``) instead of a query string so the result
    code never lands in server access logs and isn't reflected back
    in any subsequent same-origin requests. The frontend reads
    ``window.location.hash`` on mount and renders an appropriate
    toast / banner.
    """
    from urllib.parse import quote  # noqa: PLC0415
    from core.config import BACKEND_URL  # noqa: PLC0415

    # Default to the same origin that hosts the backend; the dashboard
    # SPA is typically served by the same host in production. If you
    # run a separate dashboard origin, set ``DASHBOARD_URL`` env to
    # override.
    base = os.environ.get("DASHBOARD_URL") or BACKEND_URL or ""
    base = base.rstrip("/")
    path = "/dashboard/whatsapp/connect"
    if ok:
        return RedirectResponse(f"{base}{path}#meta=ok", status_code=302)
    reason_q = quote(reason or "تعذر إكمال الربط مع Meta.", safe="")
    return RedirectResponse(f"{base}{path}#meta=error&reason={reason_q}", status_code=302)


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
        if body.redirect_uri:
            logger.warning(
                "[EmbeddedSignup] ignoring client-supplied redirect_uri tenant=%s",
                tenant_id,
            )
        token_data = await _exchange_code_for_token(
            body.code,
            js_sdk_token_exchange_redirect_uri(),
        )
        short_token = token_data["access_token"]
    else:
        raise HTTPException(status_code=400, detail="يجب إرسال access_token أو code")

    long_data = await _exchange_for_long_lived_token(short_token)
    user_token = long_data.get("access_token") or short_token
    debug_info = await _debug_token(user_token)

    # 2 — Discover WABA ID from token scopes
    waba_id = await _get_waba_id_from_token(user_token)
    logger.info("[EmbeddedSignup] waba_id=%s tenant=%s", waba_id, tenant_id)

    coexistence = str(body.connection_mode or "").strip().lower() == "coexistence"
    if coexistence:
        from services.meta_coexistence import reject_coexistence_finish_event  # noqa: PLC0415
        reject_msg = reject_coexistence_finish_event(body.finish_event)
        if reject_msg:
            raise HTTPException(status_code=400, detail=reject_msg)
        hinted_waba = str(body.waba_id or "").strip()
        if hinted_waba and hinted_waba != str(waba_id):
            raise HTTPException(
                status_code=400,
                detail="تعذر مطابقة حساب واتساب للأعمال مع نتيجة Meta. أعد المحاولة.",
            )

    # 3 — Enforce WABA uniqueness (fatal if claimed by another tenant) and
    #     store intermediate credentials via the canonical service.
    #     This also evicts stale disconnected rows that reference this WABA.
    from services.whatsapp_connection_service import (  # noqa: PLC0415
        begin_waba_session,
        subscribe_phone_webhook,
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
    if not coexistence:
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

    # 4 — List phone numbers FIRST (Meta requires phone-level subscription, not WABA-level).
    # We defer subscription until a phone is selected; if exactly one phone exists we
    # auto-select it below and subscription happens during finalize. Multi-phone setups
    # subscribe inside /select-phone via the same finalize path.
    phones = await _get_phone_numbers(waba_id, user_token)

    if coexistence:
        return await _finalize_coexistence_exchange(
            conn,
            db,
            tenant_id=tenant_id,
            waba_id=waba_id,
            user_token=user_token,
            phones=phones,
            hinted_phone_id=str(body.phone_number_id or "").strip(),
            finish_event=body.finish_event,
        )

    # If we land on the "auto-select" path the per-phone subscription happens inside
    # sync_embedded_connection_from_meta(); otherwise it happens after the merchant
    # picks a phone in /select-phone. The boolean below is reported to the UI so it
    # knows the WABA half is healthy even before the phone half completes.
    webhook_ok, webhook_err = (False, "deferred until phone is selected")

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

    if _is_coexistence_conn(conn):
        from services.whatsapp_platform.token_manager import get_token_for_operation  # noqa: PLC0415
        from services.whatsapp_platform.wa_connection_secrets import read_access_token  # noqa: PLC0415
        phones = []
        if conn.whatsapp_business_account_id:
            try:
                list_ctx = await get_token_for_operation(
                    db, conn, tenant_id=tenant_id, operation="embedded.list_phones", prefer_platform=False,
                )
                phones = await _get_phone_numbers(conn.whatsapp_business_account_id, list_ctx.token)
            except Exception as exc:
                logger.warning("[Coexistence] select-phone list failed: %s", exc)
        token = read_access_token(conn)
        if not phones:
            raise HTTPException(
                status_code=400,
                detail="تعذر جلب أرقام واتساب الأعمال من Meta. أعد المحاولة.",
            )
        return await _finalize_coexistence_exchange(
            conn,
            db,
            tenant_id=tenant_id,
            waba_id=conn.whatsapp_business_account_id or "",
            user_token=token,
            phones=phones,
            hinted_phone_id=body.phone_number_id,
            finish_event=(conn.extra_metadata or {}).get("finish_event"),
        )

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
