"""
Order Updates (تحديثات الطلبات) — settings, revisions, Meta promotion.

Enable flags live in ``TenantSettings.extra_metadata["order_updates"]``.
Revision chain uses ``WhatsAppTemplate.revision`` + ``supersedes_template_id``.

One canonical active APPROVED revision per service_key is used for BOTH
open-window session rendering and closed-window Meta template sends.
"""
from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger("nahla.commerce_lifecycle.order_updates")

ORDER_UPDATE_SERVICE_KEYS: Tuple[str, ...] = (
    "order_confirmation",
    "cod_confirmation",
    "payment_pending",
    "payment_confirmed",
    "order_preparing",
    "order_ready",
    "shipping_tracking",
    "out_for_delivery",
    "order_delivered",
    "order_cancelled",
    "order_refunded",
)

# Compatibility defaults when TenantSettings exists but a key is unset.
# Only historically-on merchant notifications stay ON. New lifecycle
# types, including checkout COD confirmation, default OFF so merge does
# not suddenly enable nine extra WhatsApp sends.
LEGACY_DEFAULT_ON_KEYS: frozenset[str] = frozenset({
    "order_confirmation",
    "shipping_tracking",
})

MASTER_ENABLED_KEY = "enabled"

REASON_SETTINGS_UNAVAILABLE = "order_update_settings_unavailable"
REASON_ORDER_UPDATE_DISABLED = "order_update_disabled"

_DISPLAY_NAMES_AR: Dict[str, str] = {
    "order_confirmation": "تأكيد الطلب",
    "cod_confirmation": "تأكيد الدفع عند الاستلام",
    "payment_pending": "بانتظار الدفع",
    "payment_confirmed": "تم استلام الدفع",
    "order_preparing": "جاري تجهيز الطلب",
    "order_ready": "تم تجهيز الطلب",
    "shipping_tracking": "تم شحن الطلب",
    "out_for_delivery": "خرج الطلب للتوصيل",
    "order_delivered": "تم تسليم الطلب",
    "order_cancelled": "تم إلغاء الطلب",
    "order_refunded": "تم استرجاع المبلغ",
}

_DEFAULT_BODIES: Dict[str, str] = {
    "order_confirmation": (
        "تم تأكيد طلبك ✅\n\n"
        "مرحباً {{1}}، تم استلام طلبك رقم #{{2}}.\n\n"
        "سنبدأ تجهيزه وسنرسل لك تحديثات الشحن عند توفرها."
    ),
    "cod_confirmation": (
        "مرحباً {{1}} 👋\n\n"
        "لديك طلب رقم #{{2}} بنظام الدفع عند الاستلام.\n\n"
        "هل تريد تأكيد هذا الطلب؟"
    ),
    "payment_pending": (
        "مرحباً {{1}} 💳\n\n"
        "طلبك رقم #{{2}} لا يزال بانتظار إكمال الدفع."
    ),
    "payment_confirmed": (
        "مرحباً {{1}}\n\n"
        "تم استلام دفع طلبك رقم #{{2}}."
    ),
    "order_preparing": (
        "مرحباً {{1}}\n\n"
        "جاري تجهيز طلبك رقم #{{2}}."
    ),
    "order_ready": (
        "مرحباً {{1}}\n\n"
        "تم تجهيز طلبك رقم #{{2}}."
    ),
    "shipping_tracking": (
        "خبر سار يا {{1}} 🚚\n\n"
        "طلبك رقم #{{2}} تم شحنه عبر {{3}}.\n"
        "رقم التتبع: {{4}}\n"
        "رابط التتبع: {{5}}\n\n"
        "يمكنك متابعة شحنتك من الرابط أعلاه."
    ),
    "out_for_delivery": (
        "مرحباً {{1}}\n\n"
        "طلبك رقم #{{2}} خرج للتوصيل."
    ),
    "order_delivered": (
        "مرحباً {{1}}\n\n"
        "تم تسليم طلبك رقم #{{2}}."
    ),
    "order_cancelled": (
        "مرحباً {{1}}\n\n"
        "تم إلغاء طلبك رقم #{{2}}."
    ),
    "order_refunded": (
        "مرحباً {{1}}\n\n"
        "تم استرجاع مبلغ طلبك رقم #{{2}}."
    ),
}

