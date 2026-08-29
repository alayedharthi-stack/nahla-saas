"""
services/meta_catalog_sync_preview.py
─────────────────────────────────────
Dry-run Meta catalog sync preview for Nahla-native products only.

No Graph write calls. No DB mutations.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from core.catalog import (
    canonical_retailer_id,
    is_meta_export_eligible,
    meta_export_rejection_detail,
    product_source,
)
from services.meta_catalog_export import preview_meta_variant_payload
from services.meta_catalog_push import (
    MetaCatalogPushError,
    _resolve_catalog_and_token,
    _resolve_connection,
)

_PREVIEW_MESSAGE_AR: Dict[str, str] = {
    "missing_image_url": "أضف رابط صورة صالح قبل المزامنة.",
    "missing_price": "أضف سعراً صالحاً قبل المزامنة.",
    "missing_url": "أضف رابط المنتج قبل المزامنة.",
    "missing_retailer_id": "معرّف المتجر (retailer_id) مطلوب قبل المزامنة.",
    "out_of_stock": "المنتج غير متوفر حالياً.",
    "variant_synthesized_for_preview": (
        "لا يوجد variant محفوظ — تم بناء معاينة افتراضية من بيانات المنتج."
    ),
    "connection_not_found": "اربط حساب واتساب قبل فحص المزامنة.",
    "catalog_id_missing": "اربط Meta Catalog ID من إعدادات الكتالوج قبل المزامنة.",
    "access_token_missing": "لا يوجد رمز وصول Meta صالح لفحص المزامنة.",
}


def _issue(code: str) -> Dict[str, str]:
    return {
        "code": code,
        "message_ar": _PREVIEW_MESSAGE_AR.get(code, code),
    }


def _load_product(db: Any, tenant_id: int, product_id: int) -> Any:
    from models import Product  # noqa: PLC0415

    return (
        db.query(Product)
        .filter(Product.id == int(product_id), Product.tenant_id == int(tenant_id))
        .first()
    )


def _load_or_synthesize_variant(db: Any, parent: Any) -> Tuple[Any, bool]:
    from models import ProductVariant  # noqa: PLC0415

    variant = (
        db.query(ProductVariant)
        .filter(
            ProductVariant.tenant_id == int(parent.tenant_id),
            ProductVariant.product_id == int(parent.id),
        )
        .order_by(ProductVariant.is_default.desc(), ProductVariant.id)
        .first()
    )
    if variant is not None:
        return variant, False

    meta = getattr(parent, "extra_metadata", None) or {}
    synthetic = SimpleNamespace(
        id=None,
        tenant_id=parent.tenant_id,
        product_id=parent.id,
        salla_variant_id=None,
        retailer_id=canonical_retailer_id(parent, fallback_to_synthetic=True),
        price=getattr(parent, "price", None),
        currency=str(meta.get("currency") or "SAR"),
        stock_quantity=getattr(parent, "stock_quantity", None),
        in_stock=bool(getattr(parent, "in_stock", True)),
        option_summary=None,
        options={},
        image_url=meta.get("image_url"),
        extra_metadata=meta,
        is_default=True,
    )
    return synthetic, True


def _classify_preview_issues(
    preview: Dict[str, Any],
    *,
    extra_warnings: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    fatal_codes = {
        "missing_retailer_id",
        "missing_price",
        "missing_image_url",
        "missing_url",
    }
    raw_warnings = list(preview.get("warnings") or [])
    if extra_warnings:
        raw_warnings.extend(extra_warnings)

    fatal_errors: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    for code in raw_warnings:
        issue = _issue(code)
        if code in fatal_codes:
            fatal_errors.append(issue)
        else:
            warnings.append(issue)
    return fatal_errors, warnings


def preview_native_meta_sync(
    db: Any,
    tenant_id: int,
    product_id: int,
    *,
    channel_publish: bool = False,
) -> Dict[str, Any]:
    """Build a dry-run Meta sync preview for one catalog product.

    ``channel_publish=True`` uses WhatsApp channel-copy eligibility
    (Salla/Zid/native). The default remains native-only for Product Studio.
    """
    parent = _load_product(db, tenant_id, product_id)
    if parent is None:
        return {
            "eligible": False,
            "error_code": "product_not_found",
            "message_ar": "المنتج غير موجود.",
        }

    if channel_publish:
        from core.catalog import whatsapp_channel_publish_rejection_detail  # noqa: PLC0415
        rejection = whatsapp_channel_publish_rejection_detail(parent)
    else:
        rejection = meta_export_rejection_detail(parent)
    if rejection is not None:
        return dict(rejection)

    try:
        conn = _resolve_connection(db, tenant_id)
        catalog_id, _token = _resolve_catalog_and_token(
            conn, require_catalog_readable=False,
        )
    except MetaCatalogPushError as exc:
        return {
            "eligible": False,
            "error_code": exc.code,
            "message_ar": _PREVIEW_MESSAGE_AR.get(exc.code, str(exc)),
        }

    variant, synthesized = _load_or_synthesize_variant(db, parent)
    preview = preview_meta_variant_payload(parent, variant)
    extra_warnings: List[str] = []
    if synthesized:
        extra_warnings.append("variant_synthesized_for_preview")
    fatal_errors, warnings = _classify_preview_issues(preview, extra_warnings=extra_warnings)

    retailer_id = (
        getattr(variant, "retailer_id", None)
        or canonical_retailer_id(parent, fallback_to_synthetic=True)
    )

    return {
        "eligible": True,
        "dry_run": True,
        "would_sync": False,
        "product_id": int(parent.id),
        "source": product_source(parent),
        "ownership_mode": getattr(parent, "ownership_mode", None),
        "meta_catalog_id": catalog_id,
        "retailer_id": retailer_id,
        "payload": dict(preview.get("payload") or {}),
        "fatal_errors": fatal_errors,
        "warnings": warnings,
    }


__all__ = ["preview_native_meta_sync"]
