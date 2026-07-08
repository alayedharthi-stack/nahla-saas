"""
services/meta_catalog_readiness.py
──────────────────────────────────
Read-only Meta Catalog readiness report (variant-level).

Wraps ``meta_catalog_eligibility`` with a unified ready/warn/blocked/skipped
taxonomy and optional Meta Graph GET comparison. No POST, no DB writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from services.meta_catalog_eligibility import (
    MetaCatalogEligibilityItem,
    build_meta_catalog_eligibility_report,
    product_has_real_variants,
)
from services.meta_catalog_export import (
    _looks_like_raw_option_ids,
    build_meta_variant_display_name,
    resolve_meta_item_group_id,
)
from services.meta_catalog_reconcile import fetch_meta_catalog_live_products

PUSHABLE_STATUSES = frozenset({"ready", "warn"})


@dataclass
class MetaCatalogReadinessItem:
    product_id: int
    title: str
    variant_id: int
    salla_variant_id: Optional[str]
    retailer_id: Optional[str]
    item_group_id: Optional[str]
    option_summary: Optional[str]
    generated_name: Optional[str]
    price: Optional[int]
    currency: Optional[str]
    availability: Optional[str]
    image_url_present: bool
    url_present: bool
    status: str
    reasons: List[str] = field(default_factory=list)
    payload_preview: Optional[Dict[str, Any]] = None
    in_meta_live: Optional[bool] = None
    meta_product_id: Optional[str] = None
    live_name: Optional[str] = None
    local_name: Optional[str] = None
    action_needed: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "product_id": self.product_id,
            "title": self.title,
            "variant_id": self.variant_id,
            "salla_variant_id": self.salla_variant_id,
            "retailer_id": self.retailer_id,
            "item_group_id": self.item_group_id,
            "option_summary": self.option_summary,
            "generated_name": self.generated_name,
            "price": self.price,
            "currency": self.currency,
            "availability": self.availability,
            "image_url_present": self.image_url_present,
            "url_present": self.url_present,
            "status": self.status,
            "reasons": list(self.reasons),
        }
        if self.payload_preview is not None:
            out["payload_preview"] = dict(self.payload_preview)
        if self.in_meta_live is not None:
            out["in_meta_live"] = self.in_meta_live
        if self.meta_product_id is not None:
            out["meta_product_id"] = self.meta_product_id
        if self.live_name is not None:
            out["live_name"] = self.live_name
        if self.local_name is not None:
            out["local_name"] = self.local_name
        if self.action_needed is not None:
            out["action_needed"] = self.action_needed
        return out


@dataclass
class MetaCatalogReadinessReport:
    tenant_id: int
    dry_run: bool = True
    counts: Dict[str, int] = field(default_factory=dict)
    items: List[MetaCatalogReadinessItem] = field(default_factory=list)
    meta_fetch: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "dry_run": self.dry_run,
            "counts": dict(self.counts),
            "items": [item.to_dict() for item in self.items],
            "error": self.error,
        }
        if self.meta_fetch is not None:
            payload["meta_fetch"] = self.meta_fetch
        return payload

    def summary_line(self) -> str:
        c = self.counts
        return (
            f"readiness tenant={self.tenant_id} "
            f"products={c.get('products_total', 0)} "
            f"candidates={c.get('candidate_items_total', 0)} "
            f"ready={c.get('ready_items', 0)} "
            f"warn={c.get('warning_items', 0)} "
            f"blocked={c.get('blocked_items', 0)} "
            f"skipped={c.get('skipped_items', 0)}"
        )


def _human_option_summary(variant: Any) -> Optional[str]:
    summary = str(getattr(variant, "option_summary", "") or "").strip()
    if not summary or _looks_like_raw_option_ids(summary):
        return None
    return summary


def _skip_reason_labels(
    skip_reason: Optional[str],
    *,
    is_default: bool,
) -> List[str]:
    if not skip_reason:
        return []
    if skip_reason == "skipped_default_variant":
        if is_default:
            return ["legacy_default_variant"]
        return ["skipped_default_variant"]
    return [skip_reason]


def _has_missing_option_summary(
    parent: Any,
    variant: Any,
    *,
    has_real_variants: bool,
    generated_name: Optional[str],
) -> bool:
    if not has_real_variants:
        return False
    parent_title = str(getattr(parent, "title", "") or "").strip()
    name = str(generated_name or "").strip()
    if not parent_title or name != parent_title:
        return False
    return _human_option_summary(variant) is None


def _normalize_reasons(warnings: List[str]) -> List[str]:
    reasons: List[str] = []
    for code in warnings:
        if code == "raw_option_label":
            reasons.append("raw_option_summary")
        elif code == "missing_price":
            reasons.append("missing_price")
        else:
            reasons.append(code)
    return reasons


def classify_readiness_status(
    eligibility: MetaCatalogEligibilityItem,
    *,
    parent: Any,
    variant: Any,
    has_real_variants: bool,
) -> tuple[str, List[str]]:
    """Map eligibility row to readiness status + structured reasons."""
    if eligibility.status == "skipped":
        is_default = bool(getattr(variant, "is_default", False))
        return "skipped", _skip_reason_labels(eligibility.skip_reason, is_default=is_default)

    payload = dict(eligibility.payload or {})
    generated_name = str(payload.get("name") or "").strip() or None
    reasons = _normalize_reasons(list(eligibility.warnings or []))

    if eligibility.fatal or eligibility.status == "fatal":
        return "blocked", reasons

    if "raw_option_summary" in reasons:
        if _has_missing_option_summary(
            parent, variant, has_real_variants=has_real_variants, generated_name=generated_name,
        ) and "missing_option_summary" not in reasons:
            reasons.append("missing_option_summary")
        return "warn", reasons

    if _has_missing_option_summary(
        parent, variant, has_real_variants=has_real_variants, generated_name=generated_name,
    ):
        if "missing_option_summary" not in reasons:
            reasons.append("missing_option_summary")
        return "warn", reasons

    if reasons:
        return "warn", reasons

    return "ready", reasons


def _payload_fields(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    payload = payload or {}
    return {
        "item_group_id": payload.get("item_group_id"),
        "generated_name": payload.get("name"),
        "price": payload.get("price"),
        "currency": payload.get("currency"),
        "availability": payload.get("availability"),
        "image_url_present": bool(payload.get("image_url")),
        "url_present": bool(payload.get("url")),
    }


def _live_payload_matches(local_payload: Dict[str, Any], live_row: Dict[str, Any]) -> bool:
    local_name = str(local_payload.get("name") or "").strip()
    live_name = str(live_row.get("name") or "").strip()
    if local_name != live_name:
        return False
    local_avail = str(local_payload.get("availability") or "").strip().lower()
    live_avail = str(live_row.get("availability") or "").strip().lower()
    if live_avail and local_avail and live_avail != local_avail:
        return False
    return True


def resolve_action_needed(
    status: str,
    local_payload: Optional[Dict[str, Any]],
    live_row: Optional[Dict[str, Any]],
) -> str:
    if status in ("blocked", "skipped"):
        return "skip"
    if not local_payload:
        return "skip"
    if not live_row:
        return "create"
    if _live_payload_matches(local_payload, live_row):
        return "noop"
    return "update"


def eligibility_to_readiness_item(
    eligibility: MetaCatalogEligibilityItem,
    *,
    parent: Any,
    variant: Any,
    has_real_variants: bool,
    live_row: Optional[Dict[str, Any]] = None,
    include_meta: bool = False,
) -> MetaCatalogReadinessItem:
    status, reasons = classify_readiness_status(
        eligibility,
        parent=parent,
        variant=variant,
        has_real_variants=has_real_variants,
    )
    payload = dict(eligibility.payload or {}) if eligibility.payload else {}
    if not payload and status != "skipped":
        payload = {
            "retailer_id": eligibility.retailer_id,
            "name": build_meta_variant_display_name(parent, variant),
        }
        group_id = resolve_meta_item_group_id(parent, variant)
        if group_id:
            payload["item_group_id"] = group_id

    fields = _payload_fields(payload if payload else None)
    option_summary = _human_option_summary(variant)
    if option_summary is None:
        raw = str(getattr(variant, "option_summary", "") or "").strip()
        option_summary = raw or None

    item = MetaCatalogReadinessItem(
        product_id=eligibility.product_id,
        title=eligibility.product_title,
        variant_id=eligibility.variant_id,
        salla_variant_id=eligibility.salla_variant_id,
        retailer_id=eligibility.retailer_id,
        item_group_id=fields["item_group_id"],
        option_summary=option_summary,
        generated_name=fields["generated_name"],
        price=fields["price"],
        currency=fields["currency"],
        availability=fields["availability"],
        image_url_present=fields["image_url_present"],
        url_present=fields["url_present"],
        status=status,
        reasons=reasons,
        payload_preview=dict(payload) if status in PUSHABLE_STATUSES and payload else None,
        local_name=fields["generated_name"],
    )

    if include_meta:
        rid = str(eligibility.retailer_id or "").strip()
        live = live_row if live_row is not None else None
        item.in_meta_live = bool(live) if rid else False
        if live:
            item.meta_product_id = live.get("meta_product_id")
            item.live_name = live.get("name")
        item.action_needed = resolve_action_needed(status, payload or None, live)

    return item


def _compute_counts(
    all_items: List[MetaCatalogReadinessItem],
    *,
    product_ids: Set[int],
    simple_product_ids: Set[int],
    variant_product_ids: Set[int],
) -> Dict[str, int]:
    counts = {
        "products_total": len(product_ids),
        "simple_products": len(simple_product_ids),
        "variant_products": len(variant_product_ids),
        "candidate_items_total": len(all_items),
        "ready_items": 0,
        "blocked_items": 0,
        "warning_items": 0,
        "skipped_items": 0,
        "out_of_stock_items": 0,
        "skipped_legacy_default": 0,
        "raw_option_label_items": 0,
        "fatal_items": 0,
        "already_live_in_meta": 0,
        "missing_in_meta": 0,
        "needs_update": 0,
        "needs_create": 0,
    }
    for item in all_items:
        if item.status == "ready":
            counts["ready_items"] += 1
        elif item.status == "warn":
            counts["warning_items"] += 1
        elif item.status == "blocked":
            counts["blocked_items"] += 1
            counts["fatal_items"] += 1
        elif item.status == "skipped":
            counts["skipped_items"] += 1

        if "out_of_stock" in item.reasons:
            counts["out_of_stock_items"] += 1
        if "legacy_default_variant" in item.reasons:
            counts["skipped_legacy_default"] += 1
        if "raw_option_summary" in item.reasons:
            counts["raw_option_label_items"] += 1

        if item.in_meta_live is True:
            counts["already_live_in_meta"] += 1
        if item.action_needed == "create":
            counts["missing_in_meta"] += 1
            counts["needs_create"] += 1
        elif item.action_needed == "update":
            counts["needs_update"] += 1

    return counts


def build_meta_catalog_readiness_report(
    db: Any,
    tenant_id: int,
    *,
    product_id: Optional[int] = None,
    limit: Optional[int] = None,
    exclude_out_of_stock: bool = False,
    include_meta_live_read: bool = False,
    only_ready: bool = False,
    only_blocked: bool = False,
    client: Any = None,
) -> MetaCatalogReadinessReport:
    """Build a variant-level Meta Catalog readiness report (read-only)."""
    from models import Product, ProductVariant, WhatsAppConnection  # noqa: PLC0415

    report = MetaCatalogReadinessReport(tenant_id=int(tenant_id), dry_run=True)

    eligibility = build_meta_catalog_eligibility_report(
        db,
        tenant_id,
        product_id=product_id,
        limit=limit,
        include_out_of_stock=True,
    )

    variant_ids = [item.variant_id for item in eligibility.items]
    variants = (
        db.query(ProductVariant)
        .filter(ProductVariant.tenant_id == int(tenant_id))
        .filter(ProductVariant.id.in_(variant_ids))
        .all()
        if variant_ids
        else []
    )
    variant_by_id = {int(v.id): v for v in variants}

    product_ids = sorted({int(v.product_id) for v in variants})
    parents = (
        db.query(Product)
        .filter(Product.tenant_id == int(tenant_id))
        .filter(Product.id.in_(product_ids))
        .all()
        if product_ids
        else []
    )
    parent_by_id = {int(p.id): p for p in parents}

    variants_by_product: Dict[int, List[Any]] = {}
    for variant in variants:
        variants_by_product.setdefault(int(variant.product_id), []).append(variant)

    real_variants_by_product = {
        pid: product_has_real_variants(rows)
        for pid, rows in variants_by_product.items()
    }
    simple_product_ids = {pid for pid, real in real_variants_by_product.items() if not real}
    variant_product_ids = {pid for pid, real in real_variants_by_product.items() if real}

    live_by_retailer: Dict[str, Dict[str, Any]] = {}
    if include_meta_live_read:
        conn = (
            db.query(WhatsAppConnection)
            .filter(WhatsAppConnection.tenant_id == int(tenant_id))
            .first()
        )
        catalog_id = str(getattr(conn, "meta_catalog_id", "") or "").strip() if conn else ""
        if not catalog_id:
            report.error = "catalog_id_missing"
            return report
        live_by_retailer, meta_fetch = fetch_meta_catalog_live_products(
            conn, catalog_id, client=client,
        )
        report.meta_fetch = meta_fetch
        if meta_fetch.get("error"):
            report.error = str(meta_fetch["error"])
            return report

    all_items: List[MetaCatalogReadinessItem] = []
    for elig in eligibility.items:
        variant = variant_by_id.get(int(elig.variant_id))
        parent = parent_by_id.get(int(elig.product_id))
        if variant is None or parent is None:
            continue

        has_real = real_variants_by_product.get(int(elig.product_id), False)
        live_row = None
        if include_meta_live_read:
            rid = str(elig.retailer_id or "").strip()
            live_row = live_by_retailer.get(rid) if rid else None

        item = eligibility_to_readiness_item(
            elig,
            parent=parent,
            variant=variant,
            has_real_variants=has_real,
            live_row=live_row,
            include_meta=include_meta_live_read,
        )

        if exclude_out_of_stock and "out_of_stock" in item.reasons:
            continue
        if only_ready and item.status != "ready":
            continue
        if only_blocked and item.status != "blocked":
            continue

        all_items.append(item)

    report.items = all_items
    report.counts = _compute_counts(
        all_items,
        product_ids=set(parent_by_id.keys()),
        simple_product_ids=simple_product_ids,
        variant_product_ids=variant_product_ids,
    )
    return report


__all__ = [
    "MetaCatalogReadinessItem",
    "MetaCatalogReadinessReport",
    "build_meta_catalog_readiness_report",
    "classify_readiness_status",
    "eligibility_to_readiness_item",
    "resolve_action_needed",
]
