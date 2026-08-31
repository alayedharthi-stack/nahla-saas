"""Persist Meta/Salla remote catalog images onto durable R2 URLs.

Manual uploads already land on ``catalog_media_storage``. Meta import
historically stored Graph CDN URLs as-is; those expire or fail in the
Nahla UI even when Commerce Manager still shows the image.

This module is the official ingest path: download → R2 → rewrite
``extra_metadata.image_url``. No Graph writes. No product/catalog create
or delete. Historical ``removed_from_meta`` rows are never processed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from core.catalog import (
    CATALOG_STATUS_ACTIVE,
    CATALOG_STATUS_REMOVED_FROM_META,
    OWNERSHIP_META_READONLY,
    catalog_status_of,
    infer_ownership_mode,
)
from core.catalog_image import coerce_image_url, resolve_product_image_url
from core.config import META_GRAPH_API_VERSION
from services.catalog_media_storage import (
    CatalogMediaStorageError,
    CatalogMediaValidationError,
    ingest_remote_catalog_image,
    is_catalog_media_storage_configured,
    is_managed_catalog_image_url,
)

logger = logging.getLogger("nahla.catalog_durable_images")

_GRAPH_IMAGE_TIMEOUT_SECONDS = 30.0


def _is_active_meta_readonly(product: Any) -> bool:
    if catalog_status_of(product) == CATALOG_STATUS_REMOVED_FROM_META:
        return False
    if catalog_status_of(product) != CATALOG_STATUS_ACTIVE:
        return False
    if getattr(product, "merchant_hidden_at", None) is not None:
        return False
    mode = infer_ownership_mode(product) or ""
    return mode == OWNERSHIP_META_READONLY


def _meta_item_id_of(product: Any) -> str:
    meta = getattr(product, "extra_metadata", None) or {}
    for candidate in (
        getattr(product, "meta_item_id", None),
        meta.get("meta_id") if isinstance(meta, dict) else None,
        getattr(product, "external_id", None),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def fetch_live_graph_image_url(
    db: Any,
    product: Any,
    *,
    token_cache: Optional[Dict[int, str]] = None,
) -> str:
    """GET the current Graph image_url. Never POST/PATCH/DELETE."""
    from models import WhatsAppConnection  # noqa: PLC0415
    from services.meta_catalog_access import select_catalog_graph_token  # noqa: PLC0415

    meta_id = _meta_item_id_of(product)
    if not meta_id:
        return ""
    tid = int(getattr(product, "tenant_id", 0) or 0)
    cache = token_cache if token_cache is not None else {}
    token = str(cache.get(tid) or "").strip()
    if not token:
        conn = (
            db.query(WhatsAppConnection)
            .filter(WhatsAppConnection.tenant_id == tid)
            .first()
        )
        catalog_id = str(getattr(conn, "meta_catalog_id", "") or "").strip()
        picked = select_catalog_graph_token(conn, catalog_id)
        token = str(picked.get("token") or "").strip()
        if not token:
            logger.warning(
                "[catalog_durable_images] graph image lookup skipped product=%s err=%s",
                getattr(product, "id", None),
                picked.get("error"),
            )
            return ""
        cache[tid] = token
    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{meta_id}"
    try:
        with httpx.Client(timeout=_GRAPH_IMAGE_TIMEOUT_SECONDS) as http:
            resp = http.get(
                url,
                params={"fields": "id,image_url"},
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception:
        logger.warning(
            "[catalog_durable_images] graph image GET failed product=%s",
            getattr(product, "id", None),
            exc_info=True,
        )
        return ""
    if resp.status_code >= 400:
        logger.warning(
            "[catalog_durable_images] graph image HTTP %s product=%s",
            resp.status_code,
            getattr(product, "id", None),
        )
        return ""
    try:
        payload = resp.json() or {}
    except Exception:
        return ""
    return coerce_image_url(payload.get("image_url")) or ""


def persist_product_display_image(
    db: Any,
    product: Any,
    *,
    source_url: Optional[str] = None,
    token_cache: Optional[Dict[int, str]] = None,
) -> Dict[str, Any]:
    """Replace a remote display URL with a durable catalog-media URL."""
    pid = int(getattr(product, "id", 0) or 0)
    tid = int(getattr(product, "tenant_id", 0) or 0)
    out: Dict[str, Any] = {
        "ok": False,
        "product_id": pid,
        "skipped": False,
        "reason": None,
        "image_url": None,
    }
    if pid <= 0 or tid <= 0:
        out["reason"] = "product_not_found"
        return out
    if not is_catalog_media_storage_configured():
        out["reason"] = "catalog_media_storage_not_configured"
        return out

    meta = dict(getattr(product, "extra_metadata", None) or {})
    variants = getattr(product, "variants", None) or []
    stored = coerce_image_url(source_url) or resolve_product_image_url(
        meta=meta, variants=variants,
    )
    out["image_url"] = stored or None
    if stored and is_managed_catalog_image_url(stored):
        out["ok"] = True
        out["skipped"] = True
        out["reason"] = "already_durable"
        return out

    ingested: Optional[Dict[str, Any]] = None
    used_source = ""
    last_error = "no_image_url"
    live = ""

    def _try_ingest(candidate: str) -> bool:
        nonlocal ingested, used_source, last_error
        try:
            ingested = ingest_remote_catalog_image(
                tenant_id=tid,
                source_url=candidate,
                product_id=pid,
            )
            used_source = candidate
            return True
        except (CatalogMediaValidationError, CatalogMediaStorageError) as exc:
            last_error = str(exc) or type(exc).__name__
            logger.warning(
                "[catalog_durable_images] ingest failed product=%s err=%s",
                pid,
                type(exc).__name__,
            )
            return False

    if stored:
        _try_ingest(stored)
    if ingested is None:
        live = fetch_live_graph_image_url(db, product, token_cache=token_cache)
        if live and live != stored:
            _try_ingest(live)

    if ingested is None:
        out["reason"] = last_error
        if not stored and not live:
            out["skipped"] = True
            out["reason"] = "no_image_url"
        return out

    durable = str(ingested.get("image_url") or "").strip()
    if not durable:
        out["reason"] = "upload_failed"
        return out

    merged = dict(meta)
    if used_source and used_source != durable:
        merged["meta_source_image_url"] = used_source
    merged["image_url"] = durable
    product.extra_metadata = merged
    db.add(product)
    out.update({
        "ok": True,
        "skipped": bool(ingested.get("skipped")),
        "reason": ingested.get("reason") or "ingested",
        "image_url": durable,
        "source_kind": "graph_live" if live and used_source == live else "stored",
    })
    return out


def backfill_active_meta_readonly_images(
    db: Any,
    tenant_id: int,
    *,
    product_id: Optional[int] = None,
    limit: int = 28,
) -> Dict[str, Any]:
    """Idempotent durable-image backfill for active meta_readonly rows only."""
    from models import Product  # noqa: PLC0415

    q = (
        db.query(Product)
        .filter(Product.tenant_id == int(tenant_id))
        .order_by(Product.id.asc())
    )
    if product_id is not None:
        q = q.filter(Product.id == int(product_id))

    report: Dict[str, Any] = {
        "tenant_id": int(tenant_id),
        "scanned": 0,
        "ingested": 0,
        "skipped": 0,
        "failed": 0,
        "ignored_removed": 0,
        "results": [],
    }
    budget = max(1, int(limit))
    token_cache: Dict[int, str] = {}
    for product in q.all():
        if catalog_status_of(product) == CATALOG_STATUS_REMOVED_FROM_META:
            report["ignored_removed"] += 1
            continue
        if not _is_active_meta_readonly(product):
            continue
        report["scanned"] += 1
        result = persist_product_display_image(db, product, token_cache=token_cache)
        report["results"].append({
            "product_id": result.get("product_id"),
            "ok": result.get("ok"),
            "skipped": result.get("skipped"),
            "reason": result.get("reason"),
        })
        if result.get("ok") and not result.get("skipped"):
            report["ingested"] += 1
        elif result.get("skipped"):
            report["skipped"] += 1
        else:
            report["failed"] += 1
        if report["scanned"] >= budget:
            break
    if report["ingested"]:
        db.commit()
    return report
