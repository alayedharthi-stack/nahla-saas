"""Canary-safe OrderFlowV2 operational gate — per-tenant enforce + shadow canary."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Tuple

from core.ai_disabled_gate import (
    REASON_STORE_AI_TEST_MODE_NOT_ALLOWED,
    StoreAIModeDecision,
    disabled_reason_for_conversation,
    is_ai_allowed_by_store_mode,
    is_ai_disabled_for_conversation,
)
from modules.ai.order_flow_v2.flags import (
    is_order_flow_v2_enabled,
    is_order_flow_v2_shadow_enabled,
)
from modules.ai.order_flow_v2.tenant_rollout import (
    is_order_flow_v2_enforce_allowlist_configured,
    order_flow_v2_disabled_tenant_ids,
    order_flow_v2_enforce_tenant_ids,
)


@dataclass(frozen=True)
class OrderFlowV2OperationalDecision:
    live: bool
    shadow_log: bool
    reason: str = ""


def _billing_block_reason(db: Any, tenant_id: int) -> str:
    try:
        from core.billing import has_billing_access  # noqa: PLC0415

        if not has_billing_access(db, int(tenant_id)):
            return "billing_denied"
    except Exception:  # noqa: BLE001
        return "billing_check_failed"
    return ""


def _live_block_reason(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    conversation: Any,
    check_billing: bool,
) -> str:
    """Return a non-empty reason when live enforcement must be blocked (fail-closed)."""
    mode_decision = is_ai_allowed_by_store_mode(db, int(tenant_id), str(customer_phone or ""))
    if not mode_decision.allowed:
        return mode_decision.reason or REASON_STORE_AI_TEST_MODE_NOT_ALLOWED

    ai_decision = is_ai_disabled_for_conversation(
        db,
        tenant_id=int(tenant_id),
        customer_phone=str(customer_phone or ""),
        conversation=conversation,
        source="order_flow_v2",
    )
    if ai_decision.disabled:
        return ai_decision.reason

    if check_billing:
        billing_reason = _billing_block_reason(db, int(tenant_id))
        if billing_reason:
            return billing_reason
    return ""


def _shadow_or_disabled(
    *,
    shadow: bool,
    reason: str,
) -> OrderFlowV2OperationalDecision:
    if not shadow:
        return OrderFlowV2OperationalDecision(live=False, shadow_log=False, reason=reason or "disabled")
    return OrderFlowV2OperationalDecision(live=False, shadow_log=True, reason=reason)


def resolve_order_flow_v2_operational(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    conversation: Any = None,
) -> OrderFlowV2OperationalDecision:
    """
    Decide whether OrderFlowV2 may send customer-facing replies this turn.

    Per-tenant rollout (``ORDER_FLOW_V2_ENFORCE_TENANTS``):
      - unset → legacy ``ORDER_FLOW_V2_ENABLED`` enables all tenants live
      - set (including empty) → only listed tenant IDs are live-eligible

    ``ORDER_FLOW_V2_DISABLED_TENANTS`` always forces disabled (no live, no shadow).
  """
    tid = int(tenant_id)
    disabled_ids = order_flow_v2_disabled_tenant_ids()
    if tid in disabled_ids:
        return OrderFlowV2OperationalDecision(
            live=False,
            shadow_log=False,
            reason="tenant_disabled_allowlist",
        )

    enforce_configured = is_order_flow_v2_enforce_allowlist_configured()
    enforce_ids = order_flow_v2_enforce_tenant_ids()
    global_enabled = is_order_flow_v2_enabled()
    shadow = is_order_flow_v2_shadow_enabled()

    tenant_enforce = enforce_configured and tid in enforce_ids
    tenant_global = (not enforce_configured) and global_enabled

    if tenant_enforce or tenant_global:
        blocker = _live_block_reason(
            db,
            tenant_id=tid,
            customer_phone=customer_phone,
            conversation=conversation,
            check_billing=tenant_enforce,
        )
        if blocker:
            return _shadow_or_disabled(shadow=shadow, reason=blocker)
        reason = "tenant_enforce_allowlist" if tenant_enforce else "global_enabled"
        return OrderFlowV2OperationalDecision(live=True, shadow_log=False, reason=reason)

    if enforce_configured and tid not in enforce_ids and not shadow:
        return OrderFlowV2OperationalDecision(live=False, shadow_log=False, reason="disabled")

    if not shadow:
        return OrderFlowV2OperationalDecision(live=False, shadow_log=False, reason="disabled")

    mode_decision: StoreAIModeDecision = is_ai_allowed_by_store_mode(
        db, tid, str(customer_phone or "")
    )
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

    billing_reason = _billing_block_reason(db, tid)
    if billing_reason:
        return OrderFlowV2OperationalDecision(
            live=False,
            shadow_log=True,
            reason=billing_reason,
        )

    # Test vs live store_ai_mode must not select a different intelligence owner.
    # OFV2 live remains ORDER_FLOW_V2_ENABLED / ENFORCE_TENANTS only.
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
