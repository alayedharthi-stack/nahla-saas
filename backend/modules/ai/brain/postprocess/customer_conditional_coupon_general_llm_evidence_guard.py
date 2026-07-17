"""Rejection-only evidence guard for general-LLM replies under active conditional-coupon facts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from modules.ai.brain.persona.customer_conditional_coupon_claim_classification import (
    classify_customer_conditional_coupon_claim_violation,
)
from modules.ai.brain.truth_surface.flags import is_customer_conditional_coupon_layer0_enabled


@dataclass(frozen=True)
class CustomerConditionalCouponGeneralLlmGuardResult:
    reply: str
    rejected: bool
    failed_reason: str = ""


def should_apply_customer_conditional_coupon_general_llm_evidence_guard(
    *,
    customer_conditional_coupon_facts: Optional[Mapping[str, Any]] = None,
    customer_conditional_coupon_compose_active: bool = False,
) -> bool:
    """True when sanitized conditional facts are active on a non-persona compose path."""
    if not is_customer_conditional_coupon_layer0_enabled():
        return False
    if customer_conditional_coupon_compose_active:
        return False
    return isinstance(customer_conditional_coupon_facts, Mapping) and bool(
        customer_conditional_coupon_facts
    )


def apply_customer_conditional_coupon_general_llm_evidence_guard(
    reply: str,
    *,
    customer_conditional_coupon_facts: Mapping[str, Any],
) -> CustomerConditionalCouponGeneralLlmGuardResult:
    """Reject unsafe general-LLM text; valid text passes through unchanged."""
    original = str(reply or "")
    if not original.strip():
        return CustomerConditionalCouponGeneralLlmGuardResult(
            reply=original,
            rejected=False,
        )

    failed_reason = classify_customer_conditional_coupon_claim_violation(
        original,
        dict(customer_conditional_coupon_facts or {}),
    )
    if failed_reason:
        return CustomerConditionalCouponGeneralLlmGuardResult(
            reply="",
            rejected=True,
            failed_reason=failed_reason,
        )

    return CustomerConditionalCouponGeneralLlmGuardResult(
        reply=original,
        rejected=False,
    )


__all__ = [
    "CustomerConditionalCouponGeneralLlmGuardResult",
    "apply_customer_conditional_coupon_general_llm_evidence_guard",
    "should_apply_customer_conditional_coupon_general_llm_evidence_guard",
]
