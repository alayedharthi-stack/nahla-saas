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
from .customer_conditional_coupon_loader import should_load_customer_conditional_coupon_facts
from .flags import is_customer_conditional_coupon_compose_enabled


def maybe_customer_conditional_coupon_compose_facts(
    *,
    message: str,
    snapshot: Optional[TrustedContextSnapshot],
    tenant_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Return compose-safe conditional-coupon facts when all activation checks pass.

    Fail-closed: returns ``None`` on any gate failure (no customer behavior change).
    """
    if not is_customer_conditional_coupon_compose_enabled():
        return None
    if snapshot is None:
        return None
    if not should_load_customer_conditional_coupon_facts(message=message):
        return None
    try:
        return project_customer_conditional_coupon_compose_facts(
            snapshot=snapshot,
            expected_tenant_id=tenant_id,
        )
    except CustomerConditionalCouponComposeProjectionError:
        return None


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
    "safe_customer_conditional_coupon_consumption_trace_metadata",
]
