"""
services/meta_catalog_sync_confirm.py
─────────────────────────────────────
Confirmed one-item Meta catalog push for Nahla-native products only.

Creates a default ProductVariant at confirm time when missing.
Updates Product.sync_* fields on success/failure.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from core.catalog import (
    canonical_retailer_id,
    meta_export_rejection_detail,
    product_source,
)
from services.meta_catalog_push import MetaCatalogPushError, push_one_meta_catalog_item
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


def _sanitize_sync_error(push_result: Dict[str, Any]) -> str:
    code = str(push_result.get("error") or "meta_push_failed")
    meta = push_result.get("meta") or {}
    response = meta.get("response")
    if isinstance(response, dict):
        graph_err = response.get("error")
        if isinstance(graph_err, dict):
            message = (
                graph_err.get("error_user_msg")
                or graph_err.get("message")
                or graph_err.get("type")
                or ""
            )
            if message:
                return f"{code}: {str(message)[:480]}"
    return code[:500]


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


def _mark_sync_success(parent: Any) -> None:
    parent.sync_status = "synced"
    parent.sync_error = None
    parent.last_synced_at = datetime.now(timezone.utc)


def _mark_sync_failed(parent: Any, message: str) -> None:
    parent.sync_status = "sync_failed"
    parent.sync_error = (message or "sync_failed")[:2000]


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

    preview = preview_native_meta_sync(db, tenant_id, product_id)
    if not preview.get("eligible"):
        return preview

    fatal_errors = list(preview.get("fatal_errors") or [])
    if fatal_errors:
        return {
            "eligible": False,
            "error_code": "preview_fatal",
            "message_ar": _CONFIRM_MESSAGE_AR["preview_fatal"],
            "fatal_errors": fatal_errors,
            "warnings": list(preview.get("warnings") or []),
            "preview": preview,
        }

    _variant, variant_created = ensure_native_default_variant(db, parent)
    retailer_id = (
        getattr(_variant, "retailer_id", None)
        or preview.get("retailer_id")
        or canonical_retailer_id(parent, fallback_to_synthetic=True)
    )

    try:
        push_result = push_one_meta_catalog_item(
            db,
            int(tenant_id),
            str(retailer_id),
            confirm=True,
            client=client,
        )
    except MetaCatalogPushError as exc:
        _mark_sync_failed(parent, exc.code)
        db.commit()
        return {
            "eligible": False,
            "ok": False,
            "error_code": exc.code,
            "message_ar": _CONFIRM_MESSAGE_AR["push_failed"],
            "sync_status": parent.sync_status,
            "sync_error": parent.sync_error,
            "source": product_source(parent),
            "ownership_mode": getattr(parent, "ownership_mode", None),
        }

    if not push_result.get("ok"):
        err_msg = _sanitize_sync_error(push_result)
        _mark_sync_failed(parent, err_msg)
        db.commit()
        return {
            "eligible": True,
            "ok": False,
            "confirm": True,
            "error_code": push_result.get("error") or "meta_push_failed",
            "message_ar": _CONFIRM_MESSAGE_AR["push_failed"],
            "product_id": int(parent.id),
            "retailer_id": retailer_id,
            "sync_status": parent.sync_status,
            "sync_error": parent.sync_error,
            "source": product_source(parent),
            "ownership_mode": getattr(parent, "ownership_mode", None),
            "push": push_result,
            "variant_created": variant_created,
        }

    _mark_sync_success(parent)
    db.commit()

    return {
        "eligible": True,
        "ok": True,
        "confirm": True,
        "product_id": int(parent.id),
        "source": product_source(parent),
        "ownership_mode": getattr(parent, "ownership_mode", None),
        "retailer_id": retailer_id,
        "sync_status": parent.sync_status,
        "sync_error": parent.sync_error,
        "last_synced_at": (
            parent.last_synced_at.isoformat() if parent.last_synced_at else None
        ),
        "variant_created": variant_created,
        "push": push_result,
    }


__all__ = ["confirm_native_meta_sync", "ensure_native_default_variant"]
