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

import asyncio
import copy
import logging
import secrets
import time
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from models import WhatsAppConnection  # noqa: E402

from core.audit import audit
from core.auth import get_jwt_user_id, is_platform_admin_role, require_admin, require_merchant_scope
from services.meta_graph_oauth_client import exchange_code_for_token as _secure_exchange_code_for_token, exchange_for_long_lived_token as _secure_exchange_for_long_lived_token
from core.config import (
    BACKEND_URL,
    D360_COHOST_ALLOW_SELF_REQUEST,
    D360_COHOST_ENABLED,
    D360_PARTNER_HUB_BASE,
    D360_PARTNER_ID,
    D360_WEBHOOK_INTERNAL_SECRET,
    META_APP_ID,
    META_APP_SECRET,
    META_GRAPH_API_VERSION,
    META_WA_CONFIG_ID,
    is_whatsapp_merchant_self_service_manual_enabled,
)
from core.coexistence_client_id import (
    client_id_is_present_for_integration,
    sanitize_coexistence_client_id,
)
from core.database import get_db
from core.log_redaction import redact_graph_id, redact_sensitive_log_text
from services.whatsapp_connection_service import connection_conflict_http_detail
from services.d360_logging import (
    d360_sanitize_live_verify_probe,
    d360_extract_remote_url,
    d360_response_summary,
    d360_safe_error_payload,
    d360_safe_exception_fields,
    d360_safe_persist_webhook_setup,
    d360_project_connection_metadata,
    d360_url_flags,
    log_d360_verify,
)
from services.wa_direct_logging import (
    log_wa_direct_exception,
    log_wa_direct_graph_result,
    log_wa_direct_stage,
)
from core.tenant import get_or_create_settings, get_or_create_tenant, resolve_tenant_id
from core.whatsapp_connection_finalization import (
    WhatsAppConnectionFinalizationError,
    finalize_successful_whatsapp_connection,
)
from services.whatsapp_platform.provider_utils import (
    WHATSAPP_CONNECTION_TYPE_ASSISTED,
    WHATSAPP_CONNECTION_TYPE_COEXISTENCE,
    WHATSAPP_CONNECTION_TYPE_DIRECT,
    WHATSAPP_CONNECTION_TYPE_EMBEDDED,
    WHATSAPP_PROVIDER_360DIALOG,
    WHATSAPP_PROVIDER_META,
    merchant_channel_label as _merchant_channel_label,
    provider_label as _provider_label,
    wa_provider as _wa_provider,
)
from services.whatsapp_platform.wa_connection_secrets import (  # noqa: E402
    access_token_present,
    read_access_token,
    store_access_token,
)
from services.whatsapp_platform.token_manager import (
    get_oauth_session_state,
    get_token_context,
    get_token_for_operation,
    update_token_state,
)
from services.whatsapp_platform.service import (
    dialog360_configure_webhook,
    dialog360_generate_api_key,
    dialog360_get_channel_info,
    dialog360_get_waba_webhook,
    dialog360_live_verify_probes,
    dialog360_set_waba_webhook,
    dialog360_resolve_channel_metadata,
)

logger = logging.getLogger("nahla-backend")


def _d360_operation_ok(result: Optional[Dict[str, Any]]) -> bool:
    """True when a safe D360 webhook operation result indicates success."""
    if not isinstance(result, dict):
        return False
    if "success" in result:
        return bool(result.get("success"))
    return "error" not in result


router = APIRouter(prefix="/whatsapp", tags=["WhatsApp Connection"])

def _sanitize_webhook_operation_result(result: Any) -> dict[str, Any]:
    if result is None:
        return {}
    if not isinstance(result, dict):
        return d360_response_summary({"error": "invalid_response"})
    payload = dict(result)
    payload.setdefault("status_code", payload.get("status_code"))
    return d360_response_summary(payload)


def _safe_meta_refresh_payload(data: Any, http_status: int | None) -> dict[str, Any]:
    err_code = None
    if isinstance(data, dict):
        err = data.get("error") or {}
        if isinstance(err, dict):
            err_code = err.get("code")
    return {
        "updated": False,
        "http_status": http_status,
        "error_code": err_code,
        "message": "تعذّر قراءة حالة الرقم من Meta.",
    }


GRAPH_BASE = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
_TENANT_FEATURES_KEY = "tenant_features"
_PLATFORM_FEATURES_KEY = "platform_features"
_COEX_FEATURE_KEY = "whatsapp_coexistence_beta"

_LIVE_VERIFY_CACHE_TTL_SEC = float(os.environ.get("NAHLA_LIVE_VERIFY_CACHE_SEC", "45"))
_LIVE_VERIFY_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}


def _live_verify_cache_get(tenant_id: int) -> Optional[dict[str, Any]]:
    hit = _LIVE_VERIFY_CACHE.get(tenant_id)
    if not hit:
        return None
    age = time.monotonic() - hit[0]
    if age >= _LIVE_VERIFY_CACHE_TTL_SEC:
        return None
    logger.debug("[WA live-verify] cache_hit tenant=%s age_sec=%.2f", tenant_id, age)
    return copy.deepcopy(hit[1])


def _live_verify_cache_put(tenant_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    _LIVE_VERIFY_CACHE[tenant_id] = (time.monotonic(), copy.deepcopy(payload))
    return payload


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
    provider:                     Optional[str]  = None
    provider_label:               Optional[str]  = None
    merchant_channel_label:       Optional[str]  = None


class CoexistenceRequestIn(BaseModel):
    phone_number: str
    display_name: Optional[str] = None
    has_whatsapp_business_app: bool = True
    understands_keep_app_installed: bool = True
    understands_open_every_13_days: bool = True
    notes: Optional[str] = None


class AssistedConnectRequestIn(BaseModel):
    """Merchant-facing assisted onboarding — contact hints only, never Meta secrets."""
    contact_phone: Optional[str] = None
    display_name: Optional[str] = None
    notes: Optional[str] = None


def _assert_merchant_self_service_secrets_allowed(request: Request) -> None:
    """Block merchant self-service manual connect unless explicitly enabled or admin."""
    if is_whatsapp_merchant_self_service_manual_enabled():
        return
    jwt_payload = getattr(request.state, "jwt_payload", None) or {}
    if is_platform_admin_role(jwt_payload.get("role")):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "الربط اليدوي الذاتي غير متاح حالياً. "
            "يرجى استخدام «طلب ربط واتساب بمساعدة فريق نحلة»."
        ),
    )


class CoexistenceOpsActivateIn(BaseModel):
    tenant_id: int
    phone_number_id: str
    phone_number: str
    display_name: Optional[str] = None
    waba_id: Optional[str] = None
    api_key: str
    channel_id: Optional[str] = None
    client_id: Optional[str] = None
    configure_webhook: bool = True
    action_required_message: Optional[str] = None


def _coexistence_state(conn: Optional[WhatsAppConnection]) -> Dict[str, Optional[str]]:
    meta = dict(getattr(conn, "extra_metadata", None) or {}) if conn else {}
    state = dict(meta.get("coexistence") or {})
    return state


def _coexistence_enabled_for_tenant(db: Session, tenant_id: int) -> bool:
    if D360_COHOST_ENABLED:
        return True
    settings = get_or_create_settings(db, tenant_id)
    meta = dict(settings.extra_metadata or {})
    tenant_flags = dict(meta.get(_TENANT_FEATURES_KEY) or {})
    platform_flags = dict(meta.get(_PLATFORM_FEATURES_KEY) or {})
    return bool(tenant_flags.get(_COEX_FEATURE_KEY) or platform_flags.get(_COEX_FEATURE_KEY))


def _ensure_coexistence_allowed(db: Session, tenant_id: int) -> None:
    if not _coexistence_enabled_for_tenant(db, tenant_id):
        raise HTTPException(status_code=403, detail="ميزة واتساب الجوال + الذكاء الاصطناعي غير مفعلة لهذا الحساب بعد.")


def _coexistence_webhook_url() -> str:
    """Channel webhook — receives normal customer messages + statuses."""
    return f"{BACKEND_URL.rstrip('/')}/webhook/whatsapp/360dialog"


def _coexistence_events_url() -> str:
    """Dedicated Coexistence webhook — receives device sync, pairing,
    phone-app handover and other Coexistence lifecycle events. Configuring
    this URL on 360dialog is recommended for the WA-Business-App + API
    side-by-side mode so message traffic and Coexistence events stay on
    separate streams."""
    return f"{BACKEND_URL.rstrip('/')}/webhook/whatsapp/360dialog/coexistence"


def _coexistence_status_url() -> str:
    """Channel/account health callbacks (account_alerts, quality updates …)."""
    return f"{BACKEND_URL.rstrip('/')}/webhook/whatsapp/360dialog/status"


def _dt_iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return None


# ── Diagnostics helpers (tenant 52-class issues) ─────────────────────────────
# Two helpers used by the verify / auto-configure / waba-read endpoints to
# emit a single canonical structured log line per call AND to mask the API
# key while still letting support confirm "the key in DB matches the key
# 360dialog has on file". Never log the full key. We use the last 4 chars
# only — the same convention as the admin debug `_mask_secret_tail` helper.

def _log_d360_verify(
    *,
    operation: str,
    tenant_id: int,
    conn: Optional[WhatsAppConnection],
    endpoint_used: str,
    response: Any,
    response_status: Optional[int] = None,
    parsed_url: Optional[str] = None,
    expected_url: Optional[str] = None,
    result: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    meta = dict(getattr(conn, "extra_metadata", None) or {}) if conn else {}
    pd = dict(meta.get("provider_details") or {})
    channel_id = pd.get("channel_id") or pd.get("channel")
    log_d360_verify(
        operation=operation,
        tenant_id=tenant_id,
        connection_id=getattr(conn, "id", None) if conn else None,
        channel_id=str(channel_id) if channel_id else None,
        phone_number_id=getattr(conn, "phone_number_id", None) if conn else None,
        waba_id=getattr(conn, "whatsapp_business_account_id", None) if conn else None,
        api_key_present=bool(getattr(conn, "access_token", None)) if conn else False,
        endpoint_used=endpoint_used,
        response=response,
        response_status=response_status,
        parsed_url=parsed_url,
        expected_url=expected_url,
        result=result,
        extra=extra,
    )


def _coexistence_webhook_block(conn: Optional[WhatsAppConnection]) -> Dict[str, Any]:
    """Return the dashboard-facing webhook block: per-URL state + timestamps.

    Receipt times prefer narrow DB columns (``last_webhook_received_at``,
    ``webhook_coexistence_received_at``, ``webhook_status_received_at``) so we
    do not depend on ``extra_metadata.coexistence.webhook`` mirrors that used to
    be rewritten on every webhook."""
    meta = dict(getattr(conn, "extra_metadata", None) or {}) if conn else {}
    coex = dict(meta.get("coexistence") or {})
    webhook = dict(coex.get("webhook") or {})

    channel_last = (
        _dt_iso_utc(getattr(conn, "last_webhook_received_at", None))
        if conn else None
    ) or webhook.get("channel_last_received_at")
    coexist_last = (
        _dt_iso_utc(getattr(conn, "webhook_coexistence_received_at", None))
        if conn else None
    ) or webhook.get("coexistence_last_received_at")
    status_last = (
        _dt_iso_utc(getattr(conn, "webhook_status_received_at", None))
        if conn else None
    ) or webhook.get("status_last_received_at")

    channel_status = webhook.get("channel_status") or (
        "verified" if (conn and conn.webhook_verified) else "unknown"
    )
    coexist_status = webhook.get("coexistence_status") or (
        "verified" if coexist_last else "unknown"
    )
    status_status = webhook.get("status_status") or (
        "verified" if status_last else "unknown"
    )

    return {
        "channel_url":                _coexistence_webhook_url(),
        "channel_status":             channel_status,
        "channel_last_received_at":   channel_last,
        "coexistence_url":            _coexistence_events_url(),
        "coexistence_status":         coexist_status,
        "coexistence_last_received_at": coexist_last,
        "status_url":                 _coexistence_status_url(),
        "status_status":              status_status,
        "status_last_received_at":    status_last,
        "internal_header_name":       "X-Nahla-Coexistence-Secret",
    }


def _set_coexistence_state(
    conn: WhatsAppConnection,
    *,
    status: str,
    request_payload: Optional[Dict[str, object]] = None,
    action_required_message: Optional[str] = None,
) -> None:
    meta = dict(conn.extra_metadata or {})
    coex = dict(meta.get("coexistence") or {})
    coex["status"] = status
    coex["last_updated_at"] = datetime.now(timezone.utc).isoformat()
    if request_payload is not None:
        coex["request"] = request_payload
    if action_required_message is not None:
        coex["action_required_message"] = action_required_message
    meta["coexistence"] = coex
    conn.extra_metadata = meta
    flag_modified(conn, "extra_metadata")


def _assisted_connect_state(conn: Optional[WhatsAppConnection]) -> Dict[str, Optional[str]]:
    meta = dict(getattr(conn, "extra_metadata", None) or {}) if conn else {}
    state = dict(meta.get("assisted_connect") or {})
    return state


def _set_assisted_connect_state(
    conn: WhatsAppConnection,
    *,
    status: str,
    request_payload: Optional[Dict[str, object]] = None,
    action_required_message: Optional[str] = None,
) -> None:
    meta = dict(conn.extra_metadata or {})
    assisted = dict(meta.get("assisted_connect") or {})
    assisted["status"] = status
    assisted["last_updated_at"] = datetime.now(timezone.utc).isoformat()
    if request_payload is not None:
        assisted["request"] = request_payload
    if action_required_message is not None:
        assisted["action_required_message"] = action_required_message
    meta["assisted_connect"] = assisted
    conn.extra_metadata = meta
    flag_modified(conn, "extra_metadata")


def _assisted_status_payload(conn: Optional[WhatsAppConnection]) -> dict:
    base = _build_wa_status(conn)
    state = _assisted_connect_state(conn)
    base.update({
        "assisted_connect_status": state.get("status"),
        "action_required_message": state.get("action_required_message"),
        "request_submitted_at": (
            (state.get("request") or {}).get("submitted_at")
            if isinstance(state.get("request"), dict) else None
        ),
    })
    return base


# ── Canonical state buckets ──────────────────────────────────────────────────
# A single source of truth used by BOTH:
#   • _coexistence_integration_complete (owner panel banner)
#   • live-verify status logic           (merchant page banner)
# so the two pages cannot disagree (which was the exact symptom seen on
# tenant=52: owner saw "status_invalid" while merchant page + AI worked).
#
#   HEALTHY  — green light, no banner needed.
#   IN_FLIGHT — onboarding / activation in progress; soft warning OK but
#               not a hard red banner if operational health is good.
#   HARD_FAIL — disconnected/error/not_connected — the merchant explicitly
#               cut the link or the system marked it broken. These NEVER
#               get auto-healed; the merchant must act.

_HEALTHY_DB_STATUSES = frozenset({"connected"})
_IN_FLIGHT_DB_STATUSES = frozenset({
    "pending",
    "pending_activation",
    "activation_pending",   # legacy synonym kept for back-compat
    "review_pending",
    "request_submitted",
    "action_required",
})
_HARD_FAIL_DB_STATUSES = frozenset({
    "disconnected",
    "error",
    "not_connected",
})


def _is_status_acceptable_for_use(db_status: Optional[str]) -> bool:
    """A connection can be USED (read inbound, send replies) as long as
    its DB status is not a hard-fail. In-flight statuses are acceptable
    when operational health is green — see ``_operational_health_ok``.

    This function answers ONE narrow question: "does this status, by
    itself, ban the merchant from using the integration?". A `None` /
    unknown status is treated as in-flight (acceptable) so a brand-new
    row that hasn't been promoted yet doesn't trigger the red banner
    while webhooks are arriving."""
    if not db_status:
        return True  # unknown → fall through to operational checks
    return db_status not in _HARD_FAIL_DB_STATUSES


def _operational_health_ok(conn: Optional[WhatsAppConnection]) -> bool:
    """All four operational signals are green:

      * api_key present (so we can talk to the provider for outbound),
      * phone_number_id present (so inbound can route to this tenant),
      * waba_id present (so business templates can send),
      * webhook traffic arrived recently (so the path is proven live).

    Notably we do NOT require ``conn.status == "connected"`` here — the
    whole point of this helper is to detect when a row's status field
    has drifted out of sync with reality.

    Used by ``_coexistence_integration_complete`` to soften the
    ``status_invalid`` verdict, and by ``_reconcile_coexistence_status``
    to decide whether it is safe to auto-promote the row."""
    if not conn:
        return False
    if not (conn.access_token or "").strip():
        return False
    if not (getattr(conn, "phone_number_id", None) or ""):
        return False
    if not (getattr(conn, "whatsapp_business_account_id", None) or ""):
        return False
    return _has_recent_webhook_traffic(conn)


def _finalize_connected_or_http(db: Session, conn: WhatsAppConnection) -> bool:
    try:
        return bool(finalize_successful_whatsapp_connection(db, conn))
    except WhatsAppConnectionFinalizationError as exc:
        raise HTTPException(status_code=502, detail="تعذر إتمام ربط واتساب.") from exc


def _reconcile_connected_or_http(
    conn: Optional[WhatsAppConnection],
    *,
    tenant_id: int,
    source: str,
    db: Session,
) -> bool:
    try:
        return _reconcile_coexistence_status(
            conn, tenant_id=tenant_id, source=source, db=db,
        )
    except WhatsAppConnectionFinalizationError as exc:
        raise HTTPException(status_code=502, detail="تعذر إتمام ربط واتساب.") from exc


def _sync_state_ready(sync_state: Dict[str, Any]) -> bool:
    return bool(sync_state.get("connected")) or str(sync_state.get("db_status") or "") == "connected"


def _non_success_db_status(sync_state: Dict[str, Any], default: str = "activation_pending") -> str:
    raw = str(sync_state.get("db_status") or default)
    if raw == "connected":
        return default
    return raw


def _reconcile_coexistence_status(
    conn: Optional[WhatsAppConnection],
    *,
    tenant_id: int,
    source: str,
    db: Session,
) -> bool:
    """If the row is operationally healthy but its DB status field is
    stale (e.g. ``action_required`` left over from an earlier failed
    bootstrap), promote it to ``connected`` + ``sending_enabled=True``
    so the owner panel and merchant page agree with reality.

    Returns True iff a change was made (so callers can decide whether to
    commit). The caller owns the transaction: pass ``db`` if you want
    this helper to commit on your behalf, otherwise commit yourself.

    Safety rails — never auto-heal when:
      * The merchant explicitly disconnected (status in HARD_FAIL set),
      * Operational health is not green (some signal is missing),
      * The row is None (defensive).

    Logs every decision so a state mismatch is recoverable from logs.
    """
    if conn is None:
        return False
    from services.meta_coexistence import is_coexistence_mode  # noqa: PLC0415
    if is_coexistence_mode(conn):
        return False
    db_status = (conn.status or "").lower()
    if db_status in _HARD_FAIL_DB_STATUSES:
        logger.info(
            "[coexistence_reconcile] SKIP tenant=%s source=%s reason=hard_fail_status status=%s",
            tenant_id, source, db_status,
        )
        return False
    if not _operational_health_ok(conn):
        logger.debug(
            "[coexistence_reconcile] SKIP tenant=%s source=%s reason=ops_not_green status=%s "
            "token_present=%s phone_id=%s waba_id=%s webhook_recent=%s",
            tenant_id, source, db_status,
            bool((conn.access_token or "").strip()),
            bool(getattr(conn, "phone_number_id", None)),
            bool(getattr(conn, "whatsapp_business_account_id", None)),
            _has_recent_webhook_traffic(conn),
        )
        return False
    if db_status == "connected" and conn.sending_enabled:
        return False  # already correct, no work to do

    prev_status = conn.status
    prev_sending = bool(conn.sending_enabled)
    conn.sending_enabled = True
    conn.last_error = None
    _set_coexistence_state(conn, status="connected")
    finalize_successful_whatsapp_connection(db, conn)
    logger.info(
        "[coexistence_reconcile] tenant=%s source=%s PROMOTED prev_status=%r prev_sending=%s "
        "→ status=connected sending_enabled=True (ops health green)",
        tenant_id, source, prev_status, prev_sending,
    )
    return True


def _has_recent_webhook_traffic(
    conn: Optional[WhatsAppConnection],
    *,
    window_days: int = 14,
) -> bool:
    """True iff this connection received a 360dialog/Meta webhook recently.

    Used as a "soft-connected" signal: when phone_number_id + access_token
    are present and webhooks are arriving, the integration IS working in
    practice — even if WABA ID hasn't been resolved into the DB yet. Use
    this to soften the merchant banner from "غير متصل فعليًا" (hard fail)
    to "التحقق المتقدم غير مكتمل" (warning).
    """
    if not conn:
        return False
    last = getattr(conn, "last_webhook_received_at", None)
    if not last:
        return False
    try:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).days < window_days
    except Exception:
        return False


