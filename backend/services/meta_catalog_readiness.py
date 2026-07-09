"""
services/meta_catalog_readiness.py
──────────────────────────────────
Read-only Meta Catalog readiness report (variant-level).

Wraps ``meta_catalog_eligibility`` with a unified ready/warn/blocked/skipped
taxonomy and optional Meta Graph GET comparison. No POST, no DB writes.
"""
from __future__ import annotations

import re
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
IN_STOCK_AVAILABILITY = "in stock"

_NAME_QUALITY_REASONS = frozenset({
    "composite_option_summary",
    "orphan_option_value_ids",
    "meta_name_no_size",
    "color_size_slash_name",
})

_LIVE_META_REVIEW_REASONS = frozenset({
    "live_name_size_mismatch",
    "stale_meta_display_name",
})

_HUMAN_OPTION_KEYS = frozenset({
    "المقاس", "مقاس", "size", "حجم",
    "اللون", "لون", "color", "colour",
    "material", "خامة", "مادة", "الخامة",
})

_SIZE_LIKE_RE = re.compile(
    r"(\d+\s*-\s*[A-Za-zXSML]+|\b[XSML]{1,3}\b)",
    re.IGNORECASE,
)


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


def _variant_options_dict(variant: Any) -> Dict[str, Any]:
    opts = getattr(variant, "options", None) or {}
    return dict(opts) if isinstance(opts, dict) else {}


def _has_orphan_option_value_ids(variant: Any) -> bool:
    opts = _variant_options_dict(variant)
    value_ids = opts.get("option_value_ids")
    if not (isinstance(value_ids, list) and value_ids):
        return False
    human_keys = {
        str(k).lower() for k in opts
        if str(k).lower() != "option_value_ids"
    }
    known = {key.lower() for key in _HUMAN_OPTION_KEYS}
    return not bool(human_keys & known)


def _is_composite_option_summary(summary: str) -> bool:
    text = (summary or "").strip()
    if not text:
        return False
    return "/" in text


