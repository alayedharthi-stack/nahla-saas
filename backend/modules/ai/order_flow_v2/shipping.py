"""OrderFlowV2 shipping readiness — evidence only, no false claims."""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.order_payment_policy import PAYMENT_METHOD_BANK_TRANSFER
from core.wa_order_lifecycle import is_payment_verified
from modules.ai.commerce_agent.policies.shipping_readiness import evaluate_shipping_readiness
from modules.ai.commerce_agent.contracts import AgentInputContext

from .payment_evidence import payment_confirmation_allowed
from .state import line_items_from_state


def evaluate_v2_shipping_readiness(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    customer_phone: str = "",
) -> Dict[str, Any]:
    items = line_items_from_state(order_prep, brain_state)
    ctx = AgentInputContext(
        tenant_id=0,
        customer_phone=customer_phone,
        message="",
        order_prep=order_prep,
        brain_state=brain_state,
        line_items=items,
    )
    verdict = evaluate_shipping_readiness(ctx)
    return {
        "allowed": verdict.allowed,
        "missing_fields": list(verdict.missing_fields),
        "shipping_ready": bool((verdict.metadata or {}).get("shipping_ready")),
    }


def can_claim_shipping_started(order_prep: Dict[str, Any]) -> bool:
    method = str(order_prep.get("payment_method") or "").strip().lower()
    if method in {PAYMENT_METHOD_BANK_TRANSFER, "bank_transfer", "transfer"}:
        return is_payment_verified(order_prep) and payment_confirmation_allowed(order_prep)
    return bool(order_prep.get("payment_confirmed") or order_prep.get("payment_verified"))