def _coexistence_integration_complete(conn: Optional[WhatsAppConnection]) -> Dict[str, Any]:
    """Single source of truth for "is the Coexistence integration record complete
    enough that the merchant page should show it as truly connected?".

    Returns the same `truly_connected / reason_code` shape as the merchant
    `/connection/live-verify` endpoint, but computed from the DB record only —
    cheap and identical for both the owner panel and the merchant page so
    they cannot disagree.

    Webhook-traffic softening
    ─────────────────────────
    When the only thing missing is ``waba_id`` (a deep-validation field used
    for sending business templates) but ``phone_number_id`` + ``access_token``
    are set AND webhooks are arriving from the provider, we report
    ``reason_code='pending_advanced_verification'`` instead of the legacy
    ``missing_waba_id``. The integration IS routing inbound traffic — the
    merchant should NOT be told "غير متصل فعليًا" while messages keep
    arriving. The owner panel still shows the missing field so the
    integrity check is honest, but the merchant page banner softens.
    """
    if not conn:
        return {"truly_connected": False, "reason_code": "no_record",
                "missing_fields": ["whatsapp_connection"],
                "webhook_active": False}

    missing: list[str] = []
    if not conn.whatsapp_business_account_id:
        missing.append("waba_id")
    if not conn.phone_number_id:
        missing.append("phone_number_id")
    if not conn.access_token:
        missing.append("api_key")
    if not conn.phone_number:
        missing.append("phone_number")

    webhook_active = _has_recent_webhook_traffic(conn)
    db_status = (conn.status or "").lower()

    # ── Operational-health override ───────────────────────────────────
    # If every operational signal is green (token + phone_id + waba_id
    # + recent inbound), the integration is FUNCTIONING regardless of
    # whatever the DB status column happens to be. This is the canonical
    # fix for the "tenant 52 syndrome": owner panel shows red while
    # the merchant page works, AI replies, and webhooks flow — the only
    # thing wrong is a stale ``conn.status="action_required"`` left over
    # from an earlier failed verification.
    #
    # Hard-fail statuses (disconnected/error/not_connected) are NEVER
    # overridden here — the merchant explicitly broke the link, so the
    # banner SHOULD stay red until they re-onboard.
    if (
        not missing
        and webhook_active
        and db_status not in _HARD_FAIL_DB_STATUSES
        and _operational_health_ok(conn)
    ):
        if db_status != "connected" or not conn.sending_enabled:
            # Surface the drift in the response so the owner panel can
            # show a "إصلاح تلقائي متاح" call-to-action that runs the
            # reconciliation endpoint. Until the operator clicks it
            # (or any verify/auto-configure/sync path runs) the row
            # keeps its stale status, but the banner softens to green.
            return {
                "truly_connected": True,
                "reason_code": "operational_healthy_status_stale",
                "missing_fields": [],
                "db_status": conn.status,
                "webhook_active": True,
                "soft_warning": False,
                "needs_status_reconcile": True,
            }
        # status is already connected + ops green → fast path
        return {
            "truly_connected": True,
            "reason_code": None,
            "missing_fields": [],
            "db_status": conn.status,
            "webhook_active": True,
        }

    # Hard fail / not-yet-bootstrapped: keep the original behaviour but
    # use the unified status set so `pending`, `request_submitted` and
    # `action_required` are recognised as in-flight (not invalid).
    acceptable_for_use = (
        db_status in _HEALTHY_DB_STATUSES
        or db_status in _IN_FLIGHT_DB_STATUSES
        or db_status == ""  # brand-new row, no status yet
    )
    if not acceptable_for_use:
        return {
            "truly_connected": False,
            "reason_code": "status_invalid",
            "missing_fields": missing,
            "db_status": conn.status,
            "webhook_active": webhook_active,
        }

    if missing:
        # If the ONLY missing field is waba_id and webhooks are flowing,
        # treat as "pending advanced verification" (soft warning) — sending
        # inbound replies still works.
        only_waba_missing = missing == ["waba_id"]
        if only_waba_missing and webhook_active and conn.phone_number_id and conn.access_token:
            return {
                "truly_connected": True,
                "reason_code": "pending_advanced_verification",
                "missing_fields": missing,
                "db_status": conn.status,
                "webhook_active": True,
                "soft_warning": True,
            }

        # Highest-priority missing field gets the canonical reason code so the
        # merchant page banner reads the same way it always has.
        priority = ["api_key", "phone_number_id", "waba_id", "phone_number"]
        first = next((f for f in priority if f in missing), missing[0])
        code_by_field = {
            "api_key":         "missing_token",
            "phone_number_id": "missing_phone_id",
            "waba_id":         "missing_waba_id",
            "phone_number":    "missing_phone_number",
        }
        return {
            "truly_connected": False,
            "reason_code": code_by_field.get(first, "incomplete"),
            "missing_fields": missing,
            "db_status": conn.status,
            "webhook_active": webhook_active,
        }

    return {
        "truly_connected": conn.status == "connected" and bool(conn.sending_enabled),
        "reason_code": None if (conn.status == "connected" and conn.sending_enabled) else "not_active",
        "missing_fields": [],
        "db_status": conn.status,
        "webhook_active": webhook_active,
    }


def _log_integration_state(
    conn: Optional[WhatsAppConnection],
    *,
    tenant_id: int,
    source: str,
    request_id: Optional[str] = None,
) -> None:
    """Emit a single structured `[coexistence_state]` log line.

    `source` identifies which code path produced this snapshot
    (admin_activate, admin_sync, admin_edit, status_endpoint, ...). The
    log shape is intentionally machine-readable — Railway / Datadog can
    grep it without parsing the message body."""
    if conn is None:
        logger.info(
            "[coexistence_state] tenant_id=%s source=%s request_id=%s "
            "integration_id=missing record=absent",
            tenant_id, source, request_id or "-",
        )
        return
    completeness = _coexistence_integration_complete(conn)
    logger.info(
        "[coexistence_state] tenant_id=%s source=%s request_id=%s "
        "integration_id=%s provider=%s connection_type=%s status=%s "
        "waba_id=%s phone_number_id=%s phone_number=%s api_key=%s "
        "channel_id=%s client_id=%s sending_enabled=%s webhook_verified=%s "
        "truly_connected=%s reason=%s missing=%s",
        tenant_id, source, request_id or "-",
        conn.id,
        conn.provider, conn.connection_type, conn.status,
        "present" if conn.whatsapp_business_account_id else "missing",
        "present" if conn.phone_number_id else "missing",
        "present" if conn.phone_number else "missing",
        "present" if conn.access_token else "missing",
        "present" if (conn.extra_metadata or {}).get("provider_details", {}).get("channel_id") else "missing",
        "present" if client_id_is_present_for_integration(
            (conn.extra_metadata or {}).get("provider_details", {}).get("client_id"),
        ) else "missing",
        bool(conn.sending_enabled), bool(conn.webhook_verified),
        completeness["truly_connected"], completeness.get("reason_code"),
        ",".join(completeness.get("missing_fields") or []) or "-",
    )


def _coexistence_status_payload(conn: Optional[WhatsAppConnection]) -> dict:
    base = _build_wa_status(conn)
    if conn and isinstance(conn.extra_metadata, dict):
        base["provider_metadata"] = d360_project_connection_metadata(conn.extra_metadata)
    state = _coexistence_state(conn)
    base.update({
        "coexistence_status": state.get("status"),
        "action_required_message": state.get("action_required_message"),
        "request_submitted_at": (
            (state.get("request") or {}).get("submitted_at")
            if isinstance(state.get("request"), dict) else None
        ),
        # ── New: per-URL webhook block + Coexistence runtime state ────────
        # Surfaces all three webhooks (channel / coexistence / status) plus
        # the live state the webhook handler updates on each receipt.
        "webhooks":                    _coexistence_webhook_block(conn),
        "coexistence_sync_state":      state.get("sync_state"),
        "pairing_state":               state.get("pairing_state"),
        "mobile_app_connection_state": state.get("mobile_app_connection_state"),
        "phone_app_handover_at":       state.get("phone_app_handover_at"),
        "last_coexistence_event":      state.get("last_event"),
        "last_coexistence_events_by_category": state.get("last_event_by_category") or {},
        "last_status_event":           (state.get("status") or {}).get("last_event")
                                       if isinstance(state.get("status"), dict) else None,
        # ── Authoritative completeness summary ────────────────────────────
        # The merchant page banner ("WABA ID مفقود — يرجى إعادة الربط") and
        # the owner panel must derive the same verdict from the same fields.
        # We expose it here so /whatsapp/status callers don't have to
        # re-implement the rule.
        "integration_complete":        _coexistence_integration_complete(conn),
    })
    return base


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_or_create_connection(db: Session, tenant_id: int) -> WhatsAppConnection:
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn:
        conn = WhatsAppConnection(
            tenant_id=tenant_id,
            status="not_connected",
            provider=WHATSAPP_PROVIDER_META,
        )
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
        "provider":                     _wa_provider(conn),
        "provider_label":               _provider_label(conn),
        "merchant_channel_label":       _merchant_channel_label(conn),
        "connection_type":              conn.connection_type,
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
    data = await _secure_exchange_code_for_token({
        "client_id": META_APP_ID,
        "client_secret": META_APP_SECRET,
        "code": code,
        "redirect_uri": "",
    })
    if "error" in data:
        raise HTTPException(
            status_code=400,
            detail="Meta token exchange failed",
        )
    return data


