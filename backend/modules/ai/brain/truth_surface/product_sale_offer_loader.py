"""
product_sale_offer_loader.py
────────────────────────────
Read-only catalog product sale facts for Trusted Context.

Strict sale definition:
  sale_price > 0 AND regular_price > 0 AND sale_price < regular_price

Store-wide COUNT is official DB-side only (PostgreSQL CTE repository).
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from core.catalog import CATALOG_STATUS_ACTIVE, is_catalog_active

from .contract import TrustedDomain, TrustedFact, TruthSource
from .product_sale_offer_price_parse import strict_sale_from_metadata
from .product_sale_offer_repository import (
    ProductSaleOfferRepositoryError,
    StoreWideSaleSnapshot,
    fetch_product_scoped_catalog_row,
    fetch_store_wide_sale_snapshot,
)

_STORE_WIDE_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        r"عندكم\s*عروض",
        r"عندك\s*عروض",
        r"في\s*عروض",
        r"عروض\s*(?:عندكم|عندك|موجودة|متوفرة)",
        r"تخفيض(?:ات)?",
        r"خصومات",
        r"\bon\s*sale\b",
        r"\bsale\b",
    )
)

_PRODUCT_SCOPED_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        r"عرض",
        r"مخفض",
        r"خصم\s*سعر",
        r"سعر\s*مخفض",
        r"\bon\s*sale\b",
        r"\bsale\b",
    )
)


def _meta_dict(product: Any) -> Dict[str, Any]:
    meta = getattr(product, "extra_metadata", None)
    if isinstance(meta, dict):
        return dict(meta)
    return {}


def is_strict_product_sale(meta: Dict[str, Any]) -> bool:
    """True when catalog metadata proves a real discount."""
    _sale, _regular, on_sale = strict_sale_from_metadata(meta)
    return on_sale


def classify_product_sale_question_kind(message: str) -> str:
    text = (message or "").strip()
    if any(p.search(text) for p in _STORE_WIDE_PATTERNS):
        return "store_wide"
    if any(p.search(text) for p in _PRODUCT_SCOPED_PATTERNS):
        return "product_scoped"
    return "store_wide"


def should_load_product_sale_offer_facts(
    message: str = "",
    brain_state: Any = None,
) -> bool:
    """Independent gate — not tied to coupon/promotion loader."""
    text = (message or "").strip()
    if any(p.search(text) for p in _STORE_WIDE_PATTERNS):
        return True
    focus = getattr(brain_state, "current_product_focus", None) if brain_state else None
    if focus and any(p.search(text) for p in _PRODUCT_SCOPED_PATTERNS):
        return True
    return False


def _product_focus_id(brain_state: Any) -> Optional[int]:
    focus = getattr(brain_state, "current_product_focus", None) if brain_state else None
    if focus is None:
        return None
    if isinstance(focus, dict):
        raw = focus.get("product_id") or focus.get("id")
    else:
        raw = getattr(focus, "product_id", None) or getattr(focus, "id", None)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _compose_sample_row(*, title: str, sale_price: str, regular_price: str) -> Dict[str, str]:
    return {
        "title": str(title or "").strip(),
        "sale_price": str(sale_price or ""),
        "regular_price": str(regular_price or ""),
    }


def _build_internal_record(
    *,
    question_kind: str,
    availability: str,
    verified_count: Optional[int] = None,
    sample_products: Optional[List[Dict[str, str]]] = None,
    target_product: Optional[Dict[str, Any]] = None,
    allow_price_mention: bool = False,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "domain": TrustedDomain.CATALOG.value,
        "bundle_namespace": "product_sale_offer",
        "question_kind": question_kind,
        "product_sale_availability": availability,
        "allow_price_mention": bool(allow_price_mention),
    }
    if verified_count is not None:
        record["verified_on_sale_product_count"] = int(verified_count)
    if sample_products is not None:
        record["sample_products"] = list(sample_products)
    if target_product is not None:
        record["target_product"] = dict(target_product)
    return record


def _build_telemetry(
    *,
    availability: str,
    question_kind: str,
    loader_duration_ms: int,
    verified_count: Optional[int] = None,
    sample_product_ids: Optional[List[int]] = None,
    loader_error_class: Optional[str] = None,
) -> Dict[str, Any]:
    obs: Dict[str, Any] = {
        "product_sale_availability": availability,
        "question_kind": question_kind,
        "loader_duration_ms": int(loader_duration_ms),
    }
    if verified_count is not None:
        obs["verified_on_sale_product_count"] = int(verified_count)
    if sample_product_ids:
        obs["sample_product_ids"] = [int(pid) for pid in sample_product_ids]
    if loader_error_class:
        obs["loader_error_class"] = loader_error_class
    return obs


def load_product_sale_offer_facts(
    *,
    db: Any,
    tenant_id: int,
    message: str = "",
    brain_state: Any = None,
) -> Tuple[List[TrustedFact], Dict[str, Any]]:
    """Load catalog product sale facts — official DB COUNT + bounded sample."""
    started = time.perf_counter()
    question_kind = classify_product_sale_question_kind(message)

    if db is None or not tenant_id:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return [], _build_telemetry(
            availability="unavailable",
            question_kind=question_kind,
            loader_duration_ms=duration_ms,
            loader_error_class="missing_db",
        )

    try:
        if question_kind == "product_scoped":
            return _load_product_scoped(
                db=db,
                tenant_id=tenant_id,
                brain_state=brain_state,
                question_kind=question_kind,
                started=started,
            )
        return _load_store_wide(
            db=db,
            tenant_id=tenant_id,
            question_kind=question_kind,
            started=started,
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return [], _build_telemetry(
            availability="unavailable",
            question_kind=question_kind,
            loader_duration_ms=duration_ms,
            loader_error_class=exc.__class__.__name__,
        )


def _load_store_wide(
    *,
    db: Any,
    tenant_id: int,
    question_kind: str,
    started: float,
) -> Tuple[List[TrustedFact], Dict[str, Any]]:
    try:
        snapshot = fetch_store_wide_sale_snapshot(db, tenant_id=int(tenant_id))
    except ProductSaleOfferRepositoryError as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return [], _build_telemetry(
            availability="unavailable",
            question_kind=question_kind,
            loader_duration_ms=duration_ms,
            loader_error_class=exc.__class__.__name__,
        )

    assert snapshot is not None
    return _store_wide_from_snapshot(
        snapshot=snapshot,
        question_kind=question_kind,
        started=started,
    )


def _store_wide_from_snapshot(
    *,
    snapshot: StoreWideSaleSnapshot,
    question_kind: str,
    started: float,
) -> Tuple[List[TrustedFact], Dict[str, Any]]:
    count = int(snapshot.verified_count)
    if count > 0:
        availability = "active_sale_present"
        allow_price_mention = True
        sample_products = [
            _compose_sample_row(
                title=row.title,
                sale_price=row.sale_price,
                regular_price=row.regular_price,
            )
            for row in snapshot.sample_rows
        ]
        trace_ids = [int(row.product_id) for row in snapshot.sample_rows]
    else:
        availability = "none_verified"
        allow_price_mention = False
        sample_products = None
        trace_ids = None

    record = _build_internal_record(
        question_kind=question_kind,
        availability=availability,
        verified_count=count,
        sample_products=sample_products,
        allow_price_mention=allow_price_mention,
    )
    facts = [
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="catalog:product_sale_offer",
            value=record,
            source=TruthSource.PRODUCTS_TABLE,
            path="products_table.on_sale_count",
        )
    ]
    duration_ms = int((time.perf_counter() - started) * 1000)
    obs = _build_telemetry(
        availability=availability,
        question_kind=question_kind,
        loader_duration_ms=duration_ms,
        verified_count=count,
        sample_product_ids=trace_ids,
    )
    return facts, obs


def _load_product_scoped(
    *,
    db: Any,
    tenant_id: int,
    brain_state: Any,
    question_kind: str,
    started: float,
) -> Tuple[List[TrustedFact], Dict[str, Any]]:
    product_id = _product_focus_id(brain_state)
    if product_id is None:
        record = _build_internal_record(
            question_kind=question_kind,
            availability="requires_product_context",
            allow_price_mention=False,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        facts = [
            TrustedFact(
                domain=TrustedDomain.CATALOG,
                key="catalog:product_sale_offer",
                value=record,
                source=TruthSource.PRODUCTS_TABLE,
                path="brain_state.current_product_focus.missing",
            )
        ]
        return facts, _build_telemetry(
            availability="requires_product_context",
            question_kind=question_kind,
            loader_duration_ms=duration_ms,
        )

    try:
        product = fetch_product_scoped_catalog_row(
            db,
            tenant_id=int(tenant_id),
            product_id=int(product_id),
        )
    except ProductSaleOfferRepositoryError:
        product = None

    if product is None or not is_catalog_active(product):
        availability = "unavailable"
        target = None
        verified_count: Optional[int] = None
        allow_price_mention = False
    else:
        meta = dict(product.extra_metadata)
        sale, regular, on_sale = strict_sale_from_metadata(meta)
        availability = "active_sale_present" if on_sale else "none_verified"
        verified_count = 1 if on_sale else 0
        allow_price_mention = bool(on_sale)
        target = {
            "title": product.title,
            "sale_price": str(sale or ""),
            "regular_price": str(regular or ""),
            "is_on_sale": bool(on_sale),
        }

    record = _build_internal_record(
        question_kind=question_kind,
        availability=availability,
        verified_count=verified_count,
        target_product=target,
        allow_price_mention=allow_price_mention,
    )
    facts = [
        TrustedFact(
            domain=TrustedDomain.CATALOG,
            key="catalog:product_sale_offer",
            value=record,
            source=TruthSource.PRODUCTS_TABLE,
            path=f"products_table.id={product_id}",
        )
    ]
    duration_ms = int((time.perf_counter() - started) * 1000)
    obs = _build_telemetry(
        availability=availability,
        question_kind=question_kind,
        loader_duration_ms=duration_ms,
        verified_count=verified_count,
        sample_product_ids=[int(product_id)] if product_id and availability != "unavailable" else None,
    )
    return facts, obs


__all__ = [
    "classify_product_sale_question_kind",
    "is_strict_product_sale",
    "load_product_sale_offer_facts",
    "should_load_product_sale_offer_facts",
    "_store_wide_from_snapshot",
]
