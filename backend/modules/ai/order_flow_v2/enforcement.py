"""Canary-safe OrderFlowV2 operational gate — shadow becomes live only on allowed test traffic."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Tuple

from core.ai_disabled_gate import (
    REASON_STORE_AI_DISABLED,
    REASON_STORE_AI_TEST_MODE_NOT_ALLOWED,
    disabled_reason_for_conversation,
    is_ai_allowed_by_store_mode,
)
from modules.ai.order_flow_v2.flags import (
    is_order_flow_v2_enabled,
    is_order_flow_v2_shadow_enabled,
)


@dataclass(frozen=True)
class OrderFlowV2OperationalDecision:
    live: bool
    shadow_log: bool
    reason: str = ""


def resolve_order_flow_v2_operational(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    conversation: Any = None,
) -> OrderFlowV2OperationalDecision:
    """
    Decide whether OrderFlowV2 may send customer-facing replies this turn.

    Global ``ORDER_FLOW_V2_ENABLED`` → live for all allowed inbound.
    Shadow + test-mode allowlisted phone + billing → canary enforcement (live).
    Otherwise shadow logs only.
    """
    enabled = is_order_flow_v2_enabled()
    shadow = is_order_flow_v2_shadow_enabled()

    if enabled:
        return OrderFlowV2OperationalDecision(live=True, shadow_log=False, reason="global_enabled")
    if not shadow:
        return OrderFlowV2OperationalDecision(live=False, shadow_log=False, reason="disabled")

    mode_decision = is_ai_allowed_by_store_mode(db, int(tenant_id), str(customer_phone or ""))
    if not mode_decision.allowed:
        return OrderFlowV2OperationalDecision(
            live=False,
            shadow_log=True,
            reason=mode_decision.reason or REASON_STORE_AI_TEST_MODE_NOT_ALLOWED,
        )

    convo_reason = disabled_reason_for_conversation(conversation)
    if convo_reason:
        return OrderFlowV2OperationalDecision(
            live=False,
            shadow_log=True,
            reason=convo_reason,
        )

    try:
        from core.billing import has_billing_access  # noqa: PLC0415

        if not has_billing_access(db, int(tenant_id)):
            return OrderFlowV2OperationalDecision(
                live=False,
                shadow_log=True,
                reason="billing_denied",
            )
    except Exception:  # noqa: BLE001
        return OrderFlowV2OperationalDecision(
            live=False,
            shadow_log=True,
            reason="billing_check_failed",
        )

    from core.tenant import STORE_AI_MODE_TEST  # noqa: PLC0415

    if mode_decision.mode == STORE_AI_MODE_TEST:
        return OrderFlowV2OperationalDecision(
            live=True,
            shadow_log=False,
            reason="test_mode_canary_enforcement",
        )

    # store_ai_mode=on but global V2 disabled — stay shadow-only
    return OrderFlowV2OperationalDecision(live=False, shadow_log=True, reason="shadow_only")


def operational_tuple(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    conversation: Any = None,
) -> Tuple[bool, bool, str]:
    """Return (live, shadow_log, reason) for owner._finalize_result."""
    decision = resolve_order_flow_v2_operational(
        db,
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        conversation=conversation,
    )
    return decision.live, decision.shadow_log, decision.reason


__all__ = [
    "OrderFlowV2OperationalDecision",
    "operational_tuple",
    "resolve_order_flow_v2_operational",
]
