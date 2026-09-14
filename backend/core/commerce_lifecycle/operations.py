"""Fail-closed operator diagnostics and post-COD notification recovery."""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.commerce_lifecycle.canary_guard import (
    commerce_lifecycle_dispatch_enabled,
    commerce_lifecycle_dispatch_recipient_permitted,
    commerce_lifecycle_dispatch_tenant_permitted,
)
from core.commerce_lifecycle.dispatch import commerce_lifecycle_send_audit_schema_status
from core.commerce_lifecycle.intents import BusinessIntent
from core.commerce_lifecycle.order_updates import resolve_lifecycle_template_for_send
from services.customer_intelligence import normalize_phone


FINAL_SERVICE_KEY = "order_confirmation"
POST_COD_STATUS = "under_review"
_COD_METHODS = frozenset({"cod", "cash_on_delivery", "cod_payment", "cash"})


def _alembic_revisions(db: Session) -> Dict[str, Any]:
    try:
        revisions = [
            str(row[0])
            for row in db.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            ).all()
        ]
        return {"alembic_revisions": revisions, "alembic_read_error": False}
    except Exception:
        db.rollback()
        return {"alembic_revisions": [], "alembic_read_error": True}


def build_lifecycle_preflight(db: Session) -> Dict[str, Any]:
    """Read the actual connected schema and effective master dispatch gate."""
    return {
        **commerce_lifecycle_send_audit_schema_status(db),
        **_alembic_revisions(db),
        "dispatch_enabled": commerce_lifecycle_dispatch_enabled(),
    }


def _order_phone(order: Any) -> str:
    info = dict(getattr(order, "customer_info", None) or {})
    return str(info.get("phone") or info.get("mobile") or "").strip()


def _masked_phone(phone: str) -> Optional[str]:
    normalized = normalize_phone(phone)
    return f"*{normalized[-4:]}" if normalized else None


def _order_evidence(order: Any) -> Dict[str, bool]:
    meta = dict(getattr(order, "extra_metadata", None) or {})
    payment_method = str(meta.get("payment_method") or "").strip().lower()
    external_id = str(getattr(order, "external_id", None) or "").strip()
    pushed_external_id = str(meta.get("cod_pushed_external_id") or "").strip()
    cod_evidence = bool(
        payment_method in _COD_METHODS
        and (meta.get("is_cod") is True or meta.get("cod_webhook_triggered") is True)
    )
    initial_evidence = bool(
        meta.get("nahla_cod_confirmation_sent") is True
        and meta.get("nahla_cod_confirmation_sent_at")
        and (
            meta.get("nahla_cod_confirmation_wamid")
            or meta.get("nahla_cod_confirmation_execution_id")
        )
    )
    store_confirmation = bool(
        external_id
        and meta.get("cod_confirmed_at")
        and pushed_external_id
        and pushed_external_id == external_id
    )
    return {
        "cod_evidence_present": cod_evidence,
        "initial_cod_confirmation_evidence_present": initial_evidence,
        "store_confirmation_evidence_present": store_confirmation,
    }


def _latest_final_ledger(db: Session, *, tenant_id: int, order_id: int):
    from models import CommerceLifecycleNotificationLedger  # noqa: PLC0415

    return (
        db.query(CommerceLifecycleNotificationLedger)
        .filter(
            CommerceLifecycleNotificationLedger.tenant_id == int(tenant_id),
            CommerceLifecycleNotificationLedger.order_id == int(order_id),
            CommerceLifecycleNotificationLedger.business_intent
            == BusinessIntent.ORDER_CONFIRMED.value,
        )
        .order_by(CommerceLifecycleNotificationLedger.id.desc())
        .first()
    )


