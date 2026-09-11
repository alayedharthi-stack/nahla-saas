"""Safe revision import for the image-backed COD confirmation template."""
from __future__ import annotations

import copy
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.commerce_lifecycle.cod_confirmation_assets import (
    COD_CONFIRMATION_HEADER_ASSET_KEY,
)
from core.commerce_lifecycle.nahla_library_order_confirmation_import import (
    NahlaLibraryImportError,
    inspect_whatsapp_template_schema,
)
from core.pg_advisory_lock import DedicatedAdvisoryLock

logger = logging.getLogger("nahla.commerce_lifecycle.nahla_library_cod_import")

LIBRARY_KEY = "cod_confirmation"
SERVICE_KEY = "cod_confirmation"
_IMPORT_LOCK_NAMESPACE = 0x4E4C_434F  # "NLCO"
_PENDING_STATUSES = ("DRAFT", "PENDING", "REJECTED")


def is_cod_confirmation_image_contract(components: Any) -> bool:
    return any(
        str((component or {}).get("type", "")).upper() == "HEADER"
        and str((component or {}).get("format", "")).upper() == "IMAGE"
        and bool(dict((component or {}).get("example") or {}).get("header_url"))
        for component in (components or [])
    )


def _find_existing_draft(db: Session, tenant_id: int) -> Optional[Any]:
    from models import WhatsAppTemplate  # noqa: PLC0415

    rows = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id == int(tenant_id),
            WhatsAppTemplate.service_key == SERVICE_KEY,
            WhatsAppTemplate.nahla_source_key == LIBRARY_KEY,
            WhatsAppTemplate.status.in_(_PENDING_STATUSES),
            WhatsAppTemplate.is_hidden.is_(False),
        )
        .order_by(WhatsAppTemplate.id.desc())
        .all()
    )
    return next((row for row in rows if is_cod_confirmation_image_contract(row.components)), None)


def _active_slot(db: Session, tenant_id: int) -> Optional[Any]:
    from models import WhatsAppTemplate  # noqa: PLC0415

    return (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id == int(tenant_id),
            WhatsAppTemplate.service_key == SERVICE_KEY,
            WhatsAppTemplate.step_number.is_(None),
            WhatsAppTemplate.is_active.is_(True),
            WhatsAppTemplate.is_hidden.is_(False),
        )
        .order_by(WhatsAppTemplate.id.desc())
        .first()
    )


def _next_revision(db: Session, tenant_id: int) -> int:
    from models import WhatsAppTemplate  # noqa: PLC0415

    value = (
        db.query(func.max(WhatsAppTemplate.revision))
        .filter(
            WhatsAppTemplate.tenant_id == int(tenant_id),
            WhatsAppTemplate.service_key == SERVICE_KEY,
        )
        .scalar()
    )
    return int(value or 0) + 1


def _unique_name(db: Session, tenant_id: int, requested: Optional[str], revision: int) -> str:
    from models import WhatsAppTemplate  # noqa: PLC0415

    base = requested or f"nahla_cod_confirmation_r{revision}_{secrets.token_hex(3)}"
    name = re.sub(r"[^a-z0-9_]", "_", base.lower())[:60]
    exists = db.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.tenant_id == int(tenant_id),
        WhatsAppTemplate.name == name,
    ).first()
    return f"{name}_{secrets.token_hex(2)}" if exists else name


def _existing_outcome(db: Session, tenant_id: int, row: Any) -> Dict[str, Any]:
    status = str(row.status or "DRAFT").upper()
    messages = {
        "PENDING": "قالب تأكيد الدفع عند الاستلام قيد مراجعة Meta ولا يمكن تخصيصه حالياً.",
        "REJECTED": "قالب تأكيد الدفع عند الاستلام مرفوض من Meta. راجع سبب الرفض ثم أعد إرساله.",
    }
    return {
        "template": row,
        "message": messages.get(status, "توجد مسودة مصوّرة جاهزة لتأكيد الدفع عند الاستلام."),
        "reused_existing_draft": True,
        "created": False,
        "active_template_preserved": _active_slot(db, tenant_id) is not None,
        "template_status": status,
        "customizable": status in {"DRAFT", "REJECTED"},
    }


def import_cod_confirmation_from_library(
    db: Session,
    tenant_id: int,
    tpl_def: Dict[str, Any],
    *,
    language: str = "ar",
    custom_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an inactive image-backed draft without disabling the approved COD template."""
    if str(language or "ar").strip().lower() != "ar":
        raise NahlaLibraryImportError(
            "قالب تأكيد الدفع عند الاستلام متاح باللغة العربية فقط.",
            error_code="nahla_import_language_unsupported",
        )

    lock = DedicatedAdvisoryLock(
        db,
        namespace=_IMPORT_LOCK_NAMESPACE,
        level_key=int(tenant_id),
    )
    lock.acquire_blocking(timeout_seconds=30.0)
    try:
        existing = _find_existing_draft(db, tenant_id)
        if existing is not None:
            return _existing_outcome(db, tenant_id, existing)

        from models import WhatsAppTemplate  # noqa: PLC0415

        schema = inspect_whatsapp_template_schema(db)
        active = _active_slot(db, tenant_id)
        revision = _next_revision(db, tenant_id)
        now = datetime.now(timezone.utc)
        metadata: Dict[str, Any] = {
            "revision_label": "image-v1",
            "header_image_asset_key": COD_CONFIRMATION_HEADER_ASSET_KEY,
        }
        if active is not None and not schema.get("supersedes_template_id_column"):
            metadata["logical_supersedes_template_id"] = int(active.id)

        kwargs: Dict[str, Any] = {
            "tenant_id": int(tenant_id),
            "meta_template_id": f"nahla_draft_{LIBRARY_KEY}",
            "name": _unique_name(db, tenant_id, custom_name, revision),
            "language": "ar",
            "category": tpl_def.get("category") or "UTILITY",
            "status": "DRAFT",
            "components": copy.deepcopy(tpl_def["components"]),
            "source": "nahla_library",
            "objective": tpl_def.get("smart_trigger"),
            "created_at": now,
            "updated_at": now,
            "synced_at": now,
            "display_name_ar": tpl_def.get("name_ar") or "تأكيد طلب الدفع عند الاستلام",
            "service_key": SERVICE_KEY,
            "nahla_source_key": LIBRARY_KEY,
            "is_active": False,
            "is_hidden": False,
            "step_number": None,
            "has_coupon": False,
            "trigger_delay_hours": tpl_def.get("trigger_delay_hours"),
            "ai_generation_metadata": metadata,
        }
        if schema.get("revision_column"):
            kwargs["revision"] = revision
        if active is not None and schema.get("supersedes_template_id_column"):
            kwargs["supersedes_template_id"] = int(active.id)

        draft = WhatsAppTemplate(**kwargs)
        db.add(draft)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            reused = _find_existing_draft(db, tenant_id)
            if reused is not None:
                return _existing_outcome(db, tenant_id, reused)
            raise NahlaLibraryImportError(
                "فشل حفظ قالب تأكيد الدفع عند الاستلام.",
                error_code="nahla_import_cod_integrity",
            ) from exc
        db.refresh(draft)
        return {
            "template": draft,
            "message": "تم استيراد قالب تأكيد الدفع عند الاستلام المصوّر كمسودة للتخصيص.",
            "reused_existing_draft": False,
            "created": True,
            "active_template_preserved": active is not None,
            "template_status": "DRAFT",
            "customizable": True,
        }
    finally:
        lock.release()


__all__ = ["import_cod_confirmation_from_library", "is_cod_confirmation_image_contract"]
