"""
services/meta_catalog_push.py
─────────────────────────────
Guarded one-item Meta Catalog push — explicit tenant + retailer_id only.

No full export, no DB writes, no product loops.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
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

# A live Graph item that merely shares a retailer_id is NOT ours to update. The
# push adopts an existing item only with publication evidence for this tenant:
# a membership row for (tenant, catalog, retailer_id) bound to that exact Graph
# item with the publication provenance, which is written only after a successful
# create/update POST. Not evidence: a retailer_id format, a link domain, a
# reconcile-derived membership (Graph presence), and the legacy product-level
# ``Product.meta_item_id`` stamp — the import path (Meta → local rows), the
# identity bind and the sibling adoption write that stamp without publishing.
from core.meta_catalog_membership import PUBLICATION_PROVENANCES  # noqa: E402
ERROR_LIVE_MATCH_OWNERSHIP_UNVERIFIED = "live_match_ownership_unverified"
ACTION_BLOCK_OWNERSHIP = "block_ownership_unverified"
REASON_NO_PUBLICATION_EVIDENCE = "live_item_without_publication_evidence"
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
    if variant is None and "-" in rid:
        ext, _, svid = rid.rpartition("-")
        if ext and svid:
            variant = (
                db.query(ProductVariant)
                .join(Product, Product.id == ProductVariant.product_id)
                .filter(
                    ProductVariant.tenant_id == int(tenant_id),
                    Product.tenant_id == int(tenant_id),
                    Product.external_id == ext,
                    ProductVariant.salla_variant_id == svid,
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


def live_item_publication_evidence(
    db: Any,
    *,
    tenant_id: int,
    catalog_id: str,
    retailer_id: str,
    meta_product_id: str,
    parent: Any = None,
) -> Dict[str, Any]:
    """Did THIS path publish the live Graph item?

    ``owned`` is True only with evidence tied to the exact Graph item id: a
    ``MetaCatalogMembership`` for (tenant, catalog, retailer_id) — the query
    filters all three, so another tenant's or another catalog's row never
    counts — whose ``meta_item_id`` equals the live id and whose provenance is
    the publication provenance. Everything else — an absent membership, a
    reconcile-derived membership, a mismatching id, the legacy
    ``Product.meta_item_id`` stamp (written by import, identity bind and sibling
    adoption too), a retailer_id format or a familiar link domain — is
    ``owned=False`` with the reasons listed, and the caller must not update.
    """
    mid = str(meta_product_id or "").strip()
    rid = str(retailer_id or "").strip()
    cid = str(catalog_id or "").strip()
    out: Dict[str, Any] = {
        "owned": False, "source": None, "meta_product_id": mid or None, "retailer_id": rid,
        "catalog_id": cid or None, "membership": None, "reasons": [],
    }
    if not mid:
        out["reasons"].append("live_item_id_missing")
        return out
    membership = None
    try:
        from models import MetaCatalogMembership  # noqa: PLC0415

        membership = (
            db.query(MetaCatalogMembership)
            .filter(
                MetaCatalogMembership.tenant_id == int(tenant_id),
                MetaCatalogMembership.catalog_id == cid,
                MetaCatalogMembership.retailer_id == rid,
            )
            .first()
        )
    except Exception as exc:  # noqa: BLE001
        out["reasons"].append(f"membership_lookup_failed:{type(exc).__name__}")
    if membership is not None:
        m_mid = str(getattr(membership, "meta_item_id", None) or "").strip()
        prov = str(getattr(membership, "provenance", None) or "").strip()
        out["membership"] = {"meta_item_id": m_mid or None, "provenance": prov or None}
        if not m_mid:
            out["reasons"].append("membership_without_meta_item_id")
        elif m_mid != mid:
            out["reasons"].append("membership_meta_item_id_mismatch")
        elif prov not in PUBLICATION_PROVENANCES:
            out["reasons"].append(f"membership_provenance_not_publication:{prov or 'none'}")
        else:
            out["owned"] = True
            out["source"] = f"membership:{prov}"
            return out
    else:
        out["reasons"].append("membership_absent")
    legacy = str(getattr(parent, "meta_item_id", None) or "").strip() if parent is not None else ""
    if legacy:
        # indicator only: the stamp is also written by import, identity bind and
        # sibling adoption, so it never proves that this path published the item
        out["legacy_product_meta_item_id"] = {"value": legacy, "matches_live_item": legacy == mid}
        out["reasons"].append("legacy_stamp_is_not_publication_evidence")
    return out


PENDING_PUBLICATIONS_KEY = "pending_publications"


def pending_publication_record(*, product_id: int, catalog_id: str, retailer_id: str, meta_item_id: str) -> Optional[Dict[str, Any]]:
    """The identity of one successful create POST: its own item id, catalog, retailer_id and product.

    Never authority by itself; see ``corroborate_pending_publication``."""
    cid, rid, mid = str(catalog_id or "").strip(), str(retailer_id or "").strip(), str(meta_item_id or "").strip()
    if not (product_id and cid and rid and mid):
        return None
    return {"product_id": int(product_id), "catalog_id": cid, "retailer_id": rid, "meta_item_id": mid,
            "posted_at": datetime.now(timezone.utc).isoformat()}


def _pending_publications(sync_meta: Any) -> Dict[str, Any]:
    pending = (sync_meta or {}).get(PENDING_PUBLICATIONS_KEY) if isinstance(sync_meta, dict) else None
    return dict(pending) if isinstance(pending, dict) else {}


def with_pending_publication(sync_meta: Any, record: Dict[str, Any]) -> Dict[str, Any]:
    pending = _pending_publications(sync_meta)
    pending[f"{record['catalog_id']}|{record['retailer_id']}"] = dict(record)
    return pending


def without_pending_publications(sync_meta: Any, retailer_ids: Any) -> Dict[str, Any]:
    drop = {str(r or "").strip() for r in (retailer_ids or [])}
    return {k: v for k, v in _pending_publications(sync_meta).items()
            if not (isinstance(v, dict) and str(v.get("retailer_id") or "").strip() in drop)}


def corroborate_pending_publication(
    db: Any,
    parent: Any,
    variant: Any,
    *,
    tenant_id: int,
    catalog_id: str,
    retailer_id: str,
    live_meta_item_id: str,
    salla_identity: Any = None,
) -> Dict[str, Any]:
    """Turn a recorded successful create into publication evidence once a live lookup proves it.

    *live_meta_item_id* comes from ``find_meta_catalog_item_by_retailer_id``
    for this tenant's connection catalog and *retailer_id*, which returns an
    id only for exactly one Graph row that has one. Evidence is written only
    when the product's recorded attempt names this same product, catalog and
    retailer_id and its POST returned exactly that id; it is written through
    the generic upserts, so an established publication is never rebound and
    a conflicting row refuses. Anything else returns ok=False and writes
    nothing.
    """
    cid, rid, live = str(catalog_id or "").strip(), str(retailer_id or "").strip(), str(live_meta_item_id or "").strip()
    sync_meta = ((getattr(parent, "extra_metadata", None) or {}).get("sync_meta")
                 if isinstance(getattr(parent, "extra_metadata", None), dict) else None)
    record = _pending_publications(sync_meta).get(f"{cid}|{rid}")
    if not isinstance(record, dict) or not live:
        return {"ok": False, "reason": "no_pending_publication"}
    try:
        same_product = int(record.get("product_id") or 0) == int(getattr(parent, "id", 0) or 0)
    except (TypeError, ValueError):
        same_product = False
    if (not same_product or str(record.get("catalog_id") or "") != cid
            or str(record.get("retailer_id") or "") != rid):
        return {"ok": False, "reason": "pending_scope_mismatch"}
    if str(record.get("meta_item_id") or "").strip() != live:
        return {"ok": False, "reason": "pending_id_not_corroborated"}
    from services.salla_variant_catalog_identity import (  # noqa: PLC0415
        upsert_native_publication_membership,
        upsert_variant_membership,
    )

    if salla_identity is not None:
        bound = upsert_variant_membership(db, tenant_id=int(tenant_id), catalog_id=cid, identity=salla_identity,
                                          meta_item_id=live)
    else:
        bound = upsert_native_publication_membership(
            db, tenant_id=int(tenant_id), catalog_id=cid, retailer_id=rid, product_id=int(parent.id),
            variant_id=getattr(variant, "id", None), meta_item_id=live)
    if not bound.get("ok"):
        return {"ok": False, "reason": str(bound.get("reason") or bound.get("error") or "evidence_refused")}
    db.flush()
    return {"ok": True, "reason": "pending_publication_corroborated", "meta_item_id": live}


def _ownership_block(result: Dict[str, Any], lookup: Dict[str, Any], meta_product_id: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    result["action"] = ACTION_BLOCK_OWNERSHIP
    result["error"] = ERROR_LIVE_MATCH_OWNERSHIP_UNVERIFIED
    result["ok"] = False
    result["meta_product_id"] = str(meta_product_id)
    result["ownership_evidence"] = evidence
    result["lookup"] = {**(lookup or {}), "reason": REASON_NO_PUBLICATION_EVIDENCE, "identity_class": None}
    return result


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


RETIRED_AVAILABILITY = "out of stock"
RETIRED_VISIBILITY = "staging"
PUBLISHED_VISIBILITY = "published"
RETIRE_LOOKUP_FIELDS = GRAPH_FIELDS + ",visibility"


def graph_error_code(response: Any) -> Tuple[Optional[int], Optional[int], str]:
    """``(code, error_subcode, message)`` from a Graph error body (or text)."""
    body = response
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (TypeError, ValueError):
            return None, None, body[:240]
    if not isinstance(body, dict):
        return None, None, ""
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    if not isinstance(err, dict):
        return None, None, ""
    try:
        code = int(err.get("code")) if err.get("code") is not None else None
    except (TypeError, ValueError):
        code = None
    try:
        sub = int(err.get("error_subcode")) if err.get("error_subcode") is not None else None
    except (TypeError, ValueError):
        sub = None
    return code, sub, str(err.get("message") or "")[:240]


def push_one_meta_catalog_item(
    db: Any,
    tenant_id: int,
    retailer_id: str,
    *,
    confirm: bool = False,
    client: Optional[httpx.Client] = None,
    payload_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Push one catalog item to Meta (dry-run unless ``confirm=True``).

    ``payload_overrides`` adds channel-state fields (for example
    ``visibility=published`` when re-publishing a retired item). It never
    overrides identity or verified content fields.
    """
    rid = (retailer_id or "").strip()
    parent, variant = load_variant_for_push(db, tenant_id, retailer_id=rid)
    preview = preview_meta_variant_payload(parent, variant)
    payload = dict(preview.get("payload") or {})
    for key, value in dict(payload_overrides or {}).items():
        if key in {"retailer_id", "price", "currency", "availability"}:
            continue
        payload[key] = value

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

    if confirm:
        from services.whatsapp_catalog_sync_scope import SCOPE_BLOCKER_CODE, product_in_sync_scope  # noqa: PLC0415

        if not product_in_sync_scope(int(tenant_id), getattr(parent, "id", None)):
            result["action"] = "scope_excluded"
            result["error"] = SCOPE_BLOCKER_CODE
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

    from services.salla_variant_catalog_identity import (  # noqa: PLC0415
        AmbiguousVariantIdentity,
        ERROR_AMBIGUOUS_VARIANT_IDENTITY,
        identity_for_retailer_id,
        is_salla_source,
    )

    sellable_salla = False
    ident = None
    if is_salla_source(parent):
        gate_variants = _parent_variants_for_gate(db, parent, variant, tenant_id)
        ident = None
        try:
            ident = identity_for_retailer_id(parent, gate_variants, rid)
        except AmbiguousVariantIdentity:
            ident = None
        if ident is None:
            result["action"] = ACTION_BLOCK
            result["error"] = ERROR_AMBIGUOUS_VARIANT_IDENTITY
            result["ok"] = False
            return result
        sellable_salla = True
        payload["retailer_id"] = ident.retailer_id
        result["payload"] = payload
        body_rid = str(payload.get("retailer_id") or "").strip()
        if (
            body_rid != rid
            or body_rid != ident.retailer_id
            or body_rid.startswith("nahla_v_")
            or body_rid.startswith("nahla_p_")
        ):
            result["action"] = ACTION_BLOCK
            result["error"] = ERROR_AMBIGUOUS_VARIANT_IDENTITY
            result["ok"] = False
            return result

    if meta_product_id:
        already = str(getattr(parent, "meta_item_id", None) or "").strip()
        if already and already != str(meta_product_id).strip() and not sellable_salla:
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
        evidence = live_item_publication_evidence(
            db, tenant_id=int(tenant_id), catalog_id=catalog_id, retailer_id=rid,
            meta_product_id=str(meta_product_id), parent=parent,
        )
        if not evidence.get("owned"):
            # An earlier successful create of this product whose verification
            # lagged: this scoped, unique lookup may corroborate its recorded id.
            corroboration = corroborate_pending_publication(
                db, parent, variant, tenant_id=int(tenant_id), catalog_id=catalog_id, retailer_id=rid,
                live_meta_item_id=str(meta_product_id), salla_identity=ident if sellable_salla else None,
            )
            result["pending_publication"] = corroboration
            if corroboration.get("ok"):
                evidence = live_item_publication_evidence(
                    db, tenant_id=int(tenant_id), catalog_id=catalog_id, retailer_id=rid,
                    meta_product_id=str(meta_product_id), parent=parent,
                )
        result["ownership_evidence"] = evidence
        if not evidence.get("owned"):
            # a retailer_id match without publication evidence is a "match that
            # needs verification": never update someone else's (or an unproven) item
            return _ownership_block(result, lookup, str(meta_product_id), evidence)
        result["action"] = "update"
        result["meta_product_id"] = meta_product_id
        post_url = _graph_product_url(meta_product_id)
    else:
        blocked = None
        if not sellable_salla:
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
            if blocked.get("action") == ACTION_LINK and blocked.get("meta_product_id"):
                # linking to a live sibling adopts it: the same evidence rule applies
                evidence = live_item_publication_evidence(
                    db, tenant_id=int(tenant_id), catalog_id=catalog_id,
                    retailer_id=str(blocked.get("sibling_retailer_id") or ""),
                    meta_product_id=str(blocked.get("meta_product_id")), parent=parent,
                )
                result["ownership_evidence"] = evidence
                if not evidence.get("owned"):
                    return _ownership_block(result, result.get("lookup") or {}, str(blocked.get("meta_product_id")), evidence)
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


