"""
Branch trigger router — keyword-driven pre-brain routing (PR-C).

When USE_STRUCTURED_BRANCH_CONTACTS is ON and tenant has structured data,
match branch_arrival_keywords and return deterministic delivery decisions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from modules.operations.branch_arrival_keyword_evidence import (
    LOCATION_MODE_PLUS_INSTRUCTIONS,
    LOCATION_MODE_PLUS_RECEPTION,
    TRIGGER_ARRIVAL_CONFIRMED,
    TRIGGER_ARRIVAL_SOFT,
    TRIGGER_LOCATION_REQUEST,
    TRIGGER_NO_RESPONSE,
    load_branch_action_config,
    match_branch_trigger,
    needs_branch_clarification,
)
from modules.ai.brain.commerce.pending_operational_choice import (  # noqa: PLC0415
    PENDING_PICKUP_MAPS_OR_CONTACT,
    evaluate_pending_operational_choice_routing,
)

logger = logging.getLogger("nahla.brain.branch_trigger_router")

MSG_ESCALATION_EXHAUSTED = (
    "حاضر، وصلنا لأعلى مستوى في سلسلة التصعيد. سنتابع معك."
)
MSG_NO_TRUSTED_BRANCH_CONTACT = (
    "ما ظهر عندي رقم فرع مؤكد الآن، برسّل طلبك للفريق يتواصلون معك."
)
MSG_BRANCH_CLARIFY = "أي فرع تقصد؟"
MSG_PICKUP_PREFERENCE_ASK = (
    "هل تفضّل أرسل لك موقع المعرض أو بيانات التواصل؟"
)


@dataclass(frozen=True)
class BranchTriggerDecision:
    trigger_type: str
    matched_phrase: str
    branch_id: int
    reason: str
    skip_brain: bool = True
    reply_text: str = ""
    maps_url: str = ""
    cta_button_label: str = "موقع المتجر"
    use_cta: bool = False
    deliver_contact: bool = False
    call_target: Any = None
    deliver_reception_after_maps: bool = False
    reception_call_target: Any = None
    reception_reply_text: str = ""
    resend_maps: bool = False
    persist_contact: bool = False
    persist_pending_choice: str = ""
    pending_branch_id: int = 0
    metadata_path: str = "branch_trigger_router"
    compose_facts: Any = None


def _compose_facts_for_location(
    message: str,
    config: Any,
    *,
    maps_url: str,
    use_cta: bool,
    needs_pickup_preference_choice: bool = False,
    maps_configured: bool = True,
) -> Any:
    from modules.ai.brain.persona.branch_action_compose import (  # noqa: PLC0415
        ACTION_KIND_LOCATION,
        BranchComposeFacts,
    )

    instructions = ""
    if config.location_response_mode == LOCATION_MODE_PLUS_INSTRUCTIONS:
        instructions = str(getattr(config, "location_instructions_text", "") or "").strip()
    return BranchComposeFacts(
        action_kind=ACTION_KIND_LOCATION,
        customer_message=message or "",
        branch_name=str(getattr(config, "name", "") or "").strip(),
        maps_cta_available=bool(use_cta),
        location_already_sent=False,
        maps_configured=maps_configured,
        needs_pickup_preference_choice=needs_pickup_preference_choice,
        location_instructions=instructions,
    )

def _cta_label_for_url(maps_url: str) -> str:
    label = "موقع المتجر"
    try:
        from core.wa_link_buttons import classify_url  # noqa: PLC0415

        cls = classify_url(maps_url)
        if cls.button_title:
            label = cls.button_title
    except Exception:  # noqa: silent-ok - CTA label fallback is acceptable
        pass
    return label


def _build_reception_targets(
    db: Any,
    tenant_id: int,
    message: str,
) -> tuple[Optional[Any], str]:
    from modules.ai.brain.commerce.arrival_contact_delivery_policy import (  # noqa: PLC0415
        MSG_ARRIVAL_CONTACT_NOT_CONFIGURED,
        resolve_arrival_contact_evidence,
        _build_arrival_reply_text,
    )
    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        build_staff_call_target,
    )

    evidence = resolve_arrival_contact_evidence(
        db, int(tenant_id), message=message or "",
    )
    if evidence is None or not evidence.phone:
        return None, MSG_ARRIVAL_CONTACT_NOT_CONFIGURED

    call_target = build_staff_call_target(
        lookup_name=evidence.lookup_name,
        phone=evidence.phone,
        role=evidence.role,
    )
    if call_target is None:
        return None, MSG_ARRIVAL_CONTACT_NOT_CONFIGURED
    return call_target, _build_arrival_reply_text(evidence.lookup_name)


def _build_location_decision(
    db: Any,
    tenant_id: int,
    message: str,
    match: Any,
    config: Any,
) -> BranchTriggerDecision:
    maps_url = config.maps_url
    use_cta = bool(maps_url)
    needs_pickup_choice = False

    from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
        is_explicit_arrival_intent,
    )
    from modules.ai.brain.commerce.link_intent import (  # noqa: PLC0415
        is_explicit_direct_location_request,
    )

    deliver_reception = config.location_response_mode == LOCATION_MODE_PLUS_RECEPTION
    reception_target = None
    reception_reply = ""
    _arrival_confirmed = is_explicit_arrival_intent(message or "")
    _direct_location = is_explicit_direct_location_request(message or "")
    if deliver_reception and not _arrival_confirmed:
        deliver_reception = False
        if not _direct_location:
            needs_pickup_choice = True
    elif deliver_reception:
        reception_target, reception_reply = _build_reception_targets(
            db, tenant_id, message,
        )
        if reception_target is None:
            deliver_reception = False

    if not maps_url and not deliver_reception:
        return BranchTriggerDecision(
            trigger_type=TRIGGER_LOCATION_REQUEST,
            matched_phrase=match.matched_phrase,
            branch_id=match.branch_id,
            reason="no_maps_url",
            compose_facts=_compose_facts_for_location(
                message, config, maps_url="", use_cta=False, maps_configured=False,
            ),
        )

    return BranchTriggerDecision(
        trigger_type=TRIGGER_LOCATION_REQUEST,
        matched_phrase=match.matched_phrase,
        branch_id=match.branch_id,
        reason=f"location_{config.location_response_mode}",
        compose_facts=_compose_facts_for_location(
            message,
            config,
            maps_url=maps_url,
            use_cta=use_cta,
            needs_pickup_preference_choice=needs_pickup_choice,
        ),
        maps_url=maps_url,
        cta_button_label=_cta_label_for_url(maps_url) if maps_url else "",
        use_cta=use_cta,
        deliver_reception_after_maps=deliver_reception,
        reception_call_target=reception_target,
        reception_reply_text=reception_reply,
        persist_contact=deliver_reception,
        persist_pending_choice=(
            PENDING_PICKUP_MAPS_OR_CONTACT if needs_pickup_choice else ""
        ),
        pending_branch_id=int(match.branch_id) if needs_pickup_choice else 0,
    )


def _build_soft_decision(
    match: Any,
    config: Any,
    message: str = "",
) -> BranchTriggerDecision:
    from modules.ai.brain.commerce.arrival_soft_delivery_policy import (  # noqa: PLC0415
        evaluate_arrival_soft_delivery,
    )
    from modules.ai.brain.persona.branch_action_compose import (  # noqa: PLC0415
        ACTION_KIND_ARRIVAL_SOFT,
        BranchComposeFacts,
    )

    soft = evaluate_arrival_soft_delivery(config)
    compose_facts = BranchComposeFacts(
        action_kind=ACTION_KIND_ARRIVAL_SOFT,
        customer_message=message or "",
        branch_name=soft.branch_name,
        maps_cta_available=soft.resend_maps,
        location_already_sent=soft.location_already_sent,
        maps_configured=bool(soft.maps_url),
    )
    return BranchTriggerDecision(
        trigger_type=TRIGGER_ARRIVAL_SOFT,
        matched_phrase=match.matched_phrase,
        branch_id=match.branch_id,
        reason=soft.reason,
        compose_facts=compose_facts,
        maps_url=soft.maps_url,
        cta_button_label=soft.cta_button_label,
        use_cta=soft.resend_maps,
        resend_maps=soft.resend_maps,
    )


def _build_confirmed_decision(
    db: Any,
    tenant_id: int,
    message: str,
    match: Any,
    config: Any,
) -> BranchTriggerDecision:
    clarify = needs_branch_clarification(
        db, tenant_id, message, config=config,
    )
    if clarify:
        names = "، ".join(clarify[:5])
        return BranchTriggerDecision(
            trigger_type=TRIGGER_ARRIVAL_CONFIRMED,
            matched_phrase=match.matched_phrase,
            branch_id=match.branch_id,
            reason="ask_branch_first",
            reply_text=f"{MSG_BRANCH_CLARIFY}\n{names}",
        )

    call_target, reply = _build_reception_targets(db, tenant_id, message)
    return BranchTriggerDecision(
        trigger_type=TRIGGER_ARRIVAL_CONFIRMED,
        matched_phrase=match.matched_phrase,
        branch_id=match.branch_id,
        reason="arrival_confirmed_reception",
        reply_text=reply,
        deliver_contact=call_target is not None,
        call_target=call_target,
        persist_contact=call_target is not None,
    )


def _load_contacts_sent(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
) -> List[dict]:
    from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
        parse_staff_contacts_sent,
    )

    if not db or not tenant_id or not phone:
        return []
    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415

        _conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
        if not bs:
            return []
        return parse_staff_contacts_sent(bs.get("staff_contacts_sent"))
    except Exception:  # noqa: BLE001
        return []


def _build_no_response_decision(
    db: Any,
    tenant_id: int,
    message: str,
    match: Any,
    *,
    customer_phone: str = "",
) -> BranchTriggerDecision:
    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        build_staff_call_target,
        resolve_contact_display_name,
    )
    from modules.ai.brain.persona.branch_action_compose import (  # noqa: PLC0415
        ACTION_KIND_BRANCH_CONTACT,
        BranchComposeFacts,
    )
    from modules.operations.branch_escalation_evidence import (  # noqa: PLC0415
        load_structured_escalation_chain,
        resolve_next_structured_escalation,
    )

    contacts_sent = _load_contacts_sent(
        db, tenant_id=tenant_id, phone=customer_phone,
    )
    chain = load_structured_escalation_chain(
        db, int(tenant_id), message=message or "",
    )
    if not chain:
        return BranchTriggerDecision(
            trigger_type=TRIGGER_NO_RESPONSE,
            matched_phrase=match.matched_phrase,
            branch_id=match.branch_id,
            reason="no_escalation_chain",
            reply_text=MSG_NO_TRUSTED_BRANCH_CONTACT,
        )

    nxt = resolve_next_structured_escalation(chain, contacts_sent)
    if nxt is None:
        if not contacts_sent and chain:
            nxt = chain[0]
        else:
            return BranchTriggerDecision(
                trigger_type=TRIGGER_NO_RESPONSE,
                matched_phrase=match.matched_phrase,
                branch_id=match.branch_id,
                reason="escalation_exhausted",
                reply_text=MSG_ESCALATION_EXHAUSTED,
            )

    call_target = build_staff_call_target(
        lookup_name=nxt.lookup_name,
        phone=nxt.phone,
        role=nxt.role or "",
    )
    if call_target is None:
        return BranchTriggerDecision(
            trigger_type=TRIGGER_NO_RESPONSE,
            matched_phrase=match.matched_phrase,
            branch_id=match.branch_id,
            reason="phone_normalize_failed",
            reply_text=MSG_ESCALATION_EXHAUSTED,
        )

    label = resolve_contact_display_name(nxt.lookup_name, role=nxt.role or "")
    config = load_branch_action_config(db, match.branch_id)
    branch_name = str(getattr(config, "name", "") or "").strip() if config else ""
    compose_facts = BranchComposeFacts(
        action_kind=ACTION_KIND_BRANCH_CONTACT,
        customer_message=message or "",
        branch_name=branch_name,
        contact_card_available=True,
        contact_name=label or nxt.lookup_name,
        contact_role=str(nxt.role or "").strip(),
        maps_configured=True,
    )
    return BranchTriggerDecision(
        trigger_type=TRIGGER_NO_RESPONSE,
        matched_phrase=match.matched_phrase,
        branch_id=match.branch_id,
        reason="no_response_escalation_advance",
        compose_facts=compose_facts,
        deliver_contact=True,
        call_target=call_target,
        persist_contact=True,
    )


def evaluate_branch_trigger_routing(
    db: Any,
    *,
    tenant_id: int,
    message: str,
    customer_phone: str = "",
    inbound_metadata: Optional[dict] = None,
) -> Optional[BranchTriggerDecision]:
    """Return a pre-brain decision when structured keyword routing matches."""
    from modules.ai.brain.turn_owner_contract import (  # noqa: PLC0415
        should_suppress_prebrain_branch_routing,
    )

    suppressed, prebrain_contract = should_suppress_prebrain_branch_routing(
        message=message or "",
        inbound_metadata=inbound_metadata,
    )
    if suppressed:
        logger.info(
            "[BRANCH_TRIGGER_ROUTER] prebrain contract suppressed tenant=%s reason=%s "
            "owner=%s topic=%s",
            tenant_id,
            prebrain_contract.suppress_reason,
            prebrain_contract.owner,
            prebrain_contract.topic,
        )
        return None

    from modules.ai.brain.commerce.operational_choice_turn_guard import (  # noqa: PLC0415
        customer_turn_probe_message,
        has_explicit_operational_intent,
        maybe_clear_stale_operational_choice,
        should_break_stale_operational_choice,
    )

    route_msg = customer_turn_probe_message(message or "")
    if should_break_stale_operational_choice(
        message or "",
        inbound_metadata=inbound_metadata,
    ) and not has_explicit_operational_intent(message or ""):
        maybe_clear_stale_operational_choice(
            db,
            tenant_id=int(tenant_id or 0),
            customer_phone=customer_phone or "",
            message=message or "",
            inbound_metadata=inbound_metadata,
        )
        return None

    pending = evaluate_pending_operational_choice_routing(
        db,
        tenant_id=int(tenant_id or 0),
        message=route_msg or (message or ""),
        customer_phone=customer_phone or "",
        inbound_metadata=inbound_metadata,
    )
    if pending is not None:
        return pending

    from modules.operations.branch_contact_evidence import (  # noqa: PLC0415
        structured_branch_contacts_enabled,
        tenant_has_structured_branch_data,
    )

    if not structured_branch_contacts_enabled():
        return None
    if not tenant_has_structured_branch_data(db, int(tenant_id or 0)):
        return None

    from modules.ai.brain.commerce.checkout_slot_contact_guard import (  # noqa: PLC0415
        should_defer_contact_routing_for_checkout_slot,
    )
    from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
        is_explicit_arrival_intent,
        should_defer_contact_policies_for_commerce,
    )
    from modules.operations.branch_contact_evidence import (  # noqa: PLC0415
        resolve_branch_for_message,
    )

    if should_defer_contact_routing_for_checkout_slot(
        db,
        tenant_id=int(tenant_id or 0),
        customer_phone=customer_phone or "",
        message=message or "",
    ):
        return None

    if should_defer_contact_policies_for_commerce(message or ""):
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
        return None

    _pickup_confirm_re = None
    try:
        import re as _re  # noqa: PLC0415

        from modules.ai.brain.commerce.checkout_slot_contact_guard import (  # noqa: PLC0415
            has_explicit_showroom_pickup_intent,
        )
        from modules.ai.brain.commerce.link_intent import (  # noqa: PLC0415
            is_explicit_direct_location_request,
        )

        _raw = route_msg or (message or "").strip()
        _pickup_confirm_re = (
            has_explicit_showroom_pickup_intent(_raw)
            and not is_explicit_direct_location_request(_raw)
            and _re.search(
                r"(?:المعرض|الفرع|استلام\s*من|أ?ستلم\s*من|أ?ج(?:ي|يك)(?:كم|ك)?\s*(?:المعرض|الفرع)?|"
                r"موقع\s*المعرض|فرع\s+)",
                _raw,
                flags=_re.UNICODE | _re.IGNORECASE,
            )
        )
    except Exception:  # noqa: BLE001
        _pickup_confirm_re = None

    if (
        _pickup_confirm_re
        and not is_explicit_arrival_intent(route_msg or message or "")
    ):
        branch = resolve_branch_for_message(db, int(tenant_id or 0), route_msg or message or "")
        if branch is not None:
            return BranchTriggerDecision(
                trigger_type=TRIGGER_ARRIVAL_SOFT,
                matched_phrase="showroom_pickup_intent",
                branch_id=branch.id,
                reason="pickup_intent_confirm_first",
                reply_text=MSG_PICKUP_PREFERENCE_ASK,
                deliver_contact=False,
                persist_pending_choice=PENDING_PICKUP_MAPS_OR_CONTACT,
                pending_branch_id=int(branch.id),
            )

    match = match_branch_trigger(db, int(tenant_id or 0), route_msg or message or "")
    if match is None:
        return None

    config = load_branch_action_config(db, match.branch_id)
    if config is None:
        return None

    if match.trigger_type == TRIGGER_LOCATION_REQUEST:
        return _build_location_decision(db, tenant_id, route_msg or message or "", match, config)
    if match.trigger_type == TRIGGER_ARRIVAL_SOFT:
        return _build_soft_decision(match, config, route_msg or message or "")
    if match.trigger_type == TRIGGER_ARRIVAL_CONFIRMED:
        return _build_confirmed_decision(db, tenant_id, route_msg or message or "", match, config)
    if match.trigger_type == TRIGGER_NO_RESPONSE:
        return _build_no_response_decision(
            db, tenant_id, route_msg or message or "", match, customer_phone=customer_phone,
        )
    return None


__all__ = [
    "BranchTriggerDecision",
    "evaluate_branch_trigger_routing",
]
