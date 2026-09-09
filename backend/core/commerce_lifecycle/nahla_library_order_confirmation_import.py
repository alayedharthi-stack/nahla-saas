"""
Nahla library import for order_confirmation (ملخص الطلب / order_summary).

Scoped to the ``order_summary`` library key only. Lifecycle imports must not
activate while another active template occupies the same service_key slot.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.commerce_lifecycle.order_confirmation_assets import (
    ORDER_CONFIRMATION_HEADER_ASSET_KEY,
    order_confirmation_header_public_url,
    order_confirmation_image_header_component,
)
from core.pg_advisory_lock import DedicatedAdvisoryLock

_VAR_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")

logger = logging.getLogger("nahla.commerce_lifecycle.nahla_library_oc_import")

LIBRARY_KEY = "order_summary"
SERVICE_KEY = "order_confirmation"
LIFECYCLE_ACTIVE_INDEX = "uq_active_lifecycle_template_null_step"
_IMPORT_LOCK_NAMESPACE = 0x4E4C_4F43  # "NLOC" — Nahla library order_confirmation

_ORDER_SUMMARY_R3_BODY = (
    "تم استلام طلبك يا {{1}} 📦\n\n"
    "من {{4}}\n"
    "رقم الطلب: #{{2}}\n"
    "المبلغ الإجمالي: {{3}} ريال\n\n"
    "سنبدأ تجهيز طلبك فوراً ونُعلمك بكل جديد."
)

MSG_ACTIVE_EXISTS_DRAFT = (
    "يوجد قالب نشط لتأكيد الطلب. أُنشئت نسخة مسودة لتخصيصها،"
    " ولن تصبح نشطة حتى اعتمادها."
)
MSG_EXISTING_DRAFT = (
    "توجد مسودة جاهزة لملخص الطلب (r3). يمكنك تخصيصها الآن."
)
MSG_GENERIC_SAVE_FAILED = "فشل حفظ القالب. يرجى المحاولة مرة أخرى أو التواصل مع الدعم."

_PENDING_STATUSES = ("DRAFT", "PENDING", "REJECTED")
_DISPLAY_NAME_AR = "ملخص الطلب"


def _has_example_com(components: Any) -> bool:
    return "example.com" in json.dumps(components or [], ensure_ascii=False)


def _param_signature(components: Any) -> Dict[str, Any]:
    body_text = ""
    buttons: List[Dict[str, Any]] = []
    for comp in components or []:
        comp_type = str((comp or {}).get("type", "")).upper()
        if comp_type == "BODY":
            body_text = str((comp or {}).get("text") or "")
        if comp_type == "BUTTONS":
            for btn in comp.get("buttons") or []:
                url = str(btn.get("url") or "")
                buttons.append({"url": url})
    placeholders = [int(x) for x in _VAR_RE.findall(body_text)]
    return {
        "body_placeholders": placeholders,
        "buttons": buttons,
    }


def order_summary_r3_components() -> List[Dict[str, Any]]:
    """Canonical r3 library/send contract for order_summary imports."""
    body: Dict[str, Any] = {
        "type": "BODY",
        "text": _ORDER_SUMMARY_R3_BODY,
        "example": {"body_text": [["سارة", "45678", "350", "متجر الأناقة"]]},
    }
    buttons = {
        "type": "BUTTONS",
        "buttons": [
            {
                "type": "URL",
                "text": "عرض تفاصيل الطلب",
                "url": "https://mtjr.at/{{1}}",
                "example": ["https://mtjr.at/orders/45678"],
            },
        ],
    }
    return [
        order_confirmation_image_header_component(),
        body,
        buttons,
    ]


def inspect_whatsapp_template_schema(db: Session) -> Dict[str, Any]:
    """Runtime schema probe via the same DB session as the import path."""
    bind = db.get_bind()
    inspector = sa_inspect(bind)
    if not inspector.has_table("whatsapp_templates"):
        return {
            "table_exists": False,
            "revision_column": False,
            "supersedes_template_id_column": False,
        }
    col_names = {c["name"] for c in inspector.get_columns("whatsapp_templates")}
    alembic_rev: Optional[str] = None
    if inspector.has_table("alembic_version"):
        try:
            row = bind.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).fetchone()
            if row:
                alembic_rev = str(row[0])
        except Exception:  # noqa: silent-ok — schema probe is best-effort; columns still reported
            logger.exception("[NahlaImport:OC:schema_probe] alembic_version read failed")
    return {
        "table_exists": True,
        "revision_column": "revision" in col_names,
        "supersedes_template_id_column": "supersedes_template_id" in col_names,
        "alembic_version": alembic_rev,
    }


def _log_schema_probe(db: Session, tenant_id: int) -> Dict[str, Any]:
    probe = inspect_whatsapp_template_schema(db)
    logger.info(
        "[NahlaImport:OC:schema_probe] tenant=%s table=%s revision_col=%s "
        "supersedes_col=%s alembic=%s",
        tenant_id,
        probe.get("table_exists"),
        probe.get("revision_column"),
        probe.get("supersedes_template_id_column"),
        probe.get("alembic_version"),
        extra={
            "event": "nahla_import_order_summary_schema_probe",
            "tenant_id": int(tenant_id),
            "schema_probe": probe,
        },
    )
    return probe


def is_order_confirmation_r3_contract(components: Any) -> bool:
    """IMAGE header + 4 BODY slots + mtjr.at button; no legacy example.com."""
    if _has_example_com(components):
        return False
    sig = _param_signature(components)
    placeholders = sig.get("body_placeholders") or []
    if len(set(placeholders)) != 4 or set(placeholders) != {1, 2, 3, 4}:
        return False
    has_image_header = any(
        str((c or {}).get("type", "")).upper() == "HEADER"
        and str((c or {}).get("format", "")).upper() == "IMAGE"
        for c in (components or [])
    )
    if not has_image_header:
        return False
    buttons = sig.get("buttons") or []
    if not buttons:
        return False
    url = str(buttons[0].get("url") or "")
    return url == "https://mtjr.at/{{1}}"


def find_existing_order_confirmation_library_draft(
    db: Session,
    tenant_id: int,
    *,
    library_key: str = LIBRARY_KEY,
) -> Optional[Any]:
    from models import WhatsAppTemplate  # noqa: PLC0415

    rows = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id == int(tenant_id),
            WhatsAppTemplate.service_key == SERVICE_KEY,
            WhatsAppTemplate.nahla_source_key == library_key,
            WhatsAppTemplate.status.in_(_PENDING_STATUSES),
            WhatsAppTemplate.is_hidden.is_(False),
        )
        .order_by(WhatsAppTemplate.id.desc())
        .all()
    )
    for row in rows:
        if is_order_confirmation_r3_contract(row.components):
            return row
    return None


def _find_active_lifecycle_slot_template(db: Session, tenant_id: int) -> Optional[Any]:
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


def _unique_template_name(db: Session, tenant_id: int, base: str) -> str:
    import re  # noqa: PLC0415

    from models import WhatsAppTemplate  # noqa: PLC0415

    name = re.sub(r"[^a-z0-9_]", "_", base.lower())[:60]
    existing = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id == int(tenant_id),
            WhatsAppTemplate.name == name,
        )
        .first()
    )
    if existing:
        name = f"{name}_{secrets.token_hex(2)}"
    return name


def _lifecycle_index_violation(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    joined = " ".join(
        str(part)
        for part in (
            exc,
            orig,
            getattr(orig, "diag", None) and getattr(orig.diag, "message_detail", None),
        )
        if part
    )
    return LIFECYCLE_ACTIVE_INDEX in joined


def _import_lock(db: Session, tenant_id: int) -> DedicatedAdvisoryLock:
    return DedicatedAdvisoryLock(
        db,
        namespace=_IMPORT_LOCK_NAMESPACE,
        level_key=int(tenant_id),
    )


def _outcome_from_existing(existing: Any, *, tenant_id: int) -> Dict[str, Any]:
    logger.info(
        "[NahlaImport:OC] Reusing existing r3 draft id=%s tenant=%s",
        existing.id,
        tenant_id,
    )
    return {
        "template": existing,
        "message": MSG_EXISTING_DRAFT,
        "reused_existing_draft": True,
        "created": False,
        "active_template_preserved": True,
    }


def build_merchant_import_api_payload(
    outcome: Dict[str, Any],
    *,
    template_to_dict: Any,
) -> Dict[str, Any]:
    """Merchant-visible import response — no schema/SQL internals."""
    new_tpl = outcome["template"]
    return {
        "success": True,
        "message": outcome["message"],
        "template": template_to_dict(new_tpl),
        "reused_existing_draft": bool(outcome.get("reused_existing_draft")),
        "created": bool(outcome.get("created")),
        "active_template_preserved": bool(outcome.get("active_template_preserved")),
        "store_url_injected": False,
    }


def import_order_summary_from_library(
    db: Session,
    tenant_id: int,
    tpl_def: Dict[str, Any],
    *,
    language: str = "ar",
    custom_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Import ``order_summary`` as an inactive DRAFT linked to any active slot holder.

    Returns a dict with ``template`` (ORM row), ``message``, and merchant-safe flags.
    """
    lock = _import_lock(db, tenant_id)
    lock.acquire_blocking(timeout_seconds=30.0)
    try:
        return _import_order_summary_locked(
            db,
            tenant_id,
            tpl_def,
            language=language,
            custom_name=custom_name,
        )
    finally:
        lock.release()


