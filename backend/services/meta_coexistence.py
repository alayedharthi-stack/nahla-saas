"""Meta WhatsApp Business App Coexistence (Cloud API) helpers.

Locked contract:
  provider = meta
  connection_type = embedded
  extra_metadata.connection_mode = coexistence
  never POST /{phone_number_id}/register on this path
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

logger = logging.getLogger("nahla.meta_coexistence")

COEXISTENCE_FINISH_EVENT = "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"
UNSAFE_FINISH_EVENTS = frozenset({"FINISH_OBO_MIGRATION"})
CONNECTION_MODE = "coexistence"
_CONFIGURING_MESSAGE = "تم الربط جزئياً. جارٍ تهيئة مزامنة تطبيق واتساب الأعمال."
_READY_MESSAGE = (
    "تم ربط رقم واتساب الأعمال على الجوال مع نحلة. "
    "أبقِ التطبيق مفتوحاً لإكمال المزامنة."
)

DEFAULT_WEBHOOK_FIELDS: List[str] = [
    "messages",
    "messaging_postbacks",
    "message_echoes",
]
COEXISTENCE_WEBHOOK_FIELDS: List[str] = DEFAULT_WEBHOOK_FIELDS + [
    "history",
    "smb_app_state_sync",
    "smb_message_echoes",
    "account_update",
]
SMB_SYNC_TYPES: Tuple[str, str] = ("smb_app_state_sync", "history")
SMB_SYNC_DEADLINE = timedelta(hours=24)

COEXISTENCE_NOT_ELIGIBLE = "coexistence_not_eligible"
STANDARD_CLOUD_API_AVAILABLE = "standard_cloud_api_available"
RECOMMENDED_MODE_CLOUD_API = "cloud_api"
RECOMMENDED_MODE_COEXISTENCE = "coexistence"

# Semantic wait keys owned by Coexistence. Unrelated OAuth / phone / WABA
# history must not be wiped when a merchant confirms STANDARD CLOUD API.
_OBSOLETE_COEXISTENCE_KEYS = (
    "connection_mode",
    "smb_sync",
    "smb_sync_deadline_at",
    "coexistence_onboarded_at",
    "finish_event",
    "client_phone_hint",
    "readiness_phone_number_id",
    "readiness_waba_id",
)
_COEXISTENCE_WAIT_KEYS = (
    "smb_sync",
    "smb_sync_deadline_at",
    "coexistence_onboarded_at",
    "readiness_phone_number_id",
    "readiness_waba_id",
)
_COEXISTENCE_FAILURE_CODES = frozenset({
    "not_eligible",
    "smb_sync_deadline",
    "phone_hint_mismatch",
    "missing_phone",
})
_COEXISTENCE_PROJECTION_REASONS = frozenset({
    "smb_incomplete",
    "webhook_unverified",
    "identity_mismatch",
    "ready",
})


def is_coexistence_mode(conn: Any) -> bool:
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    return str(meta.get("connection_mode") or "").strip().lower() == CONNECTION_MODE


def provider_is_on_biz_app(
    phone_data: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[bool]:
    """Return provider Business App truth, or None when Meta did not say.

    Graph ``is_on_biz_app`` outranks persisted metadata.
    """
    data = dict(phone_data or {})
    if "is_on_biz_app" in data:
        return bool(data.get("is_on_biz_app"))
    stored = dict(meta or {})
    if "is_on_biz_app" in stored:
        return bool(stored.get("is_on_biz_app"))
    return None


def coexistence_provider_eligible(
    phone_data: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[bool]:
    """True only when Meta proves an active Business App Cloud API number.

    False when Meta proves the number is not on the Business App.
    None when eligibility cannot be decided from available provider fields.
    """
    on_app = provider_is_on_biz_app(phone_data, meta)
    if on_app is False:
        return False
    data = dict(phone_data or {})
    stored = dict(meta or {})
    platform = str(data.get("platform_type") or stored.get("platform_type") or "").strip().upper()
    if on_app is True and platform in {"", "CLOUD_API"}:
        return True
    if on_app is True and platform == "NOT_APPLICABLE":
        return False
    return None


def should_project_as_coexistence(
    conn: Any,
    phone_data: Optional[Dict[str, Any]] = None,
) -> bool:
    """True only while local coexistence mode is still provider-eligible.

    ``is_on_biz_app=false`` outranks stale ``connection_mode=coexistence``.
    A missing provider field keeps current Coexistence projection (T35).
    """
    if not is_coexistence_mode(conn):
        return False
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    return provider_is_on_biz_app(phone_data, meta) is not False


def persist_provider_phone_truth(
    conn: Any,
    phone_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Store Graph Business App facts without committing coexistence mode."""
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    data = dict(phone_data or {})
    if "is_on_biz_app" in data:
        meta["is_on_biz_app"] = bool(data.get("is_on_biz_app"))
    if data.get("platform_type"):
        meta["platform_type"] = data.get("platform_type")
    conn.extra_metadata = meta
    return meta


