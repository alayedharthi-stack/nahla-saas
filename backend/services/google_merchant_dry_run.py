"""services/google_merchant_dry_run.py
─────────────────────────────────────
Read-only, tenant-scoped Google Merchant API v1 dry-run preview.

Off by default (``GOOGLE_MERCHANT_DRY_RUN``). When enabled it reads one
tenant's ``Product`` / ``ProductVariant`` rows, runs the pure mapper in
:mod:`services.google_merchant_products_v1` and returns the exact
``ProductInput`` payloads, exclusions and issues.

It never writes: no add / flush / commit, and every read runs under
``Session.no_autoflush`` so a caller's pending state is never flushed by
the preview. There is no router, scheduler, credential or transport
here, and nothing in the result is a Google approval.
"""
from __future__ import annotations

import os
from collections import Counter, defaultdict
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from services.google_merchant_products_v1 import (
    CrossTenantRowError,
    GoogleMerchantTarget,
    InvalidTargetError,
    OFFER_STATUS_BLOCKED,
    OFFER_STATUS_PASSES_OFFLINE_CHECKS,
    map_product,
)

DRY_RUN_FLAG_ENV = "GOOGLE_MERCHANT_DRY_RUN"
_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})

GOOGLE_REVIEW_NOT_PERFORMED = "not_performed_offline"

# Source data the current Salla ingestion drops or blurs before it reaches
# local rows. Reported only; shared ingestion is not changed by this slice.
NORMALIZATION_DATA_GAPS: tuple = (
    {"code": "gtin_mpn_barcode_not_ingested",
     "where": "store_adapters/salla_adapter.py:_normalize_variant; services/store_sync.py:_normalise_product"},
    {"code": "variant_landing_url_not_ingested",
     "where": "store_adapters/salla_adapter.py:_normalize_variant"},
    {"code": "brand_stored_raw",
     "where": "services/store_sync.py:_normalise_product"},
    {"code": "product_price_may_be_effective_price",
     "where": "services/store_sync.py:_normalise_product"},
    {"code": "variant_prices_coerced_through_float",
     "where": "store_adapters/salla_adapter.py:_coerce_salla_price_amount"},
    {"code": "currency_defaulted_by_sync",
     "where": "services/store_sync.py:_normalise_product"},
    {"code": "vanished_variants_soft_pruned_to_out_of_stock",
     "where": "services/store_sync.py variant upsert soft-prune"},
    {"code": "unlimited_quantity_folded_into_in_stock",
     "where": "services/store_sync.py:_source_in_stock"},
    {"code": "image_dimensions_not_stored",
     "where": "core/catalog_image.py"},
)


def google_merchant_dry_run_enabled() -> bool:
    raw = os.getenv(DRY_RUN_FLAG_ENV, "")
    return (raw or "").strip().lower() in _ENABLED_VALUES


def _models():
    try:
        from models import Product, ProductVariant  # noqa: PLC0415
    except ImportError:  # noqa: BLE001
        from database.models import Product, ProductVariant  # type: ignore  # noqa: PLC0415
    return Product, ProductVariant


def build_dry_run_preview(db: Session, target: GoogleMerchantTarget) -> Dict[str, Any]:
    """Return the offline Merchant API v1 preview for ``target.tenant_id``.

    Returns ``{"enabled": False, ...}`` without touching *db* when the
    flag is off. Raises :class:`CrossTenantRowError` if a row outside the
    tenant ever reaches the mapper.
    """
    if not isinstance(target, GoogleMerchantTarget):
        raise InvalidTargetError("target_invalid")
    if not google_merchant_dry_run_enabled():
        return {"enabled": False, "tenant_id": target.tenant_id}

    Product, ProductVariant = _models()
    offers: List[Dict[str, Any]] = []
    exclusions: List[Dict[str, Any]] = []
    orphan_variant_ids: List[Any] = []

    with db.no_autoflush:
        products = (
            db.query(Product)
            .filter(Product.tenant_id == target.tenant_id)
            .order_by(Product.id)
            .all()
        )
        variants = (
            db.query(ProductVariant)
            .filter(ProductVariant.tenant_id == target.tenant_id)
            .order_by(ProductVariant.id)
            .all()
        )
        by_product: Dict[Any, List[Any]] = defaultdict(list)
        for row in variants:
            by_product[row.product_id].append(row)
        known_ids = {p.id for p in products}
        orphan_variant_ids = [row.id for row in variants if row.product_id not in known_ids]

        for product in products:
            mapped = map_product(target, product, by_product.get(product.id, []))
            offers.extend(o.to_dict() for o in mapped.offers)
            exclusions.extend(e.to_dict() for e in mapped.exclusions)

    issue_counts: Counter = Counter()
    for offer in offers:
        for issue in offer["issues"]:
            issue_counts[f"{issue['severity']}:{issue['code']}"] += 1
    status_counts = Counter(o["status"] for o in offers)

    return {
        "enabled": True,
        "tenant_id": target.tenant_id,
        "content_language": target.content_language,
        "feed_label": target.feed_label,
        "google_review": GOOGLE_REVIEW_NOT_PERFORMED,
        "offers": offers,
        "exclusions": exclusions,
        "orphan_variant_ids": orphan_variant_ids,
        "summary": {
            "offers_total": len(offers),
            "offers_blocked": status_counts.get(OFFER_STATUS_BLOCKED, 0),
            "offers_passing_offline_checks": status_counts.get(OFFER_STATUS_PASSES_OFFLINE_CHECKS, 0),
            "exclusions_total": len(exclusions),
            "issue_counts": dict(sorted(issue_counts.items())),
        },
        "data_gaps": [dict(gap) for gap in NORMALIZATION_DATA_GAPS],
    }


__all__ = [
    "CrossTenantRowError",
    "DRY_RUN_FLAG_ENV",
    "GOOGLE_REVIEW_NOT_PERFORMED",
    "NORMALIZATION_DATA_GAPS",
    "build_dry_run_preview",
    "google_merchant_dry_run_enabled",
]
