"""
Staff contact policy — pre-brain deterministic guard (Phase A).

Short-circuits explicit staff / CS contact requests when evidence
exists or returns an honest not-configured reply when it does not.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.staff_contact_policy")

_FLAG_FALSY = frozenset({"0", "false", "no", "off"})


def staff_contact_policy_enabled() -> bool:
    raw = os.getenv("STAFF_CONTACT_POLICY_ENABLED", "1").strip().lower()
    return raw not in _FLAG_FALSY


@dataclass(frozen=True)
class StaffContactPolicyDecision:
    reply_text: str
    call_target: Any = None
    deliver_contact: bool = False
    reason: str = ""
    request_kind: str = ""
    evidence_source: str = ""
    skip_brain: bool = True
    staff_target_tier: str = ""
    staff_target_reason: str = ""
    staff_target_confidence: float = 0.0


def _build_call_target(record: Any) -> Optional[Any]:
    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        build_staff_call_target_from_record,
    )

    return build_staff_call_target_from_record(record)


def _load_role_graph(db: Any, tenant_id: int) -> Any:
    from modules.ai.brain.commerce.staff_contact_fallback_v0 import (  # noqa: PLC0415
        extract_staff_role_aliases_from_sections,
        load_staff_chain_sections,
    )

    sections = load_staff_chain_sections(db, int(tenant_id or 0))
    return extract_staff_role_aliases_from_sections(sections)


def _log_target_trace(
    *,
    tenant_id: int,
    request: Any,
    resolution_reason: str = "",
    deliver: bool = False,
) -> None:
    logger.info(
        "[STAFF_CONTACT_POLICY] tenant=%s staff_target_tier=%s "
        "staff_target_reason=%s staff_target_confidence=%.2f "
        "kind=%s resolution=%s deliver=%s",
        tenant_id,
        getattr(request, "target_tier", "") or "",
        getattr(request, "target_reason", "") or "",
        float(getattr(request, "target_confidence", 0.0) or 0.0),
        getattr(request, "kind", "") or "",
        resolution_reason,
        deliver,
    )


def _decision_with_target(
    request: Any,
    *,
    reply_text: str,
    deliver_contact: bool = False,
    call_target: Any = None,
    reason: str = "",
    evidence_source: str = "",
) -> StaffContactPolicyDecision:
    return StaffContactPolicyDecision(
        reply_text=reply_text,
        call_target=call_target,
        deliver_contact=deliver_contact,
        reason=reason,
        request_kind=getattr(request, "kind", "") or "",
        evidence_source=evidence_source,
        staff_target_tier=getattr(request, "target_tier", "") or "",
        staff_target_reason=getattr(request, "target_reason", "") or "",
        staff_target_confidence=float(getattr(request, "target_confidence", 0.0) or 0.0),
    )


def evaluate_staff_contact_policy(
    db: Any,
    *,
    tenant_id: int,
    message: str,
    store_contact_phone: str = "",
    customer_phone: str = "",
) -> Optional[StaffContactPolicyDecision]:
    """Return a short-circuit decision for explicit contact requests."""
    if not staff_contact_policy_enabled():
        return None

    from modules.ai.brain.commerce.checkout_slot_contact_guard import (  # noqa: PLC0415
        should_defer_contact_routing_for_checkout_slot,
    )

    if should_defer_contact_routing_for_checkout_slot(
        db,
        tenant_id=int(tenant_id or 0),
        customer_phone=customer_phone or "",
        message=message or "",
    ):
        logger.info(
            "[STAFF_CONTACT_POLICY] tenant=%s defer=true reason=checkout_slot",
            tenant_id,
        )
        return None

    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        MSG_AMBIGUOUS_STAFF_CLARIFY,
        _CONTACT_ASK_RE,
        _norm as _evidence_norm,
        build_deliver_reply_text,
        build_not_configured_reply,
        classify_staff_contact_request,
        load_staff_contact_registry,
        resolve_staff_contact,
    )
    from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
        should_defer_contact_policies_for_commerce,
        staff_policy_applies_to_named_request,
    )

    if should_defer_contact_policies_for_commerce(message or ""):
        logger.info(
            "[STAFF_CONTACT_POLICY] tenant=%s defer=true reason=commerce_flow",
            tenant_id,
        )
        return None

    registry = load_staff_contact_registry(
        db, int(tenant_id or 0), store_contact_phone=store_contact_phone,
    )
    role_graph = _load_role_graph(db, int(tenant_id or 0))
    request = classify_staff_contact_request(
        message or "",
        registry=registry,
        role_graph=role_graph,
    )

    if request.kind in {"none", "arrival", "not_responding"}:
        return None

    if request.kind == "general_channel":
        from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: PLC0415
            general_contact_reply_for_message,
        )

        logger.info(
            "[STAFF_CONTACT_POLICY] tenant=%s kind=general_channel deliver=false",
            tenant_id,
        )
        return StaffContactPolicyDecision(
            reply_text=general_contact_reply_for_message(message or ""),
            deliver_contact=False,
            reason="generic_store_contact",
            request_kind="general_channel",
        )

    registry_match = registry.match_record_in_message(message or "") is not None
    explicit_ask = bool(_CONTACT_ASK_RE.search(_evidence_norm(message or "")))

    if request.kind == "named" and not staff_policy_applies_to_named_request(
        message or "",
        registry_match=registry_match,
        explicit_contact_ask=explicit_ask,
    ):
        logger.info(
            "[STAFF_CONTACT_POLICY] tenant=%s kind=named defer=true reason=not_staff_ask",
            tenant_id,
        )
        return None

    resolution = resolve_staff_contact(registry, request, message=message or "")

    if not resolution.found and resolution.reason == "no_named_intent":
        _log_target_trace(
            tenant_id=tenant_id,
            request=request,
            resolution_reason=resolution.reason,
            deliver=False,
        )
        logger.info(
            "[STAFF_CONTACT_POLICY] tenant=%s kind=%s defer=true reason=no_named_intent",
            tenant_id, request.kind,
        )
        return None

    if resolution.found and resolution.record is not None:
        target = _build_call_target(resolution.record)
        if target is None:
            _log_target_trace(
                tenant_id=tenant_id,
                request=request,
                resolution_reason="phone_normalize_failed",
                deliver=False,
            )
            return _decision_with_target(
                request,
                reply_text=build_not_configured_reply(
                    resolution,
                    target_tier=request.target_tier,
                ),
                deliver_contact=False,
                reason="phone_normalize_failed",
                evidence_source=resolution.record.source,
            )
        _log_target_trace(
            tenant_id=tenant_id,
            request=request,
            resolution_reason=resolution.reason,
            deliver=True,
        )
        return _decision_with_target(
            request,
            reply_text=build_deliver_reply_text(resolution.record),
            call_target=target,
            deliver_contact=True,
            reason=resolution.reason,
            evidence_source=resolution.record.source,
        )

    _log_target_trace(
        tenant_id=tenant_id,
        request=request,
        resolution_reason=resolution.reason,
        deliver=False,
    )

    if resolution.reason in {"no_named_intent", "name_not_configured"} and not resolution.unknown_name:
        return None
    if should_defer_contact_policies_for_commerce(message or ""):
        return None

    if (
        request.target_tier == "ambiguous"
        and request.kind == "generic_staff"
        and resolution.reason == "escalation_not_configured"
    ):
        return _decision_with_target(
            request,
            reply_text=MSG_AMBIGUOUS_STAFF_CLARIFY,
            deliver_contact=False,
            reason="ambiguous_staff_clarify",
        )

    return _decision_with_target(
        request,
        reply_text=build_not_configured_reply(
            resolution,
            target_tier=request.target_tier,
        ),
        deliver_contact=False,
        reason=resolution.reason,
    )


def evaluate_generic_handoff_contact_policy(
    db: Any,
    *,
    tenant_id: int,
    message: str = "",
    store_contact_phone: str = "",
) -> Optional[StaffContactPolicyDecision]:
    """Contact delivery or honest not-configured for generic handoff asks."""
    if not staff_contact_policy_enabled():
        return None

    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        StaffContactRequest,
        build_deliver_reply_text,
        build_not_configured_reply,
        load_staff_contact_registry,
        resolve_staff_contact,
    )

    registry = load_staff_contact_registry(
        db, int(tenant_id or 0), store_contact_phone=store_contact_phone,
    )
    request = StaffContactRequest(
        kind="generic_staff",
        target_tier="generic_role",
        target_reason="handoff:generic_staff",
        target_confidence=0.90,
    )
    resolution = resolve_staff_contact(
        registry,
        request,
        message=message or "",
    )
    if resolution.found and resolution.record is not None:
        target = _build_call_target(resolution.record)
        if target is None:
            return _decision_with_target(
                request,
                reply_text=build_not_configured_reply(
                    resolution,
                    target_tier=request.target_tier,
                ),
                reason="phone_normalize_failed",
            )
        return _decision_with_target(
            request,
            reply_text=build_deliver_reply_text(resolution.record),
            call_target=target,
            deliver_contact=True,
            reason=resolution.reason,
            evidence_source=resolution.record.source,
        )
    return _decision_with_target(
        request,
        reply_text=build_not_configured_reply(
            resolution,
            target_tier=request.target_tier,
        ),
        deliver_contact=False,
        reason=resolution.reason,
    )


__all__ = [
    "StaffContactPolicyDecision",
    "evaluate_generic_handoff_contact_policy",
    "evaluate_staff_contact_policy",
    "staff_contact_policy_enabled",
]