_DEFAULT_VARIABLES: Dict[str, List[str]] = {
    "order_confirmation": ["customer_name", "order_number"],
    "cod_confirmation": ["customer_name", "order_number"],
    "payment_pending": ["customer_name", "order_number", "payment_url"],
    "payment_confirmed": ["customer_name", "order_number"],
    "order_preparing": ["customer_name", "order_number"],
    "order_ready": ["customer_name", "order_number"],
    "shipping_tracking": [
        "customer_name",
        "order_number",
        "carrier",
        "tracking_number",
        "tracking_url",
    ],
    "out_for_delivery": ["customer_name", "order_number", "carrier"],
    "order_delivered": ["customer_name", "order_number"],
    "order_cancelled": ["customer_name", "order_number"],
    "order_refunded": ["customer_name", "order_number"],
}

_COD_DEFAULT_BUTTONS: Dict[str, Any] = {
    "type": "BUTTONS",
    "buttons": [
        {"type": "QUICK_REPLY", "text": "تأكيد الطلب ✅"},
        {"type": "QUICK_REPLY", "text": "إلغاء الطلب ❌"},
    ],
}


def is_order_update_service_key(service_key: Optional[str]) -> bool:
    return str(service_key or "").strip() in ORDER_UPDATE_SERVICE_KEYS


def _default_enabled_for(key: str) -> bool:
    return key in LEGACY_DEFAULT_ON_KEYS


def _empty_flags() -> Dict[str, bool]:
    return {key: _default_enabled_for(key) for key in ORDER_UPDATE_SERVICE_KEYS}


def _read_master_enabled(stored: Dict[str, Any]) -> bool:
    raw = stored.get(MASTER_ENABLED_KEY, True)
    if isinstance(raw, dict):
        return bool(raw.get("enabled", True))
    return bool(raw)


def _flags_from_stored(stored: Dict[str, Any]) -> Dict[str, bool]:
    flags = _empty_flags()
    for key in ORDER_UPDATE_SERVICE_KEYS:
        if key not in stored:
            continue
        entry = stored.get(key)
        if isinstance(entry, dict):
            flags[key] = bool(entry.get("enabled", _default_enabled_for(key)))
        elif isinstance(entry, bool):
            flags[key] = entry
    return flags


def _denied_flags() -> Dict[str, bool]:
    return {key: False for key in ORDER_UPDATE_SERVICE_KEYS}


@dataclass(frozen=True)
class OrderUpdateSettingsTruth:
    """Persisted merchant preferences vs effective send permission."""

    available: bool
    reason: Optional[str]
    master_enabled: bool
    flags: Dict[str, bool]

    @property
    def effective(self) -> Dict[str, bool]:
        if not self.available or not self.master_enabled:
            return _denied_flags()
        return dict(self.flags)


def load_order_update_settings_truth(
    db: Session,
    tenant_id: int,
) -> OrderUpdateSettingsTruth:
    """
    Read order-update settings.

    Missing TenantSettings / unset keys → documented compatibility defaults.
    Database/query errors → unavailable (fail closed, not merchant consent).
    """
    from models import TenantSettings  # noqa: PLC0415

    try:
        settings = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == int(tenant_id))
            .first()
        )
    except Exception:  # noqa: BLE001 — query failure is not consent
        logger.exception(
            "[OrderUpdates] settings truth unavailable tenant=%s",
            tenant_id,
        )
        return OrderUpdateSettingsTruth(
            available=False,
            reason=REASON_SETTINGS_UNAVAILABLE,
            master_enabled=False,
            flags=_denied_flags(),
        )

    if not settings or not settings.extra_metadata:
        return OrderUpdateSettingsTruth(
            available=True,
            reason=None,
            master_enabled=True,
            flags=_empty_flags(),
        )
    stored = settings.extra_metadata.get("order_updates") or {}
    if not isinstance(stored, dict):
        return OrderUpdateSettingsTruth(
            available=True,
            reason=None,
            master_enabled=True,
            flags=_empty_flags(),
        )
    return OrderUpdateSettingsTruth(
        available=True,
        reason=None,
        master_enabled=_read_master_enabled(stored),
        flags=_flags_from_stored(stored),
    )


def get_order_updates_master_enabled(db: Session, tenant_id: int) -> bool:
    truth = load_order_update_settings_truth(db, tenant_id)
    if not truth.available:
        return False
    return truth.master_enabled


def get_order_update_flags(db: Session, tenant_id: int) -> Dict[str, bool]:
    """Persisted individual flags. Master OFF does not rewrite these."""
    return dict(load_order_update_settings_truth(db, tenant_id).flags)


