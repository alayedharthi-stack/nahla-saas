"""
Disabled-by-default shadow producer for external-store lifecycle transitions (PR 2C).

Writes PR 2B ledger rows only — no sends, templates, automation, or AI.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Mapping, Optional, Tuple

from sqlalchemy.orm import Session

from core.commerce_lifecycle.evidence import (
    OrderLifecycleEvidence,
    validate_capabilities,
    validate_evidence,
    validate_template_evidence,
)
from core.commerce_lifecycle.external_shadow_flags import (
    commerce_lifecycle_external_shadow_enabled,
)
from core.commerce_lifecycle.intents import BusinessIntent
from core.commerce_lifecycle.ledger import (
    ShadowLedgerOutcome,
    mark_shadow_outcome,
    reserve_shadow_decision,
)
from core.commerce_lifecycle.registry import get_default_registry
from core.commerce_lifecycle.strategies import ClosedWindowStrategy
from core.merchant_capabilities import resolve_merchant_capabilities
from store_integration.lifecycle_normalization import (
    build_transition_identity,
    normalize_external_lifecycle_intent,
)

logger = logging.getLogger("nahla.commerce_lifecycle.external_shadow")

_SHADOW_CHANNEL = "whatsapp"


def _parse_delivered_at(meta: Mapping[str, Any]) -> Optional[datetime]:
    raw = meta.get("delivered_at")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _non_empty(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _extract_tracking_url(
    raw_payload: Optional[Mapping[str, Any]],
    normalized_order: Mapping[str, Any],
) -> Optional[str]:
    if raw_payload:
        shipping = raw_payload.get("shipping")
        if isinstance(shipping, dict):
            link = _non_empty(shipping.get("tracking_link"))
            if link:
                return link
        for key in ("tracking_url", "tracking_link"):
            link = _non_empty(raw_payload.get(key))
            if link:
                return link
    return _non_empty(normalized_order.get("tracking_url"))


def _extract_payment_url(
    raw_payload: Optional[Mapping[str, Any]],
    normalized_order: Mapping[str, Any],
) -> Optional[str]:
    if raw_payload:
        direct = _non_empty(raw_payload.get("payment_url"))
        if direct:
            return direct
        payment = raw_payload.get("payment")
        if isinstance(payment, dict):
            nested = _non_empty(payment.get("url") or payment.get("payment_url"))
            if nested:
                return nested
    return _non_empty(normalized_order.get("payment_url"))


def build_order_lifecycle_evidence(
    *,
    order: Any,
    normalized_order: Mapping[str, Any],
    raw_payload: Optional[Mapping[str, Any]],
    source_event_id: str,
    transition_version: str,
) -> OrderLifecycleEvidence:
    meta = dict(getattr(order, "extra_metadata", None) or {})
    customer_info = dict(getattr(order, "customer_info", None) or {})

    checkout_url = _non_empty(getattr(order, "checkout_url", None)) or _non_empty(
        normalized_order.get("checkout_url")
    )
    payment_url = _extract_payment_url(raw_payload, normalized_order)
    tracking_url = _extract_tracking_url(raw_payload, normalized_order)
    tracking_number = _non_empty(meta.get("tracking_number"))
    carrier = None
    if raw_payload and isinstance(raw_payload.get("shipping"), dict):
        company = raw_payload["shipping"].get("company")
        if isinstance(company, dict):
            carrier = _non_empty(company.get("name"))
        else:
            carrier = _non_empty(company)

    return OrderLifecycleEvidence(
        order_number=_non_empty(getattr(order, "external_order_number", None))
        or _non_empty(normalized_order.get("external_order_number"))
        or _non_empty(getattr(order, "external_id", None)),
        checkout_url=checkout_url,
        payment_url=payment_url,
        tracking_url=tracking_url,
        tracking_number=tracking_number,
        carrier=carrier,
        delivered_at=_parse_delivered_at(meta),
        payment_method=_non_empty(meta.get("payment_method"))
        or _non_empty(normalized_order.get("payment_method")),
        review_url=None,
        coupon_code=None,
        customer_phone=_non_empty(customer_info.get("phone")),
        customer_name=_non_empty(customer_info.get("name"))
        or _non_empty(getattr(order, "customer_name", None)),
        status=_non_empty(getattr(order, "status", None))
        or _non_empty(normalized_order.get("status")),
        source_event_id=source_event_id,
        transition_version=transition_version,
    )


def _evidence_field_names(evidence: OrderLifecycleEvidence) -> Tuple[str, ...]:
    names: list[str] = []
    for field_name in (
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
    ):
        value = getattr(evidence, field_name)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        names.append(field_name)
    return tuple(names)


def _build_shadow_dispatch_decision(
    *,
    intent: BusinessIntent,
    reason_code: str,
    evidence_result: Any,
    cap_result: Any,
    template_result: Any,
    definition: Any,
) -> dict[str, str]:
    decision: dict[str, str] = {
        "handoff_kind": "external_lifecycle_shadow",
        "intent": intent.value,
        "reason_code": reason_code,
        "business_evidence_valid": (
            "true"
            if evidence_result is not None and evidence_result.valid
            else "false"
        ),
        "capabilities_valid": (
            "true" if cap_result is not None and cap_result.valid else "false"
        ),
    }
    if definition is None or not getattr(definition, "required_template_evidence", ()):
        decision["template_evidence_valid"] = "na"
    elif template_result is None:
        decision["template_evidence_valid"] = "na"
    else:
        decision["template_evidence_valid"] = (
            "true" if template_result.valid else "false"
        )
        missing = tuple(template_result.missing_fields or ())
        invalid = tuple(template_result.invalid_fields or ())
        if missing or invalid:
            decision["template_missing_evidence"] = ",".join(missing or invalid)
    return decision


def _decide_shadow_outcome(
    *,
    definition: Any,
    evidence_result: Any,
    cap_result: Any,
) -> Tuple[ShadowLedgerOutcome, str]:
    if definition is None:
        return ShadowLedgerOutcome.SHADOW_NO_NOTIFICATION, "intent_not_registered"

    if cap_result.forbidden_capabilities:
        return ShadowLedgerOutcome.SHADOW_BLOCKED, "forbidden_capabilities"
    if cap_result.missing_capabilities:
        return ShadowLedgerOutcome.SHADOW_NO_NOTIFICATION, "missing_capabilities"

    if evidence_result.missing_fields or evidence_result.invalid_fields:
        if evidence_result.invalid_fields:
            return ShadowLedgerOutcome.SHADOW_BLOCKED, "invalid_evidence"
        return ShadowLedgerOutcome.SHADOW_BLOCKED, "missing_evidence"

    if definition.closed_window_strategy == ClosedWindowStrategy.BLOCKED:
        return ShadowLedgerOutcome.SHADOW_NO_NOTIFICATION, "closed_window_blocked"

    if definition.closed_window_strategy == ClosedWindowStrategy.NO_MESSAGE:
        return ShadowLedgerOutcome.SHADOW_NO_NOTIFICATION, "closed_window_no_message"

    return ShadowLedgerOutcome.SHADOW_ELIGIBLE, "eligible"


def record_external_order_transition_shadow(
    db: Session,
    *,
    tenant_id: int,
    order: Any,
    provider: str,
    raw_previous_status: Optional[str],
    raw_current_status: str,
    normalized_order: Mapping[str, Any],
    raw_payload: Optional[Mapping[str, Any]] = None,
) -> Optional[int]:
    """
    Best-effort shadow ledger write. Never raises to callers.

    Returns ledger row id when reserved, else None.
    """
    if not commerce_lifecycle_external_shadow_enabled():
        return None

    order_id = int(getattr(order, "id", 0) or 0)
    if order_id <= 0:
        return None

    try:
        intent, norm_reason = normalize_external_lifecycle_intent(
            provider=provider,
            raw_previous_status=raw_previous_status,
            raw_current_status=raw_current_status,
            normalized_order=normalized_order,
        )
        if intent is None:
            return None

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
        evidence_names = _evidence_field_names(evidence)

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

        outcome, reason_code = _decide_shadow_outcome(
            definition=definition,
            evidence_result=evidence_result,
            cap_result=cap_result,
        )

        dispatch_decision = _build_shadow_dispatch_decision(
            intent=intent,
            reason_code=reason_code,
            evidence_result=evidence_result,
            cap_result=cap_result,
            template_result=template_result,
            definition=definition,
        )

        reserve = reserve_shadow_decision(
            db,
            tenant_id=int(tenant_id),
            order_id=order_id,
            business_intent=intent,
            channel=_SHADOW_CHANNEL,
            source_event_id=source_event_id,
            transition_version=transition_version,
            dispatch_decision=dispatch_decision,
            capabilities_snapshot=merchant_caps.to_dict(),
            evidence_present=evidence_names,
            commit=False,
        )
        if reserve.duplicate:
            logger.info(
                "[ExternalShadow] duplicate tenant=%s order=%s intent=%s reason=%s",
                tenant_id,
                order_id,
                intent.value,
                norm_reason,
            )
            return reserve.ledger_id

        with db.begin_nested():
            mark_shadow_outcome(
                db,
                ledger_id=reserve.ledger_id,
                tenant_id=int(tenant_id),
                outcome=outcome,
                reason_code=reason_code,
                commit=False,
            )
        logger.info(
            "[ExternalShadow] recorded tenant=%s order=%s intent=%s outcome=%s reason=%s",
            tenant_id,
            order_id,
            intent.value,
            outcome.value,
            reason_code,
        )
        return reserve.ledger_id
    except Exception:
        logger.exception(
            "[ExternalShadow] shadow write failed tenant=%s order=%s intent=%s",
            tenant_id,
            order_id,
            str(raw_current_status or ""),
        )
        return None


__all__ = [
    "build_order_lifecycle_evidence",
    "record_external_order_transition_shadow",
    "_build_shadow_dispatch_decision",
    "_decide_shadow_outcome",
]