async def _fetch_waba_info(token: str, waba_id: str) -> dict:
    """Fetch WABA details from Graph API."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/{waba_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "fields": "id,name,currency,message_template_namespace,on_behalf_of_business_info",
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
            headers={"Authorization": f"Bearer {token}"},
            params={
                "fields": "id,display_phone_number,verified_name,code_verification_status",
            },
        )
    if resp.status_code != 200:
        return {}
    return resp.json()


async def _exchange_for_long_lived_token(short_token: str) -> dict:
    """Exchange a short-lived user token for a 60-day long-lived token."""
    data = await _secure_exchange_for_long_lived_token(short_token)
    return {
        "access_token": data.get("access_token", short_token),
        "token_type": data.get("token_type", "long_lived"),
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


# ── Real-world connection health check (no message sent) ──────────────────────
#
# /whatsapp/connection plus /integrations/whatsapp/status only report what the
# DB knows. A merchant can have status="connected" while:
#   • waba_id is missing (e.g. coexistence handoff not finished)
#   • the provider token was revoked / disabled
#   • 360dialog channel is suspended
#
# This endpoint actively probes the provider with the stored credentials and
# returns a structured verdict the frontend can use to render an honest banner
# instead of a green "✅ مرتبط" lie.

@router.get("/connection/live-verify")
async def live_verify_connection(request: Request, db: Session = Depends(get_db)):
    """
    Perform a live, non-intrusive health check against the WhatsApp provider
    using the stored credentials. Does NOT send any user-visible message.

    Returns
    -------
    {
      truly_connected: bool,
      reason_code:     str | null,        # eg. missing_waba_id, token_revoked
      reason_message:  str,
      checks: [
        {name: 'has_record',      ok: bool},
        {name: 'status_ok',       ok: bool, value: <db status>},
        {name: 'has_waba_id',     ok: bool},
        {name: 'has_phone_id',    ok: bool},
        {name: 'has_token',       ok: bool},
        {name: 'provider_reachable', ok: bool, status_code: int|null,
         provider: '360dialog'|'meta', detail: str|null},
      ],
      provider:        '360dialog'|'meta',
      db_status:       str,
      verified_at:     iso datetime,
    }
    """
    tenant_id = resolve_tenant_id(request)
    _cached = _live_verify_cache_get(tenant_id)
    if _cached is not None:
        return _cached

    conn = db.query(WhatsAppConnection).filter(
        WhatsAppConnection.tenant_id == tenant_id,
    ).first()
    now_iso = datetime.now(timezone.utc).isoformat()

    checks: list[dict] = []

    def _add(name: str, ok: bool, **extra) -> None:
        checks.append({"name": name, "ok": bool(ok), **extra})

    # ── Local checks ─────────────────────────────────────────────────────────
    _add("has_record", conn is not None)
    if not conn:
        return _live_verify_cache_put(
            tenant_id,
            {
                "truly_connected": False,
                "reason_code":     "no_record",
                "reason_message":  "لا يوجد سجل ربط واتساب لهذا المتجر.",
                "checks":          checks,
                "provider":        None,
                "db_status":       None,
                "verified_at":     now_iso,
            },
        )

    provider = _wa_provider(conn)
    db_status = conn.status or "unknown"
    status_ok = db_status in ("connected", "pending", "review_pending", "activation_pending")
    _add("status_ok", status_ok, value=db_status)

    waba_id = conn.whatsapp_business_account_id or ""
    phone_id = conn.phone_number_id or ""
    has_token = bool(conn.access_token)
    webhook_active = _has_recent_webhook_traffic(conn)

    _add("has_waba_id",   bool(waba_id))
    _add("has_phone_id",  bool(phone_id))
    _add("has_token",     has_token)
    _add(
        "webhook_active", webhook_active,
        last_webhook_received_at=(
            conn.last_webhook_received_at.isoformat()
            if getattr(conn, "last_webhook_received_at", None) else None
        ),
    )

    # Decide blocking reason (priority order). When webhooks are flowing
    # we DOWNGRADE missing_waba_id from a hard fail to a soft warning —
    # inbound routing works, the merchant should not see "غير متصل فعليًا"
    # while messages keep arriving.
    reason_code: Optional[str] = None
    reason_msg = ""
    soft_warning = False
    if not status_ok:
        reason_code = "status_invalid"
        reason_msg  = f"حالة الربط في النظام «{db_status}» — لا تسمح بالاستخدام."
    elif not has_token:
        reason_code = "missing_token"
        reason_msg  = "مفتاح الوصول لمزود واتساب مفقود."
    elif not phone_id:
        reason_code = "missing_phone_id"
        reason_msg  = "phone_number_id مفقود — أعد ربط واتساب."
    elif not waba_id and webhook_active:
        # Soft path: webhooks are arriving, routing works → don't block.
        reason_code  = "pending_advanced_verification"
        reason_msg   = (
            "الربط نشط — الرسائل تصل إلى نحلة بنجاح، "
            "لكن التحقق المتقدم (WABA ID) لم يكتمل بعد. "
            "إرسال القوالب الخارجية قد يكون محدودًا حتى يكتمل التحقق."
        )
        soft_warning = True
    elif not waba_id:
        reason_code = "missing_waba_id"
        reason_msg  = "WABA ID مفقود — لا يمكن إرسال القوالب أو رسائل الأعمال. يرجى إعادة الربط."

    # ── Live provider probe (skip if local checks already failed hard) ──────
    # We still try it when we have token+phone_id but missing waba_id, since
    # the probe can confirm whether the channel itself is alive.
    provider_reachable = False
    provider_status_code: Optional[int] = None
    provider_detail: Optional[str] = None
    provider_probe: Optional[Dict[str, Any]] = None
    channel_auth_revoked = False

    if has_token and (phone_id or waba_id):
        try:
            ctx = await get_token_for_operation(
                db, conn,
                tenant_id=tenant_id,
                operation="connection_live_verify",
                prefer_platform=False,
            )
            if provider == WHATSAPP_PROVIDER_360DIALOG:
                meta_all = dict(conn.extra_metadata or {})
                pd = dict(meta_all.get("provider_details") or {})
                channel_id_resolved = pd.get("channel_id") or None
                ctype = str(conn.connection_type or "").strip().lower()

                provider_probe = await dialog360_live_verify_probes(
                    tenant_id=tenant_id,
                    api_key=ctx.token,
                    phone_number_id=phone_id,
                    waba_id=waba_id,
                    channel_id=channel_id_resolved,
                    connection_type=ctype,
                    partner_id=D360_PARTNER_ID or None,
                    timeout=10.0,
                )
                channel_auth_revoked = bool(provider_probe.get("channel_auth_revoked"))
                composite_alive = bool(provider_probe.get("composite_alive"))
                cfg_step = next(
                    (s for s in provider_probe.get("steps") or [] if s.get("step") == "v1_configs"),
                    None,
                )
                provider_status_code = cfg_step.get("status_code") if cfg_step else None
                provider_detail = str(provider_probe.get("summary") or "")
                provider_reachable = composite_alive

                # Coexistence / newer hub channels may legitimately 404 on
                # GET /v1/configs while webhooks + messaging still work.
                if not composite_alive and not channel_auth_revoked:
                    if webhook_active or bool(waba_id):
                        provider_reachable = True
                        provider_detail = (
                            f"{provider_detail} | fallback_ok=webhook_or_waba"
                        )

            else:
                # Meta Cloud API: GET /{phone_id}?fields=verified_name
                from services.whatsapp_platform.service import (  # noqa: PLC0415
                    _provider_url, _provider_headers,
                )
                headers = dict(_provider_headers(conn, ctx))
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        _provider_url(conn, phone_id),
                        headers=headers,
                        params={"fields": "verified_name,quality_rating"},
                    )
                provider_status_code = resp.status_code
                provider_reachable = 200 <= resp.status_code < 300
                if not provider_reachable:
                    try:
                        provider_detail = (resp.json().get("error") or {}).get("message") or resp.text[:200]
                    except Exception:
                        provider_detail = resp.text[:200]
        except Exception as exc:
            provider_detail = f"network_error: {type(exc).__name__}"
            provider_reachable = False
            if provider == WHATSAPP_PROVIDER_360DIALOG and (webhook_active or bool(waba_id)):
                provider_reachable = True
                provider_detail = f"{provider_detail} | probe_exception_fallback_ok"

    _add(
        "provider_reachable", provider_reachable,
        status_code=provider_status_code, provider=provider, detail=provider_detail,
    )

    meta_auth_failed = (
        provider == WHATSAPP_PROVIDER_META
        and provider_status_code is not None
        and provider_status_code in (401, 403)
    )

    if reason_code is None and (channel_auth_revoked or meta_auth_failed):
        reason_code = "token_revoked"
        reason_msg  = "مزوّد واتساب رفض المفتاح — قد يكون مُلغًى أو موقوفًا. يرجى إعادة الربط."
    elif reason_code is None and provider == WHATSAPP_PROVIDER_360DIALOG and has_token and (phone_id or waba_id):
        comp = bool(provider_probe and provider_probe.get("composite_alive"))
        if provider_probe and not comp and not channel_auth_revoked:
            if webhook_active or bool(waba_id):
                soft_warning = True
                reason_code = "provider_probe_inconclusive"
                reason_msg  = (
                    "الربط فعّال — استقبال الرسائل يعمل، لكن نقطة تحقق اختيارية في واجهة "
                    "360dialog لم تُجب (أمر شائع مع التعايش). لا حاجة لإعادة الربط إذا وصلتك رسائل واختبار الذكاء ناجح."
                )
            else:
                reason_code = "provider_unreachable"
                reason_msg  = (
                    f"تعذر التحقق من {provider} عبر أي مسار معروف، "
                    "ولا يوجد دليل حديث على تدفق الويب هوك. يُنصح بإعادة الربط أو مراجعة المفتاح."
                )
    elif reason_code is None and provider == WHATSAPP_PROVIDER_META and not provider_reachable:
        reason_code = "provider_unreachable"
        reason_msg  = (
            f"تعذر الوصول إلى {provider} للتحقق من حالة الربط فعليًا. "
            "حاول لاحقًا أو أعد الربط."
        )

    auth_failed_for_ui = channel_auth_revoked or meta_auth_failed

    # Soft path: when the only blocker is missing waba_id BUT webhooks are
    # arriving, we mark truly_connected=True so the merchant page does NOT
    # show "غير متصل فعليًا". The owner-side integrity panel still surfaces
    # the missing field for repair.
    truly_connected = (
        conn.status == "connected"
        and bool(phone_id) and has_token
        and not auth_failed_for_ui
        and (
            provider_reachable
            or (
                soft_warning
                and reason_code in (
                    "pending_advanced_verification",
                    "provider_probe_inconclusive",
                )
            )
        )
    )

    logger.info(
        "[WA live-verify] tenant=%s provider=%s db_status=%s "
        "waba=%s phone=%s token=%s webhook_active=%s probe_http=%s probe_summary=%s "
        "code=%s truly_connected=%s",
        tenant_id, provider, db_status, redact_graph_id(waba_id) if waba_id else "-", redact_graph_id(phone_id) if phone_id else "-",
        has_token, webhook_active, provider_status_code,
        (provider_probe or {}).get("summary") if provider_probe else "-",
        reason_code, truly_connected,
    )

    if truly_connected and soft_warning:
        default_msg = (reason_msg or "").strip() or "الربط فعّال — التحقق المتقدم غير مكتمل."
    elif truly_connected:
        default_msg = "الربط فعّال."
    else:
        default_msg = "غير متصل فعليًا."

    return _live_verify_cache_put(
        tenant_id,
        {
            "truly_connected": truly_connected,
            "soft_warning":    soft_warning,
            "webhook_active":  webhook_active,
            "reason_code":     reason_code,
            "reason_message":  reason_msg or default_msg,
            "checks":          checks,
            "provider":        provider,
            "db_status":       db_status,
            "verified_at":     now_iso,
            "provider_probe":  d360_sanitize_live_verify_probe(provider_probe) if provider_probe else None,
        },
    )


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

        # 5. Write through canonical service — integrity + webhook surfaced explicitly
        _pid_to_write = (phone_id or phone_info.get("id") or "").strip()
        _wid_to_write = (waba_id or waba_info.get("id") or "").strip()

        from services.whatsapp_connection_service import (  # noqa: PLC0415
            commit_connection,
            WhatsAppConnectionConflict,
            WhatsAppConnectionError,
        )
        try:
            result = commit_connection(
                db,
                tenant_id       = tenant_id,
                phone_number_id = _pid_to_write,
                waba_id         = _wid_to_write,
                access_token    = token,
                connection_type = "cloud_api",
                phone_number    = phone_info.get("display_phone_number", ""),
                display_name    = (
                    phone_info.get("verified_name") or waba_info.get("name", "")
                ),
                actor           = "oauth_callback",
            )
        except WhatsAppConnectionConflict as _exc:
            raise HTTPException(status_code=409, detail=connection_conflict_http_detail(_exc)) from _exc
        except WhatsAppConnectionError as _exc:
            raise HTTPException(
                status_code=502, detail="Meta callback failed."
            ) from _exc

        # Update Meta-specific metadata that lives outside the service's scope
        conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
        if conn:
            conn.token_type           = token_type
            conn.token_expires_at     = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            conn.meta_business_account_id = (
                body.business_id
                or (waba_info.get("on_behalf_of_business_info") or {}).get("id")
            )
            db.commit()

        log_wa_direct_stage(
            stage="oauth callback done",
            tenant_id=tenant_id,
            success=True,
            phone_number_id=_pid_to_write,
            waba_id=_wid_to_write,
            tag="whatsapp/oauth",
        )

        # Notify merchant — WhatsApp connected
        try:
            import asyncio as _asyncio  # noqa: PLC0415
            from core.wa_notify import notify_whatsapp_connected  # noqa: PLC0415
            from core.tenant import get_or_create_settings, merge_defaults, DEFAULT_WHATSAPP, DEFAULT_STORE  # noqa: PLC0415
            _s      = get_or_create_settings(db, tenant_id)
            _wa     = merge_defaults(_s.whatsapp_settings or {}, DEFAULT_WHATSAPP)
            _st     = merge_defaults(_s.store_settings    or {}, DEFAULT_STORE)
            _phone  = _wa.get("owner_whatsapp_number", "") or (conn and conn.phone_number) or ""
            _sname  = _st.get("store_name", "") or f"متجر #{tenant_id}"
            if _phone:
                _asyncio.ensure_future(notify_whatsapp_connected(_phone, _sname))
        except Exception as _exc:  # noqa: BLE001
            log_wa_direct_exception("whatsapp connected notification", _exc, tenant_id=tenant_id)

        api_dict = result.to_api_dict()
        if conn:
            api_dict.update(_safe_view(conn))
        return api_dict

    except HTTPException:
        raise
    except Exception as exc:
        conn.status     = "error"
        conn.last_error = str(exc)[:1000]
        db.commit()
        log_wa_direct_exception("whatsapp callback", exc, tenant_id=tenant_id, level="error")
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
        except HTTPException:
            raise
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
            from routers.whatsapp_embedded import _project_phone_sync_state  # noqa: PLC0415
            sync_state = _project_phone_sync_state(conn, data if isinstance(data, dict) else {})
            if _sync_state_ready(sync_state):
                conn.sending_enabled  = bool(sync_state.get("sending_enabled"))
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
                _finalize_connected_or_http(db, conn)
            else:
                conn.sending_enabled  = bool(sync_state.get("sending_enabled"))
                conn.status           = _non_success_db_status(sync_state)
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

    except HTTPException:
        raise
    except Exception as exc:
        conn.last_error = str(exc)[:500]
        db.commit()
        return {"verified": False, "reason": str(exc)}


class ManualConnectIn(BaseModel):
    phone_number_id: str
    waba_id: str
    access_token: str


class ResolveWabaIn(BaseModel):
    phone_number_id: str
    access_token: str


@router.post("/connection/resolve-waba")
async def resolve_waba(
    body: ResolveWabaIn,
    request: Request,
):
    """
    Ask Meta which WABA owns a given phone_number_id.

    Call this BEFORE manual-connect to auto-fill the correct waba_id.
    Returns the resolved waba_id, or an error explaining why it could not be determined.
    This endpoint never writes to the database.
    """
    _assert_merchant_self_service_secrets_allowed(request)
    from services.whatsapp_connection_service import resolve_waba_for_phone  # noqa: PLC0415

    pid   = body.phone_number_id.strip()
    token = body.access_token.strip()

    if not pid or not pid.isdigit():
        raise HTTPException(status_code=422, detail="phone_number_id يجب أن يحتوي على أرقام فقط")
    if not token:
        raise HTTPException(status_code=422, detail="access_token مطلوب")

    # Best-effort: no tenant_id needed here since we never write
    resolved_waba, err = resolve_waba_for_phone(pid, token, tenant_id=0)

    if resolved_waba:
        return {
            "ok":             True,
            "phone_number_id": pid,
            "resolved_waba_id": resolved_waba,
            "message": f"الـ WABA الصحيح لهذا الرقم هو: {resolved_waba}",
        }

    return {
        "ok":             False,
        "phone_number_id": pid,
        "resolved_waba_id": None,
        "error":          err,
        "message": (
            "تعذر تحديد الـ WABA تلقائياً. يرجى استخدام Meta Business Manager: "
            "WhatsApp → WhatsApp Accounts → اختر الحساب → انسخ الـ ID من الرابط."
        ),
    }


@router.post("/connection/manual-connect")
async def manual_connect(
    body: ManualConnectIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Merchant self-service manual WhatsApp connection.

    TENANT-STRICT: The connection is bound exclusively to the tenant embedded
    in the verified JWT.  No fallback, no tenant creation, no drift.
    Any ambiguity is rejected loudly before any write happens.
    """
    _assert_merchant_self_service_secrets_allowed(request)
    from core.auth import get_jwt_user_id  # noqa: PLC0415
    from models import Tenant as _Tenant   # noqa: PLC0415

    # ── Step 1: resolve tenant directly from the JWT payload ─────────────────
    # We read the raw payload instead of going through resolve_tenant_id() so
    # the code path is explicit and cannot silently fall through to any header-
    # based fallback that only exists for dev/testing.
    jwt_payload = getattr(request.state, "jwt_payload", None)
    if not jwt_payload:
        logger.critical(
            "[manual_connect] REJECTED — no JWT payload on request.state. "
            "Possible middleware bypass. ip=%s path=%s",
            request.client.host if request.client else "unknown",
            request.url.path,
        )
        raise HTTPException(
            status_code=401,
            detail="لا يمكن تحديد هوية المستخدم. يرجى تسجيل الدخول مرة أخرى.",
        )

    raw_tid = jwt_payload.get("tenant_id")
    if raw_tid is None:
        logger.critical(
            "[manual_connect] REJECTED — JWT has no tenant_id claim. sub=%s role=%s",
            jwt_payload.get("sub"), jwt_payload.get("role"),
        )
        raise HTTPException(
            status_code=401,
            detail="الرمز المميز لا يحتوي على معرّف المتجر. يرجى تسجيل الدخول مرة أخرى.",
        )

    try:
        tenant_id = int(raw_tid)
    except (ValueError, TypeError):
        logger.critical(
            "[manual_connect] REJECTED — JWT tenant_id is not an integer: %r", raw_tid
        )
        raise HTTPException(status_code=401, detail="معرّف المتجر في الرمز المميز غير صالح.")

    actor_user_id = get_jwt_user_id(request)
    client_ip     = request.client.host if request.client else "unknown"

    # ── Step 2: verify the tenant actually exists in the database ─────────────
    # A stale or fabricated JWT could carry a tenant_id that has no row in the
    # tenants table.  Writing a WhatsAppConnection for a ghost tenant causes
    # silent data drift — catch it before any write.
    tenant_row = db.query(_Tenant).filter(_Tenant.id == tenant_id).first()
    if not tenant_row:
        logger.critical(
            "[manual_connect] REJECTED — JWT tenant_id=%s has NO row in tenants table. "
            "Stale token or ghost tenant. actor_user_id=%s ip=%s",
            tenant_id, actor_user_id, client_ip,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"المتجر رقم {tenant_id} غير موجود في النظام. "
                "يرجى تسجيل الدخول مرة أخرى أو التواصل مع الدعم."
            ),
        )

    # ── Step 3: input validation ──────────────────────────────────────────────
    pid   = body.phone_number_id.strip()
    wid   = body.waba_id.strip()
    token = body.access_token.strip()

    if not pid or not pid.isdigit():
        raise HTTPException(status_code=422, detail="Phone Number ID يجب أن يكون رقمًا صحيحًا فقط")
    if not wid or not wid.isdigit():
        raise HTTPException(status_code=422, detail="WABA ID يجب أن يكون رقمًا صحيحًا فقط")
    if not token:
        raise HTTPException(status_code=422, detail="Access Token مطلوب")

    # ── Step 4: log the resolved identity BEFORE any write ────────────────────
    log_wa_direct_stage(stage="manual connect identity resolved", tenant_id=tenant_id, phone_number_id=pid, waba_id=wid, tag="manual_connect")

    # ── Step 5+6: delegate write + webhook to the canonical connection service ──
    # All integrity checks, the DB write, and webhook subscription happen inside
    # commit_connection().  Conflict errors surface as HTTP 409.  No silent
    # fallback, no broad except-swallowing here.
    from services.whatsapp_connection_service import (  # noqa: PLC0415
        commit_connection,
        WhatsAppConnectionConflict,
        WhatsAppConnectionError,
    )
    try:
        result = commit_connection(
            db,
            tenant_id       = tenant_id,
            phone_number_id = pid,
            waba_id         = wid,
            access_token    = token,
            connection_type = "cloud_api",
            actor           = str(actor_user_id),
        )
    except WhatsAppConnectionConflict as exc:
        raise HTTPException(status_code=409, detail=connection_conflict_http_detail(exc)) from exc
    except WhatsAppConnectionError as exc:
        raise HTTPException(status_code=502, detail="WhatsApp connection write failed.") from exc

    log_wa_direct_stage(stage="manual connect done", tenant_id=tenant_id, success=True, phone_number_id=pid, waba_id=wid, tag="manual_connect")
    return result.to_api_dict()


@router.post("/connection/disconnect")
async def disconnect(request: Request, db: Session = Depends(get_db)):
    """Merchant disconnects WhatsApp — wipes token, preserves identifiers for re-connect."""
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn:
        return {"status": "not_connected"}

    if _wa_provider(conn) == WHATSAPP_PROVIDER_360DIALOG:
        raise HTTPException(
            status_code=409,
            detail="فصل واتساب الجوال + الذكاء الاصطناعي يتم حاليًا عبر فريق نحلة حفاظًا على سلامة الربط.",
        )

    actor_user_id = get_jwt_user_id(request)
    now           = datetime.now(timezone.utc)

    conn.status                  = "disconnected"
    conn.access_token            = None
    conn.token_type              = None
    conn.token_expires_at        = None
    conn.sending_enabled         = False
    conn.last_error              = None
    conn.disconnect_reason       = "merchant_requested_disconnect"
    conn.disconnected_at         = now
    conn.disconnected_by_user_id = actor_user_id

    db.commit()
    audit(
        "whatsapp_disconnect",
        reason="merchant_requested_disconnect",
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
    )
    logger.info("tenant=%s WhatsApp disconnected by merchant user_id=%s", tenant_id, actor_user_id)
    return {"status": "disconnected"}


@router.post("/connection/reset-ai-live-since")
async def reset_whatsapp_ai_live_since_endpoint(request: Request, db: Session = Depends(get_db)):
    """Advance the AI cut-off to *now* so only newer WhatsApp traffic runs Brain.

    Use when a merchant finishes onboarding / wants to ignore backlog that
    already landed in Nahla after linking.
    """
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="لا يوجد ربط واتساب لهذا المتجر")
    now = datetime.now(timezone.utc)
    conn.whatsapp_ai_live_since = now
    db.add(conn)
    db.commit()
    logger.info("[whatsapp_ai_live] merchant RESET tenant=%s new_cutoff=%s", tenant_id, now.isoformat())
    return {"ok": True, "whatsapp_ai_live_since": now.isoformat()}


@router.post("/connection/reconnect")
async def reconnect(request: Request, db: Session = Depends(get_db)):
    """Reset the connection to 'pending' so the merchant can run Embedded Signup again."""
    tenant_id = resolve_tenant_id(request)
    conn = _get_or_create_connection(db, tenant_id)

    if _wa_provider(conn) == WHATSAPP_PROVIDER_360DIALOG:
        _set_coexistence_state(conn, status="pending_activation")
        conn.status                  = "pending_activation"
        conn.last_attempt_at         = datetime.now(timezone.utc)
        conn.last_error              = None
        conn.disconnect_reason       = None
        conn.disconnected_at         = None
        conn.disconnected_by_user_id = None
        db.commit()
        return {
            "status": "pending_activation",
            "message": "سيكمل فريق نحلة إعادة تفعيل هذا النوع من الربط.",
            **_coexistence_status_payload(conn),
        }

    conn.status                  = "pending"
    conn.last_attempt_at         = datetime.now(timezone.utc)
    conn.last_error              = None
    conn.disconnect_reason       = None
    conn.disconnected_at         = None
    conn.disconnected_by_user_id = None
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