def retirement_evidence_refusal(
    evidence: Optional[Dict[str, Any]], *, catalog_id: str, retailer_id: str,
) -> Optional[str]:
    """Why *evidence* does not authorize a retirement write, or None.

    Retirement obeys the publish path's rule: only an item this path
    published may be modified. *evidence* is either the result of
    ``live_item_publication_evidence`` for the row (a publication-provenance
    membership for this tenant, catalog and retailer_id) or the ledger copy of
    that same evidence, taken in the transaction that deleted the row. It must
    be owned, name a Graph item id, and be for this catalog and retailer_id.
    """
    if not isinstance(evidence, dict) or not evidence.get("owned"):
        return ERROR_LIVE_MATCH_OWNERSHIP_UNVERIFIED
    if not str(evidence.get("meta_product_id") or "").strip():
        return ERROR_LIVE_MATCH_OWNERSHIP_UNVERIFIED
    if str(evidence.get("catalog_id") or "").strip() != str(catalog_id or "").strip():
        return ERROR_LIVE_MATCH_OWNERSHIP_UNVERIFIED
    if str(evidence.get("retailer_id") or "").strip() != str(retailer_id or "").strip():
        return ERROR_LIVE_MATCH_OWNERSHIP_UNVERIFIED
    return None


def retire_meta_catalog_item(
    conn: Any,
    catalog_id: str,
    retailer_id: str,
    meta_item_id: Optional[str] = None,
    *,
    publication_evidence: Optional[Dict[str, Any]] = None,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """Withdraw one live catalog item from the channel without deleting it.

    Sets ``availability=out of stock`` and ``visibility=staging`` on the
    existing Graph item, then re-reads it. Never issues a Graph DELETE and
    never touches an item whose retailer_id is not in this catalog.
    Returns ``action=absent`` when the item does not exist in Graph.

    Every write requires *publication_evidence* (see
    ``retirement_evidence_refusal``) for the connection's current catalog, and
    the live Graph item id must equal the evidenced id. Without it nothing is
    read or written and the result is ``block_ownership_unverified``, exactly
    as the publish path refuses to update an item it cannot prove it
    published.
    """
    rid = str(retailer_id or "").strip()
    cid = str(catalog_id or "").strip() or str(getattr(conn, "meta_catalog_id", "") or "").strip()
    result: Dict[str, Any] = {
        "ok": False,
        "action": None,
        "catalog_id": cid or None,
        "retailer_id": rid,
        "meta_product_id": str(meta_item_id or "").strip() or None,
        "visibility_applied": None,
        "verified": False,
        "meta": {"http_status": None, "response": None},
        "error": None,
    }
    if not rid:
        result["error"] = "retailer_id_missing"
        return result
    if not cid:
        result["error"] = "catalog_id_missing"
        return result
    refusal = retirement_evidence_refusal(publication_evidence, catalog_id=cid, retailer_id=rid)
    if refusal:
        result["action"] = ACTION_BLOCK_OWNERSHIP
        result["error"] = refusal
        result["ownership_evidence"] = publication_evidence if isinstance(publication_evidence, dict) else None
        return result
    owned_mid = str(publication_evidence["meta_product_id"]).strip()
    if str(meta_item_id or "").strip() and str(meta_item_id).strip() != owned_mid:
        result["error"] = "meta_item_id_mismatch"
        return result
    meta_item_id = owned_mid
    result["meta_product_id"] = owned_mid
    from services.whatsapp_catalog_sync_scope import SCOPE_BLOCKER_CODE, tenant_in_sync_scope  # noqa: PLC0415

    if not tenant_in_sync_scope(int(getattr(conn, "tenant_id", 0) or 0)):
        result["action"] = "scope_excluded"
        result["error"] = SCOPE_BLOCKER_CODE
        return result
    try:
        current_cid, token = _resolve_catalog_and_token(conn, require_catalog_readable=True)
    except MetaCatalogPushError as exc:
        result["error"] = exc.code
        return result
    if str(current_cid or "").strip() != cid:
        # The token and the claim guard belong to the connection's current
        # catalog; an item in a catalog this tenant no longer holds is not
        # provably ours to modify.
        result["error"] = "catalog_not_current"
        return result

    def _lookup(fields: str) -> Tuple[Optional[str], Dict[str, Any]]:
        return find_meta_catalog_item_by_retailer_id(conn, cid, rid, client=client, fields=fields)

    meta_id, lookup = _lookup(RETIRE_LOOKUP_FIELDS)
    if lookup.get("error") and lookup.get("http_status") == 400:
        # ``visibility`` not readable on this Graph version: fall back to the
        # verified field set and record that visibility was not provable.
        meta_id, lookup = _lookup(GRAPH_FIELDS)
    if lookup.get("error"):
        result["error"] = "lookup_failed"
        result["lookup"] = lookup
        return result
    if not meta_id:
        result["ok"] = True
        result["action"] = "absent"
        result["verified"] = True
        return result
    stored = str(meta_item_id or "").strip()
    if stored and stored != str(meta_id):
        result["error"] = "meta_item_id_mismatch"
        result["meta_product_id"] = str(meta_id)
        return result
    result["meta_product_id"] = str(meta_id)

    body: Dict[str, Any] = {
        "availability": RETIRED_AVAILABILITY,
        "visibility": RETIRED_VISIBILITY,
    }
    status_code, response = _post_catalog_item(_graph_product_url(str(meta_id)), token, body, client=client)
    result["meta"]["http_status"] = status_code
    result["meta"]["response"] = response
    visibility_applied = True
    if status_code >= 400 or (isinstance(response, dict) and response.get("error")):
        code, _sub, message = graph_error_code(response)
        if code == 100 and "visibility" in message.lower():
            visibility_applied = False
            body = {"availability": RETIRED_AVAILABILITY}
            status_code, response = _post_catalog_item(
                _graph_product_url(str(meta_id)), token, body, client=client,
            )
            result["meta"]["http_status"] = status_code
            result["meta"]["response"] = response
        if status_code >= 400 or (isinstance(response, dict) and response.get("error")):
            result["error"] = "meta_http_error"
            return result
    result["action"] = "retire_update"
    result["visibility_applied"] = visibility_applied

    _verify_id, verify = _lookup(RETIRE_LOOKUP_FIELDS if visibility_applied else GRAPH_FIELDS)
    item = verify.get("item") if isinstance(verify.get("item"), dict) else {}
    live_av = str(item.get("availability") or "").strip().lower().replace("_", " ")
    if verify.get("error") or not verify.get("matched"):
        result["error"] = "verification_failed"
        result["lookup"] = verify
        return result
    if live_av != RETIRED_AVAILABILITY:
        result["error"] = "verification_failed"
        result["lookup"] = verify
        return result
    live_vis = str(item.get("visibility") or "").strip().lower()
    if visibility_applied and live_vis and live_vis != RETIRED_VISIBILITY:
        result["error"] = "verification_failed"
        result["lookup"] = verify
        return result
    result["ok"] = True
    result["verified"] = True
    result["lookup"] = verify
    logger.info(
        "[META_CATALOG_RETIRE] tenant=%s retailer_id=%s catalog=%s meta_id=%s visibility=%s",
        getattr(conn, "tenant_id", None),
        rid,
        cid,
        meta_id,
        RETIRED_VISIBILITY if visibility_applied else "unchanged",
    )
    return result


def _prepare_salla_batch_membership_slot(db: Any, tenant_id: int, retailer_id: str) -> None:
    """Durable local identity before a confirmed batch CREATE."""
    from services.salla_variant_catalog_identity import (  # noqa: PLC0415
        AmbiguousVariantIdentity,
        ERROR_AMBIGUOUS_VARIANT_IDENTITY,
        ensure_variant_membership_slot,
        identity_for_retailer_id,
        is_salla_source,
    )

    try:
        parent, variant = load_variant_for_push(db, tenant_id, retailer_id=retailer_id)
    except MetaCatalogPushError:
        return
    if not is_salla_source(parent):
        return
    variants = _parent_variants_for_gate(db, parent, variant, tenant_id)
    try:
        ident = identity_for_retailer_id(parent, variants, retailer_id)
    except AmbiguousVariantIdentity as exc:
        raise MetaCatalogPushError(
            ERROR_AMBIGUOUS_VARIANT_IDENTITY, ERROR_AMBIGUOUS_VARIANT_IDENTITY,
        ) from exc
    if ident is None:
        raise MetaCatalogPushError(
            ERROR_AMBIGUOUS_VARIANT_IDENTITY, ERROR_AMBIGUOUS_VARIANT_IDENTITY,
        )
    conn = _resolve_connection(db, tenant_id)
    catalog_id, _token = _resolve_catalog_and_token(conn, require_catalog_readable=False)
    slot = ensure_variant_membership_slot(
        db,
        tenant_id=int(tenant_id),
        catalog_id=catalog_id,
        identity=ident,
    )
    if not slot.get("ok"):
        raise MetaCatalogPushError(
            ERROR_AMBIGUOUS_VARIANT_IDENTITY,
            str(slot.get("reason") or slot.get("error") or ERROR_AMBIGUOUS_VARIANT_IDENTITY),
        )
    db.commit()


def _stamp_salla_batch_membership(
    db: Any,
    tenant_id: int,
    retailer_id: str,
    meta_item_id: str,
    catalog_id: str,
    *,
    action: str = "",
    client: Optional[httpx.Client] = None,
) -> None:
    """Publication evidence after a successful create/update POST (Salla and native)."""
    from core.meta_catalog_membership import PROVENANCE_NATIVE_PUSH_RECONCILED  # noqa: PLC0415
    from services.salla_variant_catalog_identity import (  # noqa: PLC0415
        AmbiguousVariantIdentity,
        ERROR_AMBIGUOUS_VARIANT_IDENTITY,
        PROVENANCE_VARIANT_PUSH,
        identity_for_retailer_id,
        is_salla_source,
        replace_stale_observation_after_create,
        upsert_native_publication_membership,
        upsert_variant_membership,
    )

    mid = (meta_item_id or "").strip()
    if not mid:
        return
    try:
        parent, variant = load_variant_for_push(db, tenant_id, retailer_id=retailer_id)
    except MetaCatalogPushError:
        return
    cid = (catalog_id or "").strip()
    if not cid:
        conn = _resolve_connection(db, tenant_id)
        cid, _token = _resolve_catalog_and_token(conn, require_catalog_readable=False)
    salla = is_salla_source(parent)
    ident = None
    if not salla:
        bound = upsert_native_publication_membership(
            db,
            tenant_id=int(tenant_id),
            catalog_id=cid,
            retailer_id=retailer_id,
            product_id=int(parent.id),
            variant_id=getattr(variant, "id", None),
            meta_item_id=mid,
        )
    else:
        variants = _parent_variants_for_gate(db, parent, variant, tenant_id)
        try:
            ident = identity_for_retailer_id(parent, variants, retailer_id)
        except AmbiguousVariantIdentity:
            return
        if ident is None:
            return
        bound = upsert_variant_membership(
            db,
            tenant_id=int(tenant_id),
            catalog_id=cid,
            identity=ident,
            meta_item_id=mid,
        )
    if not bound.get("ok") and bound.get("reason") == "meta_item_id_immutable" and action == "create":
        # A verified create over a stale reconcile observation of the same key:
        # corroborate the created id with a lookup scoped to this tenant's
        # connection catalog and retailer_id, then apply the narrow repair.
        conn = _resolve_connection(db, tenant_id)
        current_cid = str(getattr(conn, "meta_catalog_id", "") or "").strip()
        live_id, _lookup = (None, {})
        if current_cid == cid:
            live_id, _lookup = find_meta_catalog_item_by_retailer_id(conn, cid, retailer_id, client=client)
        bound = replace_stale_observation_after_create(
            db,
            tenant_id=int(tenant_id),
            catalog_id=cid,
            retailer_id=retailer_id,
            product_id=int(parent.id),
            variant_id=None if salla else getattr(variant, "id", None),
            created_meta_item_id=mid,
            corroborated_meta_item_id=str(live_id or ""),
            publication_provenance=PROVENANCE_VARIANT_PUSH if salla else PROVENANCE_NATIVE_PUSH_RECONCILED,
            salla_identity=ident,
        )
    if not bound.get("ok"):
        raise MetaCatalogPushError(
            ERROR_AMBIGUOUS_VARIANT_IDENTITY,
            str(bound.get("reason") or bound.get("error") or ERROR_AMBIGUOUS_VARIANT_IDENTITY),
        )
    db.commit()


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
            _prepare_salla_batch_membership_slot(db, int(tenant_id), rid)
            push_result = push_one_meta_catalog_item(
                db,
                int(tenant_id),
                rid,
                confirm=True,
                client=client,
            )
            if push_result.get("ok") and str(push_result.get("action") or "") in ("create", "update"):
                # a LINK result adopts an existing item; only a real create/update
                # POST may write the publication-provenance membership
                _stamp_salla_batch_membership(
                    db,
                    int(tenant_id),
                    rid,
                    str(push_result.get("meta_product_id") or ""),
                    str(push_result.get("catalog_id") or ""),
                    action=str(push_result.get("action") or ""),
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
    "graph_error_code",
    "retire_meta_catalog_item",
    "retirement_evidence_refusal",
    "RETIRED_AVAILABILITY",
    "RETIRED_VISIBILITY",
    "PUBLISHED_VISIBILITY",
    "existing_identity_retailer_id",
    "find_meta_catalog_item_by_retailer_id",
    "load_variant_for_push",
    "parent_would_create_in_meta",
    "push_one_meta_catalog_item",
    "push_ready_meta_catalog_batch",
    "live_item_publication_evidence",
    "PENDING_PUBLICATIONS_KEY",
    "corroborate_pending_publication",
    "pending_publication_record",
    "with_pending_publication",
    "without_pending_publications",
    "PUBLICATION_PROVENANCES",
    "ERROR_LIVE_MATCH_OWNERSHIP_UNVERIFIED",
    "ACTION_BLOCK_OWNERSHIP",
    "REASON_NO_PUBLICATION_EVIDENCE",
]
