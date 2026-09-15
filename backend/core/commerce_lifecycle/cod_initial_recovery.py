"""Idempotent recovery for a transiently blocked initial Salla COD prompt."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.commerce_lifecycle.intents import BusinessIntent

logger = logging.getLogger("nahla.commerce_lifecycle.cod_initial_recovery")

_COD_METHODS = frozenset({"cod", "cash_on_delivery", "cod_payment", "cash"})


@dataclass(frozen=True)
class CodInitialRecoveryResult:
    attempted: bool
    sent: bool
    duplicate: bool
    reason_code: str
    ledger_id: Optional[int] = None


def _metadata(order: Any) -> dict[str, Any]:
    raw = getattr(order, "extra_metadata", None)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _cod_truth_is_proven(meta: Mapping[str, Any]) -> bool:
    method = str(meta.get("payment_method") or "").strip().lower()
    return bool(
        meta.get("cod_webhook_triggered") is True
        or (meta.get("is_cod") is True and method in _COD_METHODS)
    )


def _successful_initial_send_exists(
    db: Session,
    *,
    tenant_id: int,
    order_id: int,
) -> bool:
    """Protect both lifecycle-ledger and legacy Automation send ownership."""
    from models import (  # noqa: PLC0415
        AutomationEvent,
        AutomationExecution,
        CommerceLifecycleNotificationLedger,
        SmartAutomation,
    )

    lifecycle_sent = (
        db.query(CommerceLifecycleNotificationLedger.id)
        .filter(
            CommerceLifecycleNotificationLedger.tenant_id == int(tenant_id),
            CommerceLifecycleNotificationLedger.order_id == int(order_id),
            CommerceLifecycleNotificationLedger.business_intent
            == BusinessIntent.COD_CONFIRMATION.value,
            CommerceLifecycleNotificationLedger.send_state == "sent",
        )
        .first()
    )
    if lifecycle_sent is not None:
        return True

    events = (
        db.query(AutomationEvent)
        .filter(
            AutomationEvent.tenant_id == int(tenant_id),
            AutomationEvent.event_type == "order_cod_pending",
        )
        .order_by(AutomationEvent.id.desc())
        .limit(200)
        .all()
    )
    for event in events:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, Mapping):
            continue
        try:
            bound_order_id = int(
                payload.get("order_internal_id") or payload.get("order_id")
            )
        except (TypeError, ValueError):
            continue
        if (
            bound_order_id != int(order_id)
            or str(payload.get("message_type") or "") != "initial_confirmation"
        ):
            continue
        execution = (
            db.query(AutomationExecution.id)
            .join(
                SmartAutomation,
                AutomationExecution.automation_id == SmartAutomation.id,
            )
            .filter(
                AutomationExecution.tenant_id == int(tenant_id),
                AutomationExecution.event_id == int(event.id),
                AutomationExecution.status == "sent",
                SmartAutomation.tenant_id == int(tenant_id),
                SmartAutomation.automation_type == "cod_confirmation",
            )
            .first()
        )
        if execution is not None:
            return True
    return False


def _normalized_recovery_order(order: Any, meta: Mapping[str, Any]) -> dict[str, Any]:
    customer_info = getattr(order, "customer_info", None)
    customer_info = dict(customer_info) if isinstance(customer_info, Mapping) else {}
    method = str(meta.get("payment_method") or "").strip().lower()
    return {
        "external_id": str(getattr(order, "external_id", None) or ""),
        "external_order_number": getattr(order, "external_order_number", None),
        "status": str(getattr(order, "status", None) or ""),
        "total": getattr(order, "total", None),
        "checkout_url": getattr(order, "checkout_url", None),
        "customer_name": (
            getattr(order, "customer_name", None)
            or customer_info.get("name")
        ),
        "customer_phone": (
            customer_info.get("phone")
            or customer_info.get("mobile")
        ),
        "payment_method": method,
        "payment_status": meta.get("payment_status"),
        "is_cod": meta.get("is_cod") is True,
        "lifecycle_observation": "cod_initial_reconciliation",
    }


def _resolve_and_rebind_initial_cod_template(
    db: Session,
    *,
    tenant_id: int,
) -> Any:
    """Resolve only the canonical initial COD template, never a reminder."""
    from core.commerce_lifecycle.order_updates import (  # noqa: PLC0415
        resolve_lifecycle_template_for_send,
    )
    from core.service_template_resolver import ensure_single_active  # noqa: PLC0415
    from models import WhatsAppTemplate  # noqa: PLC0415

    strict = resolve_lifecycle_template_for_send(
        db, int(tenant_id), "cod_confirmation"
    )
    if strict is not None and (
        str(getattr(strict, "nahla_source_key", None) or "") == "cod_confirmation"
        or str(getattr(strict, "name", None) or "").startswith(
            "nahla_cod_confirmation_"
        )
    ):
        return strict

    # The cod_confirmation service also owns later reminder templates.  A
    # generic same-service fallback can therefore select a reminder, which is
    # not a valid substitute for the initial order-bound prompt.  Require the
    # canonical initial library identity (or its deterministic Meta name).
    template = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id == int(tenant_id),
            WhatsAppTemplate.status == "APPROVED",
            or_(
                WhatsAppTemplate.nahla_source_key == "cod_confirmation",
                WhatsAppTemplate.name.like("nahla_cod_confirmation_%"),
            ),
        )
        .order_by(WhatsAppTemplate.updated_at.desc(), WhatsAppTemplate.id.desc())
        .first()
    )
    if template is None:
        return None

    # Repair a stale multi-step/hidden binding without broadening template
    # selection.  Keep the target inactive until competing slot rows have
    # been deactivated by the existing uniqueness helper.
    template.service_key = "cod_confirmation"
    template.step_number = None
    template.is_hidden = False
    template.is_active = False
    db.flush()
    ensure_single_active(
        db,
        int(tenant_id),
        "cod_confirmation",
        None,
        int(template.id),
    )
    db.flush()
    logger.info(
        "[CODRecovery] rebound canonical initial template tenant=%s "
        "template_id=%s name=%s",
        tenant_id,
        template.id,
        template.name,
    )
    return template


async def reconcile_missing_initial_cod_confirmation(
    db: Session,
    *,
    tenant_id: int,
    order: Any,
    provider: str = "salla",
) -> CodInitialRecoveryResult:
    """Re-evaluate one proven pending COD order through the canonical ledger.

    The caller is a normal StoreSync reconciliation cycle.  This function
    never mutates Salla, emits generic lifecycle text, or invokes AI.
    """
    from core.commerce_lifecycle.canary_guard import (  # noqa: PLC0415
        commerce_lifecycle_dispatch_tenant_permitted,
    )
    from core.commerce_lifecycle.dispatch import (  # noqa: PLC0415
        commerce_lifecycle_dispatch_enabled,
        dispatch_external_lifecycle_notification,
    )
    from core.internal_e2e_safety import is_internal_e2e_order  # noqa: PLC0415
    from store_adapters.salla_lifecycle import (  # noqa: PLC0415
        salla_cod_requires_customer_confirmation,
    )

    order_id = int(getattr(order, "id", 0) or 0)
    provider_key = str(provider or "").strip().lower()
    if order_id <= 0 or is_internal_e2e_order(order):
        return CodInitialRecoveryResult(False, False, False, "ineligible_order")
    if provider_key != "salla":
        return CodInitialRecoveryResult(False, False, False, "provider_not_salla")
    if not commerce_lifecycle_dispatch_enabled():
        return CodInitialRecoveryResult(False, False, False, "dispatch_disabled")
    if not commerce_lifecycle_dispatch_tenant_permitted(int(tenant_id)):
        return CodInitialRecoveryResult(False, False, False, "tenant_not_allowlisted")

    meta = _metadata(order)
    if not _cod_truth_is_proven(meta):
        return CodInitialRecoveryResult(False, False, False, "cod_not_proven")
    if meta.get("nahla_cod_confirmation_sent") is True:
        return CodInitialRecoveryResult(False, False, True, "already_sent_stamp")

    normalized = _normalized_recovery_order(order, meta)
    if not salla_cod_requires_customer_confirmation(
        getattr(order, "status", None), normalized
    ):
        return CodInitialRecoveryResult(False, False, False, "status_not_pending")
    if _successful_initial_send_exists(
        db,
        tenant_id=int(tenant_id),
        order_id=order_id,
    ):
        return CodInitialRecoveryResult(False, False, True, "already_sent_evidence")
    if _resolve_and_rebind_initial_cod_template(
        db, tenant_id=int(tenant_id)
    ) is None:
        return CodInitialRecoveryResult(False, False, False, "no_approved_template")

    status = str(getattr(order, "status", None) or "")
    dispatch = await dispatch_external_lifecycle_notification(
        db,
        tenant_id=int(tenant_id),
        order=order,
        provider=provider_key,
        raw_previous_status=status,
        raw_current_status=status,
        normalized_order=normalized,
        raw_payload=None,
    )
    return CodInitialRecoveryResult(
        attempted=True,
        sent=bool(dispatch.dispatched),
        duplicate=bool(dispatch.duplicate),
        reason_code=str(dispatch.reason_code or dispatch.outcome),
        ledger_id=dispatch.ledger_id,
    )


__all__ = [
    "CodInitialRecoveryResult",
    "reconcile_missing_initial_cod_confirmation",
]