def evaluate_order_update_delivery_from_truth(
    truth: OrderUpdateSettingsTruth,
    service_key: str,
) -> Tuple[bool, Optional[str]]:
    """Evaluate delivery against an already-loaded settings snapshot. No DB read."""
    if not is_order_update_service_key(service_key):
        return True, None
    if not truth.available:
        return False, REASON_SETTINGS_UNAVAILABLE
    if not truth.master_enabled:
        return False, REASON_ORDER_UPDATE_DISABLED
    if not bool(truth.flags.get(service_key, _default_enabled_for(service_key))):
        return False, REASON_ORDER_UPDATE_DISABLED
    return True, None


def evaluate_order_update_delivery(
    db: Session,
    tenant_id: int,
    service_key: str,
) -> Tuple[bool, Optional[str]]:
    """Return ``(allowed, reason)``. Query errors fail closed and do not send."""
    if not is_order_update_service_key(service_key):
        return True, None
    truth = load_order_update_settings_truth(db, tenant_id)
    return evaluate_order_update_delivery_from_truth(truth, service_key)


def set_order_update_flags(
    db: Session,
    tenant_id: int,
    updates: Dict[str, bool],
    *,
    commit: bool = False,
    master_enabled: Optional[bool] = None,
) -> Dict[str, bool]:
    from models import TenantSettings  # noqa: PLC0415

    settings = (
        db.query(TenantSettings)
        .filter(TenantSettings.tenant_id == int(tenant_id))
        .first()
    )
    if settings is None:
        settings = TenantSettings(tenant_id=int(tenant_id), extra_metadata={})
        db.add(settings)
        db.flush()

    extra: Dict[str, Any] = dict(settings.extra_metadata or {})
    bucket: Dict[str, Any] = dict(extra.get("order_updates") or {})
    if master_enabled is not None:
        bucket[MASTER_ENABLED_KEY] = bool(master_enabled)
    for key, enabled in updates.items():
        if key not in ORDER_UPDATE_SERVICE_KEYS:
            continue
        entry = dict(bucket.get(key) or {}) if isinstance(bucket.get(key), dict) else {}
        entry["enabled"] = bool(enabled)
        bucket[key] = entry
    extra["order_updates"] = bucket
    settings.extra_metadata = extra
    flag_modified(settings, "extra_metadata")
    db.flush()
    if commit:
        db.commit()
    return get_order_update_flags(db, tenant_id)


def is_order_update_enabled(db: Session, tenant_id: int, service_key: str) -> bool:
    allowed, _reason = evaluate_order_update_delivery(db, tenant_id, service_key)
    return allowed


def resolve_lifecycle_template_for_send(
    db: Session,
    tenant_id: int,
    service_key: str,
):
    """
    Strict same-slot resolver. Never substitutes a different lifecycle event.

    ``payment_pending`` resolves only an APPROVED ``payment_pending`` revision.
    Historical reminder-slot rows are never selected for this event.
    """
    from core.service_template_resolver import resolve_active_template  # noqa: PLC0415

    return resolve_active_template(db, int(tenant_id), str(service_key), None)


def default_body_for(service_key: str) -> str:
    return _DEFAULT_BODIES.get(service_key, "")


def default_components_for(service_key: str) -> List[Dict[str, Any]]:
    body = {"type": "BODY", "text": default_body_for(service_key)}
    if service_key == "cod_confirmation":
        return [body, dict(_COD_DEFAULT_BUTTONS)]
    return [body]


def variables_for(service_key: str) -> List[str]:
    return list(_DEFAULT_VARIABLES.get(service_key, []))


def display_name_ar_for(service_key: str) -> str:
    return _DISPLAY_NAMES_AR.get(service_key, service_key)


def _extract_body_text(components: Any) -> str:
    for comp in components or []:
        if str((comp or {}).get("type", "")).upper() == "BODY":
            return str((comp or {}).get("text") or "")
    return ""


