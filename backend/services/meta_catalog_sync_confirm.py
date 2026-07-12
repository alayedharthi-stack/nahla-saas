"""
services/meta_catalog_sync_confirm.py
─────────────────────────────────────
Confirmed one-item Meta catalog push for Nahla-native products only.

Creates a default ProductVariant at confirm time when missing.
Delegates push + verification to native_meta_sync_orchestrator.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from core.catalog import (
    canonical_retailer_id,
    meta_export_rejection_detail,
    product_source,
)
from services.native_meta_sync_orchestrator import (
    attempt_native_meta_sync,
    build_sync_response_fields,
    mark_native_meta_sync_pending,
)
from services.meta_catalog_sync_preview import preview_native_meta_sync

logger = logging.getLogger("nahla.meta_catalog_sync_confirm")

_CONFIRM_MESSAGE_AR = {
    "confirm_required": "يجب تأكيد الإرسال صراحةً قبل مزامنة المنتج مع Meta.",
    "preview_fatal": "لا يمكن الإرسال قبل إصلاح أخطاء المعاينة.",
    "push_failed": "تعذّر إرسال المنتج إلى Meta.",
}


def _load_product(db: Any, tenant_id: int, product_id: int) -> Any:
    from models import Product  # noqa: PLC0415

    return (
        db.query(Product)
        .filter(Product.id == int(product_id), Product.tenant_id == int(tenant_id))
        .first()
    )


def ensure_native_default_variant(db: Any, parent: Any) -> Tuple[Any, bool]:
    """Ensure a default native variant exists for confirm push."""
    from models import ProductVariant  # noqa: PLC0415

    existing = (
        db.query(ProductVariant)
        .filter(
            ProductVariant.tenant_id == int(parent.tenant_id),
            ProductVariant.product_id == int(parent.id),
        )
        .order_by(ProductVariant.is_default.desc(), ProductVariant.id)
        .first()
    )
    if existing is not None:
        return existing, False

    meta = dict(getattr(parent, "extra_metadata", None) or {})
    retailer_id = canonical_retailer_id(parent, fallback_to_synthetic=True)
    variant = ProductVariant(
        tenant_id=int(parent.tenant_id),
        product_id=int(parent.id),
        retailer_id=retailer_id,
        price=getattr(parent, "price", None),
        currency=str(meta.get("currency") or "SAR"),
        stock_quantity=getattr(parent, "stock_quantity", None),
        in_stock=bool(getattr(parent, "in_stock", True)),
        image_url=meta.get("image_url"),
        is_default=True,
        options={},
        option_summary=None,
        extra_metadata=meta,
    )
    db.add(variant)
    db.flush()
    return variant, True


def confirm_native_meta_sync(
    db: Any,
    tenant_id: int,
    product_id: int,
    *,
    confirm: bool,
    client: Any = None,
) -> Dict[str, Any]:
    """Confirm-push one Nahla-native product to Meta after all gates pass."""
    if not confirm:
        return {
            "eligible": False,
            "error_code": "confirm_required",
            "message_ar": _CONFIRM_MESSAGE_AR["confirm_required"],
        }

    parent = _load_product(db, tenant_id, product_id)
    if parent is None:
        return {
            "eligible": False,
            "error_code": "product_not_found",
            "message_ar": "المنتج غير موجود.",
        }

    rejection = meta_export_rejection_detail(parent)
    if rejection is not None:
        return dict(rejection)

    if not mark_native_meta_sync_pending(db, parent):
        return dict(rejection or {
            "eligible": False,
            "error_code": "product_not_meta_export_eligible",
            "message_ar": "هذا المنتج غير قابل للمزامنة مع Meta.",
        })
    db.commit()

    result = attempt_native_meta_sync(
        db, tenant_id, product_id, client=client,
    )
    if result.get("skipped"):
        return {
            "eligible": True,
            "ok": False,
            "error_code": result.get("error_code") or "sync_in_progress",
            "message_ar": _CONFIRM_MESSAGE_AR["push_failed"],
            **build_sync_response_fields(parent),
        }

    db.refresh(parent)
    base = {
        "eligible": True,
        "confirm": True,
        "product_id": int(product_id),
        "source": product_source(parent),
        "ownership_mode": getattr(parent, "ownership_mode", None),
        **build_sync_response_fields(parent),
    }

    if result.get("ok"):
        return {
            **base,
            "ok": True,
            "retailer_id": result.get("retailer_id"),
            "push": result.get("push"),
        }

    code = str(result.get("error_code") or "meta_push_failed")
    if code == "preview_fatal":
        return {
            **base,
            "ok": False,
            "error_code": code,
            "message_ar": _CONFIRM_MESSAGE_AR["preview_fatal"],
            "fatal_errors": result.get("fatal_errors") or [],
            "preview": preview_native_meta_sync(db, tenant_id, product_id),
        }

    return {
        **base,
        "ok": False,
        "error_code": code,
        "message_ar": _CONFIRM_MESSAGE_AR["push_failed"],
        "retailer_id": result.get("retailer_id"),
        "push": result.get("push"),
    }


__all__ = ["confirm_native_meta_sync", "ensure_native_default_variant"]
