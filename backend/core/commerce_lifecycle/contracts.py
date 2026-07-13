"""
Dispatch outcome contracts — structured facts only, no customer prose.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Literal, Tuple, Union

from core.commerce_lifecycle.intents import BusinessIntent

HANDOFF_KIND_LIFECYCLE = "lifecycle_notification"

# Stable machine-readable reason codes (not human prose).
REASON_MISSING_EVIDENCE = "missing_evidence"
REASON_INVALID_EVIDENCE = "invalid_evidence"
REASON_CAPABILITY_ABSENT = "capability_absent"
REASON_FORBIDDEN_CAPABILITY = "forbidden_capability"
REASON_NO_TEMPLATE_POLICY = "no_template_policy"
REASON_DUPLICATE = "duplicate"
REASON_UNSUPPORTED_INTENT = "unsupported_intent"
REASON_MERCHANT_ACTION_REQUIRED = "merchant_action_required"
REASON_NO_MESSAGE_POLICY = "no_message_policy"


@dataclass(frozen=True)
class SessionMessageRequired:
    """Open-window handoff — AI/message layer owns wording."""

    intent: BusinessIntent
    structured_facts: Dict[str, Any]
    handoff_kind: Literal["lifecycle_notification"] = HANDOFF_KIND_LIFECYCLE

    def __post_init__(self) -> None:
        if "message_text" in self.structured_facts:
            raise ValueError("structured_facts must not contain message_text")
        if "prompt" in self.structured_facts:
            raise ValueError("structured_facts must not contain prompt")


@dataclass(frozen=True)
class ApprovedTemplateRequired:
    """Closed-window template path — send deferred to future PR."""

    intent: BusinessIntent
    service_key: str
    variables: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NoNotification:
    intent: BusinessIntent
    reason_code: str


@dataclass(frozen=True)
class MerchantActionRequired:
    intent: BusinessIntent
    reason_code: str
    missing_requirements: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Blocked:
    intent: BusinessIntent
    reason_code: str
    missing_evidence: Tuple[str, ...] = ()
    invalid_evidence: Tuple[str, ...] = ()
    missing_capabilities: Tuple[str, ...] = ()


DispatchOutcome = Union[
    SessionMessageRequired,
    ApprovedTemplateRequired,
    NoNotification,
    MerchantActionRequired,
    Blocked,
]

STABLE_REASON_CODES: FrozenSet[str] = frozenset({
    REASON_MISSING_EVIDENCE,
    REASON_INVALID_EVIDENCE,
    REASON_CAPABILITY_ABSENT,
    REASON_FORBIDDEN_CAPABILITY,
    REASON_NO_TEMPLATE_POLICY,
    REASON_DUPLICATE,
    REASON_UNSUPPORTED_INTENT,
    REASON_MERCHANT_ACTION_REQUIRED,
    REASON_NO_MESSAGE_POLICY,
})

__all__ = [
    "ApprovedTemplateRequired",
    "Blocked",
    "DispatchOutcome",
    "HANDOFF_KIND_LIFECYCLE",
    "MerchantActionRequired",
    "NoNotification",
    "REASON_CAPABILITY_ABSENT",
    "REASON_DUPLICATE",
    "REASON_FORBIDDEN_CAPABILITY",
    "REASON_INVALID_EVIDENCE",
    "REASON_MERCHANT_ACTION_REQUIRED",
    "REASON_MISSING_EVIDENCE",
    "REASON_NO_MESSAGE_POLICY",
    "REASON_NO_TEMPLATE_POLICY",
    "REASON_UNSUPPORTED_INTENT",
    "STABLE_REASON_CODES",
    "SessionMessageRequired",
]