def _replace_body_text(components: Any, body_text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    replaced = False
    for comp in components or []:
        item = dict(comp or {})
        if str(item.get("type", "")).upper() == "BODY":
            item["text"] = body_text
            # Keep Meta example shape if present; otherwise synthesize blanks.
            placeholders = re.findall(r"\{\{(\d+)\}\}", body_text)
            if placeholders and not item.get("example"):
                n = max(int(p) for p in placeholders)
                item["example"] = {"body_text": [["مثال"] * n]}
            replaced = True
        out.append(item)
    if not replaced:
        placeholders = re.findall(r"\{\{(\d+)\}\}", body_text)
        n = max((int(p) for p in placeholders), default=0)
        body: Dict[str, Any] = {"type": "BODY", "text": body_text}
        if n:
            body["example"] = {"body_text": [["مثال"] * n]}
        out.insert(0, body)
    return out


def _header_contract(components: Any) -> Dict[str, Any]:
    for comp in components or []:
        if str((comp or {}).get("type", "")).upper() != "HEADER":
            continue
        fmt = str((comp or {}).get("format") or "TEXT").upper()
        return {
            "header_type": fmt.lower(),
            "header_format": fmt,
            "header_asset_id": (comp or {}).get("example", {}).get("header_handle")
            if isinstance((comp or {}).get("example"), dict)
            else None,
        }
    return {"header_type": "none", "header_format": None, "header_asset_id": None}


def _tpl_public(tpl: Any) -> Optional[Dict[str, Any]]:
    if tpl is None:
        return None
    components = getattr(tpl, "components", None)
    body = _extract_body_text(components)
    header = _header_contract(components)
    return {
        "id": int(tpl.id),
        "template_id": int(tpl.id),
        "name": tpl.name,
        "status": tpl.status,
        "meta_status": tpl.status,
        "revision": int(getattr(tpl, "revision", 1) or 1),
        "label": f"r{int(getattr(tpl, 'revision', 1) or 1)}",
        "supersedes_template_id": getattr(tpl, "supersedes_template_id", None),
        "is_active": bool(tpl.is_active),
        "body_text": body,
        "text": body,
        "rejection_reason": tpl.rejection_reason,
        "meta_template_id": tpl.meta_template_id,
        "meta_template_name": tpl.name,
        "language": tpl.language or "ar",
        "category": tpl.category,
        "header_type": header["header_type"],
        "header_format": header["header_format"],
        "header_asset_id": header["header_asset_id"],
    }


def resolve_active_and_pending(
    db: Session,
    tenant_id: int,
    service_key: str,
) -> Dict[str, Any]:
    from models import WhatsAppTemplate  # noqa: PLC0415
    from core.service_template_resolver import resolve_active_template  # noqa: PLC0415

    active = resolve_active_template(db, int(tenant_id), service_key, None)
    pending = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id == int(tenant_id),
            WhatsAppTemplate.service_key == service_key,
            WhatsAppTemplate.is_hidden.is_(False),
            WhatsAppTemplate.status.in_(("DRAFT", "PENDING", "REJECTED")),
        )
        .order_by(WhatsAppTemplate.revision.desc(), WhatsAppTemplate.id.desc())
        .first()
    )
    active_pub = _tpl_public(active)
    pending_pub = _tpl_public(pending)
    body = ""
    if pending_pub and pending_pub.get("body_text"):
        body = str(pending_pub["body_text"])
    elif active_pub and active_pub.get("body_text"):
        body = str(active_pub["body_text"])
    else:
        body = default_body_for(service_key)
    persisted = get_order_update_flags(db, tenant_id).get(
        service_key, _default_enabled_for(service_key)
    )
    return {
        "service_key": service_key,
        "enabled": bool(persisted),
        "variables": variables_for(service_key),
        "available_variables": variables_for(service_key),
        "default_body": default_body_for(service_key),
        "body_text": body,
        "message_text": body,
        "meta_status": (pending_pub or active_pub or {}).get("status"),
        "active": active_pub,
        "pending": pending_pub,
        "live_revision": active_pub,
        "approved_revision": active_pub,
        "last_approved_revision": active_pub,
        "pending_revision": pending_pub,
        "header_type": (active_pub or pending_pub or {}).get("header_type") or "none",
        "header_asset_id": (active_pub or {}).get("header_asset_id"),
    }


