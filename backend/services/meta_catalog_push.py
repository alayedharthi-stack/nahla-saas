"""
services/meta_catalog_push.py
─────────────────────────────
Guarded one-item Meta Catalog push — explicit tenant + retailer_id only.

No full export, no DB writes, no product loops.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.config import META_GRAPH_API_VERSION
from services.meta_catalog_export import preview_meta_variant_payload
from services.meta_catalog_import import _select_graph_token

logger = logging.getLogger("nahla.meta_catalog_push")

REQUEST_TIMEOUT: float = 45.0
GRAPH_FIELDS = "id,retailer_id,name"


class MetaCatalogPushError(RuntimeError):
    """Hard failure before or during a guarded one-item push."""

    def __init__(self, code: str, message: str = "", *, detail: Any = None):
        super().__init__(message or code)
        self.code = code
        self.detail = detail


def load_variant_for_push(
    db: Any,
    tenant_id: int,
    *,
    retailer_id: str,
) -> Tuple[Any, Any]:
    """Load parent product + variant for a tenant-scoped retailer_id."""
    from models import Product, ProductVariant  # noqa: PLC0415

    rid = (retailer_id or "").strip()
    if not rid:
        raise MetaCatalogPushError("retailer_id_missing", "retailer_id is required")

    variant = (
        db.query(ProductVariant)
        .filter(
            ProductVariant.tenant_id == int(tenant_id),
            ProductVariant.retailer_id == rid,
        )
        .first()
    )
    if variant is None:
        raise MetaCatalogPushError("variant_not_found", f"variant not found for retailer_id={rid}")

    parent = (
        db.query(Product)
        .filter(
            Product.id == variant.product_id,
            Product.tenant_id == int(tenant_id),
        )
        .first()
    )
    if parent is None:
        raise MetaCatalogPushError("product_not_found", "parent product not found for variant")
    return parent, variant


def _graph_base(catalog_id: str, path: str) -> str:
    return f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{catalog_id}/{path}"


def _graph_product_url(meta_product_id: str) -> str:
    return f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{meta_product_id}"


def _resolve_connection(db: Any, tenant_id: int) -> Any:
    from models import WhatsAppConnection  # noqa: PLC0415

    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == int(tenant_id))
        .first()
    )
    if conn is None:
        raise MetaCatalogPushError("connection_not_found", "WhatsApp connection not found")
    return conn


def _resolve_catalog_and_token(conn: Any) -> Tuple[str, str]:
    catalog_id = str(getattr(conn, "meta_catalog_id", "") or "").strip()
    if not catalog_id:
        raise MetaCatalogPushError("catalog_id_missing", "meta_catalog_id is not set")

    token_info = _select_graph_token(conn) or {}
    token = str(token_info.get("token") or "").strip()
    if not token:
        raise MetaCatalogPushError(
            "access_token_missing",
            "No Graph-compatible access token available",
            detail={"token_source": token_info.get("token_source")},
        )
    return catalog_id, token


def find_meta_catalog_item_by_retailer_id(
    conn: Any,
    catalog_id: str,
    retailer_id: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Return (meta_product_id, lookup_meta) for a catalog retailer_id."""
    rid = (retailer_id or "").strip()
    lookup: Dict[str, Any] = {
        "retailer_id": rid,
        "catalog_id": catalog_id,
        "http_status": None,
        "error": None,
        "matched": False,
    }
    if not rid:
        lookup["error"] = "retailer_id_missing"
        return None, lookup

    _catalog_id, token = _resolve_catalog_and_token(conn)
    url = _graph_base(catalog_id, "products")
    params = {
        "fields": GRAPH_FIELDS,
        "filter": json.dumps({"retailer_id": {"eq": rid}}, separators=(",", ":")),
        "access_token": token,
    }

    def _run(http: httpx.Client) -> Tuple[Optional[str], Dict[str, Any]]:
        resp = http.get(url, params=params)
        lookup["http_status"] = resp.status_code
        if resp.status_code >= 400:
            lookup["error"] = resp.text[:500]
            return None, lookup
        rows = (resp.json() or {}).get("data") or []
        if not rows:
            return None, lookup
        first = rows[0] or {}
        meta_id = str(first.get("id") or "").strip() or None
        if meta_id:
            lookup["matched"] = True
        return meta_id, lookup

    if client is not None:
        return _run(client)
    with httpx.Client(timeout=REQUEST_TIMEOUT) as owned:
        return _run(owned)


def _post_catalog_item(
    url: str,
    token: str,
    payload: Dict[str, Any],
    *,
    client: Optional[httpx.Client] = None,
) -> Tuple[int, Dict[str, Any]]:
    body = {k: v for k, v in payload.items() if v is not None}
    body["access_token"] = token

    def _run(http: httpx.Client) -> Tuple[int, Dict[str, Any]]:
        resp = http.post(url, data=body)
        try:
            parsed = resp.json() or {}
        except Exception:
            parsed = {"raw": resp.text[:1000]}
        return resp.status_code, parsed

    if client is not None:
        return _run(client)
    with httpx.Client(timeout=REQUEST_TIMEOUT) as owned:
        return _run(owned)


