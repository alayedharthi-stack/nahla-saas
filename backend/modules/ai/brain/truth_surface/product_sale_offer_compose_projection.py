"""
product_sale_offer_compose_projection.py
──────────────────────────────────────────
Pure compose-safe projections for catalog product sale offers.

Layer B only — no trace IDs, no raw metadata, no telemetry fields.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional

from .contract import TrustedContextSnapshot, TrustedDomain

SCHEMA_VERSION = "1"
SURFACE_PRODUCT_SALE = "product_sale_offer_answer"
SURFACE_GENERAL_DISCOVERY = "general_offer_discovery_answer"

_AVAILABILITY_VALUES: FrozenSet[str] = frozenset({
    "active_sale_present",
    "none_verified",
    "requires_product_context",
    "unavailable",
})

_PRODUCT_SALE_KEYS: FrozenSet[str] = frozenset({
    "schema_version",
    "surface",
    "bundle_namespace",
    "question_kind",
    "product_sale_availability",
    "verified_on_sale_product_count",
    "sample_products",
    "target_product",
    "allow_price_mention",
    "facts_snapshot_id",
})

_DISCOVERY_KEYS: FrozenSet[str] = frozenset({
    "schema_version",
    "surface",
    "question_route",
    "product_sale_offer_facts",
    "trusted_coupon_offer_facts",
    "forbidden_claims",
    "facts_snapshot_id",
})

_COMPOSE_BLOCKED_AVAILABILITY: FrozenSet[str] = frozenset({"unavailable"})


class ProductSaleOfferProjectionError(ValueError):
    """Schema validation failure for product sale compose projection."""


def _record_from_snapshot(snapshot: TrustedContextSnapshot) -> Dict[str, Any]:
    for fact in snapshot.facts_for_domain(TrustedDomain.CATALOG):
        if fact.key == "catalog:product_sale_offer":
            value = fact.value
            if isinstance(value, dict):
                return dict(value)
    return {}


def _compose_sample_rows(
    sample: Any,
    *,
    allow_price_mention: bool,
) -> List[Dict[str, str]]:
    if not allow_price_mention or not isinstance(sample, list):
        return []
    rows: List[Dict[str, str]] = []
    for row in sample[:5]:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "title": str(row.get("title") or ""),
                "sale_price": str(row.get("sale_price") or ""),
                "regular_price": str(row.get("regular_price") or ""),
            }
        )
    return rows


def _compose_target_product(
    target: Any,
    *,
    allow_price_mention: bool,
) -> Optional[Dict[str, Any]]:
    if not isinstance(target, dict):
        return None
    is_on_sale = bool(target.get("is_on_sale"))
    composed: Dict[str, Any] = {
        "title": str(target.get("title") or ""),
        "is_on_sale": is_on_sale,
    }
    if allow_price_mention and is_on_sale:
        composed["sale_price"] = str(target.get("sale_price") or "")
        composed["regular_price"] = str(target.get("regular_price") or "")
    return composed


def project_product_sale_offer_compose_facts(
    *,
    snapshot: TrustedContextSnapshot,
) -> Dict[str, Any]:
    record = _record_from_snapshot(snapshot)
    if not record:
        raise ProductSaleOfferProjectionError("missing_product_sale_record")

    question_kind = str(record.get("question_kind") or "store_wide")
    availability = str(record.get("product_sale_availability") or "unavailable")
    if availability not in _AVAILABILITY_VALUES:
        raise ProductSaleOfferProjectionError("invalid_availability")
    if availability in _COMPOSE_BLOCKED_AVAILABILITY:
        raise ProductSaleOfferProjectionError("product_sale_unavailable")

    allow_price_mention = bool(record.get("allow_price_mention"))
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "surface": SURFACE_PRODUCT_SALE,
        "bundle_namespace": "product_sale_offer",
        "question_kind": question_kind,
        "product_sale_availability": availability,
        "allow_price_mention": allow_price_mention,
        "facts_snapshot_id": snapshot.ensure_snapshot_id(),
    }
    if "verified_on_sale_product_count" in record:
        payload["verified_on_sale_product_count"] = int(
            record.get("verified_on_sale_product_count") or 0
        )
    if question_kind == "store_wide":
        if allow_price_mention and availability == "active_sale_present":
            sample_rows = _compose_sample_rows(
                record.get("sample_products"),
                allow_price_mention=allow_price_mention,
            )
            if sample_rows:
                payload["sample_products"] = sample_rows
    else:
        target = _compose_target_product(
            record.get("target_product"),
            allow_price_mention=allow_price_mention,
        )
        if target is not None:
            payload["target_product"] = target

    extra = set(payload.keys()) - _PRODUCT_SALE_KEYS
    if extra:
        raise ProductSaleOfferProjectionError(f"unknown_fields:{','.join(sorted(extra))}")
    return payload


def _product_bundle_valid_for_discovery(bundle: Optional[Dict[str, Any]]) -> bool:
    if not bundle:
        return False
    availability = str(bundle.get("product_sale_availability") or "")
    return availability not in _COMPOSE_BLOCKED_AVAILABILITY


def project_general_offer_discovery_compose_facts(
    *,
    snapshot: TrustedContextSnapshot,
    product_sale_facts: Optional[Dict[str, Any]] = None,
    trusted_coupon_offer_facts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Single compose surface for general offer discovery with namespaced bundles."""
    product_bundle = product_sale_facts
    if product_bundle is None:
        try:
            product_bundle = project_product_sale_offer_compose_facts(snapshot=snapshot)
        except ProductSaleOfferProjectionError:
            product_bundle = None

    if not _product_bundle_valid_for_discovery(product_bundle):
        product_bundle = None

    coupon_bundle = dict(trusted_coupon_offer_facts or {}) or None

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "surface": SURFACE_GENERAL_DISCOVERY,
        "question_route": "general_offer_discovery",
        "product_sale_offer_facts": product_bundle,
        "trusted_coupon_offer_facts": coupon_bundle,
        "forbidden_claims": [
            "invent_coupon_eligibility_from_catalog_sale",
            "invent_catalog_sale_from_promotion_eligibility",
            "merge_sources_into_deterministic_prose",
        ],
        "facts_snapshot_id": snapshot.ensure_snapshot_id(),
    }
    extra = set(payload.keys()) - _DISCOVERY_KEYS
    if extra:
        raise ProductSaleOfferProjectionError(f"unknown_fields:{','.join(sorted(extra))}")
    if not product_bundle and not coupon_bundle:
        raise ProductSaleOfferProjectionError("no_verified_offer_facts")
    return payload


def explain_product_sale_bundle_absence(snapshot: TrustedContextSnapshot) -> str:
    """Trace-only reason when product sale bundle is absent from general discovery."""
    record = _record_from_snapshot(snapshot)
    if not record:
        return "missing_product_sale_record"
    availability = str(record.get("product_sale_availability") or "unavailable")
    if availability in _COMPOSE_BLOCKED_AVAILABILITY:
        return "product_sale_unavailable"
    if availability == "requires_product_context":
        return "product_sale_requires_product_context"
    return "product_sale_projection_failed"


__all__ = [
    "ProductSaleOfferProjectionError",
    "SURFACE_GENERAL_DISCOVERY",
    "SURFACE_PRODUCT_SALE",
    "explain_product_sale_bundle_absence",
    "project_general_offer_discovery_compose_facts",
    "project_product_sale_offer_compose_facts",
]