def _import_order_summary_locked(
    db: Session,
    tenant_id: int,
    tpl_def: Dict[str, Any],
    *,
    language: str = "ar",
    custom_name: Optional[str] = None,
) -> Dict[str, Any]:
    from models import WhatsAppTemplate  # noqa: PLC0415

    schema_probe = _log_schema_probe(db, tenant_id)
    existing = find_existing_order_confirmation_library_draft(db, tenant_id)
    if existing is not None:
        return _outcome_from_existing(existing, tenant_id=tenant_id)

    components = order_summary_r3_components()
    active_slot = _find_active_lifecycle_slot_template(db, tenant_id)
    supersedes_id: Optional[int] = int(active_slot.id) if active_slot is not None else None
    next_revision = 1
    if active_slot is not None and schema_probe.get("revision_column"):
        next_revision = int(getattr(active_slot, "revision", 1) or 1) + 1

    suffix = secrets.token_hex(3)
    if custom_name:
        template_name = _unique_template_name(db, tenant_id, custom_name)
    elif active_slot is not None:
        template_name = _unique_template_name(
            db,
            tenant_id,
            f"nahla_{SERVICE_KEY}_r{next_revision}_{suffix}",
        )
    else:
        template_name = _unique_template_name(
            db,
            tenant_id,
            f"nahla_{LIBRARY_KEY}_{suffix}",
        )

    header_meta: Dict[str, Any] = {
        "revision_label": "r3",
        "header_image_url": order_confirmation_header_public_url(),
        "header_image_asset_key": ORDER_CONFIRMATION_HEADER_ASSET_KEY,
    }
    if supersedes_id is not None and not schema_probe.get("supersedes_template_id_column"):
        header_meta["logical_supersedes_template_id"] = supersedes_id
        header_meta["lineage_note"] = "logical_only_until_migration_0096"
        if active_slot is not None:
            header_meta["supersedes_template_name"] = str(active_slot.name or "")

    now = datetime.now(timezone.utc)
    row_kwargs: Dict[str, Any] = {
        "tenant_id": int(tenant_id),
        "meta_template_id": f"nahla_draft_{LIBRARY_KEY}",
        "name": template_name,
        "language": language,
        "category": tpl_def.get("category") or "UTILITY",
        "status": "DRAFT",
        "components": components,
        "source": "nahla_library",
        "objective": tpl_def.get("smart_trigger"),
        "created_at": now,
        "updated_at": now,
        "synced_at": now,
        "display_name_ar": tpl_def.get("name_ar") or _DISPLAY_NAME_AR,
        "service_key": SERVICE_KEY,
        "nahla_source_key": LIBRARY_KEY,
        "is_active": False,
        "is_hidden": False,
        "step_number": None,
        "has_coupon": bool(tpl_def.get("has_coupon", False)),
        "trigger_delay_hours": tpl_def.get("trigger_delay_hours"),
        "ai_generation_metadata": header_meta,
    }
    if schema_probe.get("revision_column"):
        row_kwargs["revision"] = next_revision
    if schema_probe.get("supersedes_template_id_column") and supersedes_id is not None:
        row_kwargs["supersedes_template_id"] = supersedes_id

    draft = WhatsAppTemplate(**row_kwargs)
    db.add(draft)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        error_code = "nahla_import_lifecycle_active_conflict"
        if _lifecycle_index_violation(exc):
            logger.warning(
                "[%s] lifecycle active slot conflict tenant=%s library=%s",
                error_code,
                tenant_id,
                LIBRARY_KEY,
                exc_info=True,
            )
            reused = find_existing_order_confirmation_library_draft(db, tenant_id)
            if reused is not None:
                return _outcome_from_existing(reused, tenant_id=tenant_id)
            raise NahlaLibraryImportError(MSG_ACTIVE_EXISTS_DRAFT, error_code=error_code) from exc
        logger.error(
            "[%s] integrity failure tenant=%s library=%s",
            "nahla_import_integrity",
            tenant_id,
            LIBRARY_KEY,
            exc_info=True,
        )
        raise NahlaLibraryImportError(MSG_GENERIC_SAVE_FAILED, error_code="nahla_import_integrity") from exc
    except Exception:
        db.rollback()
        logger.error(
            "[nahla_import_flush] tenant=%s library=%s",
            tenant_id,
            LIBRARY_KEY,
            exc_info=True,
        )
        raise NahlaLibraryImportError(MSG_GENERIC_SAVE_FAILED, error_code="nahla_import_flush")

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.error(
            "[nahla_import_commit] integrity tenant=%s library=%s",
            tenant_id,
            LIBRARY_KEY,
            exc_info=True,
        )
        reused = find_existing_order_confirmation_library_draft(db, tenant_id)
        if reused is not None:
            return _outcome_from_existing(reused, tenant_id=tenant_id)
        raise NahlaLibraryImportError(MSG_GENERIC_SAVE_FAILED, error_code="nahla_import_commit")
    except Exception:
        db.rollback()
        logger.error(
            "[nahla_import_commit] tenant=%s library=%s",
            tenant_id,
            LIBRARY_KEY,
            exc_info=True,
        )
        raise NahlaLibraryImportError(MSG_GENERIC_SAVE_FAILED, error_code="nahla_import_commit")

    db.refresh(draft)
    message = MSG_ACTIVE_EXISTS_DRAFT if active_slot is not None else (
        f"تم استيراد القالب '{tpl_def.get('name_ar', '')}' كمسودة."
    )
    logger.info(
        "[NahlaImport:OC] Created draft id=%s tenant=%s supersedes=%s active_preserved=%s",
        draft.id,
        tenant_id,
        supersedes_id,
        active_slot is not None,
    )
    return {
        "template": draft,
        "message": message,
        "reused_existing_draft": False,
        "created": True,
        "active_template_preserved": active_slot is not None,
    }


class NahlaLibraryImportError(Exception):
    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


__all__ = [
    "LIBRARY_KEY",
    "MSG_ACTIVE_EXISTS_DRAFT",
    "MSG_EXISTING_DRAFT",
    "NahlaLibraryImportError",
    "build_merchant_import_api_payload",
    "find_existing_order_confirmation_library_draft",
    "import_order_summary_from_library",
    "inspect_whatsapp_template_schema",
    "is_order_confirmation_r3_contract",
    "order_summary_r3_components",
]
