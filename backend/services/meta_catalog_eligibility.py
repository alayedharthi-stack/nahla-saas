"""
services/meta_catalog_eligibility.py
────────────────────────────────────
Read-only Meta Catalog variant eligibility report.

No Graph calls, no DB writes, no push decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services.meta_catalog_export import preview_meta_variant_payload


@dataclass
class MetaCatalogEligibilityItem:
    product_id: int
    product_title: str
    variant_id: int
    salla_variant_id: Optional[str]
    retailer_id: Optional[str]
    status: str
    skip_reason: Optional[str]
    warnings: List[str] = field(default_factory=list)
    fatal: bool = False
    payload: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "product_id": self.product_id,
            "product_title": self.product_title,
            "variant_id": self.variant_id,
            "salla_variant_id": self.salla_variant_id,
            "retailer_id": self.retailer_id,
            "status": self.status,
            "skip_reason": self.skip_reason,
            "warnings": list(self.warnings),
            "fatal": self.fatal,
            "payload": self.payload,
        }


@dataclass
class MetaCatalogEligibilityReport:
    tenant_id: int
    dry_run: bool = True
    items: List[MetaCatalogEligibilityItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        counts = {
            "total_variants": len(self.items),
            "eligible": 0,
            "fatal": 0,
            "skipped_default_variant": 0,
            "out_of_stock": 0,
            "raw_option_label": 0,
        }
        for item in self.items:
            if item.status == "skipped":
                if item.skip_reason == "skipped_default_variant":
                    counts["skipped_default_variant"] += 1
            elif item.status == "fatal":
                counts["fatal"] += 1
            elif item.status in ("eligible", "eligible_with_warnings"):
                counts["eligible"] += 1
            if "out_of_stock" in item.warnings:
                counts["out_of_stock"] += 1
            if "raw_option_label" in item.warnings:
                counts["raw_option_label"] += 1
        return {
            "tenant_id": self.tenant_id,
            "dry_run": self.dry_run,
            "counts": counts,
            "items": [item.to_dict() for item in self.items],
        }


def product_has_real_variants(variants: Sequence[Any]) -> bool:
    """True when the parent has real multi-SKU variants (not simple default-only)."""
    non_default = [v for v in variants if not bool(getattr(v, "is_default", False))]
    if len(non_default) > 1:
        return True
    return any(str(getattr(v, "salla_variant_id", "") or "").strip() for v in variants)


def skip_reason_for_variant(parent: Any, variant: Any, *, has_real_variants: bool) -> Optional[str]:
    """Return a skip reason, or None when the variant should be evaluated."""
    if not has_real_variants:
        return None
    if bool(getattr(variant, "is_default", False)):
        return "skipped_default_variant"
    parent_rid = str(getattr(parent, "meta_retailer_id", "") or "").strip()
    variant_rid = str(getattr(variant, "retailer_id", "") or "").strip()
    if parent_rid and variant_rid and variant_rid == parent_rid:
        return "skipped_default_variant"
    return None


def has_raw_option_label(parent: Any, variant: Any, preview: Dict[str, Any]) -> bool:
    """True when option ids exist but the display name fell back to parent title."""
    payload = preview.get("payload") or {}
    parent_title = str(getattr(parent, "title", "") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not parent_title or name != parent_title:
        return False
    options = getattr(variant, "options", None) or {}
    if not isinstance(options, dict):
        return False
    value_ids = options.get("option_value_ids")
    if isinstance(value_ids, list) and value_ids:
        return True
    return False


def classify_variant_eligibility(parent: Any, variant: Any, *, has_real_variants: bool) -> MetaCatalogEligibilityItem:
    """Evaluate one variant without side effects."""
    skip = skip_reason_for_variant(parent, variant, has_real_variants=has_real_variants)
    base = MetaCatalogEligibilityItem(
        product_id=int(getattr(parent, "id", 0) or 0),
        product_title=str(getattr(parent, "title", "") or "")[:80],
        variant_id=int(getattr(variant, "id", 0) or 0),
        salla_variant_id=str(getattr(variant, "salla_variant_id", "") or "").strip() or None,
        retailer_id=str(getattr(variant, "retailer_id", "") or "").strip() or None,
        status="skipped",
        skip_reason=skip,
    )
    if skip:
        return base

    preview = preview_meta_variant_payload(parent, variant)
    warnings = list(preview.get("warnings") or [])
    if has_raw_option_label(parent, variant, preview):
        if "raw_option_label" not in warnings:
            warnings.append("raw_option_label")

    base.warnings = warnings
    base.fatal = bool(preview.get("fatal"))
    base.payload = dict(preview.get("payload") or {})

    if base.fatal:
        base.status = "fatal"
    elif warnings:
        base.status = "eligible_with_warnings"
    else:
        base.status = "eligible"
    return base


def _load_parents(db: Any, tenant_id: int, product_ids: Sequence[int]) -> Dict[int, Any]:
    from models import Product  # noqa: PLC0415

    if not product_ids:
        return {}
    rows = (
        db.query(Product)
        .filter(Product.tenant_id == int(tenant_id))
        .filter(Product.id.in_(list(product_ids)))
        .all()
    )
    return {int(row.id): row for row in rows}


def build_meta_catalog_eligibility_report(
    db: Any,
    tenant_id: int,
    *,
    product_id: Optional[int] = None,
    limit: Optional[int] = None,
    include_out_of_stock: bool = True,
) -> MetaCatalogEligibilityReport:
    """Scan tenant variants and classify Meta push eligibility (read-only)."""
    from models import ProductVariant  # noqa: PLC0415

    report = MetaCatalogEligibilityReport(tenant_id=int(tenant_id), dry_run=True)

    query = (
        db.query(ProductVariant)
        .filter(ProductVariant.tenant_id == int(tenant_id))
        .order_by(ProductVariant.product_id.asc(), ProductVariant.id.asc())
    )
    if product_id is not None:
        query = query.filter(ProductVariant.product_id == int(product_id))
    if limit is not None:
        query = query.limit(int(limit))

    variants = query.all()
    if not variants:
        return report

    variants_by_product: Dict[int, List[Any]] = {}
    for variant in variants:
        pid = int(variant.product_id)
        variants_by_product.setdefault(pid, []).append(variant)

    all_product_ids = sorted(variants_by_product.keys())
    parents = _load_parents(db, tenant_id, all_product_ids)

    real_variants_by_product = {
        pid: product_has_real_variants(rows)
        for pid, rows in variants_by_product.items()
    }

    for variant in variants:
        pid = int(variant.product_id)
        parent = parents.get(pid)
        if parent is None:
            continue
        item = classify_variant_eligibility(
            parent,
            variant,
            has_real_variants=real_variants_by_product.get(pid, False),
        )
        if (
            not include_out_of_stock
            and item.status != "skipped"
            and "out_of_stock" in item.warnings
        ):
            continue
        report.items.append(item)

    return report


__all__ = [
    "MetaCatalogEligibilityItem",
    "MetaCatalogEligibilityReport",
    "build_meta_catalog_eligibility_report",
    "classify_variant_eligibility",
    "has_raw_option_label",
    "product_has_real_variants",
    "skip_reason_for_variant",
]