def build_order_recovery_preflight(db: Session, *, order_id: int) -> Dict[str, Any]:
    """Return masked order recovery eligibility without mutating state."""
    from models import Order  # noqa: PLC0415

    order = db.query(Order).filter(Order.id == int(order_id)).first()
    if order is None:
        return {
            "order_id": int(order_id),
            "order_exists": False,
            "recovery_eligible": False,
            "ineligibility_reasons": ["order_not_found"],
        }

    tenant_id = int(order.tenant_id)
    phone = _order_phone(order)
    normalized = normalize_phone(phone)
    evidence = _order_evidence(order)
    template = resolve_lifecycle_template_for_send(db, tenant_id, FINAL_SERVICE_KEY)
    ledger = _latest_final_ledger(db, tenant_id=tenant_id, order_id=int(order.id))
    schema = commerce_lifecycle_send_audit_schema_status(db)

    reasons = []
    if str(order.status or "").strip().lower() != POST_COD_STATUS:
        reasons.append("status_not_under_review")
    if not evidence["cod_evidence_present"]:
        reasons.append("cod_evidence_missing")
    if not evidence["initial_cod_confirmation_evidence_present"]:
        reasons.append("initial_cod_confirmation_evidence_missing")
    if not evidence["store_confirmation_evidence_present"]:
        reasons.append("store_confirmation_evidence_missing")
    if not normalized:
        reasons.append("recipient_missing")
    if not schema["schema_ready"]:
        reasons.append("migration_0095_required")
    if template is None or str(getattr(template, "status", "")).upper() != "APPROVED":
        reasons.append("no_approved_template")

    return {
        "order_id": int(order.id),
        "order_exists": True,
        "tenant_id": tenant_id,
        "order_status": str(order.status or ""),
        **evidence,
        "recipient_masked": _masked_phone(phone),
        "tenant_permitted": commerce_lifecycle_dispatch_tenant_permitted(tenant_id),
        "recipient_permitted": bool(
            normalized and commerce_lifecycle_dispatch_recipient_permitted(normalized)
        ),
        "approved_order_confirmation_template_available": bool(template is not None),
        "resolved_template_name": getattr(template, "name", None),
        "final_lifecycle_ledger_exists": bool(ledger is not None),
        "final_lifecycle_ledger_id": int(ledger.id) if ledger is not None else None,
        "final_lifecycle_send_state": getattr(ledger, "send_state", None),
        "final_lifecycle_outcome": getattr(ledger, "outcome", None),
        "schema_ready": bool(schema["schema_ready"]),
        "recovery_eligible": not reasons,
        "ineligibility_reasons": reasons,
    }


async def retry_post_cod_final_confirmation(
    db: Session,
    *,
    order_id: int,
) -> Dict[str, Any]:
    """Retry only the canonical final notification for a proven COD order."""
    from models import Order  # noqa: PLC0415
    from services.cod_confirmation import send_order_confirmation_after_cod  # noqa: PLC0415

    preflight = build_order_recovery_preflight(db, order_id=int(order_id))
    if not preflight.get("order_exists") or not preflight.get("recovery_eligible"):
        reasons = list(preflight.get("ineligibility_reasons") or ["ineligible_order"])
        reason = reasons[0] if len(reasons) == 1 else "ineligible_order"
        return {
            "outcome": reason,
            "sent": False,
            "duplicate": False,
            "service_key": FINAL_SERVICE_KEY,
            "template_name": preflight.get("resolved_template_name"),
            "ledger_id": preflight.get("final_lifecycle_ledger_id"),
            "provider_message_id": None,
            "eligibility": preflight,
        }

    order = (
        db.query(Order)
        .filter(Order.id == int(order_id), Order.tenant_id == int(preflight["tenant_id"]))
        .one()
    )
    result = await send_order_confirmation_after_cod(
        db,
        tenant_id=int(order.tenant_id),
        order=order,
    )
    sent = bool(result.get("sent"))
    duplicate = bool(result.get("duplicate"))
    return {
        "outcome": "sent" if sent else ("duplicate" if duplicate else result.get("error")),
        "sent": sent,
        "duplicate": duplicate,
        "service_key": FINAL_SERVICE_KEY,
        "template_name": preflight.get("resolved_template_name"),
        "ledger_id": result.get("ledger_id"),
        "provider_message_id": result.get("provider_message_id"),
        "eligibility": preflight,
    }


__all__ = [
    "FINAL_SERVICE_KEY",
    "build_lifecycle_preflight",
    "build_order_recovery_preflight",
    "retry_post_cod_final_confirmation",
]
