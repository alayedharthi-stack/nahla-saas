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
from services.meta_catalog_access import (
    ERROR_CATALOG_ID_MISSING,
    ERROR_CATALOG_NOT_READABLE,
    ERROR_NO_GRAPH_TOKEN,
    select_catalog_graph_token,
)
from services.meta_catalog_export import preview_meta_variant_payload
from services.meta_catalog_identity import (
    ACTION_BLOCK,
    ACTION_LINK,
    ERROR_AMBIGUOUS_SIBLING,
    REASON_ALREADY_BOUND,
    REASON_LOOKUP,
    canonical_sibling_retailer_ids,
    evaluate_canonical_sibling_bind,
    existing_identity_retailer_id,
    occupied_active_meta_item_ids,
    parent_would_create_in_meta,
)
from services.meta_catalog_import import _select_graph_token

logger = logging.getLogger("nahla.meta_catalog_push")

REQUEST_TIMEOUT: float = 45.0
# Graph Catalog Product Item fields used after a push. Identity plus
# the content fields this API version exposes on GET.
GRAPH_FIELDS = "id,retailer_id,name,price,currency,availability"
SIBLING_GRAPH_FIELDS = "id,retailer_id,price,currency,availability,url,image_url"


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


def _resolve_catalog_and_token(
    conn: Any,
    *,
    require_catalog_readable: bool = True,
) -> Tuple[str, str]:
    catalog_id = str(getattr(conn, "meta_catalog_id", "") or "").strip()
    if not catalog_id:
        raise MetaCatalogPushError("catalog_id_missing", "meta_catalog_id is not set")

    if not require_catalog_readable:
        token_info = _select_graph_token(conn) or {}
        token = str(token_info.get("token") or "").strip()
        if not token:
            raise MetaCatalogPushError(
                "access_token_missing",
                "No Graph-compatible access token available",
                detail={"token_source": token_info.get("token_source")},
            )
        return catalog_id, token

    pick = select_catalog_graph_token(conn, catalog_id) or {}
    token = str(pick.get("token") or "").strip()
    if token:
        return catalog_id, token
    error = str(pick.get("error") or ERROR_NO_GRAPH_TOKEN)
    if error == ERROR_CATALOG_ID_MISSING:
        raise MetaCatalogPushError("catalog_id_missing", "meta_catalog_id is not set")
    if error == ERROR_NO_GRAPH_TOKEN:
        raise MetaCatalogPushError(
            "access_token_missing",
            "No Graph-compatible access token available",
            detail={"probes": pick.get("probes")},
        )
    raise MetaCatalogPushError(
        "catalog_permission_denied",
        "No Graph token can read the merchant catalog",
        detail={"error": error or ERROR_CATALOG_NOT_READABLE, "probes": pick.get("probes")},
    )


def _graph_auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {(token or '').strip()}"}


def find_meta_catalog_item_by_retailer_id(
    conn: Any,
    catalog_id: str,
    retailer_id: str,
    *,
    client: Optional[httpx.Client] = None,
    fields: Optional[str] = None,
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
        "fields": (fields or GRAPH_FIELDS).strip() or GRAPH_FIELDS,
        "filter": json.dumps({"retailer_id": {"eq": rid}}, separators=(",", ":")),
    }
    headers = _graph_auth_headers(token)

    def _run(http: httpx.Client) -> Tuple[Optional[str], Dict[str, Any]]:
        resp = http.get(url, params=params, headers=headers)
        lookup["http_status"] = resp.status_code
        if resp.status_code >= 400:
            lookup["error"] = resp.text[:500]
            return None, lookup
        rows = (resp.json() or {}).get("data") or []
        if not rows:
            return None, lookup
        if len(rows) != 1:
            lookup["error"] = "ambiguous_graph_rows"
            lookup["item"] = rows[0] or {}
            return None, lookup
        first = rows[0] or {}
        meta_id = str(first.get("id") or "").strip() or None
        lookup["item"] = first
        if not meta_id:
            lookup["error"] = "missing_graph_id"
            return None, lookup
        lookup["matched"] = True
        return meta_id, lookup

    if client is not None:
        return _run(client)
    with httpx.Client(timeout=REQUEST_TIMEOUT) as owned:
        return _run(owned)


def _parent_variants_for_gate(db: Any, parent: Any, variant: Any, tenant_id: int) -> List[Any]:
    rows = list(getattr(parent, "variants", None) or [])
    if not rows and db is not None:
        from models import ProductVariant  # noqa: PLC0415

        rows = (
            db.query(ProductVariant)
            .filter(
                ProductVariant.tenant_id == int(tenant_id),
                ProductVariant.product_id == int(getattr(parent, "id", 0) or 0),
            )
            .all()
        )
        if not isinstance(rows, list):
            rows = []
    current_id = int(getattr(variant, "id", 0) or 0)
    if variant is not None and current_id and all(
        int(getattr(row, "id", 0) or 0) != current_id for row in rows
    ):
        rows.append(variant)
    elif variant is not None and not rows:
        rows.append(variant)
    return rows


def _load_occupied_meta_item_ids(db: Any, tenant_id: int, exclude_product_id: int) -> Dict[str, int]:
    from models import Product  # noqa: PLC0415

    rows = (
        db.query(Product)
        .filter(
            Product.tenant_id == int(tenant_id),
            Product.meta_item_id.isnot(None),
        )
        .all()
    )
    if not isinstance(rows, list):
        rows = []
    return occupied_active_meta_item_ids(rows, exclude_product_id=exclude_product_id)


