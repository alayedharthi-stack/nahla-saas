"""
coupon_offer_consumption_gate.py
────────────────────────────────
Fail-closed gate for trusted coupon/offer compose facts consumption.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .contract import TrustedContextSnapshot
from .coupon_offer_compose_projection import (
    CouponOfferComposeProjectionError,
    project_trusted_coupon_offer_compose_facts,
)
from .coupon_offer_loader import should_load_coupon_promotion_facts
from .flags import is_trusted_context_coupon_offer_compose_enabled


def maybe_trusted_coupon_offer_compose_facts(
    *,
    message: str,
    snapshot: Optional[TrustedContextSnapshot],
) -> Optional[Dict[str, Any]]:
    """
    Return compose-safe coupon/offer facts when all activation checks pass.

    Fail-closed: returns ``None`` on any gate failure (no customer behavior change).
    """
    if not is_trusted_context_coupon_offer_compose_enabled():
        return None
    if snapshot is None:
        return None
    if not should_load_coupon_promotion_facts(message=message):
        return None
    try:
        return project_trusted_coupon_offer_compose_facts(
            snapshot=snapshot,
            message=message,
        )
    except CouponOfferComposeProjectionError:
        return None


def safe_coupon_offer_consumption_trace_metadata(result_or_error: Any) -> Dict[str, Any]:
    """
    Safe trace metadata for logs — no exception text, no raw facts.
    """
    if isinstance(result_or_error, dict):
        return {
            "status": "ok",
            "surface": str(result_or_error.get("surface") or ""),
            "question_kind": str(result_or_error.get("question_kind") or ""),
            "facts_snapshot_id": str(result_or_error.get("facts_snapshot_id") or ""),
        }
    if isinstance(result_or_error, CouponOfferComposeProjectionError):
        return {
            "status": "error",
            "stage": "coupon_offer_compose_projection",
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
    "maybe_trusted_coupon_offer_compose_facts",
    "safe_coupon_offer_consumption_trace_metadata",
]
