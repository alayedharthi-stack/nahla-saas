"""
Order Updates (تحديثات الطلبات) — settings, revisions, Meta promotion.

Platform-wide helpers for the two Slice-B service keys only:
``order_confirmation`` and ``shipping_tracking``.

Enable flags live in ``TenantSettings.extra_metadata["order_updates"]``.
Revision chain uses ``WhatsAppTemplate.revision`` + ``supersedes_template_id``.
"""
from __future__ import annotations

import logging
import re
import secrets
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger("nahla.commerce_lifecycle.order_updates")

ORDER_UPDATE_SERVICE_KEYS: Tuple[str, ...] = (
    "order_confirmation",
    "shipping_tracking",
)

_DEFAULT_BODIES: Dict[str, str] = {
    "order_confirmation": (
        "تم تأكيد طلبك ✅\n\n"
        "مرحباً {{1}}، تم استلام طلبك رقم #{{2}}.\n\n"
        "سنبدأ تجهيزه وسنرسل لك تحديثات الشحن عند توفرها."
    ),
    "shipping_tracking": (
        "خبر سار يا {{1}} 🚚\n\n"
        "طلبك رقم #{{2}} تم شحنه عبر {{3}}.\n"
        "رقم التتبع: {{4}}\n"
        "رابط التتبع: {{5}}\n\n"
        "يمكنك متابعة شحنتك من الرابط أعلاه."
    ),
}

_DEFAULT_VARIABLES: Dict[str, List[str]] = {
    "order_confirmation": ["customer_name", "order_number"],
    "shipping_tracking": [
        "customer_name",
        "order_number",
        "carrier",
        "tracking_number",
        "tracking_url",
    ],
}


def is_order_update_service_key(service_key: Optional[str]) -> bool:
    return str(service_key or "").strip() in ORDER_UPDATE_SERVICE_KEYS


def _empty_flags() -> Dict[str, bool]:
    return {key: True for key in ORDER_UPDATE_SERVICE_KEYS}


def get_order_update_flags(db: Session, tenant_id: int) -> Dict[str, bool]:
    from models import TenantSettings  # noqa: PLC0415

    flags = _empty_flags()
    try:
        settings = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == int(tenant_id))
            .first()
        )
    except Exception:  # noqa: BLE001 — tests / partial schemas fail open (enabled)
        return flags
    if not settings or not settings.extra_metadata:
        return flags
    stored = settings.extra_metadata.get("order_updates") or {}
    if not isinstance(stored, dict):
        return flags
    for key in ORDER_UPDATE_SERVICE_KEYS:
        if key in stored:
            flags[key] = bool(stored.get(key, {}).get("enabled", True))
    return flags


def set_order_update_flags(
    db: Session,
    tenant_id: int,
    updates: Dict[str, bool],
    *,
    commit: bool = False,
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
    for key, enabled in updates.items():
        if key not in ORDER_UPDATE_SERVICE_KEYS:
            continue
        entry = dict(bucket.get(key) or {})
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
    if not is_order_update_service_key(service_key):
        return True
    return bool(get_order_update_flags(db, tenant_id).get(service_key, True))


def default_body_for(service_key: str) -> str:
    return _DEFAULT_BODIES.get(service_key, "")


def variables_for(service_key: str) -> List[str]:
    return list(_DEFAULT_VARIABLES.get(service_key, []))


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


def _tpl_public(tpl: Any) -> Optional[Dict[str, Any]]:
    if tpl is None:
        return None
    body = _extract_body_text(getattr(tpl, "components", None))
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
    return {
        "service_key": service_key,
        "enabled": is_order_update_enabled(db, tenant_id, service_key),
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
        base_components = [{"type": "BODY", "text": default_body_for(service_key)}]
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
        display_name_ar=display_name_ar
        or ("تأكيد الطلب" if service_key == "order_confirmation" else "تحديث الشحن"),
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
    "ORDER_UPDATE_SERVICE_KEYS",
    "create_revision_from_active",
    "default_body_for",
    "get_order_update_flags",
    "is_order_update_enabled",
    "is_order_update_service_key",
    "promote_approved_revision",
    "resolve_active_and_pending",
    "set_order_update_flags",
    "variables_for",
]
