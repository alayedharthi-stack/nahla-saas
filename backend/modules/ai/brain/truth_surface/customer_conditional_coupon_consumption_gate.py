"""
customer_conditional_coupon_consumption_gate.py
───────────────────────────────────────────────
Fail-closed gate for conditional-coupon compose facts consumption.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .contract import TrustedContextSnapshot
from .customer_conditional_coupon_compose_projection import (
    CustomerConditionalCouponComposeProjectionError,
    project_customer_conditional_coupon_compose_facts,
)
from .customer_conditional_coupon_compose_canary_gate import (
    compose_canary_gate_telemetry_metadata,
    evaluate_customer_conditional_coupon_compose_canary,
)
from .flags import is_customer_conditional_coupon_compose_enabled


def maybe_customer_conditional_coupon_compose_facts(
    *,
    message: str,
    snapshot: Optional[TrustedContextSnapshot],
    tenant_id: Optional[int] = None,
    customer_phone: str = "",
    ai_settings: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Return compose-safe conditional-coupon facts when all activation checks pass.

    Fail-closed: returns ``None`` on any gate failure (no customer behavior change).
    """
    canary = evaluate_customer_conditional_coupon_compose_canary(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        message=message,
        ai_settings=ai_settings,
        require_relevance=True,
    )
    if not canary.allowed:
        return None
    if snapshot is None:
        return None
    try:
        return project_customer_conditional_coupon_compose_facts(
            snapshot=snapshot,
            expected_tenant_id=tenant_id,
        )
    except CustomerConditionalCouponComposeProjectionError:
        return None


def safe_customer_conditional_coupon_compose_canary_trace_metadata(
    decision: Any,
) -> Dict[str, Any]:
    """Safe compose-canary trace metadata for logs."""
    if hasattr(decision, "allowed") and hasattr(decision, "reason"):
        return compose_canary_gate_telemetry_metadata(decision)
    return {
        "conditional_coupon_compose_canary_allowed": False,
        "conditional_coupon_compose_canary_reason": "unknown",
        "conditional_coupon_compose_master_enabled": is_customer_conditional_coupon_compose_enabled(),
        "conditional_coupon_compose_relevance_required": True,
        "conditional_coupon_compose_relevance_satisfied": False,
    }


def safe_customer_conditional_coupon_consumption_trace_metadata(
    result_or_error: Any,
) -> Dict[str, Any]:
    """Safe trace metadata for logs — no exception text, no raw facts."""
    if isinstance(result_or_error, dict):
        return {
            "status": "ok",
            "surface": str(result_or_error.get("surface") or ""),
            "min_orders_condition_state": str(
                result_or_error.get("min_orders_condition_state") or ""
            ),
            "conditional_coupon_evaluation_state": str(
                result_or_error.get("conditional_coupon_evaluation_state") or ""
            ),
            "facts_snapshot_id": str(result_or_error.get("facts_snapshot_id") or ""),
        }
    if isinstance(result_or_error, CustomerConditionalCouponComposeProjectionError):
        return {
            "status": "error",
            "stage": "customer_conditional_coupon_compose_projection",
            "error_class": type(result_or_error).__name__,
        }
    if isinstance(result_or_error, Exception):
        return {
            "status": "error",
            "stage": "gate",
            "error_class": type(result_or_error).__name__,
        }
    return {
        "status": "error",
        "stage": "gate",
        "error_class": "Unknown",
    }


__all__ = [
    "maybe_customer_conditional_coupon_compose_facts",
    "safe_customer_conditional_coupon_compose_canary_trace_metadata",
    "safe_customer_conditional_coupon_consumption_trace_metadata",
]
