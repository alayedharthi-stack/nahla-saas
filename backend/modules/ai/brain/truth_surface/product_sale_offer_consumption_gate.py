"""
product_sale_offer_consumption_gate.py
──────────────────────────────────────
Fail-closed gates for product sale offer compose consumption.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .contract import TrustedContextSnapshot
from .flags import (
    is_general_offer_discovery_compose_enabled,
    is_product_sale_offer_compose_enabled,
)
from .product_sale_offer_compose_projection import (
    ProductSaleOfferProjectionError,
    explain_product_sale_bundle_absence,
    project_general_offer_discovery_compose_facts,
    project_product_sale_offer_compose_facts,
)
from .product_sale_offer_loader import (
    classify_product_sale_question_kind,
    should_load_product_sale_offer_facts,
)


def maybe_product_sale_offer_compose_facts(
    *,
    message: str,
    snapshot: Optional[TrustedContextSnapshot],
) -> Optional[Dict[str, Any]]:
    if not is_product_sale_offer_compose_enabled():
        return None
    if snapshot is None:
        return None
    if not should_load_product_sale_offer_facts(message=message):
        return None
    if classify_product_sale_question_kind(message) != "product_scoped":
        return None
    try:
        return project_product_sale_offer_compose_facts(snapshot=snapshot)
    except ProductSaleOfferProjectionError:
        return None


def maybe_general_offer_discovery_compose_facts(
    *,
    message: str,
    snapshot: Optional[TrustedContextSnapshot],
    trusted_coupon_offer_facts: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not is_general_offer_discovery_compose_enabled():
        return None
    if snapshot is None:
        return None
    if classify_product_sale_question_kind(message) != "store_wide":
        return None
    if not should_load_product_sale_offer_facts(message=message):
        coupon_only = bool(trusted_coupon_offer_facts)
        if not coupon_only:
            return None
    try:
        return project_general_offer_discovery_compose_facts(
            snapshot=snapshot,
            trusted_coupon_offer_facts=trusted_coupon_offer_facts,
        )
    except ProductSaleOfferProjectionError:
        if trusted_coupon_offer_facts:
            try:
                return project_general_offer_discovery_compose_facts(
                    snapshot=snapshot,
                    product_sale_facts=None,
                    trusted_coupon_offer_facts=trusted_coupon_offer_facts,
                )
            except ProductSaleOfferProjectionError:
                return None
        return None


def general_offer_discovery_bundle_trace(
    *,
    message: str,
    snapshot: Optional[TrustedContextSnapshot],
    trusted_coupon_offer_facts: Optional[Dict[str, Any]] = None,
    discovery_facts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Trace-only bundle presence/absence without raw facts or customer text."""
    trace: Dict[str, Any] = {
        "chosen_path": "general_offer_discovery_compose",
        "product_sale_bundle_present": False,
        "trusted_coupon_bundle_present": False,
    }
    if discovery_facts:
        trace["product_sale_bundle_present"] = bool(
            discovery_facts.get("product_sale_offer_facts")
        )
        trace["trusted_coupon_bundle_present"] = bool(
            discovery_facts.get("trusted_coupon_offer_facts")
        )
        return trace

    if snapshot is None:
        trace["product_sale_bundle_absence_reason"] = "missing_snapshot"
        trace["trusted_coupon_bundle_absence_reason"] = (
            "missing_coupon_facts" if not trusted_coupon_offer_facts else None
        )
        return trace

    if should_load_product_sale_offer_facts(message=message):
        try:
            project_product_sale_offer_compose_facts(snapshot=snapshot)
            trace["product_sale_bundle_present"] = True
        except ProductSaleOfferProjectionError:
            trace["product_sale_bundle_absence_reason"] = explain_product_sale_bundle_absence(
                snapshot
            )
    else:
        trace["product_sale_bundle_absence_reason"] = "product_sale_gate_not_triggered"

    if trusted_coupon_offer_facts:
        trace["trusted_coupon_bundle_present"] = True
    else:
        trace["trusted_coupon_bundle_absence_reason"] = "missing_coupon_facts"

    return trace


def safe_product_sale_consumption_trace_metadata(result_or_error: Any) -> Dict[str, Any]:
    if isinstance(result_or_error, dict):
        return {
            "status": "ok",
            "surface": str(result_or_error.get("surface") or ""),
            "question_kind": str(
                result_or_error.get("question_kind")
                or result_or_error.get("question_route")
                or ""
            ),
            "facts_snapshot_id": str(result_or_error.get("facts_snapshot_id") or ""),
        }
    if isinstance(result_or_error, ProductSaleOfferProjectionError):
        return {
            "status": "error",
            "stage": "product_sale_offer_compose_projection",
            "error_class": type(result_or_error).__name__,
        }
    if isinstance(result_or_error, Exception):
        return {
            "status": "error",
            "stage": "gate",
            "error_class": type(result_or_error).__name__,
        }
    return {"status": "error", "stage": "gate", "error_class": "Unknown"}


def safe_product_sale_loader_telemetry(obs: Dict[str, Any]) -> Dict[str, Any]:
    """Telemetry-safe projection of loader observability — no titles/prices."""
    if not isinstance(obs, dict):
        return {}
    out: Dict[str, Any] = {
        "product_sale_availability": str(obs.get("product_sale_availability") or ""),
        "question_kind": str(obs.get("question_kind") or ""),
        "loader_duration_ms": int(obs.get("loader_duration_ms") or 0),
    }
    if "verified_on_sale_product_count" in obs:
        out["verified_on_sale_product_count"] = int(obs.get("verified_on_sale_product_count") or 0)
    if obs.get("sample_product_ids"):
        out["sample_product_ids"] = [int(pid) for pid in obs.get("sample_product_ids") or []]
    if obs.get("loader_error_class"):
        out["loader_error_class"] = str(obs.get("loader_error_class"))
    return out


__all__ = [
    "general_offer_discovery_bundle_trace",
    "maybe_general_offer_discovery_compose_facts",
    "maybe_product_sale_offer_compose_facts",
    "safe_product_sale_consumption_trace_metadata",
    "safe_product_sale_loader_telemetry",
]