def persist_ineligible_coexistence_outcome(
    conn: Any,
    phone_data: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> Dict[str, Any]:
    """Keep Coexistence consent until explicit Cloud API confirm; stop SMB wait.

    ``connection_mode`` stays coexistence so GET /status cannot silently
    ``/register``. The merchant must call confirm-standard-cloud-api.
    """
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    for key in _COEXISTENCE_WAIT_KEYS:
        meta.pop(key, None)
    meta["connection_mode"] = CONNECTION_MODE
    conn.extra_metadata = meta
    persist_provider_phone_truth(conn, phone_data)
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    meta["connection_mode"] = CONNECTION_MODE
    meta["last_coexistence_outcome"] = COEXISTENCE_NOT_ELIGIBLE
    meta["recommended_mode"] = RECOMMENDED_MODE_CLOUD_API
    meta["standard_cloud_api_available"] = True
    meta["failure_code"] = "not_eligible"
    if error_message:
        meta["embedded_status_message"] = error_message
    conn.extra_metadata = meta
    return meta


def clear_coexistence_wait_state(conn: Any) -> Dict[str, Any]:
    """Drop SMB wait / readiness identity without clearing Coexistence consent."""
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    for key in _COEXISTENCE_WAIT_KEYS:
        meta.pop(key, None)
    conn.extra_metadata = meta
    return meta


def clear_obsolete_coexistence_state(conn: Any) -> Dict[str, Any]:
    """Drop Coexistence wait/mode fields without erasing unrelated history."""
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    for key in _OBSOLETE_COEXISTENCE_KEYS:
        meta.pop(key, None)
    if str(meta.get("failure_code") or "") in _COEXISTENCE_FAILURE_CODES:
        meta.pop("failure_code", None)
    if str(meta.get("status_projection_reason") or "") in _COEXISTENCE_PROJECTION_REASONS:
        meta.pop("status_projection_reason", None)
    if str(meta.get("embedded_status_message") or "") in {_CONFIGURING_MESSAGE, _READY_MESSAGE}:
        meta.pop("embedded_status_message", None)
    meta["recommended_mode"] = RECOMMENDED_MODE_CLOUD_API
    meta["last_coexistence_outcome"] = COEXISTENCE_NOT_ELIGIBLE
    conn.extra_metadata = meta
    return meta


def coexistence_webhook_fields() -> List[str]:
    return list(COEXISTENCE_WEBHOOK_FIELDS)


def reject_coexistence_finish_event(finish_event: Optional[str]) -> Optional[str]:
    """Return an Arabic error if the session event is present and unsafe/wrong.

    Missing finish_event is allowed (code-only exchange). The backend still
    rediscovers WABA/phone from Graph before marking connected.
    """
    event = str(finish_event or "").strip()
    if not event:
        return None
    if event in UNSAFE_FINISH_EVENTS or "migrat" in event.lower():
        return (
            "توقّف: مسار Meta الحالي يبدو ترحيلاً أو فصلاً للرقم. "
            "لا يمكن إكمال ربط واتساب الأعمال على الجوال بهذه الشاشة."
        )
    if event != COEXISTENCE_FINISH_EVENT:
        return (
            "لم يكتمل مسار ربط واتساب الأعمال على الجوال. "
            "أعد المحاولة من الزر الموصى به دون إنشاء رقم جديد."
        )
    return None


def merge_coexistence_metadata(
    conn: Any,
    **updates: Any,
) -> Dict[str, Any]:
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    meta["connection_mode"] = CONNECTION_MODE
    for key, value in updates.items():
        if value is not None:
            meta[key] = value
    conn.extra_metadata = meta
    return meta


def _graph_base() -> str:
    from core.config import META_GRAPH_API_VERSION  # noqa: PLC0415
    return f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"


def verify_coexistence_phone(
    phone_number_id: str,
    access_token: str,
    tenant_id: int,
) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """GET phone?fields=is_on_biz_app,platform_type.

    Returns (eligible, data, error_message). Never logs the token.
    """
    try:
        resp = httpx.get(
            f"{_graph_base()}/{phone_number_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "id,display_phone_number,verified_name,is_on_biz_app,platform_type"},
            timeout=15,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[Coexistence] phone verify exception tenant=%s phone_id=%s err=%s",
            tenant_id, phone_number_id, exc,
        )
        return False, {}, "تعذر التحقق من أهلية الرقم لربط واتساب الأعمال على الجوال."

    if "error" in data:
        msg = (data.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
        logger.warning(
            "[Coexistence] phone verify Graph error tenant=%s phone_id=%s err=%s",
            tenant_id, phone_number_id, msg,
        )
        return False, data, "Meta رفضت التحقق من الرقم. تأكد أن الرقم ما زال على تطبيق واتساب الأعمال."

    on_app = bool(data.get("is_on_biz_app"))
    platform = str(data.get("platform_type") or "").strip().upper()
    if on_app and platform == "CLOUD_API":
        return True, data, None
    return False, data, (
        "هذا الرقم غير مؤهل لمسار Coexistence. "
        "يجب أن يبقى على تطبيق WhatsApp Business وأن يكون مربوطاً بـ Cloud API."
    )


def initiate_smb_app_data(
    phone_number_id: str,
    access_token: str,
    tenant_id: int,
    sync_types: Sequence[str] = SMB_SYNC_TYPES,
) -> Dict[str, Any]:
    """POST /{phone_id}/smb_app_data for each sync_type. Returns status map."""
    results: Dict[str, Any] = {}
    for sync_type in sync_types:
        try:
            resp = httpx.post(
                f"{_graph_base()}/{phone_number_id}/smb_app_data",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"messaging_product": "whatsapp", "sync_type": sync_type},
                timeout=20,
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Coexistence] smb_app_data exception tenant=%s phone_id=%s type=%s err=%s",
                tenant_id, phone_number_id, sync_type, exc,
            )
            results[sync_type] = {"accepted": False, "error": str(exc)}
            continue
        request_id = data.get("request_id")
        accepted = resp.status_code == 200 and bool(request_id)
        if "error" in data:
            err = (data.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
            logger.warning(
                "[Coexistence] smb_app_data failed tenant=%s phone_id=%s type=%s err=%s",
                tenant_id, phone_number_id, sync_type, err,
            )
            results[sync_type] = {"accepted": False, "error": err}
            continue
        if not accepted:
            logger.warning(
                "[Coexistence] smb_app_data missing request_id tenant=%s phone_id=%s type=%s status=%s",
                tenant_id, phone_number_id, sync_type, resp.status_code,
            )
            results[sync_type] = {"accepted": False, "error": "missing_request_id"}
            continue
        results[sync_type] = {
            "accepted": True,
            "request_id": request_id,
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            "[Coexistence] smb_app_data accepted tenant=%s phone_id=%s type=%s request_id=%s",
            tenant_id, phone_number_id, sync_type, request_id,
        )
    return results


def _smb_entry_complete(entry: Dict[str, Any]) -> bool:
    return bool(entry.get("accepted") and str(entry.get("request_id") or "").strip())


def smb_syncs_accepted(meta: Dict[str, Any]) -> bool:
    sync = dict(meta.get("smb_sync") or {})
    return all(_smb_entry_complete(sync.get(kind) or {}) for kind in SMB_SYNC_TYPES)


def missing_smb_syncs(meta: Dict[str, Any]) -> List[str]:
    sync = dict(meta.get("smb_sync") or {})
    return [kind for kind in SMB_SYNC_TYPES if not _smb_entry_complete(sync.get(kind) or {})]


def parse_deadline(meta: Dict[str, Any]) -> Optional[datetime]:
    raw = meta.get("smb_sync_deadline_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:  # noqa: silent-ok — malformed deadline must not fail onboarding
        return None


def bind_coexistence_readiness_identity(conn: Any) -> Dict[str, Any]:
    """Stamp the identity that earned Coexistence SMB/webhook readiness."""
    return merge_coexistence_metadata(
        conn,
        readiness_phone_number_id=str(getattr(conn, "phone_number_id", "") or "").strip() or None,
        readiness_waba_id=str(getattr(conn, "whatsapp_business_account_id", "") or "").strip() or None,
    )


def coexistence_readiness_identity_matches(conn: Any) -> bool:
    """True when stored Coexistence readiness facts belong to the current identity."""
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    bound_phone = str(meta.get("readiness_phone_number_id") or "").strip()
    bound_waba = str(meta.get("readiness_waba_id") or "").strip()
    phone = str(getattr(conn, "phone_number_id", "") or "").strip()
    waba = str(getattr(conn, "whatsapp_business_account_id", "") or "").strip()
    if not bound_phone and not bound_waba:
        return False
    if bound_phone and bound_phone != phone:
        return False
    if bound_waba and bound_waba != waba:
        return False
    return True


_LEGACY_DEMOTION_STATUSES = frozenset({
    "otp_pending",
    "configuring",
    "activation_pending",
    "review_pending",
    "disconnected",
})


def _legacy_unstamped_demotion_repair(conn: Any, phone_data: Optional[Dict[str, Any]]) -> bool:
    """Allow unstamped historical Coexistence facts only to repair a demoted same-identity row.

    Replacement that overwrites phone/WABA while leaving leftover SMB/webhook must
    not inherit readiness. A still-``connected`` row with unstamped facts is not
    granted either — it must re-earn identity-scoped readiness.
    """
    if getattr(conn, "connected_at", None) is None:
        return False
    if str(getattr(conn, "status", "") or "") not in _LEGACY_DEMOTION_STATUSES:
        return False
    return coexistence_identity_matches(conn, phone_data)


def apply_smb_sync_results(conn: Any, results: Dict[str, Any]) -> Dict[str, Any]:
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    sync = dict(meta.get("smb_sync") or {})
    accepted_any = False
    for kind, payload in results.items():
        prev = dict(sync.get(kind) or {})
        prev.update(payload)
        sync[kind] = prev
        if _smb_entry_complete(prev):
            accepted_any = True
    meta["smb_sync"] = sync
    conn.extra_metadata = meta
    if accepted_any:
        meta = bind_coexistence_readiness_identity(conn)
    return meta


def start_coexistence_deadline(conn: Any, *, reset: bool = False) -> Dict[str, Any]:
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    if not reset and parse_deadline(meta) is not None:
        return merge_coexistence_metadata(conn)
    now = datetime.now(timezone.utc)
    return merge_coexistence_metadata(
        conn,
        coexistence_onboarded_at=now.isoformat(),
        smb_sync_deadline_at=(now + SMB_SYNC_DEADLINE).isoformat(),
    )


def maybe_fail_sync_deadline(conn: Any, now: Optional[datetime] = None) -> bool:
    """Mark failed if configuring and 24h deadline passed. Returns True if failed."""
    if not should_project_as_coexistence(conn):
        return False
    if str(getattr(conn, "status", "") or "") not in {"configuring", "authorizing"}:
        return False
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    if provider_is_on_biz_app(None, meta) is False:
        return False
    deadline = parse_deadline(meta)
    stamp = now or datetime.now(timezone.utc)
    if deadline is None or stamp < deadline:
        return False
    if smb_syncs_accepted(meta):
        return False
    conn.status = "failed"
    conn.sending_enabled = False
    conn.last_error = (
        "انتهت مهلة 24 ساعة لمزامنة تطبيق واتساب الأعمال. "
        "افصل الحساب من التطبيق ثم أعد مسار الربط."
    )
    merge_coexistence_metadata(conn, failure_code="smb_sync_deadline")
    return True


_PHONE_HARD_FAIL_TOKENS = ("RESTRICT", "DISABLE", "BLOCK", "DELETE", "FLAG")


def _meta_token(value: Any) -> str:
    return str(value or "").strip().upper()


def coexistence_phone_hard_fail(phone_data: Optional[Dict[str, Any]]) -> bool:
    """True only for real Meta phone restriction/disable — not Cloud OTP."""
    flag = _meta_token((phone_data or {}).get("status"))
    return any(token in flag for token in _PHONE_HARD_FAIL_TOKENS)


def coexistence_identity_matches(conn: Any, phone_data: Optional[Dict[str, Any]]) -> bool:
    graph_id = str((phone_data or {}).get("id") or "").strip()
    stored_id = str(getattr(conn, "phone_number_id", "") or "").strip()
    if not stored_id:
        return False
    if not graph_id:
        return True
    return graph_id == stored_id


def project_coexistence_sync_state(
    conn: Any,
    *,
    phone_data: Optional[Dict[str, Any]] = None,
    cloud_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Project Coexistence readiness from canonical Coexistence facts.

    Cloud OTP / ``code_verification_status`` / ``/{phone}/register`` are not
    readiness for this flow. This helper never writes ``status=connected``;
    callers still go through ``finalize_successful_whatsapp_connection``.
    """
    cloud = dict(cloud_state or {})
    data = dict(phone_data or {})
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    observed = {
        "verification_status": cloud.get("verification_status") or _meta_token(
            data.get("code_verification_status")
        ) or None,
        "name_status": cloud.get("name_status") or _meta_token(data.get("name_status")) or None,
        "meta_phone_status": cloud.get("meta_phone_status") or _meta_token(data.get("status")) or None,
        "quality_rating": cloud.get("quality_rating") if "quality_rating" in cloud else data.get("quality_rating"),
        "otp_required": False,
    }

    if provider_is_on_biz_app(data, meta) is False:
        projected = dict(cloud) if cloud else {
            **observed,
            "connected": False,
            "sending_enabled": False,
            "db_status": "activation_pending",
            "message": cloud.get("message"),
        }
        projected["otp_required"] = projected.get("db_status") == "otp_pending"
        projected["projection_reason"] = COEXISTENCE_NOT_ELIGIBLE
        projected["recommended_mode"] = RECOMMENDED_MODE_CLOUD_API
        projected["standard_cloud_api_available"] = True
        projected["coexistence_not_eligible"] = True
        return projected

    if coexistence_phone_hard_fail(data):
        return {
            **observed,
            "connected": False,
            "sending_enabled": False,
            "db_status": "error",
            "projection_reason": "phone_restricted",
            "message": cloud.get("message") or "Meta أوقفت هذا الرقم أو قيّدته، لذلك لا يمكن تفعيله حاليًا.",
        }

    if not coexistence_identity_matches(conn, data):
        return {
            **observed,
            "connected": False,
            "sending_enabled": False,
            "db_status": "configuring",
            "projection_reason": "identity_mismatch",
            "message": _CONFIGURING_MESSAGE,
        }

    identity_scoped = coexistence_readiness_identity_matches(conn)
    if not identity_scoped and not _legacy_unstamped_demotion_repair(conn, data):
        return {
            **observed,
            "connected": False,
            "sending_enabled": False,
            "db_status": "configuring",
            "projection_reason": "identity_mismatch",
            "message": _CONFIGURING_MESSAGE,
        }

    if not smb_syncs_accepted(meta):
        return {
            **observed,
            "connected": False,
            "sending_enabled": False,
            "db_status": "configuring",
            "projection_reason": "smb_incomplete",
            "message": getattr(conn, "last_error", None) or _CONFIGURING_MESSAGE,
        }

    if not bool(getattr(conn, "webhook_verified", False)):
        return {
            **observed,
            "connected": False,
            "sending_enabled": False,
            "db_status": "configuring",
            "projection_reason": "webhook_unverified",
            "message": getattr(conn, "last_error", None) or _CONFIGURING_MESSAGE,
        }

    return {
        **observed,
        "connected": True,
        "sending_enabled": True,
        "db_status": "connected",
        "projection_reason": "ready",
        "message": _READY_MESSAGE,
    }