def _looks_like_size_token(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    return bool(_SIZE_LIKE_RE.search(text))


def _canonical_size_label(text: str) -> Optional[str]:
    """Normalize one apparel size token for conservative comparisons."""
    text = (text or "").strip()
    if not text:
        return None
    match = _SIZE_LIKE_RE.search(text)
    if not match:
        return None
    token = match.group(0).strip()
    token = re.sub(r"\s*-\s*", " - ", token)
    return token.upper()


def _local_size_label(payload: Dict[str, Any], variant: Any) -> Optional[str]:
    size = str(payload.get("size") or "").strip()
    if size:
        return _canonical_size_label(size) or size.upper()
    summary = str(getattr(variant, "option_summary", "") or "").strip()
    if not summary or _looks_like_raw_option_ids(summary):
        return None
    if "/" in summary:
        return None
    if not _looks_like_size_token(summary):
        return None
    return _canonical_size_label(summary)


def _live_size_tokens(live_name: str, *, parent_title: Optional[str] = None) -> Set[str]:
    """Extract distinct size tokens present in a Meta live display name."""
    live_name = (live_name or "").strip()
    if not live_name:
        return set()

    tokens: Set[str] = set()
    segments = [part.strip() for part in live_name.split("/") if part.strip()]
    if not segments:
        segments = [live_name]

    title_prefix = f"{parent_title} - " if parent_title else None
    for segment in segments:
        text = segment
        if title_prefix and text.startswith(title_prefix):
            text = text[len(title_prefix):].strip()
        token = _canonical_size_label(text)
        if token:
            tokens.add(token)
    return tokens


def collect_live_meta_display_reasons(
    payload: Dict[str, Any],
    variant: Any,
    live_row: Optional[Dict[str, Any]],
    *,
    parent: Any = None,
) -> List[str]:
    """Read-only Meta live display drift flags (no payload mutation)."""
    if not live_row:
        return []

    live_name = str(live_row.get("name") or "").strip()
    generated_name = str(payload.get("name") or "").strip()
    if not live_name:
        return []

    reasons: List[str] = []
    parent_title = str(getattr(parent, "title", "") or "").strip() if parent else None
    local_size = _local_size_label(payload, variant)
    live_sizes = _live_size_tokens(live_name, parent_title=parent_title or None)

    if local_size and live_sizes and local_size not in live_sizes:
        reasons.append("live_name_size_mismatch")
    elif (
        generated_name
        and live_name != generated_name
        and "live_name_size_mismatch" not in reasons
    ):
        reasons.append("stale_meta_display_name")
    return reasons


def _is_color_size_slash_name(summary: str) -> bool:
    text = (summary or "").strip()
    if "/" not in text:
        return False
    parts = [part.strip() for part in text.split("/") if part.strip()]
    if len(parts) < 2:
        return False
    has_size_part = any(_looks_like_size_token(part) for part in parts)
    has_non_size_part = any(not _looks_like_size_token(part) for part in parts)
    return has_size_part and has_non_size_part


def _looks_like_apparel_variant(variant: Any, summary: str) -> bool:
    opts = _variant_options_dict(variant)
    human_keys = {
        str(k).lower() for k in opts
        if str(k).lower() != "option_value_ids"
    }
    apparel_keys = {
        "المقاس", "مقاس", "size", "حجم",
        "اللون", "لون", "color", "colour",
    }
    if human_keys & apparel_keys:
        return True
    if opts.get("option_value_ids"):
        return True
    if _is_composite_option_summary(summary):
        return True
    if _looks_like_size_token(summary):
        return True
    return False


def _needs_meta_name_no_size_review(
    variant: Any,
    payload: Dict[str, Any],
    *,
    has_real_variants: bool,
) -> bool:
    """Conservative: grouped apparel SKU missing Meta ``size`` needs operator review."""
    if not has_real_variants:
        return False
    if not str(getattr(variant, "salla_variant_id", "") or "").strip():
        return False
    if str(payload.get("size") or "").strip():
        return False
    summary = str(getattr(variant, "option_summary", "") or "").strip()
    if summary and _looks_like_raw_option_ids(summary):
        return False
    if not _looks_like_apparel_variant(variant, summary):
        return False
    return True


def collect_variant_name_quality_reasons(
    variant: Any,
    payload: Dict[str, Any],
    *,
    has_real_variants: bool,
) -> List[str]:
    """Read-only name/option quality flags for operator review (no payload mutation)."""
    if not has_real_variants:
        return []
    if not str(getattr(variant, "salla_variant_id", "") or "").strip():
        return []

    reasons: List[str] = []
    summary = str(getattr(variant, "option_summary", "") or "").strip()

    if _has_orphan_option_value_ids(variant):
        reasons.append("orphan_option_value_ids")
    if summary and not _looks_like_raw_option_ids(summary):
        if _is_composite_option_summary(summary):
            reasons.append("composite_option_summary")
        if _is_color_size_slash_name(summary):
            reasons.append("color_size_slash_name")
    if _needs_meta_name_no_size_review(
        variant, payload, has_real_variants=has_real_variants,
    ):
        reasons.append("meta_name_no_size")
    return reasons


# TODO(platform): duplicate_option_summary_siblings — flag repeated option_summary
# across siblings on the same parent when building the full tenant report.


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
    name_quality = collect_variant_name_quality_reasons(
        variant, payload, has_real_variants=has_real_variants,
    )

    def _merge_name_quality(current: List[str]) -> List[str]:
        merged = list(current)
        for code in name_quality:
            if code not in merged:
                merged.append(code)
        return merged

    if eligibility.fatal or eligibility.status == "fatal":
        return "blocked", reasons

    if "raw_option_summary" in reasons:
        if _has_missing_option_summary(
            parent, variant, has_real_variants=has_real_variants, generated_name=generated_name,
        ) and "missing_option_summary" not in reasons:
            reasons.append("missing_option_summary")
        return "warn", _merge_name_quality(reasons)

    if _has_missing_option_summary(
        parent, variant, has_real_variants=has_real_variants, generated_name=generated_name,
    ):
        if "missing_option_summary" not in reasons:
            reasons.append("missing_option_summary")
        return "warn", _merge_name_quality(reasons)

    if reasons:
        return "warn", _merge_name_quality(reasons)

    reasons = _merge_name_quality(reasons)
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
        live_reasons = collect_live_meta_display_reasons(
            payload, variant, live, parent=parent,
        )
        for code in live_reasons:
            if code not in item.reasons:
                item.reasons.append(code)
        if item.status == "ready" and "live_name_size_mismatch" in live_reasons:
            item.status = "warn"
        item.action_needed = resolve_action_needed(item.status, payload or None, live)

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
        "review_name_quality_items": 0,
        "composite_option_summary_items": 0,
        "orphan_option_value_ids_items": 0,
        "meta_name_no_size_items": 0,
        "color_size_slash_name_items": 0,
        "live_name_size_mismatch_items": 0,
        "stale_meta_display_name_items": 0,
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
        if any(code in item.reasons for code in _NAME_QUALITY_REASONS):
            counts["review_name_quality_items"] += 1
        if "composite_option_summary" in item.reasons:
            counts["composite_option_summary_items"] += 1
        if "orphan_option_value_ids" in item.reasons:
            counts["orphan_option_value_ids_items"] += 1
        if "meta_name_no_size" in item.reasons:
            counts["meta_name_no_size_items"] += 1
        if "color_size_slash_name" in item.reasons:
            counts["color_size_slash_name_items"] += 1
        if "live_name_size_mismatch" in item.reasons:
            counts["live_name_size_mismatch_items"] += 1
        if "stale_meta_display_name" in item.reasons:
            counts["stale_meta_display_name_items"] += 1

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


def is_ready_create_in_stock_candidate(
    item: MetaCatalogReadinessItem,
    *,
    include_updates: bool = False,
) -> bool:
    """True when an item is eligible for guarded batch create push."""
    if item.status != "ready":
        return False
    if (item.availability or "").strip().lower() != IN_STOCK_AVAILABILITY:
        return False
    action = (item.action_needed or "").strip().lower()
    if action == "create":
        return True
    if include_updates and action == "update":
        return True
    return False


def select_ready_create_push_candidates(
    items: List[MetaCatalogReadinessItem],
    *,
    product_id: Optional[int] = None,
    limit: Optional[int] = None,
    include_updates: bool = False,
) -> List[MetaCatalogReadinessItem]:
    """Filter readiness items to ready + in-stock + create (or update when flagged)."""
    selected: List[MetaCatalogReadinessItem] = []
    for item in items:
        if product_id is not None and int(item.product_id) != int(product_id):
            continue
        if not is_ready_create_in_stock_candidate(item, include_updates=include_updates):
            continue
        selected.append(item)
        if limit is not None and len(selected) >= int(limit):
            break
    return selected


def candidate_push_row(item: MetaCatalogReadinessItem, *, would_push: bool) -> Dict[str, Any]:
    """Serialize a batch candidate for CLI / operator review."""
    return {
        "product_id": item.product_id,
        "title": item.title,
        "variant_id": item.variant_id,
        "retailer_id": item.retailer_id,
        "item_group_id": item.item_group_id,
        "generated_name": item.generated_name,
        "price": item.price,
        "currency": item.currency,
        "availability": item.availability,
        "action_needed": item.action_needed,
        "status": item.status,
        "would_push": would_push,
    }


__all__ = [
    "IN_STOCK_AVAILABILITY",
    "MetaCatalogReadinessItem",
    "MetaCatalogReadinessReport",
    "build_meta_catalog_readiness_report",
    "candidate_push_row",
    "classify_readiness_status",
    "collect_live_meta_display_reasons",
    "collect_variant_name_quality_reasons",
    "eligibility_to_readiness_item",
    "is_ready_create_in_stock_candidate",
    "resolve_action_needed",
    "select_ready_create_push_candidates",
]
