"""
services/meta_catalog_reconcile.py
──────────────────────────────────
Reconcile ``Product.meta_catalog_published_at`` against Meta Graph catalog
membership for a tenant.

Operational only — stamps mean: *this retailer_id exists in the tenant's
linked Meta catalog right now*. No product export, no deletes, no price/stock
changes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import httpx

from core.catalog import effective_retailer_id
from core.config import META_GRAPH_API_VERSION
from services.meta_catalog_import import _select_graph_token

logger = logging.getLogger("nahla.meta_catalog_reconcile")

REQUEST_TIMEOUT: float = 45.0
SAMPLE_LIMIT = 20


@dataclass(frozen=True)
class ReconcileRowRef:
    product_id: int
    meta_retailer_id: str
    effective_retailer_id: str
    title: str
    variant_retailer_ids: Tuple[str, ...] = ()
    published_at: Optional[str] = None


@dataclass
class MetaCatalogReconcileReport:
    tenant_id: int
    catalog_id: str
    dry_run: bool
    meta_live_count: int = 0
    nahla_meta_retailer_id_count: int = 0
    nahla_stamped_count: int = 0
    intersection_meta_and_stamped: int = 0
    to_stamp: List[ReconcileRowRef] = field(default_factory=list)
    to_clear: List[ReconcileRowRef] = field(default_factory=list)
    local_not_in_meta: List[ReconcileRowRef] = field(default_factory=list)
    meta_not_stamped: List[Dict[str, str]] = field(default_factory=list)
    applied_stamp_count: int = 0
    applied_clear_count: int = 0
    meta_fetch: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        def _rows(rows: Iterable[ReconcileRowRef]) -> List[Dict[str, Any]]:
            return [
                {
                    "product_id": r.product_id,
                    "meta_retailer_id": r.meta_retailer_id,
                    "effective_retailer_id": r.effective_retailer_id,
                    "title": r.title,
                    "variant_retailer_ids": list(r.variant_retailer_ids),
                    "published_at": r.published_at,
                }
                for r in rows
            ]

        return {
            "tenant_id": self.tenant_id,
            "catalog_id": self.catalog_id,
            "dry_run": self.dry_run,
            "counts": {
                "meta_live_retailer_ids": self.meta_live_count,
                "nahla_meta_retailer_id": self.nahla_meta_retailer_id_count,
                "nahla_stamped": self.nahla_stamped_count,
                "intersection_meta_and_stamped": self.intersection_meta_and_stamped,
                "to_stamp": len(self.to_stamp),
                "to_clear": len(self.to_clear),
                "local_not_in_meta": len(self.local_not_in_meta),
                "meta_not_stamped": len(self.meta_not_stamped),
                "applied_stamp_count": self.applied_stamp_count,
                "applied_clear_count": self.applied_clear_count,
            },
            "to_stamp": _rows(self.to_stamp[:SAMPLE_LIMIT]),
            "to_clear": _rows(self.to_clear[:SAMPLE_LIMIT]),
            "local_not_in_meta": _rows(self.local_not_in_meta[:SAMPLE_LIMIT]),
            "meta_not_stamped": self.meta_not_stamped[:SAMPLE_LIMIT],
            "meta_fetch": self.meta_fetch,
            "error": self.error,
        }


def fetch_meta_catalog_live_products(
    conn: Any,
    catalog_id: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Paginate Meta Graph ``/{catalog_id}/products`` (GET) into a retailer_id map."""
    catalog_id = str(catalog_id or "").strip()
    meta_info: Dict[str, Any] = {
        "catalog_id": catalog_id,
        "pages": 0,
        "items": 0,
        "http_status": None,
        "error": None,
    }
    live: Dict[str, Dict[str, Any]] = {}
    if not catalog_id:
        meta_info["error"] = "catalog_id_missing"
        return live, meta_info

    token = str((_select_graph_token(conn) or {}).get("token") or "").strip()
    if not token:
        meta_info["error"] = "no_graph_token"
        return live, meta_info

    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{catalog_id}/products"
    params: Optional[Dict[str, str]] = {
        "fields": "id,retailer_id,name,price,availability",
        "limit": "250",
        "access_token": token,
    }

    def _consume(resp: httpx.Response) -> bool:
        meta_info["http_status"] = resp.status_code
        if resp.status_code >= 400:
            meta_info["error"] = resp.text[:500]
            return False
        body = resp.json() or {}
        rows = body.get("data") or []
        meta_info["pages"] += 1
        for row in rows:
            rid = str(row.get("retailer_id") or "").strip()
            if not rid:
                continue
            live[rid] = {
                "meta_product_id": str(row.get("id") or "").strip() or None,
                "name": str(row.get("name") or "").strip() or None,
                "price": row.get("price"),
                "availability": str(row.get("availability") or "").strip() or None,
            }
        meta_info["items"] = len(live)
        return True

    if client is not None:
        while url:
            resp = client.get(url, params=params if "graph.facebook.com" in url else None)
            if not _consume(resp):
                break
            url = (resp.json() or {}).get("paging", {}).get("next")
            params = None
        return live, meta_info

    with httpx.Client(timeout=REQUEST_TIMEOUT) as owned:
        while url:
            resp = owned.get(url, params=params if "graph.facebook.com" in url else None)
            if not _consume(resp):
                break
            url = (resp.json() or {}).get("paging", {}).get("next")
            params = None
    return live, meta_info


