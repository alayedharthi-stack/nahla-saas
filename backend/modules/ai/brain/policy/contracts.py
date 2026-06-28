"""Dataclasses for merchant operational policy hints (shadow metadata only)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class EscalationPolicyHint:
    use_configured_levels: bool = False
    start_level: str = ""
    max_auto_level: Optional[str] = None


@dataclass(frozen=True)
class ContactPolicyHint:
    source: str = ""
    allow_named_staff: bool = False
    require_configured_only: bool = True


@dataclass(frozen=True)
class ShowroomPolicyHint:
    send_location_first: bool = False
    send_contact_after_location: bool = False
    escalate_after_contact: bool = False


@dataclass(frozen=True)
class MerchantOperationalPolicyHint:
    response_purpose: Optional[str] = None
    required_action: Optional[str] = None
    allowed_actions: Tuple[str, ...] = ()
    forbidden_actions: Tuple[str, ...] = ()
    escalation_policy_hint: Optional[EscalationPolicyHint] = None
    contact_policy_hint: Optional[ContactPolicyHint] = None
    showroom_policy_hint: Optional[ShowroomPolicyHint] = None
    confidence: float = 0.0
    evidence: Tuple[str, ...] = ()
    source_sections: Tuple[str, ...] = ()
    conflict: bool = False
    missing_config_reason: Optional[str] = None


def hint_to_log_dict(hint: MerchantOperationalPolicyHint) -> Dict[str, Any]:
    """Structured payload for ``[MERCHANT_OP_POLICY]`` logs."""
    return {
        "response_purpose": hint.response_purpose,
        "required_action": hint.required_action,
        "allowed_actions": list(hint.allowed_actions),
        "forbidden_actions": list(hint.forbidden_actions),
        "confidence": round(float(hint.confidence or 0.0), 3),
        "conflict": bool(hint.conflict),
        "source_sections": list(hint.source_sections),
        "missing_config_reason": hint.missing_config_reason,
        "evidence": list(hint.evidence),
        "escalation_policy_hint": (
            {
                "use_configured_levels": hint.escalation_policy_hint.use_configured_levels,
                "start_level": hint.escalation_policy_hint.start_level,
                "max_auto_level": hint.escalation_policy_hint.max_auto_level,
            }
            if hint.escalation_policy_hint is not None
            else None
        ),
        "contact_policy_hint": (
            {
                "source": hint.contact_policy_hint.source,
                "allow_named_staff": hint.contact_policy_hint.allow_named_staff,
                "require_configured_only": hint.contact_policy_hint.require_configured_only,
            }
            if hint.contact_policy_hint is not None
            else None
        ),
        "showroom_policy_hint": (
            {
                "send_location_first": hint.showroom_policy_hint.send_location_first,
                "send_contact_after_location": hint.showroom_policy_hint.send_contact_after_location,
                "escalate_after_contact": hint.showroom_policy_hint.escalate_after_contact,
            }
            if hint.showroom_policy_hint is not None
            else None
        ),
    }