def _variant_for_sibling_rid(ext: str, sibling_rid: str, variants: List[Any]) -> Optional[Any]:
    suffix = sibling_rid[len(ext) + 1 :] if ext and sibling_rid.startswith(f"{ext}-") else ""
    for row in variants:
        if suffix and str(getattr(row, "salla_variant_id", "") or "").strip() == suffix:
            return row
    for row in variants:
        if str(getattr(row, "retailer_id", "") or "").strip() == sibling_rid:
            return row
    return None


def _decision_to_push_block(decision: Any) -> Dict[str, Any]:
    return {
        "action": decision.action,
        "error": decision.error,
        "reason": decision.reason,
        "identity_class": decision.identity_class,
        "meta_product_id": decision.meta_product_id,
        "sibling_retailer_id": decision.sibling_retailer_id,
        "idempotent": bool(decision.idempotent),
        "content_mismatches": list(decision.content_mismatches or []),
        "canonical_rule": decision.canonical_rule,
    }


def _canonical_sibling_gate(
    db: Any,
    conn: Any,
    catalog_id: str,
    parent: Any,
    variant: Any,
    retailer_id: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Optional[Dict[str, Any]]:
    """LINK a unique safe sibling, BLOCK if unproven, else None (CREATE)."""
    tenant_id = int(getattr(parent, "tenant_id", 0) or getattr(conn, "tenant_id", 0) or 0)
    variants = _parent_variants_for_gate(db, parent, variant, tenant_id)
    candidates = canonical_sibling_retailer_ids(
        parent, exclude_rid=retailer_id, variants=variants,
    )
    live_by_rid: Dict[str, Dict[str, Any]] = {}
    lookup_unproven = False
    for candidate in candidates:
        meta_id, lookup = find_meta_catalog_item_by_retailer_id(
            conn, catalog_id, candidate, client=client, fields=SIBLING_GRAPH_FIELDS,
        )
        if lookup.get("error"):
            lookup_unproven = True
            continue
        if meta_id:
            item = dict(lookup.get("item") or {})
            item["id"] = meta_id
            if not str(item.get("retailer_id") or "").strip():
                item["retailer_id"] = candidate
            live_by_rid[candidate] = item
    if lookup_unproven:
        return {
            "action": ACTION_BLOCK,
            "error": ERROR_AMBIGUOUS_SIBLING,
            "reason": REASON_LOOKUP,
            "identity_class": None,
            "meta_product_id": None,
            "sibling_retailer_id": None,
            "idempotent": False,
            "content_mismatches": [],
            "canonical_rule": None,
        }

    ext = str(getattr(parent, "external_id", None) or "").strip()
    sibling_payloads: Dict[str, Dict[str, Any]] = {}
    for sibling_rid in live_by_rid:
        sibling_variant = _variant_for_sibling_rid(ext, sibling_rid, variants)
        if sibling_variant is None:
            continue
        preview = preview_meta_variant_payload(parent, sibling_variant)
        sibling_payloads[sibling_rid] = dict(preview.get("payload") or {})

    occupied = _load_occupied_meta_item_ids(
        db, tenant_id, int(getattr(parent, "id", 0) or 0),
    )
    decision = evaluate_canonical_sibling_bind(
        parent,
        current_rid=retailer_id,
        variants=variants,
        live_by_rid=live_by_rid,
        occupied_meta_item_ids=occupied,
        sibling_payloads=sibling_payloads,
    )
    if decision.allow_create:
        return None
    return _decision_to_push_block(decision)


def _post_catalog_item(
    url: str,
    token: str,
    payload: Dict[str, Any],
    *,
    client: Optional[httpx.Client] = None,
) -> Tuple[int, Dict[str, Any]]:
    body = {k: v for k, v in payload.items() if v is not None}
    headers = _graph_auth_headers(token)

    def _run(http: httpx.Client) -> Tuple[int, Dict[str, Any]]:
        resp = http.post(url, data=body, headers=headers)
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
    catalog_id, token = _resolve_catalog_and_token(
        conn, require_catalog_readable=bool(confirm),
    )
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
        already = str(getattr(parent, "meta_item_id", None) or "").strip()
        if already and already != str(meta_product_id).strip():
            result["action"] = ACTION_BLOCK
            result["error"] = ERROR_AMBIGUOUS_SIBLING
            result["ok"] = False
            result["meta_product_id"] = meta_product_id
            result["lookup"] = {
                **(result.get("lookup") or {}),
                "reason": REASON_ALREADY_BOUND,
                "identity_class": None,
            }
            return result
        result["action"] = "update"
        result["meta_product_id"] = meta_product_id
        post_url = _graph_product_url(meta_product_id)
    else:
        blocked = _canonical_sibling_gate(
            db, conn, catalog_id, parent, variant, rid, client=client,
        )
        if blocked is not None:
            result["action"] = blocked.get("action")
            result["error"] = blocked.get("error")
            result["ok"] = blocked.get("action") == ACTION_LINK
            result["meta_product_id"] = blocked.get("meta_product_id")
            result["lookup"] = {
                **(result.get("lookup") or {}),
                "identity_class": blocked.get("identity_class"),
                "sibling_retailer_id": blocked.get("sibling_retailer_id"),
                "reason": blocked.get("reason"),
                "idempotent": blocked.get("idempotent"),
                "content_mismatches": blocked.get("content_mismatches") or [],
                "canonical_rule": blocked.get("canonical_rule"),
            }
            return result
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
    "existing_identity_retailer_id",
    "find_meta_catalog_item_by_retailer_id",
    "load_variant_for_push",
    "parent_would_create_in_meta",
    "push_one_meta_catalog_item",
    "push_ready_meta_catalog_batch",
]