def fetch_meta_catalog_retailer_ids(
    conn: Any,
    catalog_id: str,
    *,
    client: Optional[httpx.Client] = None,
) -> Tuple[Set[str], Dict[str, Any]]:
    """Paginate Meta Graph ``/{catalog_id}/products`` and collect retailer ids."""
    live, meta_info = fetch_meta_catalog_live_products(
        conn, catalog_id, client=client,
    )
    return set(live.keys()), meta_info


def _variant_ids_by_product(db: Any, tenant_id: int) -> Dict[int, Tuple[str, ...]]:
    from models import ProductVariant  # noqa: PLC0415

    out: Dict[int, Tuple[str, ...]] = {}
    rows = (
        db.query(ProductVariant)
        .filter(ProductVariant.tenant_id == int(tenant_id))
        .all()
    )
    for row in rows:
        rid = str(getattr(row, "retailer_id", "") or "").strip()
        if not rid:
            continue
        pid = int(row.product_id)
        existing = out.get(pid, ())
        out[pid] = existing + (rid,)
    return out


def _row_ref(product: Any, variant_ids: Tuple[str, ...]) -> ReconcileRowRef:
    published = getattr(product, "meta_catalog_published_at", None)
    return ReconcileRowRef(
        product_id=int(product.id),
        meta_retailer_id=str(getattr(product, "meta_retailer_id", "") or "").strip(),
        effective_retailer_id=effective_retailer_id(product),
        title=str(getattr(product, "title", "") or "")[:80],
        variant_retailer_ids=variant_ids,
        published_at=published.isoformat() if published else None,
    )


def _catalog_retailer_ids(row: ReconcileRowRef) -> Set[str]:
    ids: Set[str] = set()
    if row.meta_retailer_id:
        ids.add(row.meta_retailer_id)
    for rid in row.variant_retailer_ids:
        if rid:
            ids.add(rid)
    return ids


def _is_meta_verified(row: ReconcileRowRef, meta_live: Set[str]) -> bool:
    return bool(_catalog_retailer_ids(row) & meta_live)


def build_meta_catalog_reconcile_plan(
    db: Any,
    tenant_id: int,
    meta_live: Set[str],
) -> Tuple[List[ReconcileRowRef], List[ReconcileRowRef], List[ReconcileRowRef], List[Dict[str, str]], Dict[str, int]]:
    from models import Product  # noqa: PLC0415

    variant_map = _variant_ids_by_product(db, tenant_id)
    products = (
        db.query(Product)
        .filter(Product.tenant_id == int(tenant_id))
        .order_by(Product.id.asc())
        .all()
    )

    nahla_meta_ids: Set[str] = set()
    nahla_catalog_ids: Set[str] = set()
    stamped_rows: List[ReconcileRowRef] = []
    to_stamp: List[ReconcileRowRef] = []
    to_clear: List[ReconcileRowRef] = []
    local_not_in_meta: List[ReconcileRowRef] = []

    for product in products:
        ref = _row_ref(product, variant_map.get(int(product.id), ()))
        catalog_ids = _catalog_retailer_ids(ref)
        nahla_catalog_ids |= catalog_ids
        if ref.meta_retailer_id:
            nahla_meta_ids.add(ref.meta_retailer_id)
        if ref.published_at:
            stamped_rows.append(ref)

        verified = _is_meta_verified(ref, meta_live)
        if verified and not ref.published_at:
            to_stamp.append(ref)
        elif ref.published_at and not verified:
            to_clear.append(ref)

        if ref.meta_retailer_id and not _is_meta_verified(ref, meta_live):
            local_not_in_meta.append(ref)

    stamped_verified = sum(
        1 for ref in stamped_rows if _is_meta_verified(ref, meta_live)
    )
    meta_not_stamped: List[Dict[str, str]] = []
    for rid in sorted(meta_live):
        if rid not in nahla_catalog_ids:
            continue
        if any(ref.published_at and rid in _catalog_retailer_ids(ref) for ref in stamped_rows):
            continue
        meta_not_stamped.append({"retailer_id": rid})

    counts = {
        "nahla_meta_retailer_id_count": len(nahla_meta_ids),
        "nahla_stamped_count": len(stamped_rows),
        "intersection_meta_and_stamped": stamped_verified,
    }
    return to_stamp, to_clear, local_not_in_meta, meta_not_stamped, counts


