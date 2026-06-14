"""
Arrival contact delivery — pre-brain deterministic showroom contact.

When a customer signals arrival / on-the-way / at-door AND merchant KB
has compiled ``arrival_contact`` evidence with a resolvable phone, deliver
the showroom contact (vCard) before the LLM runs.

Platform-wide: no hardcoded names, no CS/owner fallback unless evidence
exists in the arrival compile path.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.arrival_contact_delivery")

_FLAG_FALSY = frozenset({"0", "false", "no", "off"})

MSG_ARRIVAL_CONTACT_NOT_CONFIGURED = (
    "أبشر، في انتظارك. رقم استقبال المعرض غير مهيّأ حالياً لهذا المتجر."
)


def arrival_contact_delivery_enabled() -> bool:
    raw = os.getenv("ARRIVAL_CONTACT_DELIVERY_ENABLED", "1").strip().lower()
    return raw not in _FLAG_FALSY


@dataclass(frozen=True)
class ArrivalContactEvidence:
    lookup_name: str
    phone: str
    section_id: Optional[int]
    role: str = "showroom"
    compile_reason: str = ""
    source_sections: tuple[int, ...] = ()


@dataclass(frozen=True)
class ArrivalContactDeliveryDecision:
    reply_text: str
    call_target: Any = None
    deliver_contact: bool = False
    reason: str = ""
    contact_lookup_name: str = ""
    contact_phone: str = ""
    skip_brain: bool = True


def _build_arrival_reply_text(lookup_name: str) -> str:
    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        resolve_contact_display_name,
    )

    label = resolve_contact_display_name(lookup_name, role="showroom")
    return f"أبشر، في انتظارك 🌷 تقدر تتواصل مع {label}."


def resolve_arrival_contact_evidence(
    db: Any,
    tenant_id: int,
) -> Optional[ArrivalContactEvidence]:
    """Return compiled showroom contact when policy + phone evidence exist."""
    if db is None or not tenant_id:
        return None

    from modules.ai.brain.commerce.arrival_contact_policy import (  # noqa: PLC0415
        resolve_arrival_contact_policy,
    )
    from modules.ai.brain.commerce.arrival_contact_compile_v0 import (  # noqa: PLC0415
        resolve_showroom_contact_for_delivery,
    )

    verdict = resolve_arrival_contact_policy(db, int(tenant_id))
    if not verdict.allowed:
        return None
    if getattr(verdict, "policy_source", "") != "compiled_v0":
        return None

    try:
        from modules.ai.brain.commerce.staff_contact_fallback_v0 import (  # noqa: PLC0415
            load_staff_chain_sections,
        )

        sections = load_staff_chain_sections(db, int(tenant_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[ARRIVAL_CONTACT_DELIVERY] sections_load_failed tenant=%s err=%s",
            tenant_id, exc,
        )
        return None

    preferred = tuple(getattr(verdict, "source_sections", ()) or ())
    contact = resolve_showroom_contact_for_delivery(
        sections, preferred_section_ids=preferred,
    )
    if contact is None or not contact.phone:
        lookup_hint = str(getattr(verdict, "contact_lookup_name", "") or "").strip()
        if lookup_hint:
            try:
                from modules.ai.postprocess.safety_nets import (  # noqa: PLC0415
                    _lookup_staff_phone_in_kb,
                )

                phone, _kind, sid = _lookup_staff_phone_in_kb(
                    db, int(tenant_id), lookup_hint,
                )
                if phone:
                    section_id = int(sid) if sid else None
                    return ArrivalContactEvidence(
                        lookup_name=lookup_hint,
                        phone=phone,
                        section_id=section_id,
                        compile_reason=getattr(verdict, "reason", "") or "",
                        source_sections=preferred,
                    )
            except Exception:  # noqa: silent-ok - optional KB phone lookup fallback must not block arrival compile
                pass
        return None

    return ArrivalContactEvidence(
        lookup_name=contact.lookup_name,
        phone=contact.phone,
        section_id=contact.section_id,
        compile_reason=getattr(verdict, "reason", "") or "",
        source_sections=preferred,
    )


def evaluate_arrival_contact_delivery(
    db: Any,
    *,
    tenant_id: int,
    message: str,
) -> Optional[ArrivalContactDeliveryDecision]:
    """Pre-brain short-circuit for arrival / visit signals."""
    if not arrival_contact_delivery_enabled():
        return None

    from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
        is_explicit_arrival_intent,
        should_defer_contact_policies_for_commerce,
    )

    if should_defer_contact_policies_for_commerce(message or ""):
        return None

    if not is_explicit_arrival_intent(message or ""):
        return None

    evidence = resolve_arrival_contact_evidence(db, int(tenant_id or 0))
    if evidence is None or not evidence.phone:
        logger.info(
            "[ARRIVAL_CONTACT_DELIVERY] tenant=%s deliver=false reason=no_evidence",
            tenant_id,
        )
        return ArrivalContactDeliveryDecision(
            reply_text=MSG_ARRIVAL_CONTACT_NOT_CONFIGURED,
            deliver_contact=False,
            reason="no_arrival_evidence",
            skip_brain=True,
        )

    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        build_staff_call_target,
    )

    call_target = build_staff_call_target(
        lookup_name=evidence.lookup_name,
        phone=evidence.phone,
        role=evidence.role,
    )
    if call_target is None:
        logger.info(
            "[ARRIVAL_CONTACT_DELIVERY] tenant=%s deliver=false "
            "reason=phone_normalize_failed lookup=%r",
            tenant_id,
            (evidence.lookup_name or "")[:32],
        )
        return ArrivalContactDeliveryDecision(
            reply_text=MSG_ARRIVAL_CONTACT_NOT_CONFIGURED,
            deliver_contact=False,
            reason="phone_normalize_failed",
            skip_brain=True,
        )

    logger.info(
        "[ARRIVAL_CONTACT_DELIVERY] tenant=%s deliver=true reason=%s "
        "section_id=%s name_len=%d",
        tenant_id,
        evidence.compile_reason or "compiled_v0",
        evidence.section_id if evidence.section_id else "-",
        len(evidence.lookup_name or ""),
    )
    return ArrivalContactDeliveryDecision(
        reply_text=_build_arrival_reply_text(evidence.lookup_name),
        call_target=call_target,
        deliver_contact=True,
        reason=evidence.compile_reason or "compiled_v0",
        contact_lookup_name=evidence.lookup_name,
        contact_phone=evidence.phone,
        skip_brain=True,
    )


__all__ = [
    "ArrivalContactDeliveryDecision",
    "ArrivalContactEvidence",
    "MSG_ARRIVAL_CONTACT_NOT_CONFIGURED",
    "arrival_contact_delivery_enabled",
    "evaluate_arrival_contact_delivery",
    "resolve_arrival_contact_evidence",
]
