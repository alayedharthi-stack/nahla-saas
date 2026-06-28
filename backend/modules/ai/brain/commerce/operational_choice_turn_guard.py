"""
operational_choice_turn_guard.py
────────────────────────────────
Break stale location/contact operational replay on current social/media turns.

Pending pickup maps-or-contact choice and branch-trigger disambiguation must
not replay unless the customer's current authored message carries explicit
operational intent.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.operational_choice_turn_guard")

_PICKUP_PREFERENCE_MARKERS = (
    "موقع المعرض",
    "بيانات التواصل",
    "pickup_maps_or_contact",
)


def customer_turn_probe_message(message: str) -> str:
    """Customer-authored text for current-turn ownership probes."""
    from modules.ai.brain.commerce.link_intent_media_source_guard import (  # noqa: PLC0415
        link_intent_message,
    )

    return link_intent_message(message or "").strip()


def has_explicit_operational_intent(message: str) -> bool:
    """True when the current authored turn explicitly asks for location/contact."""
    raw = customer_turn_probe_message(message or "")
    if not raw:
        return False
    try:
        from modules.ai.brain.commerce.pending_operational_choice import (  # noqa: PLC0415
            is_pending_choice_confirmation,
        )

        if is_pending_choice_confirmation(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional pending confirm probe
        pass
    try:
        from modules.ai.brain.commerce.link_intent import (  # noqa: PLC0415
            LinkIntentType,
            resolve_inbound_link_intent,
        )

        if resolve_inbound_link_intent(raw) != LinkIntentType.UNKNOWN_LINK:
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional link intent probe
        pass
    try:
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            classify_staff_contact_request,
        )

        if classify_staff_contact_request(raw).kind != "none":
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional staff contact probe
        pass
    try:
        from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
            is_explicit_arrival_intent,
        )

        if is_explicit_arrival_intent(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional arrival intent probe
        pass
    return False


def should_break_stale_operational_choice(
    message: str,
    *,
    inbound_metadata: Optional[dict[str, Any]] = None,
    intent: Any = None,
    state: Any = None,
) -> bool:
    """
    True when stale operational choice replay/routing must stop for this turn.

    Captionless media, social/greeting/dua turns, and other current-turn social
    ownership all break replay unless the customer explicitly asked operationally.
    """
    full = (message or "").strip()
    if has_explicit_operational_intent(full):
        return False

    from modules.ai.brain.commerce.staff_contact_media_source_guard import (  # noqa: PLC0415
        is_media_framed_inbound_message,
    )

    probe = customer_turn_probe_message(full)
    if is_media_framed_inbound_message(full) and not probe:
        return True

    if not probe and not full:
        return True

    try:
        from modules.ai.brain.current_turn_social_non_commerce import (  # noqa: PLC0415
            resolve_current_turn_social_non_commerce,
        )

        verdict = resolve_current_turn_social_non_commerce(
            probe or full,
            intent=intent,
            inbound_metadata=inbound_metadata,
            state=state,
        )
        if verdict.matched:
            return True
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[OPERATIONAL_CHOICE_TURN_GUARD] social_probe_failed err=%s",
            exc,
        )
    return False


def pending_question_is_operational_choice(last_question: str) -> bool:
    lq = (last_question or "").strip().lower()
    if not lq:
        return False
    return any(marker in lq for marker in _PICKUP_PREFERENCE_MARKERS)


def maybe_clear_stale_operational_choice(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    message: str,
    inbound_metadata: Optional[dict[str, Any]] = None,
    intent: Any = None,
    state: Any = None,
) -> bool:
    """Clear persisted pickup maps/contact pending choice when turn ownership breaks."""
    if not should_break_stale_operational_choice(
        message,
        inbound_metadata=inbound_metadata,
        intent=intent,
        state=state,
    ):
        return False
    if not db or not tenant_id or not customer_phone:
        return False
    try:
        from modules.ai.brain.commerce.pending_operational_choice import (  # noqa: PLC0415
            clear_pending_operational_choice,
            read_pending_operational_choice,
        )

        choice, _branch_id = read_pending_operational_choice(
            db,
            tenant_id=int(tenant_id or 0),
            customer_phone=customer_phone,
        )
        if not choice:
            return False
        cleared = clear_pending_operational_choice(
            db,
            tenant_id=int(tenant_id or 0),
            phone=customer_phone,
        )
        if cleared:
            logger.info(
                "[OPERATIONAL_CHOICE_TURN_GUARD] cleared pending=%s tenant=%s",
                choice,
                tenant_id,
            )
        return cleared
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[OPERATIONAL_CHOICE_TURN_GUARD] clear_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return False


__all__ = [
    "customer_turn_probe_message",
    "has_explicit_operational_intent",
    "maybe_clear_stale_operational_choice",
    "pending_question_is_operational_choice",
    "should_break_stale_operational_choice",
]
