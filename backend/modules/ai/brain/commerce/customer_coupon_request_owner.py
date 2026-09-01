"""Bounded ownership for customer_coupon_request capability (Phase 2C).

Does not mutate intent.name. Canary-off and probe-none leave the existing
decision untouched. Issuance is delegated to customer_request_coupon_service.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from modules.ai.brain.decision.actions import (
    ACTION_CUSTOMER_COUPON_REQUEST,
    ACTION_LLM_REPLY,
)
from modules.ai.brain.commerce.fact_answer import STATUS_KNOWN_EMPTY, STATUS_KNOWN_VALUE
from modules.ai.brain.types import Decision
from services.customer_request_coupon_canary import is_customer_coupon_canary_tenant
from services.customer_request_coupon_service import (
    CUSTOMER_COUPON_LIVE_ISSUANCE,
    CUSTOMER_COUPON_LIVE_ROUTING,
    CustomerCouponIssuanceResult,
    REASON_REUSED,
)

logger = logging.getLogger("nahla.brain.customer_coupon_request_owner")

CAPABILITY_CUSTOMER_COUPON_REQUEST = "customer_coupon_request"

# Coupon capability may replace proven generic LLM fallback only.
_ELIGIBLE_FALLBACK_ACTIONS = frozenset({ACTION_LLM_REPLY})


def global_live_routing_enabled() -> bool:
    return bool(CUSTOMER_COUPON_LIVE_ROUTING)


def global_live_issuance_enabled() -> bool:
    return bool(CUSTOMER_COUPON_LIVE_ISSUANCE)


def should_own_customer_coupon_request_turn(
    *,
    tenant_id: Optional[int],
    capability: str,
    parse_ok: bool,
    current_action: str,
) -> bool:
    """Canary + positive parsed capability owns the turn before LLM fallback."""
    if global_live_routing_enabled():
        # Global live routing stays off in this PR. Fail closed if ever flipped
        # without a canary tenant — canary remains the only live gate.
        pass
    if not is_customer_coupon_canary_tenant(tenant_id):
        return False
    if not parse_ok:
        return False
    if str(capability or "") != CAPABILITY_CUSTOMER_COUPON_REQUEST:
        return False
    if str(current_action or "") not in _ELIGIBLE_FALLBACK_ACTIONS:
        return False
    return True


def _human_priority_blocks_ownership(decision: Decision) -> bool:
    return bool((decision.args or {}).get("human_priority"))


def maybe_own_customer_coupon_request_turn(
    decision: Decision,
    *,
    tenant_id: Optional[int],
    coupon_capability_telemetry: Optional[Dict[str, Any]] = None,
) -> Decision:
    """Replace catch-all decisions with ACTION_CUSTOMER_COUPON_REQUEST when eligible."""
    telemetry = dict(coupon_capability_telemetry or {})
    capability = str(telemetry.get("coupon_capability") or "none")
    parse_ok = bool(telemetry.get("coupon_capability_parse_ok"))
    if _human_priority_blocks_ownership(decision):
        return decision
    if not should_own_customer_coupon_request_turn(
        tenant_id=tenant_id,
        capability=capability,
        parse_ok=parse_ok,
        current_action=str(decision.action or ""),
    ):
        return decision
    args = dict(decision.args or {})
    args["coupon_capability"] = CAPABILITY_CUSTOMER_COUPON_REQUEST
    args["tenant_canary_enabled"] = True
    return Decision(
        action=ACTION_CUSTOMER_COUPON_REQUEST,
        args=args,
        reason="customer_coupon_request_canary_capability",
        confidence=max(float(decision.confidence or 0), 0.9),
        next_slot=decision.next_slot,
    )


def project_customer_request_coupon_facts(
    result: CustomerCouponIssuanceResult,
) -> Dict[str, Any]:
    """Customer-visible structured facts for compose. No DB ids or lock state."""
    issued = bool(result.issued)
    facts: Dict[str, Any] = {
        "requested": True,
        "issued": issued,
        "reason": str(result.reason_code or ""),
        "coupon_level": result.resolved_level,
        "reused_assignment": str(result.reason_code or "") == REASON_REUSED,
        "policy_allowed": bool(result.policy_allowed),
        "countable_orders": int(result.countable_orders or 0),
    }
    if issued:
        if result.code:
            facts["coupon_code"] = str(result.code)
        if result.discount_type:
            facts["discount_type"] = str(result.discount_type)
        if result.discount_value is not None:
            facts["discount_value"] = str(result.discount_value)
        if result.expires_at:
            facts["expires_at"] = str(result.expires_at)
        if result.min_order_amount is not None:
            facts["min_order_amount"] = result.min_order_amount
    return facts


def coupon_answer_contract_from_facts(facts: Dict[str, Any]) -> Dict[str, Any]:
    """Existing answer_contract surface — no new prompt, no canned prose."""
    issued = bool(facts.get("issued"))
    code = str(facts.get("coupon_code") or "").strip()
    reason = str(facts.get("reason") or "")
    if issued and code:
        return {
            "fact_kind": "customer_request_coupon",
            "status": STATUS_KNOWN_VALUE,
            "claimable_values": [code],
            "closed_reason_code": reason,
            "evidence_refs": ["customer_request_coupon_facts"],
        }
    return {
        "fact_kind": "customer_request_coupon",
        "status": STATUS_KNOWN_EMPTY,
        "claimable_values": [],
        "closed_reason_code": reason,
        "evidence_refs": ["customer_request_coupon_facts"],
    }


def attach_customer_request_coupon_facts_to_reply_state(
    reply_state: Any,
    facts: Optional[Dict[str, Any]],
) -> None:
    if reply_state is None or not isinstance(facts, dict) or not facts:
        return
    known = dict(getattr(reply_state, "known_facts", None) or {})
    known["customer_request_coupon_facts"] = dict(facts)
    known["answer_contract"] = coupon_answer_contract_from_facts(facts)
    reply_state.known_facts = known


__all__ = [
    "CAPABILITY_CUSTOMER_COUPON_REQUEST",
    "_ELIGIBLE_FALLBACK_ACTIONS",
    "attach_customer_request_coupon_facts_to_reply_state",
    "coupon_answer_contract_from_facts",
    "global_live_issuance_enabled",
    "global_live_routing_enabled",
    "maybe_own_customer_coupon_request_turn",
    "project_customer_request_coupon_facts",
    "should_own_customer_coupon_request_turn",
]
