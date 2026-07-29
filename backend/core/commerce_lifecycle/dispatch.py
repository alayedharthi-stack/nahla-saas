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



# First production slice — confirmation + shipment only.

_DISPATCHABLE_INTENTS: frozenset[BusinessIntent] = frozenset({

    BusinessIntent.ORDER_CONFIRMED,

    BusinessIntent.SHIPMENT_AVAILABLE,

})





def commerce_lifecycle_dispatch_enabled() -> bool:

    val = str(

        os.environ.get("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "false")

    ).strip().lower()

    return val in {"1", "true", "yes", "on"}





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

) -> Optional[str]:

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



    mark_send_sending(

        db,

        ledger_id=reserve.ledger_id,

        tenant_id=int(tenant_id),

        template_name=template.name,

        template_service_key=service_key,

        commit=True,

    )



    from core.automation_engine import send_lifecycle_whatsapp_template  # noqa: PLC0415



    send_outcome, send_info = await send_lifecycle_whatsapp_template(

        db,

        int(tenant_id),

        to_phone,

        template,

        _build_dispatch_payload(evidence),

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

            commit=True,

        )

        logger.info(

            "[LifecycleDispatch] sent tenant=%s order=%s intent=%s wamid=%s",

            tenant_id,

            order_id,

            intent.value,

            final.provider_message_id,

        )

        return LifecycleDispatchResult(

            ledger_id=reserve.ledger_id,

            dispatched=True,

            duplicate=False,

            recovered=reserve.recovered,

            outcome=SendLedgerOutcome.SENT.value,

            provider_message_id=final.provider_message_id,

        )



    terminal = (

        SendLedgerOutcome.AMBIGUOUS

        if send_outcome == "ambiguous"

        else SendLedgerOutcome.FAILED

    )

    final = finalize_send_outcome(

        db,

        ledger_id=reserve.ledger_id,

        tenant_id=int(tenant_id),

        outcome=terminal,

        send_error_code=str(send_info.get("error_code") or send_outcome),

        template_name=template.name,

        commit=True,

    )

    logger.warning(

        "[LifecycleDispatch] %s tenant=%s order=%s intent=%s code=%s",

        terminal.value,

        tenant_id,

        order_id,

        intent.value,

        final.send_error_code,

    )

    return LifecycleDispatchResult(

        ledger_id=reserve.ledger_id,

        dispatched=False,

        duplicate=False,

        recovered=reserve.recovered,

        outcome=terminal.value,

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



        if intent == BusinessIntent.OUT_FOR_DELIVERY:

            return LifecycleDispatchResult(

                ledger_id=None,

                dispatched=False,

                duplicate=False,

                outcome="skipped",

                reason_code="intent_not_dispatchable",

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



        service_key = _resolve_service_key(intent, definition)

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

    "dispatch_external_lifecycle_notification",

]


