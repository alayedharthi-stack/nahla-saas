"""
Bounded lifecycle notification dispatcher — reserve, provider send, finalize.

Platform-wide deterministic order lifecycle WhatsApp template sends gated by
``commerce_lifecycle_notification_ledger``. No customer prose generation.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from core.commerce_lifecycle.evidence import (
    OrderLifecycleEvidence,
    validate_capabilities,
    validate_evidence,
    validate_template_evidence,
)
from core.commerce_lifecycle.external_shadow_producer import (
    build_order_lifecycle_evidence,
)
from core.commerce_lifecycle.intents import BusinessIntent
from core.commerce_lifecycle.ledger import (
    SendLedgerOutcome,
    finalize_send_dispatch_error,
    finalize_send_outcome,
    mark_send_sending,
    reserve_send_decision,
)
from core.commerce_lifecycle.registry import get_default_registry
from core.commerce_lifecycle.strategies import ClosedWindowStrategy
from core.merchant_capabilities import resolve_merchant_capabilities
from store_integration.lifecycle_normalization import (
    build_transition_identity,
    normalize_external_lifecycle_intent,
)

logger = logging.getLogger("nahla.commerce_lifecycle.dispatch")

_DISPATCH_CHANNEL = "whatsapp"
_SEND_AUDIT_0094_COLUMNS: frozenset[str] = frozenset({
    "send_state",
    "send_reserved_at",
    "send_attempt_count",
    "reclaim_count",
    "send_attempted_at",
    "send_completed_at",
    "send_error_code",
    "provider_message_id",
    "template_name",
    "template_service_key",
    "last_reclaimed_at",
    "send_method",
})

def commerce_lifecycle_send_audit_schema_ready(db: Session) -> bool:
    """True when migration 0094/0095 send-audit columns are present on the ledger table."""
    try:
        bind = db.get_bind()
        insp = sa_inspect(bind)
        table_names = set(insp.get_table_names())
        if "commerce_lifecycle_notification_ledger" not in table_names:
            return False
        columns = {
            col["name"]
            for col in insp.get_columns("commerce_lifecycle_notification_ledger")
        }
        return _SEND_AUDIT_0094_COLUMNS.issubset(columns)
    except Exception:
        return False

# First production slice — confirmation + shipment only.
_DISPATCHABLE_INTENTS: frozenset[BusinessIntent] = frozenset({
    BusinessIntent.ORDER_CONFIRMED,
    BusinessIntent.PAYMENT_NEEDED,
    BusinessIntent.PAYMENT_CONFIRMED,
    BusinessIntent.SHIPMENT_AVAILABLE,
    BusinessIntent.OUT_FOR_DELIVERY,
    BusinessIntent.ORDER_DELIVERED,
    BusinessIntent.ORDER_CANCELLED,
    BusinessIntent.ORDER_REFUNDED,
    BusinessIntent.INCOMPLETE_ORDER,
})


def commerce_lifecycle_dispatch_enabled() -> bool:
    val = str(
        os.environ.get("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "false")
    ).strip().lower()
    return val in {"1", "true", "yes", "on"}
_ENV_DISPATCH_TENANT_ALLOWLIST = "COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST"
_ENV_DISPATCH_RECIPIENT_ALLOWLIST = "COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST"


def _parse_dispatch_tenant_allowlist() -> frozenset[int]:
    raw = str(os.environ.get(_ENV_DISPATCH_TENANT_ALLOWLIST, "")).strip()
    if not raw:
        return frozenset()
    allowed: set[int] = set()
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        try:
            tenant_id = int(piece)
        except ValueError:
            continue
        if tenant_id > 0:
            allowed.add(tenant_id)
    return frozenset(allowed)


def _parse_dispatch_recipient_allowlist() -> frozenset[str]:
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415
    raw = str(os.environ.get(_ENV_DISPATCH_RECIPIENT_ALLOWLIST, "")).strip()
    if not raw:
        return frozenset()
    allowed: set[str] = set()
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        normalized = normalize_phone(piece) or piece
        allowed.add(normalized)
    return frozenset(allowed)


def commerce_lifecycle_dispatch_tenant_allowlist() -> frozenset[int]:
    """Tenant IDs permitted when lifecycle dispatch master flag is on."""
    return _parse_dispatch_tenant_allowlist()


def commerce_lifecycle_dispatch_recipient_allowlist() -> frozenset[str]:
    """E.164 recipient phones permitted for lifecycle provider sends."""
    return _parse_dispatch_recipient_allowlist()


def commerce_lifecycle_dispatch_tenant_permitted(tenant_id: int) -> bool:
    if not commerce_lifecycle_dispatch_enabled():
        return False
    allowlist = commerce_lifecycle_dispatch_tenant_allowlist()
    if not allowlist:
        return False
    return int(tenant_id) in allowlist


def commerce_lifecycle_dispatch_recipient_permitted(phone: str) -> bool:
    if not commerce_lifecycle_dispatch_enabled():
        return False
    allowlist = commerce_lifecycle_dispatch_recipient_allowlist()
    if not allowlist:
        return False
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415
    normalized = normalize_phone(str(phone or "").strip()) or str(phone or "").strip()
    if not normalized:
        return False
    return normalized in allowlist



@dataclass(frozen=True)
class LifecycleDispatchResult:
    ledger_id: Optional[int]
    dispatched: bool
    duplicate: bool
    outcome: str
    reason_code: Optional[str] = None
    provider_message_id: Optional[str] = None
    recovered: bool = False


def _resolve_service_key(
    intent: BusinessIntent,
    definition: Any,
    evidence: Any = None,
) -> Optional[str]:
    if intent == BusinessIntent.PAYMENT_NEEDED and evidence is not None:
        method = str(getattr(evidence, "payment_method", "") or "").strip().lower()
        if method in {"cod", "cash_on_delivery", "cod_payment"}:
            return "cod_confirmation"
    if definition is not None and getattr(definition, "service_key", None):
        return str(definition.service_key)
    return None


def _build_dispatch_payload(evidence: OrderLifecycleEvidence) -> Dict[str, str]:
    payload: Dict[str, str] = {}
    for field_name in (
        "order_number",
        "checkout_url",
        "payment_url",
        "tracking_url",
        "tracking_number",
        "carrier",
        "payment_method",
        "customer_phone",
        "customer_name",
        "status",
    ):
        value = getattr(evidence, field_name, None)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        payload[field_name] = str(value)
    if evidence.order_number:
        payload.setdefault("external_order_number", evidence.order_number)
    return payload


def _has_tracking_evidence(evidence: OrderLifecycleEvidence) -> bool:
    if str(evidence.tracking_url or "").strip():
        return True
    if str(evidence.tracking_number or "").strip():
        return True
    return False


def _decide_dispatch_eligibility(
    *,
    intent: BusinessIntent,
    definition: Any,
    evidence: OrderLifecycleEvidence,
    evidence_result: Any,
    cap_result: Any,
    template_result: Any,
) -> Tuple[bool, str]:
    if intent not in _DISPATCHABLE_INTENTS:
        return False, "intent_not_dispatchable"

    if intent == BusinessIntent.SHIPMENT_AVAILABLE:
        if not _has_tracking_evidence(evidence):
            return False, "missing_tracking_evidence"

    if definition is None:
        return False, "intent_not_registered"

    if definition.closed_window_strategy != ClosedWindowStrategy.APPROVED_TEMPLATE:
        return False, "no_template_policy"
    if cap_result.forbidden_capabilities:
        return False, "forbidden_capabilities"
    if cap_result.missing_capabilities:
        return False, "missing_capabilities"
    if evidence_result.missing_fields or evidence_result.invalid_fields:
        if evidence_result.invalid_fields:
            return False, "invalid_evidence"
        return False, "missing_evidence"
    if template_result is not None and not template_result.valid:
        return False, "missing_template_evidence"

    return True, "eligible"


async def _execute_reserved_send(
    db: Session,
    *,
    tenant_id: int,
    order_id: int,
    intent: BusinessIntent,
    reserve: Any,
    evidence: OrderLifecycleEvidence,
    service_key: str,
) -> LifecycleDispatchResult:
    to_phone = str(evidence.customer_phone or "").strip()

    if not commerce_lifecycle_dispatch_recipient_permitted(to_phone):
        finalize_send_outcome(
            db,
            ledger_id=reserve.ledger_id,
            tenant_id=int(tenant_id),
            outcome=SendLedgerOutcome.SEND_BLOCKED,
            send_error_code="recipient_not_allowlisted",
            commit=True,
        )
        return LifecycleDispatchResult(
            ledger_id=reserve.ledger_id,
            dispatched=False,
            duplicate=False,
            recovered=reserve.recovered,
            outcome=SendLedgerOutcome.SEND_BLOCKED.value,
            reason_code="recipient_not_allowlisted",
        )

    if not to_phone:
        finalize_send_outcome(
            db,
            ledger_id=reserve.ledger_id,
            tenant_id=int(tenant_id),
            outcome=SendLedgerOutcome.SEND_BLOCKED,
            send_error_code="missing_customer_phone",
            commit=True,
        )
        return LifecycleDispatchResult(
            ledger_id=reserve.ledger_id,
            dispatched=False,
            duplicate=False,
            recovered=reserve.recovered,
            outcome=SendLedgerOutcome.SEND_BLOCKED.value,
            reason_code="missing_customer_phone",
        )

    from core.service_template_resolver import resolve_template_for_send  # noqa: PLC0415

    template = resolve_template_for_send(
        db,
        int(tenant_id),
        service_key,
        step_number=None,
    )
    # Slice A: approved active template is required even when the service
    # window is open — session text must never come from a parallel source.
    if template is None:
        finalize_send_outcome(
            db,
            ledger_id=reserve.ledger_id,
            tenant_id=int(tenant_id),
            outcome=SendLedgerOutcome.SEND_BLOCKED,
            send_error_code="no_approved_template",
            commit=True,
        )
        return LifecycleDispatchResult(
            ledger_id=reserve.ledger_id,
            dispatched=False,
            duplicate=False,
            recovered=reserve.recovered,
            outcome=SendLedgerOutcome.SEND_BLOCKED.value,
            reason_code="no_approved_template",
        )

    from core.commerce_lifecycle.order_updates import is_order_update_enabled  # noqa: PLC0415
    if not is_order_update_enabled(db, int(tenant_id), service_key):
        finalize_send_outcome(
            db,
            ledger_id=reserve.ledger_id,
            tenant_id=int(tenant_id),
            outcome=SendLedgerOutcome.SEND_BLOCKED,
            send_error_code="order_update_disabled",
            commit=True,
        )
        return LifecycleDispatchResult(
            ledger_id=reserve.ledger_id,
            dispatched=False,
            duplicate=False,
            recovered=reserve.recovered,
            outcome=SendLedgerOutcome.SEND_BLOCKED.value,
            reason_code="order_update_disabled",
        )

    from core.wa_usage import has_open_service_window  # noqa: PLC0415
    from services.customer_intelligence import normalize_phone  # noqa: PLC0415
    from models import CommerceLifecycleNotificationLedger  # noqa: PLC0415
    from core.commerce_lifecycle.ledger import sanitize_dispatch_decision  # noqa: PLC0415

    window_phone = normalize_phone(to_phone) or to_phone
    try:
        window_open = has_open_service_window(db, int(tenant_id), window_phone)
    except Exception as exc:  # noqa: BLE001 — fail closed to template path
        logger.warning(
            "[LifecycleDispatch] window_check_failed tenant=%s order=%s err=%s",
            tenant_id,
            order_id,
            exc,
        )
        window_open = False
    send_method = "session_message" if window_open else "approved_template"

    # Persist path decision on the reserved ledger row before CAS→sending.
    row = (
        db.query(CommerceLifecycleNotificationLedger)
        .filter_by(id=int(reserve.ledger_id), tenant_id=int(tenant_id))
        .one()
    )
    decision = dict(row.dispatch_decision_json or {})
    decision["send_method"] = send_method
    row.dispatch_decision_json = sanitize_dispatch_decision(decision)
    row.send_method = send_method
    db.flush()
    db.commit()

    sending = mark_send_sending(
        db,
        ledger_id=reserve.ledger_id,
        tenant_id=int(tenant_id),
        template_name=template.name,
        template_service_key=service_key,
        send_method=send_method,
        commit=True,
    )
    if not sending.transitioned:
        logger.info(
            "[LifecycleDispatch] send_cas_loser tenant=%s order=%s intent=%s ledger=%s state=%s",
            tenant_id,
            order_id,
            intent.value,
            reserve.ledger_id,
            sending.send_state,
        )
        return LifecycleDispatchResult(
            ledger_id=reserve.ledger_id,
            dispatched=False,
            duplicate=True,
            recovered=reserve.recovered,
            outcome=sending.outcome,
            reason_code="duplicate",
        )

    payload = _build_dispatch_payload(evidence)
    if send_method == "session_message":
        from core.automation_engine import send_lifecycle_whatsapp_session_body  # noqa: PLC0415

        send_outcome, send_info = await send_lifecycle_whatsapp_session_body(
            db,
            int(tenant_id),
            to_phone,
            template,
            payload,
            customer_name=evidence.customer_name,
            service_key=service_key,
        )
    else:
        from core.automation_engine import send_lifecycle_whatsapp_template  # noqa: PLC0415

        send_outcome, send_info = await send_lifecycle_whatsapp_template(
            db,
            int(tenant_id),
            to_phone,
            template,
            payload,
            customer_name=evidence.customer_name,
            service_key=service_key,
        )

    if send_outcome == "sent":
        final = finalize_send_outcome(
            db,
            ledger_id=reserve.ledger_id,
            tenant_id=int(tenant_id),
            outcome=SendLedgerOutcome.SENT,
            provider_message_id=str(send_info.get("wa_message_id") or ""),
            template_name=template.name,
            send_method=send_method,
            commit=True,
        )
        logger.info(
            "[LifecycleDispatch] sent tenant=%s order=%s intent=%s method=%s wamid=%s",
            tenant_id,
            order_id,
            intent.value,
            send_method,
            final.provider_message_id,
        )
        return LifecycleDispatchResult(
            ledger_id=reserve.ledger_id,
            dispatched=True,
            duplicate=False,
            recovered=reserve.recovered,
            outcome=SendLedgerOutcome.SENT.value,
            reason_code=None,
            provider_message_id=final.provider_message_id,
        )

    if send_outcome == "ambiguous":
        final = finalize_send_outcome(
            db,
            ledger_id=reserve.ledger_id,
            tenant_id=int(tenant_id),
            outcome=SendLedgerOutcome.AMBIGUOUS,
            send_error_code=str(send_info.get("error_code") or "provider_empty_response"),
            template_name=template.name,
            send_method=send_method,
            commit=True,
        )
        return LifecycleDispatchResult(
            ledger_id=reserve.ledger_id,
            dispatched=False,
            duplicate=False,
            recovered=reserve.recovered,
            outcome=SendLedgerOutcome.AMBIGUOUS.value,
            reason_code=final.send_error_code,
        )

    final = finalize_send_outcome(
        db,
        ledger_id=reserve.ledger_id,
        tenant_id=int(tenant_id),
        outcome=SendLedgerOutcome.FAILED,
        send_error_code=str(send_info.get("error_code") or "provider_send_failed"),
        template_name=template.name,
        send_method=send_method,
        commit=True,
    )
    return LifecycleDispatchResult(
        ledger_id=reserve.ledger_id,
        dispatched=False,
        duplicate=False,
        recovered=reserve.recovered,
        outcome=SendLedgerOutcome.FAILED.value,
        reason_code=final.send_error_code,
    )


async def dispatch_external_lifecycle_notification(
    db: Session,
    *,
    tenant_id: int,
    order: Any,
    provider: str,
    raw_previous_status: Optional[str],
    raw_current_status: str,
    normalized_order: Mapping[str, Any],
    raw_payload: Optional[Mapping[str, Any]] = None,
) -> LifecycleDispatchResult:
    """
    Normalize transition → reserve ledger → provider template send → finalize.

    Duplicate reservation or ambiguous prior send never triggers a provider call.
    """
    if not commerce_lifecycle_dispatch_enabled():
        return LifecycleDispatchResult(
            ledger_id=None,
            dispatched=False,
            duplicate=False,
            outcome="disabled",
            reason_code="dispatch_disabled",
        )

    if not commerce_lifecycle_dispatch_tenant_permitted(int(tenant_id)):
        return LifecycleDispatchResult(
            ledger_id=None,
            dispatched=False,
            duplicate=False,
            outcome="skipped",
            reason_code="tenant_not_allowlisted",
        )

    order_id = int(getattr(order, "id", 0) or 0)
    if order_id <= 0:
        return LifecycleDispatchResult(
            ledger_id=None,
            dispatched=False,
            duplicate=False,
            outcome="skipped",
            reason_code="missing_order_id",
        )

    ledger_id: Optional[int] = None
    reserve = None

    try:
        intent, _norm_reason = normalize_external_lifecycle_intent(
            provider=provider,
            raw_previous_status=raw_previous_status,
            raw_current_status=raw_current_status,
            normalized_order=normalized_order,
        )
        if intent is None:
            return LifecycleDispatchResult(
                ledger_id=None,
                dispatched=False,
                duplicate=False,
                outcome="skipped",
                reason_code="no_intent",
            )

        external_order_id = str(
            getattr(order, "external_id", None)
            or normalized_order.get("external_id")
            or ""
        ).strip()
        source_event_id, transition_version = build_transition_identity(
            provider=provider,
            external_order_id=external_order_id,
            raw_previous_status=raw_previous_status,
            raw_current_status=raw_current_status,
            raw_payload=raw_payload,
        )

        evidence = build_order_lifecycle_evidence(
            order=order,
            normalized_order=normalized_order,
            raw_payload=raw_payload,
            source_event_id=source_event_id,
            transition_version=transition_version,
        )

        if not commerce_lifecycle_dispatch_recipient_permitted(
            str(evidence.customer_phone or "")
        ):
            logger.info(
                "[LifecycleDispatch] recipient_blocked tenant=%s order=%s intent=%s",
                tenant_id,
                order_id,
                intent.value,
            )
            return LifecycleDispatchResult(
                ledger_id=None,
                dispatched=False,
                duplicate=False,
                outcome="skipped",
                reason_code="recipient_not_allowlisted",
            )

        registry = get_default_registry()
        definition = registry.try_get(intent)
        merchant_caps = resolve_merchant_capabilities(db, int(tenant_id))

        evidence_result = (
            validate_evidence(definition, evidence)
            if definition is not None
            else None
        )
        template_result = (
            validate_template_evidence(definition, evidence)
            if definition is not None
            else None
        )
        cap_result = (
            validate_capabilities(definition, merchant_caps)
            if definition is not None
            else None
        )

        eligible, reason_code = _decide_dispatch_eligibility(
            intent=intent,
            definition=definition,
            evidence=evidence,
            evidence_result=evidence_result,
            cap_result=cap_result,
            template_result=template_result,
        )
        if not eligible:
            logger.info(
                "[LifecycleDispatch] skipped tenant=%s order=%s intent=%s reason=%s",
                tenant_id,
                order_id,
                intent.value,
                reason_code,
            )
            return LifecycleDispatchResult(
                ledger_id=None,
                dispatched=False,
                duplicate=False,
                outcome="skipped",
                reason_code=reason_code,
            )

        service_key = _resolve_service_key(intent, definition, evidence)
        if not service_key:
            return LifecycleDispatchResult(
                ledger_id=None,
                dispatched=False,
                duplicate=False,
                outcome="skipped",
                reason_code="no_service_key",
            )

        dispatch_decision = {
            "handoff_kind": "lifecycle_notification",
            "intent": intent.value,
            "service_key": service_key,
            "reason_code": reason_code,
            "business_evidence_valid": "true",
            "capabilities_valid": "true",
            "template_evidence_valid": (
                "true"
                if template_result is None or template_result.valid
                else "false"
            ),
        }

        if not commerce_lifecycle_send_audit_schema_ready(db):
            logger.warning(
                "[LifecycleDispatch] migration_0095_required tenant=%s order=%s intent=%s",
                tenant_id,
                order_id,
                intent.value,
            )
            return LifecycleDispatchResult(
                ledger_id=None,
                dispatched=False,
                duplicate=False,
                outcome="skipped",
                reason_code="migration_0095_required",
            )

        reserve = reserve_send_decision(
            db,
            tenant_id=int(tenant_id),
            order_id=order_id,
            business_intent=intent,
            channel=_DISPATCH_CHANNEL,
            source_event_id=source_event_id,
            transition_version=transition_version,
            dispatch_decision=dispatch_decision,
            capabilities_snapshot=merchant_caps.to_dict(),
            evidence_present=tuple(
                name
                for name in (
                    "order_number",
                    "checkout_url",
                    "payment_url",
                    "tracking_url",
                    "tracking_number",
                    "carrier",
                    "delivered_at",
                    "payment_method",
                    "review_url",
                    "coupon_code",
                    "customer_phone",
                    "customer_name",
                    "status",
                    "source_event_id",
                    "transition_version",
                )
                if getattr(evidence, name, None) is not None
            ),
            template_service_key=service_key,
            commit=True,
        )
        ledger_id = reserve.ledger_id

        if reserve.duplicate:
            logger.info(
                "[LifecycleDispatch] duplicate tenant=%s order=%s intent=%s ledger=%s",
                tenant_id,
                order_id,
                intent.value,
                reserve.ledger_id,
            )
            return LifecycleDispatchResult(
                ledger_id=reserve.ledger_id,
                dispatched=False,
                duplicate=True,
                outcome=reserve.outcome,
                reason_code="duplicate",
            )

        return await _execute_reserved_send(
            db,
            tenant_id=int(tenant_id),
            order_id=order_id,
            intent=intent,
            reserve=reserve,
            evidence=evidence,
            service_key=service_key,
        )
    except SQLAlchemyError:
        if ledger_id and reserve is not None and not reserve.duplicate:
            try:
                finalize_send_dispatch_error(
                    db,
                    ledger_id=ledger_id,
                    tenant_id=int(tenant_id),
                    commit=True,
                )
            except Exception:
                logger.exception(
                    "[LifecycleDispatch] failed to finalize SQL error tenant=%s ledger=%s",
                    tenant_id,
                    ledger_id,
                )
        raise
    except Exception as exc:
        if ledger_id and reserve is not None and not reserve.duplicate:
            try:
                finalize_send_dispatch_error(
                    db,
                    ledger_id=ledger_id,
                    tenant_id=int(tenant_id),
                    send_error_code="dispatch_error",
                    commit=True,
                )
            except Exception:
                logger.exception(
                    "[LifecycleDispatch] failed to finalize dispatch error tenant=%s ledger=%s",
                    tenant_id,
                    ledger_id,
                )
        logger.exception(
            "[LifecycleDispatch] failed tenant=%s order=%s status=%s ledger=%s",
            tenant_id,
            order_id,
            str(raw_current_status or ""),
            ledger_id,
        )
        return LifecycleDispatchResult(
            ledger_id=ledger_id,
            dispatched=False,
            duplicate=False,
            outcome="error",
            reason_code="dispatch_error",
        )


__all__ = [
    "LifecycleDispatchResult",
    "commerce_lifecycle_dispatch_enabled",
    "commerce_lifecycle_dispatch_tenant_allowlist",
    "commerce_lifecycle_dispatch_recipient_allowlist",
    "commerce_lifecycle_dispatch_tenant_permitted",
    "commerce_lifecycle_dispatch_recipient_permitted",
    "commerce_lifecycle_send_audit_schema_ready",
    "dispatch_external_lifecycle_notification",
]