def reconcile_meta_catalog_publish_stamps(
    db: Any,
    tenant_id: int,
    *,
    apply: bool = False,
    client: Optional[httpx.Client] = None,
) -> MetaCatalogReconcileReport:
    """Compare Meta Graph membership to local publish stamps; optionally apply."""
    from models import Product, WhatsAppConnection  # noqa: PLC0415

    report = MetaCatalogReconcileReport(
        tenant_id=int(tenant_id),
        catalog_id="",
        dry_run=not apply,
    )
    conn = (
        db.query(WhatsAppConnection)
        .filter(WhatsAppConnection.tenant_id == int(tenant_id))
        .first()
    )
    catalog_id = str(getattr(conn, "meta_catalog_id", "") or "").strip() if conn else ""
    report.catalog_id = catalog_id
    if not catalog_id:
        report.error = "catalog_id_missing"
        return report

    meta_live, meta_fetch = fetch_meta_catalog_retailer_ids(
        conn, catalog_id, client=client,
    )
    report.meta_fetch = meta_fetch
    report.meta_live_count = len(meta_live)
    if meta_fetch.get("error"):
        report.error = str(meta_fetch["error"])
        return report

    to_stamp, to_clear, local_not_in_meta, meta_not_stamped, counts = (
        build_meta_catalog_reconcile_plan(db, tenant_id, meta_live)
    )
    report.to_stamp = to_stamp
    report.to_clear = to_clear
    report.local_not_in_meta = local_not_in_meta
    report.meta_not_stamped = meta_not_stamped
    report.nahla_meta_retailer_id_count = counts["nahla_meta_retailer_id_count"]
    report.nahla_stamped_count = counts["nahla_stamped_count"]
    report.intersection_meta_and_stamped = counts["intersection_meta_and_stamped"]

    if not apply:
        logger.info(
            "[META_CATALOG_RECONCILE] dry_run tenant=%s catalog=%s stamp=%d clear=%d",
            tenant_id,
            catalog_id,
            len(to_stamp),
            len(to_clear),
        )
        return report

    now = datetime.now(timezone.utc)
    stamp_ids = {ref.product_id for ref in to_stamp}
    clear_ids = {ref.product_id for ref in to_clear}
    if not stamp_ids and not clear_ids:
        return report

    rows = (
        db.query(Product)
        .filter(Product.tenant_id == int(tenant_id))
        .filter(Product.id.in_(sorted(stamp_ids | clear_ids)))
        .all()
    )
    for row in rows:
        pid = int(row.id)
        if pid in stamp_ids:
            row.meta_catalog_published_at = now
            report.applied_stamp_count += 1
        elif pid in clear_ids:
            row.meta_catalog_published_at = None
            report.applied_clear_count += 1

    db.flush()
    logger.info(
        "[META_CATALOG_RECONCILE] apply tenant=%s catalog=%s stamped=%d cleared=%d",
        tenant_id,
        catalog_id,
        report.applied_stamp_count,
        report.applied_clear_count,
    )
    return report


__all__ = [
    "MetaCatalogReconcileReport",
    "ReconcileRowRef",
    "build_meta_catalog_reconcile_plan",
    "fetch_meta_catalog_live_products",
    "fetch_meta_catalog_retailer_ids",
    "reconcile_meta_catalog_publish_stamps",
]