def push_one_meta_catalog_item(
    db: Any,
    tenant_id: int,
    retailer_id: str,
    *,
    confirm: bool = False,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """Push one catalog item to Meta (dry-run unless ``confirm=True``)."""
    rid = (retailer_id or "").strip()
    parent, variant = load_variant_for_push(db, tenant_id, retailer_id=rid)
    preview = preview_meta_variant_payload(parent, variant)
    payload = dict(preview.get("payload") or {})

    result: Dict[str, Any] = {
        "action": "dry_run",
        "dry_run": not confirm,
        "ok": False,
        "tenant_id": int(tenant_id),
        "retailer_id": rid,
        "catalog_id": None,
        "meta_product_id": None,
        "payload": payload,
        "preview": preview,
        "lookup": None,
        "meta": {
            "http_status": None,
            "response": None,
        },
        "error": None,
    }

    if preview.get("fatal"):
        result["error"] = "preview_fatal"
        result["fatal_warnings"] = list(preview.get("warnings") or [])
        return result

    conn = _resolve_connection(db, tenant_id)
    catalog_id, token = _resolve_catalog_and_token(conn)
    result["catalog_id"] = catalog_id

    if not confirm:
        result["ok"] = True
        return result

    meta_product_id, lookup = find_meta_catalog_item_by_retailer_id(
        conn, catalog_id, rid, client=client,
    )
    result["lookup"] = lookup

    if lookup.get("error"):
        result["error"] = "lookup_failed"
        return result

    if meta_product_id:
        result["action"] = "update"
        result["meta_product_id"] = meta_product_id
        post_url = _graph_product_url(meta_product_id)
    else:
        result["action"] = "create"
        post_url = _graph_base(catalog_id, "products")

    status_code, response = _post_catalog_item(post_url, token, payload, client=client)
    result["meta"]["http_status"] = status_code
    result["meta"]["response"] = response

    if status_code >= 400 or (isinstance(response, dict) and response.get("error")):
        result["error"] = "meta_http_error"
        return result

    if not meta_product_id and isinstance(response, dict):
        created_id = str(response.get("id") or "").strip() or None
        if created_id:
            result["meta_product_id"] = created_id

    result["ok"] = True
    logger.info(
        "[META_CATALOG_PUSH] tenant=%s action=%s retailer_id=%s catalog=%s meta_id=%s status=%s",
        tenant_id,
        result["action"],
        rid,
        catalog_id,
        result.get("meta_product_id"),
        status_code,
    )
    return result


def push_ready_meta_catalog_batch(
    db: Any,
    tenant_id: int,
    *,
    confirm: bool = False,
    product_id: Optional[int] = None,
    limit: Optional[int] = None,
    include_updates: bool = False,
    stop_on_first_error: bool = True,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """Push a filtered batch of ready create items (dry-run unless ``confirm=True``)."""
    from services.meta_catalog_readiness import (  # noqa: PLC0415
        build_meta_catalog_readiness_report,
        candidate_push_row,
        is_ready_create_in_stock_candidate,
        select_ready_create_push_candidates,
    )

    report = build_meta_catalog_readiness_report(
        db,
        int(tenant_id),
        product_id=product_id,
        include_meta_live_read=True,
        client=client,
    )

    batch: Dict[str, Any] = {
        "dry_run": not confirm,
        "tenant_id": int(tenant_id),
        "error": report.error,
        "meta_fetch": report.meta_fetch,
        "summary": {
            "candidate_count": 0,
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "stopped_on_error": False,
        },
        "candidates": [],
        "results": [],
    }

    if report.error:
        return batch

    candidates = select_ready_create_push_candidates(
        report.items,
        product_id=product_id,
        limit=limit,
        include_updates=include_updates,
    )
    batch["summary"]["candidate_count"] = len(candidates)
    batch["candidates"] = [
        candidate_push_row(item, would_push=True)
        for item in candidates
    ]

    if not confirm:
        return batch

    for item in candidates:
        if not is_ready_create_in_stock_candidate(item, include_updates=include_updates):
            batch["summary"]["skipped"] += 1
            batch["results"].append({
                "retailer_id": item.retailer_id,
                "ok": False,
                "skipped": True,
                "error": "not_ready_create_in_stock",
            })
            continue

        rid = str(item.retailer_id or "").strip()
        try:
            push_result = push_one_meta_catalog_item(
                db,
                int(tenant_id),
                rid,
                confirm=True,
                client=client,
            )
        except MetaCatalogPushError as exc:
            push_result = {
                "ok": False,
                "retailer_id": rid,
                "error": exc.code,
                "message": str(exc),
                "detail": exc.detail,
            }

        batch["summary"]["attempted"] += 1
        row = {
            "retailer_id": rid,
            "action": push_result.get("action"),
            "ok": bool(push_result.get("ok")),
            "meta_product_id": push_result.get("meta_product_id"),
            "http_status": (push_result.get("meta") or {}).get("http_status"),
            "error": push_result.get("error"),
        }
        batch["results"].append(row)

        if push_result.get("ok"):
            batch["summary"]["succeeded"] += 1
        else:
            batch["summary"]["failed"] += 1
            if stop_on_first_error:
                batch["summary"]["stopped_on_error"] = True
                break

    return batch


__all__ = [
    "MetaCatalogPushError",
    "find_meta_catalog_item_by_retailer_id",
    "load_variant_for_push",
    "push_one_meta_catalog_item",
    "push_ready_meta_catalog_batch",
]