@router.post("/assisted-connect/request")
async def request_assisted_connect(
    body: AssistedConnectRequestIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Merchant requests Nahla-team-assisted WhatsApp onboarding — no Meta secrets."""
    tenant_id = resolve_tenant_id(request)
    conn = _get_or_create_connection(db, tenant_id)

    if conn.status == "connected" and conn.sending_enabled:
        raise HTTPException(status_code=409, detail="واتساب مرتبط بالفعل لهذا المتجر.")

    if (
        conn.connection_type == WHATSAPP_CONNECTION_TYPE_ASSISTED
        and conn.status in {"request_submitted", "pending_activation", "action_required"}
    ):
        raise HTTPException(
            status_code=409,
            detail="يوجد طلب ربط معلّق بالفعل — سيتواصل معك فريق نحلة قريباً.",
        )

    now = datetime.now(timezone.utc)
    contact_phone = (body.contact_phone or "").strip() or None
    display_name = (body.display_name or "").strip() or None
    notes = (body.notes or "").strip() or None

    conn.provider = WHATSAPP_PROVIDER_META
    conn.connection_type = WHATSAPP_CONNECTION_TYPE_ASSISTED
    conn.status = "request_submitted"
    conn.phone_number = contact_phone
    conn.business_display_name = display_name or conn.business_display_name
    conn.phone_number_id = None
    conn.whatsapp_business_account_id = None
    conn.meta_business_account_id = None
    conn.access_token = None
    conn.token_type = "meta_permanent"
    conn.token_expires_at = None
    conn.connected_at = None
    conn.webhook_verified = False
    conn.sending_enabled = False
    conn.last_error = None
    conn.last_attempt_at = now
    _set_assisted_connect_state(
        conn,
        status="request_submitted",
        request_payload={
            "contact_phone": contact_phone,
            "display_name": display_name,
            "notes": notes,
            "submitted_at": now.isoformat(),
        },
    )
    db.commit()
    db.refresh(conn)

    logger.info(
        "[ASSISTED_CONNECT_REQUEST_CREATED] tenant_id=%s request_id=%s status=%s",
        tenant_id,
        conn.id,
        conn.status,
    )

    audit(
        "WHATSAPP_ASSISTED_CONNECT_REQUEST",
        tenant_id=tenant_id,
        contact_phone=contact_phone or "-",
        display_name=display_name,
    )

    return {
        "status": "request_submitted",
        "message": "تم استلام طلب الربط. سيتواصل معك فريق نحلة لإتمام ربط واتساب.",
        **_assisted_status_payload(conn),
    }


@router.get("/assisted-connect/status")
async def assisted_connect_status(request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    return _assisted_status_payload(conn)


@router.post("/coexistence/request")
async def request_coexistence(
    body: CoexistenceRequestIn,
    request: Request,
    db: Session = Depends(get_db),
):
    tenant_id = resolve_tenant_id(request)
    conn = _get_or_create_connection(db, tenant_id)
    _ensure_coexistence_allowed(db, tenant_id)
    if not D360_COHOST_ALLOW_SELF_REQUEST:
        raise HTTPException(status_code=403, detail="تفعيل هذا الخيار يتم حاليًا عبر فريق نحلة.")

    conn.provider = WHATSAPP_PROVIDER_360DIALOG
    conn.connection_type = WHATSAPP_CONNECTION_TYPE_COEXISTENCE
    conn.status = "request_submitted"
    conn.phone_number = body.phone_number
    conn.business_display_name = body.display_name or conn.business_display_name
    conn.phone_number_id = None
    conn.whatsapp_business_account_id = None
    conn.meta_business_account_id = None
    conn.access_token = None
    conn.token_type = "dialog360_api_key"
    conn.token_expires_at = None
    conn.connected_at = None
    conn.webhook_verified = False
    conn.sending_enabled = False
    conn.last_error = None
    conn.last_attempt_at = datetime.now(timezone.utc)
    _set_coexistence_state(
        conn,
        status="request_submitted",
        request_payload={
            "phone_number": body.phone_number,
            "display_name": body.display_name,
            "has_whatsapp_business_app": body.has_whatsapp_business_app,
            "understands_keep_app_installed": body.understands_keep_app_installed,
            "understands_open_every_13_days": body.understands_open_every_13_days,
            "notes": body.notes,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    db.commit()
    return {
        "status": "request_submitted",
        "message": "تم استلام طلب التفعيل. سيكمل فريق نحلة ربط واتساب الجوال + الذكاء الاصطناعي معك.",
        **_coexistence_status_payload(conn),
    }


@router.get("/coexistence/status")
async def coexistence_status(request: Request, db: Session = Depends(get_db)):
    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    _ensure_coexistence_allowed(db, tenant_id)
    return _coexistence_status_payload(conn)


# ── Partner Embedded Signup (self-service) ────────────────────────────────────

@router.get("/coexistence/signup-url")
async def coexistence_signup_url(request: Request, db: Session = Depends(get_db)):
    """
    Return the 360dialog Partner Integrated Onboarding URL for this tenant.
    The merchant opens this URL (as a popup) to complete self-service setup.
    """
    _ensure_coexistence_allowed(db, resolve_tenant_id(request))
    if not D360_PARTNER_ID:
        raise HTTPException(
            status_code=503,
            detail="D360_PARTNER_ID is not configured. Contact Nahla support.",
        )
    tenant_id = resolve_tenant_id(request)
    import base64 as _b64  # noqa: PLC0415
    state = _b64.urlsafe_b64encode(f"tid={tenant_id}".encode()).decode()
    redirect_url = f"{BACKEND_URL.rstrip('/')}/whatsapp/coexistence/partner-redirect"
    hub_base = D360_PARTNER_HUB_BASE.rstrip("/")
    import urllib.parse as _up  # noqa: PLC0415
    signup_url = (
        f"{hub_base}/dashboard/app/{D360_PARTNER_ID}/permissions"
        f"?state={_up.quote(state)}"
        f"&redirect_url={_up.quote(redirect_url)}"
    )
    return {"signup_url": signup_url, "partner_id": D360_PARTNER_ID}


class PartnerConnectIn(BaseModel):
    """Payload sent by frontend after 360dialog popup returns client_id + channels."""
    client_id: str
    channels: list  # list of channel IDs (strings) from 360dialog


@router.post("/coexistence/partner-connect")
async def coexistence_partner_connect(
    body: PartnerConnectIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Called by the frontend immediately after the 360dialog popup closes
    and posts back client_id + channels[].

    1. Fetches channel info from 360dialog Partner API
    2. Generates API key for the channel
    3. Saves connection with provider=dialog360 + api_key
    4. Configures webhook
    5. Returns connection status
    """
    tenant_id = resolve_tenant_id(request)
    _ensure_coexistence_allowed(db, tenant_id)

    if not D360_PARTNER_ID:
        raise HTTPException(status_code=503, detail="D360_PARTNER_ID is not configured.")
    if not body.channels:
        raise HTTPException(status_code=400, detail="No channels returned from 360dialog.")

    channel_id = body.channels[0] if isinstance(body.channels[0], str) else body.channels[0].get("id", "")
    if not channel_id:
        raise HTTPException(status_code=400, detail="Could not extract channel_id from 360dialog response.")

    # Fetch channel details from Partner API
    channel_info = await dialog360_get_channel_info(
        partner_id=D360_PARTNER_ID,
        channel_id=channel_id,
    )
    log_wa_direct_stage(stage="partner-connect channel", tenant_id=tenant_id, success=bool(channel_id), tag="coexistence")

    # Attempt to generate an API key
    api_key_resp = await dialog360_generate_api_key(
        partner_id=D360_PARTNER_ID,
        channel_id=channel_id,
    )
    api_key = (
        api_key_resp.get("api_key")
        or api_key_resp.get("key")
        or api_key_resp.get("token")
    )
    channel_status = channel_info.get("status", "unknown")

    conn = _get_or_create_connection(db, tenant_id)
    internal_secret = D360_WEBHOOK_INTERNAL_SECRET or secrets.token_urlsafe(24)

    conn.provider = WHATSAPP_PROVIDER_360DIALOG
    conn.connection_type = WHATSAPP_CONNECTION_TYPE_COEXISTENCE
    conn.phone_number = channel_info.get("phone_number") or conn.phone_number
    conn.business_display_name = channel_info.get("name") or conn.business_display_name
    conn.whatsapp_business_account_id = channel_info.get("waba_id")
    conn.phone_number_id = channel_id
    conn.token_type = "dialog360_api_key"
    conn.token_expires_at = None
    conn.last_attempt_at = datetime.now(timezone.utc)
    conn.last_error = None

    meta = dict(conn.extra_metadata or {})
    meta["provider_details"] = {
        "channel_id": channel_id,
        "client_id": sanitize_coexistence_client_id(body.client_id),
        "webhook_url": _coexistence_webhook_url(),
        "internal_header_name": "X-Nahla-Coexistence-Secret",
        "channel_status_at_connect": channel_status,
    }
    meta["coexistence_internal_secret"] = internal_secret

    if api_key:
        from services.whatsapp_platform.wa_connection_secrets import store_access_token  # noqa: PLC0415
        store_access_token(conn, api_key)
        conn.status = "pending_activation"
        conn.sending_enabled = False
        conn.webhook_verified = False
        _set_coexistence_state(conn, status="pending_activation")
        try:
            webhook_result = await dialog360_configure_webhook(
                api_key=api_key,
                url=_coexistence_webhook_url(),
                headers={"X-Nahla-Coexistence-Secret": internal_secret},
                timeout=5.0,
            )
        except Exception as exc:
            log_wa_direct_exception("coexistence partner webhook configure", exc, tag="coexistence")
            webhook_result = d360_safe_error_payload(exc, operation="dialog360_configure_webhook")
        # ALSO set the WABA-level webhook (override_all=True) — this is the
        # only scope that guarantees inbound delivery when 360dialog rotates
        # phone_number_id during a re-bind. Channel scope alone has been
        # observed to fall to N/A even when the partner UI shows it green.
        try:
            waba_webhook_result = await dialog360_set_waba_webhook(
                api_key=api_key,
                url=_coexistence_webhook_url(),
                headers={"X-Nahla-Coexistence-Secret": internal_secret},
                override_all=True,
                timeout=8.0,
            )
        except Exception as exc:
            log_wa_direct_exception("coexistence partner waba webhook", exc, tag="coexistence")
            waba_webhook_result = d360_safe_error_payload(exc, operation="dialog360_configure_webhook")
        channel_ok = _d360_operation_ok(webhook_result)
        waba_ok = _d360_operation_ok(waba_webhook_result)
        if channel_ok or waba_ok:
            conn.webhook_verified = True
            if channel_status == "ready":
                conn.sending_enabled = True
                _set_coexistence_state(conn, status="connected")
        meta["last_webhook_setup"] = d360_safe_persist_webhook_setup(webhook_result)
        meta["last_waba_webhook_setup"] = d360_safe_persist_webhook_setup(waba_webhook_result)
    else:
        # Channel not ready yet — store pending state, webhook from 360dialog will follow
        conn.access_token = None
        conn.status = "pending_activation"
        conn.sending_enabled = False
        conn.webhook_verified = False
        _set_coexistence_state(
            conn,
            status="pending_activation",
            action_required_message=(
                "تم تسجيل القناة لدى 360dialog. "
                "ننتظر تفعيلها (يستغرق دقائق). ستحصل على إشعار عند الجاهزية."
            ),
        )

    conn.extra_metadata = meta
    if api_key and channel_status == "ready" and conn.sending_enabled:
        _finalize_connected_or_http(db, conn)
    else:
        db.commit()

    return {
        "status": conn.status,
        "channel_id": channel_id,
        "client_id": sanitize_coexistence_client_id(body.client_id),
        "api_key_obtained": bool(api_key),
        "channel_status": channel_status,
        **_coexistence_status_payload(conn),
    }


@router.get("/coexistence/partner-redirect")
async def coexistence_partner_redirect():
    """
    Landing page that 360dialog redirects to after Integrated Onboarding.
    This page is opened in a popup — it posts its query params to the opener
    window (the Nahla dashboard) and closes itself.
    """
    from fastapi.responses import HTMLResponse  # noqa: PLC0415
    html = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>نحلة — جارٍ الربط...</title></head>
<body>
<script>
function processParams() {
  var params = window.location.search;
  if (window.opener) {
    window.opener.postMessage(params, '*');
    window.close();
  } else {
    document.body.innerText = 'تم الربط. يمكنك إغلاق هذه النافذة.';
  }
}
window.onload = processParams;
</script>
<p style="font-family:sans-serif;text-align:center;margin-top:80px">جارٍ إتمام الربط...</p>
</body>
</html>"""
    return HTMLResponse(content=html)


@router.post("/admin/coexistence/activate")
async def admin_activate_coexistence(
    body: CoexistenceOpsActivateIn,
    db: Session = Depends(get_db),
    _admin: Dict[str, object] = Depends(require_admin),
):
    request_id = secrets.token_hex(6)
    conn = _get_or_create_connection(db, body.tenant_id)
    _ensure_coexistence_allowed(db, body.tenant_id)

    internal_secret = D360_WEBHOOK_INTERNAL_SECRET or secrets.token_urlsafe(24)
    conn.provider = WHATSAPP_PROVIDER_360DIALOG
    conn.connection_type = WHATSAPP_CONNECTION_TYPE_COEXISTENCE
    conn.status = "pending_activation"
    conn.phone_number_id = body.phone_number_id
    conn.phone_number = body.phone_number
    conn.business_display_name = body.display_name or conn.business_display_name
    conn.whatsapp_business_account_id = body.waba_id
    store_access_token(conn, body.api_key)
    conn.token_type = "dialog360_api_key"
    conn.token_expires_at = None
    conn.last_attempt_at = datetime.now(timezone.utc)
    conn.last_error = None
    conn.sending_enabled = True

    meta = dict(conn.extra_metadata or {})
    meta["provider_details"] = {
        "channel_id": body.channel_id,
        "client_id": sanitize_coexistence_client_id(body.client_id),
        "webhook_url": _coexistence_webhook_url(),
        "internal_header_name": "X-Nahla-Coexistence-Secret",
    }
    meta["coexistence_internal_secret"] = internal_secret
    conn.extra_metadata = meta
    _set_coexistence_state(
        conn,
        status="pending_activation",
        action_required_message=body.action_required_message,
    )

    webhook_result = None
    waba_webhook_result: Optional[Dict[str, Any]] = None
    should_finalize = False
    if body.configure_webhook:
        try:
            webhook_result = await dialog360_configure_webhook(
                api_key=body.api_key,
                url=_coexistence_webhook_url(),
                headers={"X-Nahla-Coexistence-Secret": internal_secret},
                timeout=5.0,
            )
        except Exception as exc:
            log_wa_direct_exception("coexistence activate webhook configure", exc, tag="coexistence")
            webhook_result = d360_safe_error_payload(exc, operation="dialog360_configure_webhook")
        try:
            waba_webhook_result = await dialog360_set_waba_webhook(
                api_key=body.api_key,
                url=_coexistence_webhook_url(),
                headers={"X-Nahla-Coexistence-Secret": internal_secret},
                override_all=True,
                timeout=8.0,
            )
        except Exception as exc:
            log_wa_direct_exception("coexistence activate waba webhook", exc, tag="coexistence")
            waba_webhook_result = d360_safe_error_payload(exc, operation="dialog360_configure_webhook")
        channel_ok = _d360_operation_ok(webhook_result)
        waba_ok = _d360_operation_ok(waba_webhook_result)
        meta["last_webhook_setup"] = d360_safe_persist_webhook_setup(webhook_result)
        meta["last_waba_webhook_setup"] = d360_safe_persist_webhook_setup(waba_webhook_result)
        conn.extra_metadata = meta
        flag_modified(conn, "extra_metadata")
        if not channel_ok and not waba_ok:
            conn.status = "action_required"
            conn.webhook_verified = False
            conn.sending_enabled = False
            conn.last_error = str((webhook_result or {}).get("error_type") or (webhook_result or {}).get("error") or "webhook_setup_failed")[:500]
            _set_coexistence_state(
                conn,
                status="action_required",
                action_required_message=body.action_required_message or "فشل إعداد webhook لدى المزود ويحتاج تدخل فريق نحلة.",
            )
        else:
            conn.webhook_verified = True
            conn.sending_enabled = True
            _set_coexistence_state(conn, status="connected")
            should_finalize = True

    # ── Auto-resolve missing WABA ID / phone metadata at activation time ──
    # The activation form treats waba_id as optional ("الحقول الاختيارية")
    # because operators usually only have the API key + phone_number_id at
    # hand. Without WABA ID the merchant page would (correctly) refuse to
    # claim the integration is healthy — see `_coexistence_integration_complete`.
    # We try every 360dialog read endpoint we have credentials for and merge
    # whatever we discover into the record. Failures are logged but never
    # block activation: the operator can always run "Sync / Repair" later.
    completeness = _coexistence_integration_complete(conn)
    if not completeness["truly_connected"] and completeness.get("missing_fields"):
        try:
            await _resolve_and_apply_metadata(conn, request_id=request_id, source="activate")
        except Exception as exc:
            log_wa_direct_exception("coexistence activate auto-resolve", exc, tenant_id=body.tenant_id, tag="coexistence")

    if should_finalize:
        _finalize_connected_or_http(db, conn)
    else:
        db.commit()
    _log_integration_state(conn, tenant_id=body.tenant_id, source="admin_activate", request_id=request_id)

    return {
        "status": conn.status,
        "webhook_result": _sanitize_webhook_operation_result(webhook_result) if webhook_result else None,
        "request_id": request_id,
        **_coexistence_status_payload(conn),
    }


# ── Resolver helper: pulls fresh metadata from 360dialog and persists it ────
async def _resolve_and_apply_metadata(
    conn: WhatsAppConnection,
    *,
    request_id: str,
    source: str,
) -> Dict[str, Any]:
    """Call the 360dialog metadata resolver and apply any discovered fields
    to ``conn`` in place. Returns the raw resolver payload for the caller
    to surface to the operator.

    Existing values on the record win — we only fill MISSING fields, so
    this is safe to call repeatedly without losing manual overrides."""
    api_key = read_access_token(conn)
    pnid = conn.phone_number_id
    channel_id = (conn.extra_metadata or {}).get("provider_details", {}).get("channel_id")
    if not channel_id:
        # Pre-2026 tenants stored channel_id directly as phone_number_id; use
        # that as the partner-API channel reference.
        channel_id = pnid

    resolved = await dialog360_resolve_channel_metadata(
        api_key=api_key or "",
        phone_number_id=pnid,
        channel_id=channel_id,
        partner_id=D360_PARTNER_ID or None,
    )

    if resolved.get("waba_id") and not conn.whatsapp_business_account_id:
        conn.whatsapp_business_account_id = resolved["waba_id"]
    if resolved.get("phone_number_id") and not conn.phone_number_id:
        conn.phone_number_id = resolved["phone_number_id"]
    if resolved.get("phone_number") and not conn.phone_number:
        conn.phone_number = resolved["phone_number"]
    if resolved.get("display_name") and not conn.business_display_name:
        conn.business_display_name = resolved["display_name"]

    meta = dict(conn.extra_metadata or {})
    coex = dict(meta.get("coexistence") or {})
    coex["last_resolver"] = {
        "at":      datetime.now(timezone.utc).isoformat(),
        "source":  source,
        "request_id": request_id,
        "sources_used": resolved.get("sources") or [],
        "errors":  resolved.get("errors") or {},
    }
    meta["coexistence"] = coex
    conn.extra_metadata = meta
    flag_modified(conn, "extra_metadata")

    logger.info(
        "[coexistence/resolver] tenant=%s source=%s request_id=%s sources_used=%s "
        "filled_waba=%s filled_phone=%s filled_pnid=%s",
        conn.tenant_id, source, request_id, resolved.get("sources"),
        bool(resolved.get("waba_id")), bool(resolved.get("phone_number")),
        bool(resolved.get("phone_number_id")),
    )
    return resolved


# ── Shared admin request body for tenant-scoped coexistence operations ──────
# Defined here (before first use) so FastAPI/Pydantic v2 can resolve the
# annotation during schema generation. Forward-referencing it caused
# `PydanticUndefinedAnnotation: name '_TenantOnly' is not defined` at boot.

class _TenantOnly(BaseModel):
    tenant_id: int


# ── Admin: Sync / Repair Integration Record ─────────────────────────────────
# Re-reads channel metadata from 360dialog (Partner API + per-tenant API key)
# and fills the integration record. Use this when activation finished without
# a WABA ID, or when the merchant page reports `missing_waba_id`.

@router.post("/admin/coexistence/sync-record")
async def admin_coexistence_sync_record(
    body: _TenantOnly,
    db: Session = Depends(get_db),
    _admin: Dict[str, object] = Depends(require_admin),
):
    request_id = secrets.token_hex(6)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=body.tenant_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="لا يوجد سجل واتساب لهذا المتجر")
    if _wa_provider(conn) != WHATSAPP_PROVIDER_360DIALOG:
        raise HTTPException(status_code=400, detail="هذا التاجر ليس على 360dialog")

    before = _coexistence_integration_complete(conn)
    resolved = await _resolve_and_apply_metadata(conn, request_id=request_id, source="admin_sync")

    # If everything is now present, promote the record to `connected` so the
    # merchant page no longer shows the "غير متصل فعليًا" banner.
    after = _coexistence_integration_complete(conn)
    if after["truly_connected"]:
        conn.sending_enabled = True
        _set_coexistence_state(conn, status="connected")
        conn.last_error = None
        _finalize_connected_or_http(db, conn)
    else:
        if after.get("missing_fields"):
            conn.last_error = (
                f"sync_incomplete: missing {', '.join(after['missing_fields'])}"
            )
        db.commit()
    _log_integration_state(conn, tenant_id=body.tenant_id, source="admin_sync", request_id=request_id)
    audit(
        "admin_coexistence_sync_record",
        admin=_admin.get("sub") if isinstance(_admin, dict) else None,
        tenant_id=body.tenant_id,
        request_id=request_id,
        before=before,
        after=after,
    )

    return {
        "tenant_id":   body.tenant_id,
        "request_id":  request_id,
        "before":      before,
        "after":       after,
        "resolved":    {
            "waba_id":         resolved.get("waba_id"),
            "phone_number_id": resolved.get("phone_number_id"),
            "phone_number":    resolved.get("phone_number"),
            "display_name":    resolved.get("display_name"),
            "channel_status":  resolved.get("channel_status"),
            "sources":         resolved.get("sources") or [],
            "errors":          resolved.get("errors") or {},
        },
        "integration_complete": after,
        **_coexistence_status_payload(conn),
    }


# ── Admin: manual edit of the integration record ────────────────────────────
# Lets the operator override any of the canonical fields — used when 360dialog
# can't auto-resolve (e.g. partner API not configured, or a value needs to be
# fixed by hand). Only fields explicitly provided are touched; unspecified
# fields keep their current value.

class CoexistenceEditPayload(BaseModel):
    tenant_id:       int
    waba_id:         Optional[str] = None
    phone_number_id: Optional[str] = None
    phone_number:    Optional[str] = None
    channel_id:      Optional[str] = None
    client_id:       Optional[str] = None
    api_key:         Optional[str] = None
    display_name:    Optional[str] = None
    # Operator can force-promote to connected after fixing fields by hand.
    promote_to_connected: bool = True


@router.post("/admin/coexistence/edit-record")
async def admin_coexistence_edit_record(
    body: CoexistenceEditPayload,
    db: Session = Depends(get_db),
    _admin: Dict[str, object] = Depends(require_admin),
):
    request_id = secrets.token_hex(6)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=body.tenant_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="لا يوجد سجل واتساب لهذا المتجر")

    changed: list[str] = []
    if body.waba_id is not None and body.waba_id != conn.whatsapp_business_account_id:
        conn.whatsapp_business_account_id = body.waba_id.strip() or None
        changed.append("waba_id")
    if body.phone_number_id is not None and body.phone_number_id != conn.phone_number_id:
        conn.phone_number_id = body.phone_number_id.strip() or None
        changed.append("phone_number_id")
    if body.phone_number is not None and body.phone_number != conn.phone_number:
        conn.phone_number = body.phone_number.strip() or None
        changed.append("phone_number")
    if body.api_key is not None and body.api_key != read_access_token(conn):
        store_access_token(conn, body.api_key.strip() or None)
        conn.token_type = "dialog360_api_key"
        changed.append("api_key")
    if body.display_name is not None and body.display_name != conn.business_display_name:
        conn.business_display_name = body.display_name.strip() or None
        changed.append("display_name")

    meta = dict(conn.extra_metadata or {})
    pd = dict(meta.get("provider_details") or {})
    if body.channel_id is not None and body.channel_id != pd.get("channel_id"):
        pd["channel_id"] = body.channel_id.strip() or None
        changed.append("channel_id")
    if body.client_id is not None:
        new_cid = sanitize_coexistence_client_id(body.client_id)
        if new_cid != pd.get("client_id"):
            pd["client_id"] = new_cid
            changed.append("client_id")
    pd["webhook_url"]          = _coexistence_webhook_url()
    pd["coexistence_url"]      = _coexistence_events_url()
    pd["status_url"]           = _coexistence_status_url()
    pd["internal_header_name"] = "X-Nahla-Coexistence-Secret"
    meta["provider_details"]   = pd
    conn.extra_metadata        = meta
    flag_modified(conn, "extra_metadata")

    # Make sure provider/connection_type are correctly typed even if the
    # record was created in some other path.
    conn.provider        = WHATSAPP_PROVIDER_360DIALOG
    conn.connection_type = WHATSAPP_CONNECTION_TYPE_COEXISTENCE
    if not conn.token_type:
        conn.token_type = "dialog360_api_key"

    completeness = _coexistence_integration_complete(conn)
    if body.promote_to_connected and completeness["truly_connected"]:
        conn.sending_enabled = True
        _set_coexistence_state(conn, status="connected")
        conn.last_error = None
        _finalize_connected_or_http(db, conn)
    else:
        db.commit()
    _log_integration_state(conn, tenant_id=body.tenant_id, source="admin_edit", request_id=request_id)
    audit(
        "admin_coexistence_edit_record",
        admin=_admin.get("sub") if isinstance(_admin, dict) else None,
        tenant_id=body.tenant_id,
        request_id=request_id,
        changed_fields=changed,
        promote_to_connected=body.promote_to_connected,
    )

    return {
        "tenant_id":   body.tenant_id,
        "request_id":  request_id,
        "changed":     changed,
        "integration_complete": completeness,
        **_coexistence_status_payload(conn),
    }


# ── Admin: per-tenant Coexistence webhook tooling ───────────────────────────
# Test / Verify / Auto-Configure for each of the three 360dialog webhooks.
# These endpoints are owner-panel only (require_admin). They share the
# `_TenantOnly` request model defined earlier in the file.

@router.post("/admin/coexistence/test-webhook")
async def admin_coexistence_test_webhook(
    body: _TenantOnly,
    db: Session = Depends(get_db),
    _admin: Dict[str, object] = Depends(require_admin),
):
    """Send a Nahla-internal probe to each of the three Coexistence webhook
    endpoints to confirm they're reachable from the public internet.

    This does NOT involve 360dialog — it's a self-test. We POST a tiny
    well-known payload with the tenant's `X-Nahla-Coexistence-Secret`
    header. A 2xx response means our own router accepts the URL; a
    non-2xx (or transport error) means the deployment / load balancer is
    misconfigured. Use this BEFORE asking 360dialog to verify, so you
    know the issue is on Nahla's side, not 360dialog's.
    """
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=body.tenant_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="لا يوجد اتصال واتساب لهذا المتجر")

    secret = str((conn.extra_metadata or {}).get("coexistence_internal_secret") or "")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Nahla-Coexistence-Secret"] = secret

    targets = {
        "channel":     _coexistence_webhook_url(),
        "coexistence": _coexistence_events_url(),
        "status":      _coexistence_status_url(),
    }
    probe_field_by_target = {
        "channel":     "messages",
        "coexistence": "device_sync",
        "status":      "channel_status",
    }
    results: Dict[str, Dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        for name, url in targets.items():
            payload = {
                "object": "whatsapp_business_account",
                "entry": [{
                    "id": "nahla-self-test",
                    "changes": [{
                        "field": probe_field_by_target[name],
                        "value": {
                            "metadata": {
                                "phone_number_id": conn.phone_number_id or "",
                                "display_phone_number": conn.phone_number or "",
                            },
                            "_nahla_self_test": True,
                        },
                    }],
                }],
            }
            try:
                resp = await client.post(url, headers=headers, json=payload)
                results[name] = {
                    "ok":          200 <= resp.status_code < 300,
                    "url":         url,
                    "status_code": resp.status_code,
                    "body":        resp.text[:300],
                }
            except Exception as exc:
                results[name] = {"ok": False, "url": url, "error": str(exc)}

    audit(
        "admin_coexistence_test_webhook",
        admin=_admin.get("sub") if isinstance(_admin, dict) else None,
        tenant_id=body.tenant_id,
        results={k: v.get("ok") for k, v in results.items()},
    )
    return {
        "tenant_id": body.tenant_id,
        "all_ok":    all(v.get("ok") for v in results.values()),
        "results":   results,
    }


@router.post("/admin/coexistence/verify-webhook")
async def admin_coexistence_verify_webhook(
    body: _TenantOnly,
    db: Session = Depends(get_db),
    _admin: Dict[str, object] = Depends(require_admin),
):
    """Read the channel webhook 360dialog has on file for this tenant and
    compare it to the URL Nahla expects. Surfaces drift instead of relying
    on the local cache.

    Note: 360dialog's public WABA API only returns the *channel* webhook
    config. The Coexistence and status URLs are configured by Nahla as
    custom-headered routes pointing at the same channel — so a verified
    channel URL implies the others are routable too. The webhook block in
    `extra_metadata.coexistence.webhook` is updated to reflect the result.
    """
    from services.whatsapp_platform.service import dialog360_get_webhook_config  # noqa: PLC0415

    conn = db.query(WhatsAppConnection).filter_by(tenant_id=body.tenant_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="لا يوجد اتصال واتساب لهذا المتجر")
    if not conn.access_token:
        raise HTTPException(status_code=400, detail="مفتاح API لـ 360dialog غير مخزّن لهذا التاجر")

    expected_url = _coexistence_webhook_url()
    cfg: Dict[str, object] = {}
    verify_error: bool = False
    try:
        cfg = await asyncio.wait_for(
            dialog360_get_webhook_config(api_key=read_access_token(conn), timeout=5.0, expected_url=expected_url),
            timeout=6.0,
        )
    except Exception as exc:
        api_key = read_access_token(conn)
        verify_error = True
        log_wa_direct_exception("coexistence verify-webhook", exc, tenant_id=body.tenant_id, tag="coexistence", secrets=[api_key])
        cfg = d360_safe_error_payload(exc, secrets=[api_key], operation="verify_webhook")

    remote_url = ""
    response_status: Optional[int] = None
    if isinstance(cfg, dict):
        remote_url = d360_extract_remote_url(cfg)
        # 360dialog helpers normalise error responses to `{"error": ..., "status_code": ...}`
        sc = cfg.get("status_code")
        if isinstance(sc, int):
            response_status = sc
    matches = bool(remote_url) and remote_url.rstrip("/") == expected_url.rstrip("/")

    # ── Structured verify log ──────────────────────────────────────────
    # Single canonical line per call so support can correlate "merchant
    # sees Invalid api token in UI" with the actual remote response. The
    # Structured verify log uses api_key_present only — never key material.
    if verify_error:
        verify_result = "transport_error"
    elif isinstance(cfg, dict) and "error" in cfg:
        verify_result = "remote_error"
    elif matches:
        verify_result = "verified_match"
    elif remote_url:
        verify_result = "url_mismatch"
    else:
        verify_result = "no_remote_url"
    _log_d360_verify(
        operation="verify_webhook",
        tenant_id=body.tenant_id,
        conn=conn,
        endpoint_used="GET /v1/configs/webhook",
        response=cfg,
        response_status=response_status,
        parsed_url=remote_url or None,
        expected_url=expected_url,
        result=verify_result,
    )

    meta = dict(conn.extra_metadata or {})
    coex = dict(meta.get("coexistence") or {})
    webhook = dict(coex.get("webhook") or {})
    if verify_error:
        webhook["channel_status"] = "failed"
        webhook["channel_last_verify_error_type"] = cfg.get("error_type") if isinstance(cfg, dict) else "transport_error"
    else:
        webhook["channel_status"] = "verified" if matches else (
            "url_mismatch" if remote_url else "unverified"
        )
        webhook.pop("channel_last_verify_error", None)
    webhook["channel_last_verified_at"] = datetime.now(timezone.utc).isoformat()
    webhook["channel_remote_url_present"] = bool(remote_url)
    webhook["channel_url_matches"] = matches
    coex["webhook"] = webhook
    meta["coexistence"] = coex
    conn.extra_metadata = meta
    flag_modified(conn, "extra_metadata")
    if matches and not verify_error:
        conn.webhook_verified = True
    # Operational-health auto-heal: when verify confirms the URL matches
    # AND the row is operationally healthy, promote a stale status to
    # ``connected`` so the owner banner stops showing status_invalid.
    reconciled = _reconcile_connected_or_http(
        conn, tenant_id=body.tenant_id, source="admin_verify_webhook", db=db,
    )
    if not reconciled:
        db.commit()

    audit(
        "admin_coexistence_verify_webhook",
        admin=_admin.get("sub") if isinstance(_admin, dict) else None,
        tenant_id=body.tenant_id,
        matches=matches,
        reconciled=reconciled,
    )
    url_diag = d360_url_flags(remote_url, expected_url)
    return {
        "tenant_id":     body.tenant_id,
        "matches":       matches,
        **url_diag,
        "response":      d360_response_summary(cfg),
        "webhooks":      _coexistence_webhook_block(conn),
        "status_reconciled": reconciled,
        "integration_complete": _coexistence_integration_complete(conn),
    }


@router.post("/admin/coexistence/reconcile-status")
async def admin_coexistence_reconcile_status(
    body: _TenantOnly,
    db: Session = Depends(get_db),
    _admin: Dict[str, object] = Depends(require_admin),
):
    """Force the canonical status-reconciliation pass for this tenant.

    Use when the owner panel keeps showing ``status_invalid`` while the
    merchant page + AI are clearly working. This endpoint:

      1. Re-reads operational health (token / phone_id / waba_id /
         recent webhook traffic),
      2. If all four signals are green AND the row is not in a
         hard-fail state (disconnected / error), promotes it to
         ``status=connected`` and ``sending_enabled=True``.

    Read-only when nothing needs healing. Returns the before/after
    snapshot so the operator can confirm what changed.
    """
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=body.tenant_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="لا يوجد سجل واتساب لهذا المتجر")

    before_status = conn.status
    before_sending = bool(conn.sending_enabled)
    before_complete = _coexistence_integration_complete(conn)

    reconciled = _reconcile_connected_or_http(
        conn, tenant_id=body.tenant_id, source="admin_manual_reconcile", db=db,
    )

    after_complete = _coexistence_integration_complete(conn)
    audit(
        "admin_coexistence_reconcile_status",
        admin=_admin.get("sub") if isinstance(_admin, dict) else None,
        tenant_id=body.tenant_id,
        reconciled=reconciled,
        before_status=before_status,
        after_status=conn.status,
    )
    return {
        "tenant_id":     body.tenant_id,
        "reconciled":    reconciled,
        "before": {
            "status":             before_status,
            "sending_enabled":    before_sending,
            "integration_complete": before_complete,
        },
        "after": {
            "status":             conn.status,
            "sending_enabled":    bool(conn.sending_enabled),
            "integration_complete": after_complete,
        },
        "webhooks": _coexistence_webhook_block(conn),
    }


@router.post("/admin/coexistence/auto-configure")
async def admin_coexistence_auto_configure(
    body: _TenantOnly,
    db: Session = Depends(get_db),
    _admin: Dict[str, object] = Depends(require_admin),
):
    """Push Nahla's canonical channel webhook URL to 360dialog, including
    the per-tenant `X-Nahla-Coexistence-Secret` header.

    This is the one-click "Auto Configure" action. After it succeeds, the
    Coexistence and status URLs are reachable too — they share the same
    routing infrastructure — but operators should still configure them
    explicitly in 360dialog if they want clean separation in 360dialog's
    own dashboard."""
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=body.tenant_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="لا يوجد اتصال واتساب لهذا المتجر")
    if not conn.access_token:
        raise HTTPException(status_code=400, detail="مفتاح API لـ 360dialog غير مخزّن لهذا التاجر")

    secret = str((conn.extra_metadata or {}).get("coexistence_internal_secret") or "")
    if not secret:
        secret = D360_WEBHOOK_INTERNAL_SECRET or secrets.token_urlsafe(24)

    # ── 1. Channel-level webhook (phone-number scope) ───────────────────
    try:
        result = await asyncio.wait_for(
            dialog360_configure_webhook(
                api_key=read_access_token(conn),
                url=_coexistence_webhook_url(),
                headers={"X-Nahla-Coexistence-Secret": secret},
                timeout=5.0,
            ),
            timeout=6.0,
        )
    except Exception as exc:
        log_wa_direct_exception("coexistence auto-configure", exc, tenant_id=body.tenant_id, tag="coexistence")
        result = d360_safe_error_payload(exc, secrets=[read_access_token(conn)], operation="auto_configure_channel")
    ok = _d360_operation_ok(result)
    _log_d360_verify(
        operation="auto_configure_channel",
        tenant_id=body.tenant_id,
        conn=conn,
        endpoint_used="POST /v1/configs/webhook",
        response=result,
        response_status=(result or {}).get("status_code") if isinstance(result, dict) else None,
        parsed_url=None,
        expected_url=_coexistence_webhook_url(),
        result="ok" if ok else "failed",
    )

    # ── 2. WABA-level webhook (whole WABA fallback) ─────────────────────
    # Why we always push BOTH:
    #
    #   • Channel webhook only covers the specific phone_number_id the
    #     API-key was minted for. If 360dialog rotates the phone_number_id
    #     during a coexistence re-bind (e.g. 100543193146977 →
    #     1061057720431678) the *old* channel webhook is orphaned and the
    #     *new* number has no webhook at all — until the WABA webhook
    #     fallback kicks in.
    #
    #   • In the merchant's 360dialog dashboard the "Waba Webhook" panel
    #     reads "N/A" by default because the hub does not auto-replicate
    #     the channel webhook there. The result is: inbound stops, the
    #     channel still shows green, our recent-webhook-events buffer
    #     reports `events_returned=0`.
    #
    # Setting both, with `override_all=True`, guarantees that EVERY
    # Cloud API number on this WABA — current and future — routes to
    # Nahla, even if the channel-level entry is stale.
    waba_result: Dict[str, Any] = {"skipped": True, "reason": "not_attempted"}
    waba_ok = False
    try:
        waba_result = await asyncio.wait_for(
            dialog360_set_waba_webhook(
                api_key=read_access_token(conn),
                url=_coexistence_webhook_url(),
                headers={"X-Nahla-Coexistence-Secret": secret},
                override_all=True,
                timeout=8.0,
            ),
            timeout=10.0,
        )
        waba_ok = _d360_operation_ok(waba_result)
    except Exception as exc:
        log_wa_direct_exception("coexistence auto-configure waba webhook", exc, tenant_id=body.tenant_id, tag="coexistence")
        waba_result = d360_safe_error_payload(exc, secrets=[read_access_token(conn)], operation="auto_configure_waba")
    _log_d360_verify(
        operation="auto_configure_waba",
        tenant_id=body.tenant_id,
        conn=conn,
        endpoint_used="POST /waba_webhook",
        response=waba_result,
        response_status=(waba_result or {}).get("status_code") if isinstance(waba_result, dict) else None,
        parsed_url=None,
        expected_url=_coexistence_webhook_url(),
        result="ok" if waba_ok else "failed",
        extra={"override_all": True},
    )

    meta = dict(conn.extra_metadata or {})
    meta["coexistence_internal_secret"] = secret
    coex = dict(meta.get("coexistence") or {})
    webhook = dict(coex.get("webhook") or {})
    webhook["channel_status"] = "verified" if ok else "failed"
    webhook["channel_last_configured_at"] = datetime.now(timezone.utc).isoformat()
    if not ok:
        webhook["channel_last_error"] = str((result or {}).get("error"))[:500]
    webhook["waba_status"] = "verified" if waba_ok else "failed"
    webhook["waba_last_configured_at"] = datetime.now(timezone.utc).isoformat()
    if not waba_ok:
        webhook["waba_last_error"] = str((waba_result or {}).get("error"))[:500]
    else:
        webhook.pop("waba_last_error", None)
    coex["webhook"] = webhook
    meta["coexistence"] = coex
    meta.setdefault("provider_details", {}).update({
        "webhook_url":           _coexistence_webhook_url(),
        "coexistence_url":       _coexistence_events_url(),
        "status_url":            _coexistence_status_url(),
        "internal_header_name":  "X-Nahla-Coexistence-Secret",
    })
    conn.extra_metadata = meta
    flag_modified(conn, "extra_metadata")
    if ok or waba_ok:
        # Either scope is enough for inbound delivery (WABA acts as a
        # fallback when the channel webhook is missing). Mark verified so
        # the dashboard reflects a working pipe even if one scope failed.
        conn.webhook_verified = True
    if not ok and not waba_ok:
        conn.last_error = (
            str((result or {}).get("error"))[:400]
            + " | waba=" + str((waba_result or {}).get("error"))[:100]
        )
    # Auto-heal: if the configure succeeded AND operational health is
    # green, promote the row out of any stale ``action_required``
    # state so the owner panel and merchant page agree.
    reconciled = _reconcile_connected_or_http(
        conn, tenant_id=body.tenant_id, source="admin_auto_configure", db=db,
    )
    if not reconciled:
        db.commit()

    audit(
        "admin_coexistence_auto_configure",
        admin=_admin.get("sub") if isinstance(_admin, dict) else None,
        tenant_id=body.tenant_id,
        ok=ok,
        waba_ok=waba_ok,
        reconciled=reconciled,
    )
    return {
        "tenant_id":      body.tenant_id,
        "ok":             ok and waba_ok,
        "channel_ok":     ok,
        "waba_ok":        waba_ok,
        "channel_result": d360_response_summary(result),
        "waba_result":    d360_response_summary(waba_result),
        "webhooks":       _coexistence_webhook_block(conn),
        "status_reconciled":     reconciled,
        "integration_complete":  _coexistence_integration_complete(conn),
    }


@router.get("/admin/coexistence/waba-webhook")
async def admin_coexistence_waba_webhook_read(
    tenant_id: int,
    db: Session = Depends(get_db),
    _admin: Dict[str, object] = Depends(require_admin),
):
    """Read the WABA-level webhook 360dialog currently has on file for this
    tenant's channel, plus the channel-level config and the local
    ``WhatsAppConnection.phone_number_id`` — operator-grade snapshot for
    diagnosing "channel green, WABA = N/A" coexistence drops.

    Response:

    ```
    {
      "tenant_id":             int,
      "expected_url":          "https://api.nahlah.ai/webhook/whatsapp/360dialog",
      "channel": {
        "url":                 str | null,
        "matches":             bool,
        "raw":                 <360dialog response>,
      },
      "waba": {
        "url":                 str | null,
        "matches":             bool,
        "waba_id":             str | null,
        "numbers_on_this_waba_count": int,
        "raw":                 <360dialog response>,
      },
      "local_connection": {
        "phone_number_id":     str | null,
        "waba_id":             str | null,
      },
      "phone_id_drift_with_360dialog": bool,
    }
    ```
    """
    from services.whatsapp_platform.service import dialog360_get_webhook_config  # noqa: PLC0415

    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="لا يوجد اتصال واتساب لهذا المتجر")
    if not conn.access_token:
        raise HTTPException(status_code=400, detail="مفتاح API لـ 360dialog غير مخزّن لهذا التاجر")

    expected_url = _coexistence_webhook_url()

    # Channel read
    try:
        chan_cfg = await asyncio.wait_for(
            dialog360_get_webhook_config(api_key=read_access_token(conn), timeout=5.0, expected_url=expected_url),
            timeout=6.0,
        )
    except Exception as exc:
        chan_cfg = d360_safe_error_payload(exc, secrets=[read_access_token(conn)], operation="waba_webhook_read_channel")
    chan_matches = bool((chan_cfg or {}).get("url_matches_expected")) if isinstance(chan_cfg, dict) else False
    chan_url = expected_url if chan_matches else ""

    _log_d360_verify(
        operation="waba_webhook_read_channel",
        tenant_id=tenant_id,
        conn=conn,
        endpoint_used="GET /v1/configs/webhook",
        response=chan_cfg,
        response_status=(chan_cfg or {}).get("status_code") if isinstance(chan_cfg, dict) else None,
        parsed_url=chan_url or None,
        expected_url=expected_url,
        result=(
            "verified_match" if chan_matches
            else ("remote_error" if isinstance(chan_cfg, dict) and "error" in chan_cfg
                  else ("url_mismatch" if chan_url else "no_remote_url"))
        ),
    )

    # WABA read
    try:
        waba_cfg = await asyncio.wait_for(
            dialog360_get_waba_webhook(
                api_key=read_access_token(conn),
                timeout=5.0,
                expected_url=expected_url,
                local_phone_number_id=str(getattr(conn, "phone_number_id", "") or "") or None,
            ),
            timeout=6.0,
        )
    except Exception as exc:
        waba_cfg = d360_safe_error_payload(exc, secrets=[read_access_token(conn)], operation="waba_webhook_read_waba")
    waba_matches = bool((waba_cfg or {}).get("url_matches_expected")) if isinstance(waba_cfg, dict) else False
    waba_url = expected_url if waba_matches else ""
    waba_id_remote_present = False
    numbers_on_waba: List[str] = []
    if isinstance(waba_cfg, dict):
        waba_id_remote_present = bool(waba_cfg.get("has_waba_id"))
        count = waba_cfg.get("numbers_count")
        if isinstance(count, int) and count > 0:
            numbers_on_waba = ["*"] * count

    _log_d360_verify(
        operation="waba_webhook_read_waba",
        tenant_id=tenant_id,
        conn=conn,
        endpoint_used="GET /waba_webhook",
        response=waba_cfg,
        response_status=(waba_cfg or {}).get("status_code") if isinstance(waba_cfg, dict) else None,
        parsed_url=waba_url or None,
        expected_url=expected_url,
        result=(
            "verified_match" if waba_matches
            else ("remote_error" if isinstance(waba_cfg, dict) and "error" in waba_cfg
                  else ("url_mismatch" if waba_url else "no_remote_url"))
        ),
        extra={
            "waba_id_remote_present": waba_id_remote_present if isinstance(waba_cfg, dict) else False,
            "numbers_on_this_waba_count": len(numbers_on_waba),
        },
    )

    local_phone = str(getattr(conn, "phone_number_id", "") or "") or None
    local_waba  = str(getattr(conn, "whatsapp_business_account_id", "") or "") or None
    phone_drift = bool(
        isinstance(waba_cfg, dict)
        and waba_cfg.get("numbers_count")
        and waba_cfg.get("local_phone_listed") is False
    )

    audit(
        "admin_coexistence_waba_webhook_read",
        admin=_admin.get("sub") if isinstance(_admin, dict) else None,
        tenant_id=tenant_id,
        channel_matches=chan_matches,
        waba_matches=waba_matches,
        phone_drift=phone_drift,
    )

    return {
        "tenant_id": tenant_id,
        "channel": {
            "matches": chan_matches,
            **d360_url_flags(chan_url, expected_url),
            **d360_response_summary(chan_cfg),
        },
        "waba": {
            "matches": waba_matches,
            **d360_url_flags(waba_url, expected_url),
            "waba_id_remote_present": waba_id_remote_present,
            "numbers_on_this_waba_count": len(numbers_on_waba),
            **d360_response_summary(waba_cfg),
        },
        "local_connection": {
            "phone_number_id": redact_graph_id(local_phone) if local_phone else None,
            "waba_id": redact_graph_id(local_waba) if local_waba else None,
        },
        "phone_id_drift_with_360dialog": phone_drift,
    }


# ── Admin: full diagnostic snapshot (tenant 52-class issues) ────────────────
# Returns the *complete* picture support needs when a tenant says:
#   "Auto Configure failed with Invalid api token, but the 360dialog
#    dashboard Test Webhook is green."
#
# Three independent signals are decoupled here so the operator can read
# them without conflating them:
#
#   1. token_check         — does the api_key on the WhatsAppConnection
#                            row authenticate against 360dialog's
#                            management API right now? (Probes
#                            GET /v1/configs/webhook AND GET /waba_webhook.)
#   2. registration        — what URL does 360dialog currently have on
#                            file for this tenant's channel + WABA,
#                            and does it match Nahla's expected URL?
#   3. inbound_evidence    — has Nahla actually received a real webhook
#                            on each of the three families (channel /
#                            coexistence / status), and how long ago?
#
# Plus duplicate detection: every other WhatsAppConnection row that
# shares the same phone_number_id, channel_id, or display phone — the
# #1 reason inbound silently routes to the wrong tenant after a
# re-onboarding.
#
# This endpoint is READ-ONLY. It never mutates the connection. Safe to
# call repeatedly while diagnosing.

@router.get("/admin/coexistence/diagnose")
async def admin_coexistence_diagnose(
    tenant_id: int,
    db: Session = Depends(get_db),
    _admin: Dict[str, object] = Depends(require_admin),
):
    """Comprehensive read-only snapshot for diagnosing 360dialog channel
    issues. Surfaces the three independent signals (token / registration
    / inbound evidence) plus duplicate detection across tenants.

    Response shape:

    ```
    {
      "tenant_id":  int,
      "connection": {
        "found":              bool,
        "connection_id":      int | null,
        "provider":           str,
        "connection_type":    str | null,
        "status":             str | null,
        "phone_number_id":    str | null,
        "waba_id":            str | null,
        "phone_number":       str | null,
        "channel_id":         str | null,
        "api_key_present":    bool,
        "api_key_present":    bool,
        "webhook_verified":   bool,
        "sending_enabled":    bool,
        "last_webhook_received_at":          ISO | null,
        "last_coexistence_received_at":      ISO | null,
        "last_status_received_at":           ISO | null,
        "last_error":         str | null,
      },
      "token_check": {
        "channel_endpoint_ok": bool,     # GET /v1/configs/webhook returned 2xx
        "channel_status_code": int | null,
        "channel_response":     dict,
        "waba_endpoint_ok":    bool,     # GET /waba_webhook returned 2xx
        "waba_status_code":    int | null,
        "waba_response":        dict,
        "verdict":             "valid" | "rejected" | "transport_error" | "no_token",
      },
      "registration": {
        "expected_url":        str,
        "channel_remote_url_present": bool,
        "channel_matches":     bool,
        "waba_remote_url_present": bool,
        "waba_matches":        bool,
        "waba_id_remote_present": bool,
        "numbers_on_this_waba_count": int,
        "phone_id_drift":      bool,
      },
      "inbound_evidence": {
        "channel_received_recently":      bool,
        "coexistence_received_recently":  bool,
        "status_received_recently":       bool,
        "any_inbound_ever":               bool,
        "freshness_seconds": {
          "channel":     int | null,
          "coexistence": int | null,
          "status":      int | null,
        }
      },
      "duplicates": {
        "by_phone_number_id":  [ {tenant_id, connection_id, status, provider, phone_number}, ... ],
        "by_channel_id":       [ ... ],
        "by_display_phone":    [ ... ],
        "has_duplicates":      bool,
      }
    }
    ```
    """
    from services.whatsapp_platform.service import dialog360_get_webhook_config  # noqa: PLC0415

    request_id = secrets.token_hex(6)
    now = datetime.now(timezone.utc)
    expected_url = _coexistence_webhook_url()

    conn = (
        db.query(WhatsAppConnection)
        .filter_by(tenant_id=tenant_id)
        .order_by(WhatsAppConnection.id.desc())
        .first()
    )

    # ── connection snapshot ─────────────────────────────────────────────
    connection_block: Dict[str, Any] = {"found": False, "connection_id": None}
    api_key = ""
    channel_id_local: Optional[str] = None
    phone_id_local: Optional[str] = None
    phone_display_local: Optional[str] = None
    if conn is not None:
        meta = dict(conn.extra_metadata or {})
        pd = dict(meta.get("provider_details") or {})
        channel_id_local = (pd.get("channel_id") or pd.get("channel") or None)
        api_key = str(getattr(conn, "access_token", "") or "")
        phone_id_local = getattr(conn, "phone_number_id", None)
        phone_display_local = getattr(conn, "phone_number", None)
        connection_block = {
            "found":                          True,
            "connection_id":                  conn.id,
            "provider":                       _wa_provider(conn),
            "connection_type":                getattr(conn, "connection_type", None),
            "status":                         getattr(conn, "status", None),
            "phone_number_id":                redact_graph_id(phone_id_local) if phone_id_local else None,
            "waba_id":                        redact_graph_id(getattr(conn, "whatsapp_business_account_id", None)) or None,
            "phone_number_present":           bool((phone_display_local or "").strip()),
            "channel_id":                     redact_graph_id(channel_id_local) if channel_id_local else None,
            "api_key_present":                bool(api_key.strip()),
            "webhook_verified":               bool(getattr(conn, "webhook_verified", False)),
            "sending_enabled":                bool(getattr(conn, "sending_enabled", False)),
            "last_webhook_received_at":       _dt_iso_utc(getattr(conn, "last_webhook_received_at", None)),
            "last_coexistence_received_at":   _dt_iso_utc(getattr(conn, "webhook_coexistence_received_at", None)),
            "last_status_received_at":        _dt_iso_utc(getattr(conn, "webhook_status_received_at", None)),
            "last_error":                     getattr(conn, "last_error", None),
        }

    # ── token_check: probe both endpoints with the stored key ───────────
    token_check: Dict[str, Any] = {
        "channel_endpoint_ok":  False,
        "channel_status_code":  None,
        "channel_response":     {},
        "waba_endpoint_ok":     False,
        "waba_status_code":     None,
        "waba_response":        {},
        "verdict":              "no_token",
    }
    channel_remote_url: Optional[str] = None
    waba_remote_url: Optional[str] = None
    waba_id_remote: Optional[str] = None
    numbers_on_waba: List[str] = []
    if api_key.strip():
        # GET /v1/configs/webhook
        try:
            chan_cfg = await asyncio.wait_for(
                dialog360_get_webhook_config(api_key=api_key, timeout=5.0),
                timeout=6.0,
            )
        except Exception as exc:
            chan_cfg = d360_safe_error_payload(exc, secrets=[api_key], operation="diagnose_channel_read")
        chan_err = isinstance(chan_cfg, dict) and "error" in chan_cfg
        chan_sc = (chan_cfg or {}).get("status_code") if isinstance(chan_cfg, dict) else None
        token_check["channel_endpoint_ok"]  = not chan_err
        token_check["channel_status_code"]  = chan_sc if isinstance(chan_sc, int) else None
        token_check["channel_response"] = d360_response_summary(chan_cfg)
        if isinstance(chan_cfg, dict):
            channel_remote_url = d360_extract_remote_url(chan_cfg) or None
        _log_d360_verify(
            operation="diagnose_channel_read",
            tenant_id=tenant_id,
            conn=conn,
            endpoint_used="GET /v1/configs/webhook",
            response=chan_cfg,
            response_status=token_check["channel_status_code"],
            parsed_url=channel_remote_url,
            expected_url=expected_url,
            result="remote_error" if chan_err else "ok",
            extra={"request_id": request_id},
        )

        # GET /waba_webhook
        try:
            waba_cfg = await asyncio.wait_for(
                dialog360_get_waba_webhook(api_key=api_key, timeout=5.0),
                timeout=6.0,
            )
        except Exception as exc:
            waba_cfg = d360_safe_error_payload(exc, secrets=[api_key], operation="diagnose_waba_read")
        waba_err = isinstance(waba_cfg, dict) and "error" in waba_cfg
        waba_sc = (waba_cfg or {}).get("status_code") if isinstance(waba_cfg, dict) else None
        token_check["waba_endpoint_ok"]  = not waba_err
        token_check["waba_status_code"]  = waba_sc if isinstance(waba_sc, int) else None
        token_check["waba_response"] = d360_response_summary(waba_cfg)
        if isinstance(waba_cfg, dict):
            waba_remote_url = str(waba_cfg.get("url") or "") or None
            wid_r = waba_cfg.get("waba_id")
            waba_id_remote = str(wid_r) if wid_r is not None else None
            nums = waba_cfg.get("numbers_on_this_waba")
            if isinstance(nums, list):
                numbers_on_waba = [str(n) for n in nums]
        _log_d360_verify(
            operation="diagnose_waba_read",
            tenant_id=tenant_id,
            conn=conn,
            endpoint_used="GET /waba_webhook",
            response=waba_cfg,
            response_status=token_check["waba_status_code"],
            parsed_url=waba_remote_url,
            expected_url=expected_url,
            result="remote_error" if waba_err else "ok",
            extra={"request_id": request_id},
        )

        # Verdict — distinguishes "key is valid but URL is stale" from
        # "key is rejected outright". 401/403 from EITHER endpoint while
        # the merchant's 360dialog dashboard webhook test shows green
        # is the textbook "stored API key was rotated and never
        # re-saved in Nahla" symptom.
        if token_check["channel_endpoint_ok"] or token_check["waba_endpoint_ok"]:
            token_check["verdict"] = "valid"
        else:
            both_auth = (
                token_check["channel_status_code"] in (401, 403)
                or token_check["waba_status_code"] in (401, 403)
            )
            token_check["verdict"] = "rejected" if both_auth else "transport_error"

    # ── registration block ──────────────────────────────────────────────
    chan_matches = bool(channel_remote_url) and channel_remote_url.rstrip("/") == expected_url.rstrip("/")
    waba_matches = bool(waba_remote_url) and waba_remote_url.rstrip("/") == expected_url.rstrip("/")
    phone_drift = bool(
        phone_id_local and numbers_on_waba and str(phone_id_local) not in numbers_on_waba
    )
    registration_block = {
        "expected_url_present": bool((expected_url or "").strip()),
        "channel_matches":      chan_matches,
        "waba_matches":         waba_matches,
        "channel_remote_url_present": bool(channel_remote_url),
        "waba_remote_url_present": bool(waba_remote_url),
        "waba_id_remote_present": bool(waba_id_remote),
        "numbers_on_this_waba_count": len(numbers_on_waba),
        "phone_id_drift":       phone_drift,
    }

    # ── inbound_evidence block ──────────────────────────────────────────
    def _ago(dt: Optional[datetime]) -> Optional[int]:
        if not dt:
            return None
        try:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0, int((now - dt).total_seconds()))
        except Exception:
            return None

    recent_window = 7 * 24 * 3600
    last_chan = getattr(conn, "last_webhook_received_at", None) if conn else None
    last_coex = getattr(conn, "webhook_coexistence_received_at", None) if conn else None
    last_stat = getattr(conn, "webhook_status_received_at", None) if conn else None
    chan_age = _ago(last_chan)
    coex_age = _ago(last_coex)
    stat_age = _ago(last_stat)
    inbound_block = {
        "channel_received_recently":     chan_age is not None and chan_age <= recent_window,
        "coexistence_received_recently": coex_age is not None and coex_age <= recent_window,
        "status_received_recently":      stat_age is not None and stat_age <= recent_window,
        "any_inbound_ever": any([last_chan, last_coex, last_stat]),
        "freshness_seconds": {
            "channel":     chan_age,
            "coexistence": coex_age,
            "status":      stat_age,
        },
    }

    # ── duplicates block ────────────────────────────────────────────────
    # Three independent searches — each surfaces a different drift mode
    # (rotated phone_id, partner re-binding, same display phone on two
    # accounts).
    def _row_to_dup(row: WhatsAppConnection) -> Dict[str, Any]:
        m = dict(row.extra_metadata or {})
        pd_ = dict(m.get("provider_details") or {})
        return {
            "tenant_id":        row.tenant_id,
            "connection_id":    row.id,
            "status":           getattr(row, "status", None),
            "provider":         _wa_provider(row),
            "connection_type":  getattr(row, "connection_type", None),
            "phone_number_id":  redact_graph_id(getattr(row, "phone_number_id", None)) or None,
            "phone_number_present": bool((getattr(row, "phone_number", None) or "").strip()),
            "channel_id":       redact_graph_id(pd_.get("channel_id") or pd_.get("channel")) or None,
            "is_this_tenant":   row.tenant_id == tenant_id,
        }

    dup_by_phone_id: List[Dict[str, Any]] = []
    if phone_id_local:
        rows = (
            db.query(WhatsAppConnection)
            .filter(WhatsAppConnection.phone_number_id == phone_id_local)
            .all()
        )
        dup_by_phone_id = [_row_to_dup(r) for r in rows]

    dup_by_display: List[Dict[str, Any]] = []
    if phone_display_local:
        rows = (
            db.query(WhatsAppConnection)
            .filter(WhatsAppConnection.phone_number == phone_display_local)
            .all()
        )
        dup_by_display = [_row_to_dup(r) for r in rows]

    # channel_id lives in extra_metadata['provider_details'] so we need a
    # JSONB path query. Use a sql fragment so this stays portable across
    # Postgres minor versions.
    dup_by_channel_id: List[Dict[str, Any]] = []
    if channel_id_local:
        try:
            from sqlalchemy import text as _text  # noqa: PLC0415
            sql = _text(
                "SELECT id FROM whatsapp_connections "
                "WHERE extra_metadata #>> '{provider_details,channel_id}' = :cid "
                "   OR extra_metadata #>> '{provider_details,channel}'    = :cid"
            )
            rows = db.execute(sql, {"cid": channel_id_local}).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                wa_rows = (
                    db.query(WhatsAppConnection)
                    .filter(WhatsAppConnection.id.in_(ids))
                    .all()
                )
                dup_by_channel_id = [_row_to_dup(r) for r in wa_rows]
        except Exception as exc:
            log_wa_direct_exception("coexistence diagnose channel duplicate", exc, tenant_id=tenant_id, tag="coexistence")

    has_duplicates = any(
        len([d for d in lst if not d["is_this_tenant"]]) > 0
        for lst in (dup_by_phone_id, dup_by_channel_id, dup_by_display)
    )
    duplicates_block = {
        "by_phone_number_id": dup_by_phone_id,
        "by_channel_id":      dup_by_channel_id,
        "by_display_phone":   dup_by_display,
        "has_duplicates":     has_duplicates,
    }

    # Opportunistic auto-heal: if all operational signals are green but
    # ``conn.status`` is stale, promote it now. Running diagnose is the
    # canonical "tell me the truth" action, so it is also the right place
    # to bring the stored truth into agreement with reality.
    reconciled = _reconcile_connected_or_http(
        conn, tenant_id=tenant_id, source="admin_diagnose", db=db,
    )

    audit(
        "admin_coexistence_diagnose",
        admin=_admin.get("sub") if isinstance(_admin, dict) else None,
        tenant_id=tenant_id,
        request_id=request_id,
        token_verdict=token_check["verdict"],
        channel_matches=chan_matches,
        waba_matches=waba_matches,
        has_duplicates=has_duplicates,
        status_reconciled=reconciled,
    )

    # Refresh connection_block.status after possible auto-heal.
    if reconciled and conn is not None:
        connection_block["status"] = conn.status
        connection_block["sending_enabled"] = bool(conn.sending_enabled)

    return {
        "tenant_id":            tenant_id,
        "request_id":           request_id,
        "connection":           connection_block,
        "token_check":          token_check,
        "registration":         registration_block,
        "inbound_evidence":     inbound_block,
        "duplicates":           duplicates_block,
        "webhooks":             _coexistence_webhook_block(conn),
        "status_reconciled":    reconciled,
        "integration_complete": _coexistence_integration_complete(conn),
    }


@router.get("/usage")
async def get_usage(
    request:  Request,
    db:       Session = Depends(get_db),
    breakdown: bool   = False,
    _scope:   dict    = Depends(require_merchant_scope),
):
    """
    Return this month's WhatsApp conversation usage for the tenant.

    Also auto-refreshes Meta tier data when stale (configurable via
    ``NAHLA_META_TIER_STALE_HOURS``, default 6 h) or missing.
    """
    from core.wa_usage import get_current_period_usage, get_daily_breakdown  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)

    # ── Opportunistic Meta tier refresh ──────────────────────────────────
    await _maybe_refresh_meta_tier(db, tenant_id)

    data = get_current_period_usage(db, tenant_id)

    if breakdown:
        data["daily_breakdown"] = get_daily_breakdown(
            db, tenant_id, data["year"], data["month"]
        )

    return data


@router.get("/usage/audit")
async def get_usage_audit(
    request: Request,
    db:      Session = Depends(get_db),
    _scope:  dict    = Depends(require_merchant_scope),
):
    """
    Debug/audit snapshot for conversation usage semantics.

    Surfaces period bounds, billable-window counts, and message-event counts
    so operators can explain dashboard numbers without tenant-specific logic.
    """
    from core.wa_usage import get_usage_audit_snapshot  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    return get_usage_audit_snapshot(db, tenant_id)


# NOTE: the canonical stale horizon lives in ``core.wa_usage``. We keep a local
# reference here so the legacy `_maybe_refresh_meta_tier` path (which runs
# inline on /whatsapp/usage requests) stays in lockstep with the value the
# UI reads from the response. Override via ``NAHLA_META_TIER_STALE_HOURS``.
import os as _os  # noqa: PLC0415
_META_TIER_STALE_HOURS = int(_os.environ.get("NAHLA_META_TIER_STALE_HOURS", "6"))


async def _maybe_refresh_meta_tier(db: "Session", tenant_id: int) -> None:
    """Fetch Meta tier from Graph API if cached data is missing or stale."""
    import logging as _log  # noqa: PLC0415
    _logger = _log.getLogger("nahla.whatsapp.tier")
    try:
        from datetime import datetime, timedelta, timezone as tz  # noqa: PLC0415
        conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
        if not conn or conn.status != "connected":
            return

        now = datetime.now(tz.utc)
        last = conn.meta_tier_updated_at
        if last and last.tzinfo is None:
            last = last.replace(tzinfo=tz.utc)
        if last and (now - last).total_seconds() < _META_TIER_STALE_HOURS * 3600:
            return

        ctx = get_token_context(conn)
        if not ctx.token:
            return

        from services.whatsapp_platform.service import fetch_meta_phone_tier  # noqa: PLC0415
        tier_data = await fetch_meta_phone_tier(conn, ctx, tenant_id=tenant_id)
        if not tier_data:
            return

        if tier_data.get("messaging_limit"):
            conn.meta_messaging_limit = tier_data["messaging_limit"]
        if tier_data.get("quality_rating"):
            conn.meta_quality_rating = tier_data["quality_rating"]
        conn.meta_tier_updated_at = now
        db.commit()
        _logger.info("[WA tier] refreshed tenant=%s limit=%s quality=%s",
                     tenant_id, conn.meta_messaging_limit, conn.meta_quality_rating)
    except Exception as exc:
        _log.getLogger("nahla.whatsapp.tier").warning(
            "[WA tier] auto-refresh failed tenant=%s: %s", tenant_id, exc,
        )


@router.post("/refresh-meta-tier")
async def refresh_meta_tier(
    request: Request,
    db:      Session = Depends(get_db),
    _scope:  dict    = Depends(require_merchant_scope),
):
    """Fetch the current messaging_limit tier and quality_rating from Meta.

    Returns both the resolved value AND a ``diagnostics`` array so the
    UI can show the raw provider response when the merchant wants to
    verify what's actually being returned. This is provider-agnostic —
    works for direct Meta (single call) and for 360dialog (multi-path
    probe) without any branching at the UI layer.
    """
    from services.whatsapp_platform.service import fetch_meta_phone_tier  # noqa: PLC0415

    tenant_id = resolve_tenant_id(request)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
    if not conn or conn.status != "connected":
        return {"updated": False, "reason": "not_connected"}

    ctx = get_token_context(conn)
    if not ctx.token:
        return {"updated": False, "reason": "no_token"}

    tier_data = await fetch_meta_phone_tier(conn, ctx, tenant_id=tenant_id)
    diagnostics = (tier_data or {}).get("_diagnostics") or []
    fresh_tier   = (tier_data or {}).get("messaging_limit")
    fresh_qual   = (tier_data or {}).get("quality_rating")

    # We always commit a fresh ``meta_tier_updated_at`` even when the
    # provider returned nothing — that way the UI knows the LAST attempt
    # time (so "قبل لحظات" is honest) even when the value didn't change.
    from datetime import datetime, timezone as tz  # noqa: PLC0415
    updated = False
    if fresh_tier:
        conn.meta_messaging_limit = fresh_tier
        updated = True
    if fresh_qual:
        conn.meta_quality_rating = fresh_qual
        updated = True
    conn.meta_tier_updated_at = datetime.now(tz.utc)
    db.commit()

    return {
        "updated":          updated,
        "messaging_limit":  conn.meta_messaging_limit,
        "quality_rating":   conn.meta_quality_rating,
        "provider":         (getattr(conn, "provider", None) or "meta"),
        # Surfacing the diagnostics empowers merchants to verify
        # "is this number coming from the provider or is it cached?"
        # without us needing to ship a separate admin endpoint.
        "diagnostics":      diagnostics,
        "reason":           None if updated else "provider_returned_no_tier",
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

    if _wa_provider(conn) == WHATSAPP_PROVIDER_360DIALOG:
        token_ctx = get_token_context(conn)
        checks = {
            "has_connection": conn.status in ("request_submitted", "pending_activation", "action_required", "connected"),
            "token_present": bool(token_ctx.token),
            "token_valid": bool(token_ctx.token),
            "webhook_verified": bool(conn.webhook_verified),
            "sending_enabled": bool(conn.sending_enabled),
        }
        healthy = checks["has_connection"] and checks["token_present"] and checks["webhook_verified"] and checks["sending_enabled"]
        return {
            "healthy": healthy,
            "status": conn.status,
            "phone_number": conn.phone_number,
            "checks": checks,
            "last_verified": conn.last_verified_at.isoformat() if conn.last_verified_at else None,
            "last_error": conn.last_error,
            "provider": _wa_provider(conn),
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
        "[PhoneNorm] cc=%s valid=%s",
        cc,
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
    log_wa_direct_stage(
        stage="request-otp start",
        tenant_id=tenant_id,
        waba_id=WA_BUSINESS_ACCOUNT_ID,
    )

    # Validate after normalization — reject early with a clear Arabic message
    phone_err = _validate_phone(cc, national)
    if phone_err:
        log_wa_direct_stage(
            stage="request-otp phone validation failed",
            tenant_id=tenant_id,
            success=False,
            level="warning",
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

    log_wa_direct_stage(
        stage="request-otp meta request",
        tenant_id=tenant_id,
        waba_id=WA_BUSINESS_ACCOUNT_ID,
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
        log_wa_direct_stage(
            stage="pending resume validate",
            tenant_id=tenant_id,
            phone_number_id=stored_phone_id,
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
                log_wa_direct_graph_result(
                    stage="pending resume stale id",
                    tenant_id=tenant_id,
                    response=chk_data,
                    phone_number_id=stored_phone_id,
                )
                # Clear stale ID so the add-step runs below
                existing_conn.phone_number_id = None
                db.commit()
            else:
                id_valid = True
                log_wa_direct_stage(stage="pending resume id valid", tenant_id=tenant_id, success=True, phone_number_id=stored_phone_id)
        except Exception as exc:
            log_wa_direct_exception("pending resume validate", exc, tenant_id=tenant_id, secrets=[token_ctx.token if token_ctx else None, stored_phone_id])

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
                    log_wa_direct_graph_result(stage="pending resume request otp", tenant_id=tenant_id, response=otp_data, phone_number_id=phone_number_id)

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
                log_wa_direct_exception("pending resume request otp", exc, tenant_id=tenant_id, secrets=[token_ctx.token if token_ctx else None, phone_number_id])
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
                log_wa_direct_stage(
                    stage="waba phone-list match",
                    tenant_id=tenant_id,
                    success=True,
                    phone_number_id=phone_number_id,
                    waba_id=WA_BUSINESS_ACCOUNT_ID,
                )
                break
    except Exception as lookup_exc:
        log_wa_direct_exception("waba phone-list lookup", lookup_exc, tenant_id=tenant_id, secrets=[token_ctx.token if token_ctx else None, WA_BUSINESS_ACCOUNT_ID])

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
            log_wa_direct_exception("add phone", exc, tenant_id=tenant_id, level="error", secrets=[token_ctx.token if token_ctx else None, WA_BUSINESS_ACCOUNT_ID, national, bare_number])
            raise HTTPException(status_code=503, detail="خطأ في الاتصال بـ Meta")

        if "error" in add_data:
            err      = add_data["error"]
            code     = err.get("code", 0)
            subcode  = err.get("error_subcode", 0)
            msg      = err.get("message", "")
            user_msg = err.get("error_user_msg", "") or err.get("error_user_title", "")
            log_wa_direct_graph_result(stage="add phone", tenant_id=tenant_id, response=add_data, waba_id=WA_BUSINESS_ACCOUNT_ID)
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
    conn.provider        = WHATSAPP_PROVIDER_META
    conn.connection_type = WHATSAPP_CONNECTION_TYPE_DIRECT
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
        log_wa_direct_exception("request otp", exc, tenant_id=tenant_id, level="error", secrets=[token_ctx.token if token_ctx else None, phone_number_id])
        raise HTTPException(status_code=503, detail=_UX_MESSAGES[META_UNKNOWN_ERROR])

    if "error" in otp_data:
        err      = otp_data["error"]
        err_code = err.get("code", 0)
        err_sub  = err.get("error_subcode", 0)
        err_msg  = err.get("message", "")
        log_wa_direct_graph_result(stage="request otp", tenant_id=tenant_id, response=otp_data, phone_number_id=phone_number_id)
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
            log_wa_direct_stage(stage="request otp failed resume step2", tenant_id=tenant_id, success=False, phone_number_id=phone_number_id)
            return {
                "status":          META_CODE_SENT,
                "code":            META_CODE_SENT,
                "phone_number_id": phone_number_id,
                "message":         "أدخل رمز التحقق الذي وصلك، أو انتظر دقائق قبل طلب رمز جديد.",
                "already_sent":    True,
            }
        raise HTTPException(status_code=400, detail=ux, headers={"X-Nahla-Error-Code": ic})

    log_wa_direct_stage(stage="request otp sent", tenant_id=tenant_id, success=True, phone_number_id=phone_number_id)

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
        log_wa_direct_exception("resend otp", exc, tenant_id=tenant_id, level="error", tag="WA Resend", secrets=[token_ctx.token if token_ctx else None, phone_number_id])
        raise HTTPException(status_code=503, detail="خطأ في الاتصال بـ Meta")

    if "error" in data:
        err = data["error"]
        err_code    = err.get("code", 0)
        err_subcode = err.get("error_subcode", 0)
        log_wa_direct_graph_result(stage="resend otp", tenant_id=tenant_id, response=data, phone_number_id=phone_number_id, tag="WA Resend")

        # ── Stale / invalid phone_number_id (Meta error 100, subcode 33) ──────
        # The stored ID no longer exists on Meta — try to find the fresh one or reset.
        if err_code == 100 or err_subcode == 33:
            from core.config import WA_BUSINESS_ACCOUNT_ID  # noqa: PLC0415
            fresh_id = ""
            try:
                stored_phone = (conn.phone_number or "").replace("+","").replace(" ","").replace("-","")
                log_wa_direct_stage(stage="resend stale id lookup", tenant_id=tenant_id, tag="WA Resend")
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
                        log_wa_direct_stage(stage="resend fresh id found", tenant_id=tenant_id, success=True, phone_number_id=fresh_id, tag="WA Resend")
                        break
            except Exception as lookup_exc:
                log_wa_direct_exception("resend waba lookup", lookup_exc, tenant_id=tenant_id, level="error", tag="WA Resend", secrets=[token_ctx.token if token_ctx else None])

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
                    log_wa_direct_graph_result(stage="resend retry", tenant_id=tenant_id, response=d2, phone_number_id=fresh_id, tag="WA Resend")
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
                    log_wa_direct_exception("resend retry", retry_exc, tenant_id=tenant_id, level="error", tag="WA Resend", secrets=[token_ctx.token if token_ctx else None, fresh_id])

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
    from services.meta_coexistence import is_coexistence_mode  # noqa: PLC0415
    if conn_for_token is not None and is_coexistence_mode(conn_for_token):
        raise HTTPException(
            status_code=400,
            detail="هذا الرقم مربوط بمسار واتساب الأعمال على الجوال. لا تستخدم تفعيل OTP المباشر.",
        )
    token_ctx = await get_token_for_operation(
        db,
        conn_for_token,
        tenant_id=tenant_id,
        operation="phone_verify",
        prefer_platform=True,
    )
    headers   = {
        "Authorization": f"Bearer {token_ctx.token}",
        "Content-Type":  "application/json",
    }

    log_wa_direct_stage(stage="verify start", tenant_id=tenant_id, phone_number_id=phone_id, waba_id=WA_BUSINESS_ACCOUNT_ID, success=bool(token_ctx.token))

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
        log_wa_direct_graph_result(stage="verify pre-check", tenant_id=tenant_id, response=chk_data, http_status=chk_status, phone_number_id=phone_id, tag="WA verify")
        if "error" in chk_data:
            chk_err = chk_data["error"]
            log_wa_direct_graph_result(stage="verify pre-check failed", tenant_id=tenant_id, response=chk_data, phone_number_id=phone_id, tag="WA verify")
            # ── Fallback: find correct phone_number_id from WABA phone list ──
            fresh_id = ""
            try:
                conn_rec = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
                stored_phone = (conn_rec.phone_number or "").replace("+", "").replace(" ", "").replace("-", "")
                log_wa_direct_stage(stage="verify fallback lookup", tenant_id=tenant_id, waba_id=WA_BUSINESS_ACCOUNT_ID, tag="WA verify")
                async with httpx.AsyncClient(timeout=15) as client:
                    list_resp = await client.get(
                        f"{graph}/{WA_BUSINESS_ACCOUNT_ID}/phone_numbers",
                        headers=headers,
                        params={"fields": "id,display_phone_number,code_verification_status"},
                    )
                    list_data = list_resp.json()
                log_wa_direct_graph_result(stage="verify waba phone-list", tenant_id=tenant_id, response=list_data, waba_id=WA_BUSINESS_ACCOUNT_ID, tag="WA verify")
                for entry in list_data.get("data", []):
                    dp = entry.get("display_phone_number", "").replace(" ", "").replace("-", "").replace("+", "")
                    if stored_phone and (stored_phone in dp or dp in stored_phone):
                        fresh_id = entry["id"]
                        log_wa_direct_stage(stage="verify fallback fresh id", tenant_id=tenant_id, success=True, phone_number_id=fresh_id, tag="WA verify")
                        break
            except Exception as lookup_exc:
                log_wa_direct_exception("verify fallback lookup", lookup_exc, tenant_id=tenant_id, level="error", tag="WA verify", secrets=[token_ctx.token, phone_id])

            if fresh_id:
                log_wa_direct_stage(stage="verify replace stale id", tenant_id=tenant_id, phone_number_id=fresh_id, tag="WA verify")
                phone_id = fresh_id
                # Update DB with fresh ID
                conn_u = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
                if conn_u:
                    conn_u.phone_number_id = fresh_id
                    db.commit()
            else:
                log_wa_direct_stage(stage="verify no fresh id", tenant_id=tenant_id, success=False, phone_number_id=phone_id, waba_id=WA_BUSINESS_ACCOUNT_ID, level="error", tag="WA verify")
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
        log_wa_direct_exception("verify pre-check network", pre_exc, tenant_id=tenant_id, tag="WA verify", secrets=[token_ctx.token, phone_id, body.code])

    # ── Step A: verify_code ───────────────────────────────────────────────────
    verify_endpoint = f"{graph}/{phone_id}/verify_code"
    verify_payload  = {"code": body.code}
    log_wa_direct_stage(stage="verify_code request", tenant_id=tenant_id, phone_number_id=phone_id, tag="WA verify")
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
        log_wa_direct_exception("verify_code", exc, tenant_id=tenant_id, level="error", tag="WA verify", secrets=[token_ctx.token, phone_id, body.code])
        raise HTTPException(status_code=503, detail=_UX_MESSAGES[META_UNKNOWN_ERROR])

    log_wa_direct_graph_result(stage="verify_code", tenant_id=tenant_id, response=verify_data, http_status=verify_status, phone_number_id=phone_id, tag="WA verify")

    if "error" in verify_data:
        err  = verify_data["error"]
        code = err.get("code", 0)
        log_wa_direct_graph_result(stage="submit otp", tenant_id=tenant_id, response=verify_data, phone_number_id=phone_id)
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

    log_wa_direct_stage(stage="register request", tenant_id=tenant_id, phone_number_id=phone_id)
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
        log_wa_direct_exception("register", exc, tenant_id=tenant_id, level="error", secrets=[token_ctx.token, phone_id])
        register_data = {}
        reg_status    = 0

    log_wa_direct_graph_result(stage="register", tenant_id=tenant_id, response=register_data, http_status=reg_status, phone_number_id=phone_id)

    if "error" in register_data:
        reg_err = register_data["error"]
        reg_code = reg_err.get("code", 0)
        # Error 80007 = already registered — that is acceptable
        if reg_code != 80007:
            log_wa_direct_graph_result(stage="register failed", tenant_id=tenant_id, response=register_data, phone_number_id=phone_id)
            ic, ux = _normalize_meta_error(reg_code, reg_err.get("message", ""), reg_err.get("error_subcode", 0))
            raise HTTPException(
                status_code=400,
                detail=f"فشل تفعيل الرقم في Meta: {ux}",
                headers={"X-Nahla-Error-Code": ic},
            )

    # ── Step C: fetch real phone status from Meta ─────────────────────────────
    log_wa_direct_stage(stage="fetch phone status request", tenant_id=tenant_id, phone_number_id=phone_id)
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

        log_wa_direct_graph_result(stage="fetch phone status", tenant_id=tenant_id, response=info, http_status=info_status, phone_number_id=phone_id)

        phone_number = info.get("display_phone_number", "")
        display_name = info.get("verified_name", "")
        meta_status  = info.get("code_verification_status", "")
    except Exception as exc:
        log_wa_direct_exception("fetch phone status", exc, tenant_id=tenant_id, level="error", secrets=[token_ctx.token, phone_id])

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

    conn.status                       = (
        "activation_pending" if _sync_state_ready(sync_state)
        else _non_success_db_status(sync_state)
    )
    conn.phone_number_id              = phone_id
    conn.phone_number                 = phone_number
    conn.business_display_name        = display_name
    conn.whatsapp_business_account_id = WA_BUSINESS_ACCOUNT_ID
    conn.connection_type              = "direct"
    conn.provider                     = WHATSAPP_PROVIDER_META
    conn.access_token                 = None
    conn.token_type                   = None
    conn.token_expires_at             = None
    conn.webhook_verified             = bool(sync_state.get("connected"))
    conn.sending_enabled              = bool(sync_state.get("sending_enabled"))
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

    if sync_state.get("connected"):
        _finalize_connected_or_http(db, conn)
    else:
        db.commit()

    log_wa_direct_stage(stage="verify finalized", tenant_id=tenant_id, success=bool(conn.sending_enabled), phone_number_id=phone_id)

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
            "provider": WHATSAPP_PROVIDER_META,
            "provider_label": "meta",
            "merchant_channel_label": None,
            "connection_type": None,
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
        "whatsapp_ai_live_since": (
            conn.whatsapp_ai_live_since.isoformat()
            if getattr(conn, "whatsapp_ai_live_since", None) else None
        ),
        "whatsapp_history_sync_status": getattr(conn, "whatsapp_history_sync_status", None) or "completed",
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
        "provider":              _wa_provider(conn),
        "provider_label":        _provider_label(conn),
        "merchant_channel_label": _merchant_channel_label(conn),
        "connection_type":       conn.connection_type,
        "connection_mode":       meta.get("connection_mode"),
        "otp_required":          bool(
            str(meta.get("connection_mode") or "").strip().lower() != "coexistence"
            and conn.status == "otp_pending"
        ),
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
    try:
        tenant_id = resolve_tenant_id(request)
        conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).first()
        if conn and _wa_provider(conn) == WHATSAPP_PROVIDER_360DIALOG:
            payload = _coexistence_status_payload(conn)
            payload["coexistence_available"] = _coexistence_enabled_for_tenant(db, tenant_id)
            return payload
        if conn and conn.connection_type == WHATSAPP_CONNECTION_TYPE_ASSISTED:
            payload = _assisted_status_payload(conn)
            payload["coexistence_available"] = _coexistence_enabled_for_tenant(db, tenant_id)
            return payload
        if conn and conn.connection_type == "embedded" and conn.phone_number_id:
            try:
                from routers.whatsapp_embedded import sync_embedded_connection_from_meta  # noqa: PLC0415
                payload = await sync_embedded_connection_from_meta(
                    conn, db, attempt_register=False, allow_demotion=False,
                )
                payload["coexistence_available"] = _coexistence_enabled_for_tenant(db, tenant_id)
                return payload
            except HTTPException:
                raise
            except Exception as exc:
                log_wa_direct_exception("whatsapp status embedded sync", exc, tenant_id=tenant_id)
        payload = _build_wa_status(conn)
        payload["coexistence_available"] = _coexistence_enabled_for_tenant(db, tenant_id)
        return payload
    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        log_wa_direct_exception("whatsapp status unhandled", exc, tenant_id=getattr(request.state, "tenant_id", None), level="error", tag="whatsapp/status")
        from fastapi.responses import JSONResponse as _JSONResponse
        return _JSONResponse(
            status_code=500,
            content={"detail": "خطأ داخلي في خدمة واتساب", "error": str(exc), "type": type(exc).__name__},
        )


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

    log_wa_direct_stage(stage="refresh fetch phone status", tenant_id=tenant_id, phone_number_id=phone_id, tag="WA refresh")

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
        log_wa_direct_exception("refresh meta api", exc, tenant_id=tenant_id, level="error", tag="WA refresh", secrets=[token_ctx.token if token_ctx else None, phone_id])
        return {"updated": False, "message": "تعذّر الاتصال بـ Meta. حاول مرة أخرى."}

    log_wa_direct_graph_result(stage="refresh fetch phone status", tenant_id=tenant_id, response=data, http_status=resp_status, phone_number_id=phone_id, tag="WA refresh")

    if "error" in data:
        err = data["error"]
        log_wa_direct_graph_result(stage="refresh meta error", tenant_id=tenant_id, response=data, http_status=resp_status, phone_number_id=phone_id, tag="WA refresh")
        return _safe_meta_refresh_payload(data, resp_status)

    verification_status = data.get("code_verification_status", "")
    display_phone       = data.get("display_phone_number", conn.phone_number or "")
    verified_name       = data.get("verified_name", conn.business_display_name or "")

    log_wa_direct_stage(stage="refresh status parsed", tenant_id=tenant_id, success=True, phone_number_id=phone_id, tag="WA refresh")

    # If NOT_VERIFIED: attempt register to re-activate, except Meta Coexistence.
    from services.meta_coexistence import is_coexistence_mode, smb_syncs_accepted  # noqa: PLC0415
    coexistence = is_coexistence_mode(conn)
    if verification_status != "VERIFIED" and not coexistence:
        from routers.whatsapp_embedded import _resolve_register_pin as _rrp  # noqa: PLC0415
        _refresh_pin = _rrp(conn) if conn else "000000"
        if conn:
            db.commit()

        log_wa_direct_stage(stage="refresh re-register request", tenant_id=tenant_id, phone_number_id=phone_id, tag="WA refresh")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                reg = await client.post(
                    f"{graph}/{phone_id}/register",
                    headers=headers,
                    json={"messaging_product": "whatsapp", "pin": _refresh_pin},
                )
                reg_data = reg.json()
            log_wa_direct_graph_result(stage="refresh re-register", tenant_id=tenant_id, response=reg_data, http_status=reg.status_code, phone_number_id=phone_id, tag="WA refresh")
        except Exception as exc:
            log_wa_direct_exception("refresh re-register", exc, tenant_id=tenant_id, level="error", tag="WA refresh", secrets=[token_ctx.token if token_ctx else None, phone_id])
            reg_data = {}

    from routers.whatsapp_embedded import _project_phone_sync_state as _bps  # noqa: PLC0415
    sync_state = _bps(conn, data if isinstance(data, dict) else {})

    if sync_state.get("connected"):
        if coexistence and not smb_syncs_accepted(dict(conn.extra_metadata or {})):
            conn.status = "configuring"
            conn.sending_enabled = False
            db.commit()
            return {
                "updated": False,
                "message": "تم الربط جزئياً. جارٍ تهيئة مزامنة تطبيق واتساب الأعمال.",
                **_build_wa_status(conn),
            }
        conn.provider              = WHATSAPP_PROVIDER_META
        conn.sending_enabled       = True
        conn.phone_number          = display_phone
        conn.business_display_name = verified_name or conn.business_display_name
        conn.last_error            = None
        conn.extra_metadata        = {
            **(conn.extra_metadata or {}),
            "meta_code_verification_status": sync_state.get("verification_status"),
            "meta_name_status": sync_state.get("name_status"),
            "meta_phone_status": sync_state.get("meta_phone_status"),
            "meta_quality_rating": sync_state.get("quality_rating"),
            "embedded_status_message": sync_state.get("message"),
        }
        _finalize_connected_or_http(db, conn)
        logger.info("[WA refresh] CONNECTED tenant=%s", tenant_id)
        return {
            "updated": True,
            "message": "✅ تم التحقق من الرقم في Meta وتم تحديث حالة الاتصال.",
            **_build_wa_status(conn),
        }

    # Not verified yet — return full Meta response for diagnosis
    conn.status          = _non_success_db_status(sync_state)
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
        log_wa_direct_exception("save profile", exc, tenant_id=tenant_id, tag="WA Direct", secrets=[token_ctx.token if token_ctx else None])
        raise HTTPException(status_code=503, detail="خطأ في حفظ الملف التجاري")

    if "error" in data:
        err = data["error"]
        log_wa_direct_graph_result(stage="save profile", tenant_id=tenant_id, response=data, tag="WA Direct")
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
    logger.debug("[MetaNormalizer] error code=%s subcode=%s", code, subcode)

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
    logger.info("[MetaNormalizer] unmapped error code=%s subcode=%s", code, subcode)
    return META_UNKNOWN_ERROR, _UX_MESSAGES[META_UNKNOWN_ERROR]


def _meta_error_to_arabic(code: int, message: str, subcode: int = 0, user_msg: str = "") -> str:
    """Thin wrapper kept for backwards compatibility — returns only the UX message."""
    _, msg = _normalize_meta_error(code, message, subcode, user_msg)
    return msg
