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


def _staff_vcard_already_sent(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    name: str = "",
    contact_phone: str = "",
) -> bool:
    from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
        contact_already_sent,
        parse_staff_contacts_sent,
    )

    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415

        _conv, bs = _load_brain_state(
            db,
            tenant_id=int(tenant_id or 0),
            phone=str(customer_phone or ""),
        )
        sent = parse_staff_contacts_sent((bs or {}).get("staff_contacts_sent"))
        return contact_already_sent(sent, name=name, phone=contact_phone)
    except Exception:  # noqa: BLE001
        return False


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

    from modules.ai.brain.commerce.unstructured_turn_ownership import (  # noqa: PLC0415
        unstructured_natural_language_requires_brain,
    )

    if unstructured_natural_language_requires_brain(message=message or ""):
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

    try:
        from modules.ai.brain.commerce.staff_contact_media_source_guard import (  # noqa: PLC0415
            staff_contact_intent_message,
        )

        _intent_msg = staff_contact_intent_message(message or "")
    except Exception:  # noqa: BLE001
        _intent_msg = (message or "").strip()

    registry = load_staff_contact_registry(
        db, int(tenant_id or 0), store_contact_phone=store_contact_phone,
    )
    role_graph = _load_role_graph(db, int(tenant_id or 0))

    _continuity_synthetic = ""
    _brain_turn = 0
    _continuity = None
    _skip_checkout_defer = False
    if customer_phone:
        try:
            from core.order_flow import _load_brain_state  # noqa: PLC0415
            from modules.ai.brain.commerce.staff_contact_target_continuity import (  # noqa: PLC0415
                capture_pending_target_from_inbound,
                clear_pending_contact_target,
                is_bare_who_to_call_followup,
                persist_pending_contact_target,
                resolve_pending_contact_followup,
                should_clear_pending_on_topic_switch,
                try_persist_pending_contact_target_from_outbound,
            )

            _cont_conv, _cont_bs = _load_brain_state(
                db,
                tenant_id=int(tenant_id or 0),
                phone=str(customer_phone or ""),
            )
            _brain_turn = int((_cont_bs or {}).get("turn") or 0)
            if should_clear_pending_on_topic_switch(message or ""):
                clear_pending_contact_target(
                    db,
                    tenant_id=int(tenant_id or 0),
                    phone=str(customer_phone or ""),
                )
            else:
                _inbound_target = capture_pending_target_from_inbound(
                    _intent_msg,
                    registry=registry,
                    created_turn=_brain_turn,
                )
                if _inbound_target is not None:
                    persist_pending_contact_target(
                        db,
                        tenant_id=int(tenant_id or 0),
                        phone=str(customer_phone or ""),
                        target=_inbound_target,
                    )

            if is_bare_who_to_call_followup(message or ""):
                _continuity = resolve_pending_contact_followup(
                    db,
                    tenant_id=int(tenant_id or 0),
                    customer_phone=str(customer_phone or ""),
                    message=message or "",
                    store_contact_phone=store_contact_phone,
                )
                if _continuity is not None:
                    _skip_checkout_defer = True
                    logger.info(
                        "[STAFF_CONTACT_POLICY] tenant=%s checkout_override=true "
                        "reason=operational_who_to_call_continuity",
                        tenant_id,
                    )
        except Exception as _cont_exc:  # noqa: BLE001
            logger.exception(
                "[STAFF_CONTACT_POLICY] continuity_failed tenant=%s err=%s",
                tenant_id,
                _cont_exc,
            )
            _continuity = None
            _skip_checkout_defer = False

    if not _skip_checkout_defer:
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

        from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: PLC0415
            should_defer_staff_location_for_checkout_route,
        )

        if should_defer_staff_location_for_checkout_route(
            db,
            tenant_id=int(tenant_id or 0),
            customer_phone=customer_phone or "",
            message=message or "",
        ):
            logger.info(
                "[STAFF_CONTACT_POLICY] tenant=%s defer=true reason=checkout_route_owner",
                tenant_id,
            )
            return None

    if should_defer_contact_policies_for_commerce(message or ""):
        logger.info(
            "[STAFF_CONTACT_POLICY] tenant=%s defer=true reason=commerce_flow",
            tenant_id,
        )
        return None

    if customer_phone and _continuity is None:
        try:
            from modules.ai.brain.commerce.staff_contact_target_continuity import (  # noqa: PLC0415
                resolve_pending_contact_followup,
            )

            _continuity = resolve_pending_contact_followup(
                db,
                tenant_id=int(tenant_id or 0),
                customer_phone=str(customer_phone or ""),
                message=message or "",
                store_contact_phone=store_contact_phone,
            )
        except Exception as _cont_exc:  # noqa: BLE001
            logger.exception(
                "[STAFF_CONTACT_POLICY] continuity_failed tenant=%s err=%s",
                tenant_id,
                _cont_exc,
            )
            _continuity = None

    request = classify_staff_contact_request(
        _intent_msg,
        registry=registry,
        role_graph=role_graph,
    )

    if _continuity is not None:
        request, _pending_target, _continuity_synthetic = _continuity
        logger.info(
            "[STAFF_CONTACT_POLICY] tenant=%s continuity_override lookup=%r",
            tenant_id,
            _pending_target.lookup_name,
        )

    if request.kind in {"none", "arrival", "not_responding"}:
        return None

    if request.kind == "general_channel":
        from modules.ai.brain.commerce.prebrain_order_flow_arbiter import (  # noqa: PLC0415
            should_yield_prebrain_to_order_flow,
        )

        if should_yield_prebrain_to_order_flow(
            db,
            tenant_id=int(tenant_id or 0),
            customer_phone=customer_phone or "",
            message=message or "",
        ):
            logger.info(
                "[STAFF_CONTACT_POLICY] tenant=%s defer=true reason=order_flow_arbiter",
                tenant_id,
            )
            return None

        # Pack A2: when MERCHANT_PROFILE has structured contact channels,
        # yield to brain FAQ owner_contact (do not emit channel-only prose).
        try:
            from core.merchant_profile import resolve_merchant_profile  # noqa: PLC0415

            _profile = resolve_merchant_profile(db, int(tenant_id or 0))
            _has_structured = bool(
                str(getattr(_profile, "email", "") or "").strip()
                or str(getattr(_profile, "phone", "") or "").strip()
                or (
                    isinstance(getattr(_profile, "social_links", None), dict)
                    and any(
                        str(v or "").strip()
                        for v in (getattr(_profile, "social_links", None) or {}).values()
                    )
                )
            )
            if _has_structured:
                logger.info(
                    "[STAFF_CONTACT_POLICY] tenant=%s defer=true "
                    "reason=merchant_profile_contact_channels",
                    tenant_id,
                )
                return None
        except Exception:  # noqa: BLE001  # noqa: silent-ok — profile yield best-effort
            pass

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

    _resolution_message = _continuity_synthetic or _intent_msg or (message or "")
    registry_match = registry.match_record_in_message(_resolution_message) is not None
    explicit_ask = bool(_CONTACT_ASK_RE.search(_evidence_norm(_intent_msg)))
    if _continuity is not None:
        explicit_ask = True

    if request.kind == "named" and not staff_policy_applies_to_named_request(
        _intent_msg,
        registry_match=registry_match,
        explicit_contact_ask=explicit_ask,
    ):
        logger.info(
            "[STAFF_CONTACT_POLICY] tenant=%s kind=named defer=true reason=not_staff_ask",
            tenant_id,
        )
        return None

    resolution = resolve_staff_contact(
        registry,
        request,
        message=_resolution_message,
    )

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
        from modules.operations.contact_visibility import (  # noqa: PLC0415
            may_share_with_customer,
        )

        if not may_share_with_customer(resolution.record):
            _log_target_trace(
                tenant_id=tenant_id,
                request=request,
                resolution_reason="internal_escalation_only",
                deliver=False,
            )
            logger.info(
                "[STAFF_CONTACT_POLICY] tenant=%s defer=true reason=internal_only",
                tenant_id,
            )
            return None
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
        contact_phone = (
            getattr(target, "raw_phone", "")
            or getattr(target, "wa_id", "")
            or getattr(resolution.record, "phone", "")
        )
        already_sent = _staff_vcard_already_sent(
            db,
            tenant_id=int(tenant_id or 0),
            customer_phone=customer_phone or "",
            name=getattr(resolution.record, "lookup_name", "") or "",
            contact_phone=contact_phone,
        )
        if already_sent:
            _log_target_trace(
                tenant_id=tenant_id,
                request=request,
                resolution_reason="contact_already_sent",
                deliver=False,
            )
            return _decision_with_target(
                request,
                reply_text=build_deliver_reply_text(resolution.record),
                deliver_contact=False,
                reason="contact_already_sent",
                evidence_source=resolution.record.source,
            )
        _log_target_trace(
            tenant_id=tenant_id,
            request=request,
            resolution_reason=resolution.reason,
            deliver=True,
        )
        decision = _decision_with_target(
            request,
            reply_text=build_deliver_reply_text(resolution.record),
            call_target=target,
            deliver_contact=True,
            reason=resolution.reason,
            evidence_source=resolution.record.source,
        )
        if customer_phone:
            try:
                from modules.ai.brain.commerce.staff_contact_target_continuity import (  # noqa: PLC0415
                    pending_target_from_record,
                    persist_pending_contact_target,
                    try_persist_pending_contact_target_from_outbound,
                )

                _pt = pending_target_from_record(
                    resolution.record,
                    source="contact_delivered",
                    confidence=0.98,
                    created_turn=_brain_turn,
                )
                if _pt is not None:
                    persist_pending_contact_target(
                        db,
                        tenant_id=int(tenant_id or 0),
                        phone=str(customer_phone or ""),
                        target=_pt,
                    )
                try_persist_pending_contact_target_from_outbound(
                    db,
                    tenant_id=int(tenant_id or 0),
                    phone=str(customer_phone or ""),
                    outbound_text=decision.reply_text,
                    store_contact_phone=store_contact_phone,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[STAFF_CONTACT_POLICY] pending_contact_target_capture_failed",
                )
        return decision

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

    not_cfg = _decision_with_target(
        request,
        reply_text=build_not_configured_reply(
            resolution,
            target_tier=request.target_tier,
        ),
        deliver_contact=False,
        reason=resolution.reason,
    )
    if customer_phone and _continuity is not None:
        try:
            from modules.ai.brain.commerce.staff_contact_target_continuity import (  # noqa: PLC0415
                try_persist_pending_contact_target_from_outbound,
            )

            try_persist_pending_contact_target_from_outbound(
                db,
                tenant_id=int(tenant_id or 0),
                phone=str(customer_phone or ""),
                outbound_text=not_cfg.reply_text,
                store_contact_phone=store_contact_phone,
                source="continuity_not_configured",
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[STAFF_CONTACT_POLICY] pending_contact_target_outbound_persist_failed",
            )
    return not_cfg


def evaluate_generic_handoff_contact_policy(
    db: Any,
    *,
    tenant_id: int,
    message: str = "",
    store_contact_phone: str = "",
    customer_phone: str = "",
) -> Optional[StaffContactPolicyDecision]:
    """Contact delivery or honest not-configured for generic handoff asks."""
    if not staff_contact_policy_enabled():
        return None

    from modules.ai.brain.commerce.prebrain_order_flow_arbiter import (  # noqa: PLC0415
        should_yield_prebrain_to_order_flow,
    )

    if should_yield_prebrain_to_order_flow(
        db,
        tenant_id=int(tenant_id or 0),
        customer_phone=customer_phone or "",
        message=message or "",
    ):
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
        from modules.operations.contact_visibility import (  # noqa: PLC0415
            may_share_with_customer,
        )

        if not may_share_with_customer(resolution.record):
            return None
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
