"""
services/native_meta_sync_orchestrator.py
─────────────────────────────────────────
Automatic + retry Meta catalog sync for Nahla-native manual products.

Reuses preview / push / lookup helpers — no duplicate Graph client.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, Optional, Set

from sqlalchemy.orm.attributes import flag_modified

from core.catalog import (
    canonical_retailer_id,
    is_meta_export_eligible,
    meta_export_rejection_detail,
)
from services.meta_catalog_linking import get_waba_catalog_link_status
from services.meta_catalog_push import (
    MetaCatalogPushError,
    find_meta_catalog_item_by_retailer_id,
    push_one_meta_catalog_item,
    _resolve_connection,
)
from services.meta_catalog_sync_preview import preview_native_meta_sync

logger = logging.getLogger("nahla.native_meta_sync")

SYNC_STALE_TTL = timedelta(minutes=12)

META_RELEVANT_PATCH_KEYS: FrozenSet[str] = frozenset({
    "title",
    "description",
    "price",
    "currency",
    "in_stock",
    "stock_quantity",
    "image_url",
    "product_url",
    "meta_retailer_id",
    "availability",
})

_ACQUIRABLE_STATUSES = frozenset({"pending", "failed", "blocked", "sync_failed", ""})


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _read_sync_meta(product: Any) -> Dict[str, Any]:
    meta = getattr(product, "extra_metadata", None) or {}
    if not isinstance(meta, dict):
        return {}
    sync_meta = meta.get("sync_meta")
    return dict(sync_meta) if isinstance(sync_meta, dict) else {}


def _write_sync_meta(product: Any, **updates: Any) -> Dict[str, Any]:
    meta = dict(getattr(product, "extra_metadata", None) or {})
    sync_meta = dict(meta.get("sync_meta") or {})
    sync_meta.update({k: v for k, v in updates.items() if v is not None or k in updates})
    meta["sync_meta"] = sync_meta
    product.extra_metadata = meta
    if getattr(product, "_sa_instance_state", None) is not None:
        flag_modified(product, "extra_metadata")
    return sync_meta


def _syncing_is_stale(sync_meta: Dict[str, Any], now: datetime) -> bool:
    started = _parse_iso_dt(
        sync_meta.get("syncing_started_at") or sync_meta.get("last_attempt_at"),
    )
    if started is None:
        return True
    return (now - started) >= SYNC_STALE_TTL


def meta_relevant_patch_keys(keys: Set[str]) -> bool:
    return bool(keys & META_RELEVANT_PATCH_KEYS)


def sync_error_summary(product: Any) -> Optional[str]:
    err = getattr(product, "sync_error", None)
    if err:
        return str(err)[:500]
    sync_meta = _read_sync_meta(product)
    summary = sync_meta.get("last_error_summary")
    return str(summary)[:500] if summary else None


def retry_allowed_for_status(sync_status: Optional[str]) -> bool:
    status = str(sync_status or "").strip().lower()
    if status in ("syncing",):
        return False
    if status == "synced":
        return False
    return True


def build_sync_response_fields(product: Any) -> Dict[str, Any]:
    sync_meta = _read_sync_meta(product)
    status = getattr(product, "sync_status", None)
    return {
        "sync_status": status,
        "sync_error_summary": sync_error_summary(product),
        "meta_item_id": getattr(product, "meta_item_id", None),
        "last_sync_attempt_at": sync_meta.get("last_attempt_at"),
        "last_synced_at": (
            product.last_synced_at.isoformat()
            if getattr(product, "last_synced_at", None) else None
        ),
        "retry_allowed": retry_allowed_for_status(status),
    }


def mark_native_meta_sync_pending(db: Any, product: Any) -> bool:
    """Mark a native product pending for Meta sync. Returns False if ineligible."""
    if not is_meta_export_eligible(product):
        return False
    now = _now()
    product.sync_status = "pending"
    product.sync_error = None
    _write_sync_meta(
        product,
        pending_at=now.isoformat(),
        last_error_code=None,
        last_error_summary=None,
    )
    db.flush()
    return True


def _try_acquire_sync_lock(db: Any, tenant_id: int, product_id: int) -> Optional[Any]:
    """CAS: pending|failed|blocked|stale-syncing → syncing. Returns product or None."""
    from models import Product  # noqa: PLC0415

    now = _now()
    row = (
        db.query(Product)
        .filter(
            Product.id == int(product_id),
            Product.tenant_id == int(tenant_id),
        )
        .with_for_update(skip_locked=True)
        .first()
    )
    if row is None:
        return None

    if not is_meta_export_eligible(row):
        return None

    status = str(row.sync_status or "").strip().lower()
    sync_meta = _read_sync_meta(row)

    if status == "syncing" and not _syncing_is_stale(sync_meta, now):
        db.rollback()
        return None

    if status == "synced":
        db.rollback()
        return None

    if status not in _ACQUIRABLE_STATUSES and status != "syncing":
        if row.sync_status is not None:
            db.rollback()
            return None

    row.sync_status = "syncing"
    _write_sync_meta(
        row,
        syncing_started_at=now.isoformat(),
        last_attempt_at=now.isoformat(),
    )
    db.commit()
    db.refresh(row)
    return row


def _mark_blocked(product: Any, *, error_code: str, summary: str) -> None:
    product.sync_status = "blocked"
    product.sync_error = summary[:2000]
    _write_sync_meta(
        product,
        last_error_code=error_code,
        last_error_summary=summary[:500],
        syncing_started_at=None,
    )


def _mark_failed(product: Any, *, error_code: str, summary: str) -> None:
    product.sync_status = "failed"
    product.sync_error = summary[:2000]
    _write_sync_meta(
        product,
        last_error_code=error_code,
        last_error_summary=summary[:500],
        syncing_started_at=None,
    )


def _mark_synced(
    product: Any,
    *,
    meta_item_id: str,
    waba_linked: Optional[bool],
) -> None:
    now = _now()
    product.sync_status = "synced"
    product.sync_error = None
    product.meta_item_id = meta_item_id
    product.last_synced_at = now
    product.meta_catalog_published_at = now
    _write_sync_meta(
        product,
        last_error_code=None,
        last_error_summary=None,
        syncing_started_at=None,
        waba_catalog_linked=waba_linked,
        verified_at=now.isoformat(),
    )


def _waba_linked_flag(waba_status: Dict[str, Any]) -> Optional[bool]:
    if not waba_status:
        return None
    if waba_status.get("ok"):
        linked = waba_status.get("expected_catalog_linked")
        return bool(linked) if linked is not None else None
    return None


def attempt_native_meta_sync(
    db: Any,
    tenant_id: int,
    product_id: int,
    *,
    client: Any = None,
    allow_synced_retry: bool = False,
) -> Dict[str, Any]:
    """Run one Meta sync attempt. Caller must not pass request-scoped ORM objects."""
    if allow_synced_retry:
        parent = _load_product(db, tenant_id, product_id)
        if parent is None:
            return {"ok": False, "skipped": True, "error_code": "product_not_found"}
        if not mark_native_meta_sync_pending(db, parent):
            return {"ok": False, "skipped": True, "error_code": "not_eligible"}
        db.commit()

    parent = _try_acquire_sync_lock(db, tenant_id, product_id)
    if parent is None:
        return {"ok": False, "skipped": True, "error_code": "sync_lock_not_acquired"}

    try:
        conn = _resolve_connection(db, tenant_id)
        if not bool(getattr(conn, "catalog_enabled", False)):
            parent.sync_status = "pending"
            parent.sync_error = None
            _write_sync_meta(parent, last_error_code="catalog_disabled")
            db.commit()
            return {
                "ok": False,
                "skipped": True,
                "error_code": "catalog_disabled",
                "sync_status": parent.sync_status,
            }
    except MetaCatalogPushError as exc:
        _mark_failed(parent, error_code=exc.code, summary=exc.code)
        db.commit()
        return {
            "ok": False,
            "sync_status": parent.sync_status,
            "error_code": exc.code,
        }

    rejection = meta_export_rejection_detail(parent)
    if rejection is not None:
        _mark_blocked(parent, error_code="not_eligible", summary=rejection.get("message_ar", "not_eligible"))
        db.commit()
        return {"ok": False, "sync_status": parent.sync_status, **rejection}

    preview = preview_native_meta_sync(db, tenant_id, product_id)
    if not preview.get("eligible"):
        code = str(preview.get("error_code") or "preview_ineligible")
        summary = str(preview.get("message_ar") or code)
        _mark_failed(parent, error_code=code, summary=summary)
        db.commit()
        return {"ok": False, "sync_status": parent.sync_status, **preview}

    fatal_errors = list(preview.get("fatal_errors") or [])
    if fatal_errors:
        codes = ", ".join(e.get("code", "") for e in fatal_errors[:3])
        _mark_blocked(
            parent,
            error_code="preview_fatal",
            summary=f"preview_fatal: {codes}"[:500],
        )
        db.commit()
        return {
            "ok": False,
            "sync_status": parent.sync_status,
            "error_code": "preview_fatal",
            "fatal_errors": fatal_errors,
        }

    from services.meta_catalog_sync_confirm import ensure_native_default_variant  # noqa: PLC0415

    _variant, _variant_created = ensure_native_default_variant(db, parent)
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
        _mark_failed(parent, error_code=exc.code, summary=exc.code)
        db.commit()
        return {
            "ok": False,
            "sync_status": parent.sync_status,
            "error_code": exc.code,
            "retailer_id": retailer_id,
        }

    if not push_result.get("ok"):
        err_msg = _sanitize_sync_error(push_result)
        code = str(push_result.get("error") or "meta_push_failed")
        _mark_failed(parent, error_code=code, summary=err_msg)
        db.commit()
        return {
            "ok": False,
            "sync_status": parent.sync_status,
            "error_code": code,
            "retailer_id": retailer_id,
        }

    try:
        conn = _resolve_connection(db, tenant_id)
        catalog_id = str(getattr(conn, "meta_catalog_id", "") or "").strip()
        if not catalog_id:
            raise MetaCatalogPushError("catalog_id_missing", "meta_catalog_id is not set")
        meta_item_id, lookup = find_meta_catalog_item_by_retailer_id(
            conn, catalog_id, str(retailer_id), client=client,
        )
    except MetaCatalogPushError as exc:
        _mark_failed(parent, error_code=exc.code, summary=exc.code)
        db.commit()
        return {
            "ok": False,
            "sync_status": parent.sync_status,
            "error_code": exc.code,
            "retailer_id": retailer_id,
        }

    if not meta_item_id or not (lookup or {}).get("matched"):
        _mark_failed(
            parent,
            error_code="verification_failed",
            summary="verification_failed: retailer_id not found after push",
        )
        db.commit()
        return {
            "ok": False,
            "sync_status": parent.sync_status,
            "error_code": "verification_failed",
            "retailer_id": retailer_id,
        }

    waba_status = get_waba_catalog_link_status(db, tenant_id)
    waba_linked = _waba_linked_flag(waba_status)

    _mark_synced(parent, meta_item_id=str(meta_item_id), waba_linked=waba_linked)
    db.commit()

    logger.info(
        "[NATIVE_META_SYNC] tenant=%s product=%s retailer_id=%s meta_item_id=%s",
        tenant_id,
        product_id,
        retailer_id,
        meta_item_id,
    )

    return {
        "ok": True,
        "sync_status": parent.sync_status,
        "meta_item_id": parent.meta_item_id,
        "retailer_id": retailer_id,
        "last_synced_at": parent.last_synced_at.isoformat() if parent.last_synced_at else None,
        "waba_catalog_linked": waba_linked,
        "push": push_result,
        "lookup": lookup,
    }


def _load_product(db: Any, tenant_id: int, product_id: int) -> Optional[Any]:
    from models import Product  # noqa: PLC0415

    return (
        db.query(Product)
        .filter(Product.id == int(product_id), Product.tenant_id == int(tenant_id))
        .first()
    )


def run_native_meta_sync_background(tenant_id: int, product_id: int) -> None:
    """Background entry — fresh DB session only."""
    from core.database import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        attempt_native_meta_sync(db, int(tenant_id), int(product_id))
    except Exception:  # noqa: BLE001
        logger.exception(
            "[NATIVE_META_SYNC] background failed tenant=%s product=%s",
            tenant_id,
            product_id,
        )
        db.rollback()
    finally:
        db.close()


def schedule_native_meta_sync(background_tasks: Any, tenant_id: int, product_id: int) -> None:
    background_tasks.add_task(run_native_meta_sync_background, int(tenant_id), int(product_id))


__all__ = [
    "META_RELEVANT_PATCH_KEYS",
    "attempt_native_meta_sync",
    "build_sync_response_fields",
    "mark_native_meta_sync_pending",
    "meta_relevant_patch_keys",
    "retry_allowed_for_status",
    "run_native_meta_sync_background",
    "schedule_native_meta_sync",
    "sync_error_summary",
]