def create_revision_from_active(
    db: Session,
    tenant_id: int,
    service_key: str,
    body_text: str,
    *,
    display_name_ar: Optional[str] = None,
    commit: bool = False,
) -> Any:
    """
    Create a DRAFT successor for an APPROVED active template (or a first draft).

    The prior APPROVED row stays ``is_active=True`` until Meta approves the
    successor and ``promote_approved_revision`` runs.
    """
    from models import WhatsAppTemplate  # noqa: PLC0415
    from core.service_template_resolver import resolve_active_template  # noqa: PLC0415

    if not is_order_update_service_key(service_key):
        raise ValueError("unsupported_service_key")
    text = str(body_text or "").strip()
    if not text:
        raise ValueError("empty_body")

    active = resolve_active_template(db, int(tenant_id), service_key, None)
    if active is not None and str(active.status).upper() == "APPROVED":
        base_components = list(active.components or [])
        supersedes_id = int(active.id)
        next_revision = int(getattr(active, "revision", 1) or 1) + 1
        language = active.language or "ar"
        category = active.category or "UTILITY"
        source_key = active.nahla_source_key
    else:
        base_components = default_components_for(service_key)
        # Prefer any existing pending draft as base for components.
        existing_pending = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id == int(tenant_id),
                WhatsAppTemplate.service_key == service_key,
            )
            .order_by(WhatsAppTemplate.id.desc())
            .first()
        )
        if existing_pending is not None:
            base_components = list(existing_pending.components or base_components)
            next_revision = int(getattr(existing_pending, "revision", 1) or 1) + 1
            language = existing_pending.language or "ar"
            category = existing_pending.category or "UTILITY"
            source_key = existing_pending.nahla_source_key
        else:
            next_revision = 1
            language = "ar"
            category = "UTILITY"
            source_key = service_key
        supersedes_id = int(active.id) if active is not None else None

    # Hide older drafts/pending for the same slot so UI shows one pending path.
    (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id == int(tenant_id),
            WhatsAppTemplate.service_key == service_key,
            WhatsAppTemplate.status.in_(("DRAFT", "PENDING", "REJECTED")),
            WhatsAppTemplate.is_hidden.is_(False),
        )
        .update({WhatsAppTemplate.is_hidden: True}, synchronize_session=False)
    )

    suffix = secrets.token_hex(3)
    name = f"nahla_{service_key}_r{next_revision}_{suffix}"[:512]
    draft = WhatsAppTemplate(
        tenant_id=int(tenant_id),
        name=name,
        language=language,
        category=category,
        status="DRAFT",
        components=_replace_body_text(base_components, text),
        display_name_ar=display_name_ar or display_name_ar_for(service_key),
        service_key=service_key,
        nahla_source_key=source_key or service_key,
        is_active=False,
        is_hidden=False,
        step_number=None,
        revision=next_revision,
        supersedes_template_id=supersedes_id,
        source="merchant",
    )
    db.add(draft)
    db.flush()
    if commit:
        db.commit()
        db.refresh(draft)
    return draft


def promote_approved_revision(
    db: Session,
    *,
    tenant_id: int,
    template_id: int,
    commit: bool = False,
) -> bool:
    """
    When a successor becomes APPROVED, atomically activate it and deactivate
    the superseded APPROVED row. No-op when template is not an order-update
    revision or not APPROVED.
    """
    from models import WhatsAppTemplate  # noqa: PLC0415
    from core.service_template_resolver import ensure_single_active  # noqa: PLC0415

    tpl = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.id == int(template_id),
            WhatsAppTemplate.tenant_id == int(tenant_id),
        )
        .first()
    )
    if tpl is None:
        return False
    if not is_order_update_service_key(tpl.service_key):
        return False
    if str(tpl.status or "").upper() != "APPROVED":
        return False

    ensure_single_active(db, int(tenant_id), str(tpl.service_key), None, int(tpl.id))
    tpl.is_active = True
    tpl.is_hidden = False
    db.flush()
    if commit:
        db.commit()
    logger.info(
        "[OrderUpdates] promoted revision tenant=%s service=%s template=%s rev=%s",
        tenant_id,
        tpl.service_key,
        tpl.id,
        getattr(tpl, "revision", None),
    )
    return True


__all__ = [
    "LEGACY_DEFAULT_ON_KEYS",
    "MASTER_ENABLED_KEY",
    "ORDER_UPDATE_SERVICE_KEYS",
    "REASON_ORDER_UPDATE_DISABLED",
    "REASON_SETTINGS_UNAVAILABLE",
    "OrderUpdateSettingsTruth",
    "create_revision_from_active",
    "default_body_for",
    "display_name_ar_for",
    "evaluate_order_update_delivery",
    "evaluate_order_update_delivery_from_truth",
    "get_order_update_flags",
    "get_order_updates_master_enabled",
    "is_order_update_enabled",
    "is_order_update_service_key",
    "load_order_update_settings_truth",
    "promote_approved_revision",
    "resolve_active_and_pending",
    "resolve_lifecycle_template_for_send",
    "set_order_update_flags",
    "variables_for",
]
