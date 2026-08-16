"""
Staff contact recovery — pre-LLM deterministic escalation chain advance.

When ``staff_contacts_sent`` is populated and the customer reports the last
contact did not respond («ما يرد»), skip the brain/LLM generic greeting reset
and send the next merchant-defined contact from the KB chain via
``staff_contact_fallback_v0``.

Platform-wide: no tenant names, no hardcoded staff identities — chain order
comes from merchant KB sections only.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

logger = logging.getLogger("nahla.brain.staff_contact_recovery")


@dataclass(frozen=True)
class StaffContactRecoveryDecision:
    """Deterministic short-circuit payload for the webhook."""

    reply_text: str
    call_target: Any = None
    deliver_contact: bool = True
    next_contact_name: str = ""
    next_contact_phone: str = ""
    reason: str = ""
    trigger: str = ""
    conversation_turn: int = 0


def staff_contact_recovery_enabled() -> bool:
    """Kill-switch — default ON (``STAFF_CONTACT_RECOVERY_ENABLED=0`` to disable)."""
    raw = os.getenv("STAFF_CONTACT_RECOVERY_ENABLED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _build_recovery_reply_text(contact_name: str, *, role: str = "") -> str:
    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        resolve_contact_display_name,
    )

    label = resolve_contact_display_name(contact_name, role=role, fallback="")
    if label:
        return f"حاضر، جرّب التواصل مع {label}."
    return "حاضر، هذا رقم التواصل التالي."


def _load_contacts_sent(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    contacts_sent_raw: Optional[Sequence[Any]] = None,
) -> List[dict]:
    from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
        parse_staff_contacts_sent,
    )

    if contacts_sent_raw is not None:
        return parse_staff_contacts_sent(contacts_sent_raw)

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


def _conversation_turn(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
) -> int:
    if not db or not tenant_id or not phone:
        return 0
    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415

        _conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
        return int((bs or {}).get("turn") or 0)
    except Exception:  # noqa: BLE001
        return 0


def evaluate_staff_contact_recovery(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    message: str,
    contacts_sent_raw: Optional[Sequence[Any]] = None,
    conversation_id: Optional[int] = None,
) -> Optional[StaffContactRecoveryDecision]:
    """Return a recovery decision when the narrow gate fires; else ``None``."""
    if not staff_contact_recovery_enabled():
        logger.info(
            "[STAFF_CONTACT_RECOVERY] tenant=%s conversation_id=%s "
            "fired=false reason=flag_disabled",
            tenant_id,
            conversation_id if conversation_id is not None else "-",
        )
        return None

    from modules.ai.brain.commerce.unstructured_turn_ownership import (  # noqa: PLC0415
        unstructured_natural_language_requires_brain,
    )

    if unstructured_natural_language_requires_brain(message=message or ""):
        return None

    from modules.ai.brain.commerce.checkout_slot_contact_guard import (  # noqa: PLC0415
        should_defer_contact_routing_for_checkout_slot,
    )

    if should_defer_contact_routing_for_checkout_slot(
        db,
        tenant_id=int(tenant_id or 0),
        customer_phone=phone or "",
        message=message or "",
    ):
        return None

    from modules.ai.brain.commerce.contact_escalation import (  # noqa: PLC0415
        classify_employee_not_responding,
    )

    enr = classify_employee_not_responding(message or "")
    if enr is None:
        return None

    contacts_sent = _load_contacts_sent(
        db,
        tenant_id=tenant_id,
        phone=phone,
        contacts_sent_raw=contacts_sent_raw,
    )
    if not contacts_sent:
        logger.info(
            "[STAFF_CONTACT_RECOVERY] tenant=%s conversation_id=%s "
            "fired=false reason=no_prior_contacts_sent trigger=employee_not_responding",
            tenant_id,
            conversation_id if conversation_id is not None else "-",
        )
        return None

    from modules.ai.brain.commerce.staff_contact_fallback_v0 import (  # noqa: PLC0415
        load_staff_chain_sections,
        resolve_staff_contact_fallback_v0,
    )

    sections = load_staff_chain_sections(db, int(tenant_id or 0))
    verdict = resolve_staff_contact_fallback_v0(
        sections,
        contacts_sent=contacts_sent,
        customer_msg=message or "",
        trigger="employee_not_responding",
        tenant_id=tenant_id,
        db=db,
    )

    if not verdict.enabled or not verdict.next_phone:
        turn = _conversation_turn(db, tenant_id=tenant_id, phone=phone)
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
            MSG_NO_NEXT_ESCALATION,
        )

        if contacts_sent and verdict.reason == "chain_exhausted":
            logger.info(
                "[STAFF_CONTACT_RECOVERY] tenant=%s conversation_id=%s "
                "fired=true trigger=employee_not_responding reason=chain_exhausted "
                "deliver_contact=false contacts_sent_count=%d chain_len=%d",
                tenant_id,
                conversation_id if conversation_id is not None else "-",
                len(contacts_sent),
                verdict.chain_len,
            )
            return StaffContactRecoveryDecision(
                reply_text=MSG_NO_NEXT_ESCALATION,
                call_target=None,
                deliver_contact=False,
                reason="chain_exhausted",
                trigger="employee_not_responding",
                conversation_turn=turn,
            )
        logger.info(
            "[STAFF_CONTACT_RECOVERY] tenant=%s conversation_id=%s "
            "fired=false reason=%s trigger=employee_not_responding "
            "contacts_sent_count=%d chain_len=%d",
            tenant_id,
            conversation_id if conversation_id is not None else "-",
            verdict.reason or "fallback_disabled",
            len(contacts_sent),
            verdict.chain_len,
        )
        return None

    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        build_staff_call_target,
        resolve_contact_display_name,
    )

    display_name = resolve_contact_display_name(
        verdict.next_lookup_name,
        fallback="الإدارة",
    )
    call_target = build_staff_call_target(
        lookup_name=verdict.next_lookup_name,
        phone=verdict.next_phone,
    )
    if call_target is None:
        logger.info(
            "[STAFF_CONTACT_RECOVERY] tenant=%s conversation_id=%s "
            "fired=false reason=phone_normalize_failed",
            tenant_id,
            conversation_id if conversation_id is not None else "-",
        )
        return None

    turn = _conversation_turn(db, tenant_id=tenant_id, phone=phone)

    logger.info(
        "[STAFF_CONTACT_RECOVERY] tenant=%s conversation_id=%s "
        "fired=true trigger=employee_not_responding reason=%s "
        "selected=%r contacts_sent_count=%d chain_index=%d "
        "skip_brain=true",
        tenant_id,
        conversation_id if conversation_id is not None else "-",
        verdict.reason or "next_in_chain",
        display_name[:48],
        len(contacts_sent),
        verdict.last_sent_index + 1,
    )

    return StaffContactRecoveryDecision(
        reply_text=_build_recovery_reply_text(verdict.next_lookup_name),
        call_target=call_target,
        deliver_contact=True,
        next_contact_name=call_target.name,
        next_contact_phone=verdict.next_phone,
        reason=verdict.reason or "next_in_chain",
        trigger="employee_not_responding",
        conversation_turn=turn,
    )


def maybe_staff_contact_recovery(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    message: str,
    contacts_sent_raw: Optional[Sequence[Any]] = None,
    conversation_id: Optional[int] = None,
) -> Optional[StaffContactRecoveryDecision]:
    """Public entry — same as :func:`evaluate_staff_contact_recovery`."""
    return evaluate_staff_contact_recovery(
        db,
        tenant_id=tenant_id,
        phone=phone,
        message=message,
        contacts_sent_raw=contacts_sent_raw,
        conversation_id=conversation_id,
    )


__all__ = [
    "StaffContactRecoveryDecision",
    "evaluate_staff_contact_recovery",
    "maybe_staff_contact_recovery",
    "staff_contact_recovery_enabled",
]
