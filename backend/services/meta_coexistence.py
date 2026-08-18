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


def is_coexistence_mode(conn: Any) -> bool:
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    return str(meta.get("connection_mode") or "").strip().lower() == CONNECTION_MODE


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
    except Exception:
        return None


def apply_smb_sync_results(conn: Any, results: Dict[str, Any]) -> Dict[str, Any]:
    meta = dict(getattr(conn, "extra_metadata", None) or {})
    sync = dict(meta.get("smb_sync") or {})
    for kind, payload in results.items():
        prev = dict(sync.get(kind) or {})
        prev.update(payload)
        sync[kind] = prev
    meta["smb_sync"] = sync
    conn.extra_metadata = meta
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
    if not is_coexistence_mode(conn):
        return False
    if str(getattr(conn, "status", "") or "") not in {"configuring", "authorizing"}:
        return False
    meta = dict(getattr(conn, "extra_metadata", None) or {})
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
