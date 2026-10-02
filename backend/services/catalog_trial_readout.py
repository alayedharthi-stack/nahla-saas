"""
services/catalog_trial_readout.py
─────────────────────────────────
Read-only readout of one tenant's WhatsApp-catalog trial preconditions.

Built for the operator who must prove, before a limited publish trial,
what the store actually holds: every product with its variants and
channel identities, the WhatsApp connection and its catalog binding, the
plan entitlement, the publish readiness blocker, and (opt-in) what Meta
Graph reports about the WABA ↔ catalog link, the token's catalog
permission and the live catalog items. It also proposes the trial's
product list from explicit criteria and the exact scope environment.

Guarantees:
- No database write, no Graph POST. Graph reads happen only with
  ``include_graph=True`` and only GET endpoints are used.
- No secret leaves the process: tokens are never placed in the result
  (only their *source* label), and the result is scrubbed for any value
  equal to a token that was read.
- Platform-wide: nothing here is specific to one merchant; the tenant id
  is an argument.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger("nahla.catalog_trial_readout")

ANOMALY_NO_VARIANTS = "no_variant_rows"
ANOMALY_VARIANT_WITHOUT_RETAILER_ID = "variant_without_retailer_id"
ANOMALY_DUPLICATE_RETAILER_ID = "duplicate_retailer_id"
ANOMALY_PRICE_NOT_NUMERIC = "price_not_numeric"
ANOMALY_MISSING_IMAGE = "missing_image"
ANOMALY_MISSING_URL = "missing_url"

_NUMERIC_RE = re.compile(r"^\d+(\.\d+)?$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _strip(value: Any) -> str:
    return str(value or "").strip()


def _meta(product: Any) -> Dict[str, Any]:
    meta = getattr(product, "extra_metadata", None)
    return meta if isinstance(meta, dict) else {}


def _product_anomalies(product: Any, variants: List[Any]) -> List[str]:
    out: List[str] = []
    meta = _meta(product)
    if not variants:
        out.append(ANOMALY_NO_VARIANTS)
    seen: Set[str] = set()
    for v in variants:
        rid = _strip(getattr(v, "retailer_id", None))
        if not rid:
            out.append(ANOMALY_VARIANT_WITHOUT_RETAILER_ID)
            continue
        if rid in seen and ANOMALY_DUPLICATE_RETAILER_ID not in out:
            out.append(ANOMALY_DUPLICATE_RETAILER_ID)
        seen.add(rid)
    price = _strip(getattr(product, "price", None))
    if price and not _NUMERIC_RE.match(price):
        out.append(ANOMALY_PRICE_NOT_NUMERIC)
    if not _strip(meta.get("image_url")) and not _strip(getattr(product, "image_url", None)):
        out.append(ANOMALY_MISSING_IMAGE)
    if not _strip(meta.get("product_url")) and not _strip(getattr(product, "product_url", None)):
        out.append(ANOMALY_MISSING_URL)
    # keep order stable, drop duplicates
    deduped: List[str] = []
    for a in out:
        if a not in deduped:
            deduped.append(a)
    return deduped


def _product_entry(product: Any, variants: List[Any], memberships: List[Any]) -> Dict[str, Any]:
    from core.catalog import (  # noqa: PLC0415
        is_whatsapp_channel_publish_eligible,
        source_platform_status,
        whatsapp_channel_publish_rejection_detail,
    )

    meta = _meta(product)
    eligible = bool(is_whatsapp_channel_publish_eligible(product))
    rejection = None if eligible else (whatsapp_channel_publish_rejection_detail(product) or {}).get("error_code")
    return {
        "product_id": int(product.id),
        "external_id": _strip(getattr(product, "external_id", None)) or None,
        "title": _strip(getattr(product, "title", None)),
        "source": _strip(getattr(product, "source", None)) or None,
        "ownership_mode": _strip(getattr(product, "ownership_mode", None)) or None,
        "catalog_status": _strip(getattr(product, "catalog_status", None)) or None,
        "merchant_hidden": getattr(product, "merchant_hidden_at", None) is not None,
        "in_stock": bool(getattr(product, "in_stock", False)),
        "stock_quantity": getattr(product, "stock_quantity", None),
        "price": _strip(getattr(product, "price", None)) or None,
        "currency": _strip(meta.get("currency")) or None,
        "source_status": source_platform_status(product) or None,
        "source_event_at": meta.get("source_event_at"),
        "sync_status": _strip(getattr(product, "sync_status", None)) or None,
        "meta_item_id_present": bool(_strip(getattr(product, "meta_item_id", None))),
        "last_synced_at": getattr(product, "last_synced_at", None).isoformat()
        if getattr(product, "last_synced_at", None) else None,
        "has_image": bool(_strip(meta.get("image_url")) or _strip(getattr(product, "image_url", None))),
        "has_url": bool(_strip(meta.get("product_url")) or _strip(getattr(product, "product_url", None))),
        "variant_count": len(variants),
        "variants": [
            {
                "variant_id": int(v.id),
                "salla_variant_id": _strip(getattr(v, "salla_variant_id", None)) or None,
                "retailer_id": _strip(getattr(v, "retailer_id", None)) or None,
                "in_stock": getattr(v, "in_stock", None),
                "stock_quantity": getattr(v, "stock_quantity", None),
                "price": _strip(getattr(v, "price", None)) or None,
            }
            for v in variants
        ],
        "membership_count": len(memberships),
        "membership_catalog_ids": sorted({_strip(m.catalog_id) for m in memberships if _strip(m.catalog_id)}),
        "publish_eligible": eligible,
        "publish_rejection": rejection,
        "anomalies": _product_anomalies(product, variants),
    }


def _select_candidates(products: List[Dict[str, Any]], count: int) -> Dict[str, Any]:
    """Pick trial products by explicit criteria.

    Order: (a) one single-variant product, (b) one multi-variant product
    (proves ``item_group_id``), (c) further in-stock products. Only rows
    that are publish-eligible, in stock, with image and url, from an
    external platform source and with no anomaly qualify.
    """
    pool = [
        p for p in products
        if p["publish_eligible"] and p["in_stock"] and p["has_image"] and p["has_url"]
        and p["external_id"] and not p["anomalies"] and p["variant_count"] > 0
    ]
    pool.sort(key=lambda p: p["product_id"])
    chosen: List[Dict[str, Any]] = []
    chosen_ids: Set[int] = set()
    reasons: List[str] = []

    def _take(pred, label: str) -> None:
        for p in pool:
            if p["product_id"] in chosen_ids:
                continue
            if pred(p):
                chosen.append(dict(p, selection_role=label))
                chosen_ids.add(p["product_id"])
                return
        reasons.append(f"no_candidate_for:{label}")

    if count >= 1:
        _take(lambda p: p["variant_count"] == 1, "single_variant")
    if count >= 2:
        _take(lambda p: p["variant_count"] >= 2, "multi_variant")
    while len(chosen) < count:
        before = len(chosen)
        _take(lambda p: True, "in_stock_with_media")
        if len(chosen) == before:
            break
    return {
        "criteria": [
            "publish_eligible", "in_stock", "has_image", "has_url", "external_platform_source",
            "no_anomalies", "roles: single_variant, multi_variant, in_stock_with_media",
        ],
        "eligible_pool_size": len(pool),
        "requested": int(count),
        "selected": [
            {
                "product_id": p["product_id"],
                "external_id": p["external_id"],
                "title": p["title"],
                "role": p["selection_role"],
                "variant_count": p["variant_count"],
                "retailer_ids": [v["retailer_id"] for v in p["variants"] if v["retailer_id"]],
            }
            for p in chosen
        ],
        "selection_complete": len(chosen) >= count,
        "shortfall_reasons": reasons,
    }


def _scrub(value: Any, secrets: Iterable[str]) -> Any:
    """Remove any value equal to a known secret and any key named like a token."""
    secret_set = {s for s in secrets if s}
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            low = key.lower()
            if low in ("token", "access_token", "refresh_token", "token_tail") or (
                low.endswith("_token") and isinstance(v, str)
            ):
                out[key] = "<redacted>"
                continue
            out[key] = _scrub(v, secret_set)
        return out
    if isinstance(value, list):
        return [_scrub(v, secret_set) for v in value]
    if isinstance(value, str):
        for s in secret_set:
            if s and s in value:
                return "<redacted>"
    return value


def build_catalog_trial_readout(
    db: Any,
    tenant_id: int,
    *,
    include_graph: bool = False,
    candidate_count: int = 3,
    client: Any = None,
) -> Dict[str, Any]:
    """Read-only readout; see module docstring."""
    from models import (  # noqa: PLC0415
        MetaCatalogMembership,
        Product,
        ProductVariant,
        Tenant,
        WhatsAppConnection,
    )
    from services.whatsapp_catalog_sync_scope import (  # noqa: PLC0415
        scope_description,
        tenant_in_sync_scope,
    )

    tid = int(tenant_id)
    secrets: Set[str] = set()
    out: Dict[str, Any] = {
        "tenant_id": tid,
        "generated_at": _now_iso(),
        "read_only": True,
        "graph_reads_included": bool(include_graph),
        "secrets_included": False,
    }

    tenant = db.get(Tenant, tid)
    out["tenant"] = {
        "exists": tenant is not None,
        "name": _strip(getattr(tenant, "name", None)) or None,
        "is_active": bool(getattr(tenant, "is_active", False)) if tenant is not None else None,
    }

    # ── products ────────────────────────────────────────────────────────
    rows = db.query(Product).filter(Product.tenant_id == tid).order_by(Product.id).all()
    variants_by_pid: Dict[int, List[Any]] = {}
    for v in db.query(ProductVariant).filter(ProductVariant.tenant_id == tid).order_by(ProductVariant.id).all():
        variants_by_pid.setdefault(int(v.product_id), []).append(v)
    members_by_pid: Dict[int, List[Any]] = {}
    for m in db.query(MetaCatalogMembership).filter(MetaCatalogMembership.tenant_id == tid).all():
        if m.product_id is not None:
            members_by_pid.setdefault(int(m.product_id), []).append(m)
    products = [
        _product_entry(p, variants_by_pid.get(int(p.id), []), members_by_pid.get(int(p.id), []))
        for p in rows
    ]
    out["products"] = products
    out["product_counts"] = {
        "total": len(products),
        "by_source": _count_by(products, "source"),
        "publish_eligible": sum(1 for p in products if p["publish_eligible"]),
        "hidden_at_source": sum(1 for p in products if p["source_status"] in ("hidden", "deleted")),
        "with_anomalies": sum(1 for p in products if p["anomalies"]),
        "stamped_source_event_at": sum(1 for p in products if p["source_event_at"]),
        "with_memberships": sum(1 for p in products if p["membership_count"]),
        "variants_total": sum(p["variant_count"] for p in products),
    }
    out["anomalies"] = [
        {"product_id": p["product_id"], "external_id": p["external_id"], "anomalies": p["anomalies"]}
        for p in products if p["anomalies"]
    ]

    # ── connection ───────────────────────────────────────────────────────
    conn = db.query(WhatsAppConnection).filter(WhatsAppConnection.tenant_id == tid).first()
    token_pick: Dict[str, Any] = {}
    if conn is not None:
        try:
            from services.meta_catalog_import import _select_graph_token  # noqa: PLC0415

            token_pick = _select_graph_token(conn) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[trial_readout] token selection failed tenant=%s err=%s", tid, type(exc).__name__)
            token_pick = {"token_source": "unavailable"}
        if token_pick.get("token"):
            secrets.add(str(token_pick["token"]))
        secrets.add(_strip(getattr(conn, "access_token", None)))
    extra = getattr(conn, "extra_metadata", None) if conn is not None else None
    out["connection"] = {
        "exists": conn is not None,
        "provider": _strip(getattr(conn, "provider", None)) or None,
        "connection_type": _strip(getattr(conn, "connection_type", None)) or None,
        "waba_id": _strip(getattr(conn, "whatsapp_business_account_id", None)) or None,
        "phone_number_id": _strip(getattr(conn, "phone_number_id", None)) or None,
        "catalog_enabled": bool(getattr(conn, "catalog_enabled", False)) if conn is not None else None,
        "meta_catalog_id": _strip(getattr(conn, "meta_catalog_id", None)) or None,
        "has_merchant_access_token": bool(_strip(getattr(conn, "access_token", None))) if conn is not None else None,
        "graph_token_source": _strip(token_pick.get("token_source")) or None,
        "extra_metadata_keys": sorted(extra.keys()) if isinstance(extra, dict) else [],
    }

    # ── entitlement ──────────────────────────────────────────────────────
    try:
        from core.plan_entitlements import get_entitlements  # noqa: PLC0415

        ent = get_entitlements(db, tid, strict_lookup=True)
        out["entitlement"] = {
            "plan_slug": getattr(ent, "plan_slug", None),
            "is_active": bool(getattr(ent, "is_active", False)),
            "is_blocked": bool(getattr(ent, "is_blocked", False)),
            "meta_catalog_sync": bool(ent.has_feature("meta_catalog_sync")),
        }
    except Exception as exc:  # noqa: BLE001
        out["entitlement"] = {"error": type(exc).__name__}

    # ── readiness + scope ────────────────────────────────────────────────
    try:
        from services.whatsapp_catalog_sync import evaluate_whatsapp_catalog_sync_readiness  # noqa: PLC0415

        readiness = evaluate_whatsapp_catalog_sync_readiness(db, tid)
        out["readiness"] = {
            "ready": bool(readiness.get("ready")),
            "blocker_code": readiness.get("blocker_code"),
        }
    except Exception as exc:  # noqa: BLE001
        out["readiness"] = {"error": type(exc).__name__}
    out["sync_scope"] = dict(scope_description(), tenant_in_scope=tenant_in_sync_scope(tid))

    # ── candidates + proposed env ────────────────────────────────────────
    selection = _select_candidates(products, int(candidate_count))
    chosen_ids = [c["product_id"] for c in selection["selected"]]
    selection["expected_meta_items"] = sum(c["variant_count"] for c in selection["selected"])
    selection["proposed_env"] = {
        "NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS": str(tid),
        "NAHLA_WHATSAPP_CATALOG_SYNC_PRODUCT_IDS": ",".join(f"{tid}:{pid}" for pid in chosen_ids),
    }
    out["trial_candidates"] = selection

    # ── Graph (GET only, opt-in) ─────────────────────────────────────────
    if include_graph:
        out["graph"] = _graph_section(db, tid, conn, token_pick, chosen_ids, client=client)

    # ── missing requirements ─────────────────────────────────────────────
    out["missing_requirements"] = _missing_requirements(out)

    scrubbed = _scrub(out, secrets)
    scrubbed["secrets_included"] = False
    return scrubbed


def _count_by(items: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for it in items:
        k = str(it.get(key) or "unknown")
        out[k] = out.get(k, 0) + 1
    return out


def _graph_section(
    db: Any,
    tid: int,
    conn: Any,
    token_pick: Dict[str, Any],
    chosen_ids: List[int],
    *,
    client: Any,
) -> Dict[str, Any]:
    section: Dict[str, Any] = {"reads": []}
    if conn is None:
        section["skipped"] = "connection_not_found"
        return section
    token = _strip(token_pick.get("token"))
    if not token:
        section["skipped"] = "no_graph_token"
        return section
    waba_id = _strip(getattr(conn, "whatsapp_business_account_id", None))
    expected_catalog = _strip(getattr(conn, "meta_catalog_id", None))

    # 1. link status (GET /{waba}/product_catalogs)
    try:
        from services.meta_catalog_linking import (  # noqa: PLC0415
            fetch_waba_owner_business_id,
            get_waba_catalog_link_status,
        )

        link = get_waba_catalog_link_status(db, tid)
        section["waba_link"] = {
            k: link.get(k)
            for k in (
                "ok", "connected", "link_status", "waba_id", "expected_catalog_id",
                "expected_catalog_linked", "linked_catalogs", "linked_catalog_ids",
                "catalog_exists", "token_source", "error", "error_category", "http_status",
            )
            if k in link
        }
        section["reads"].append(f"GET /{waba_id}/product_catalogs")
        owner = fetch_waba_owner_business_id(waba_id, token, client=client)
        section["waba_owner_business"] = {
            k: owner.get(k) for k in ("ok", "business_id", "business_name", "error") if k in owner
        }
        section["reads"].append(f"GET /{waba_id}?fields=owner_business_info")
    except Exception as exc:  # noqa: BLE001
        section["waba_link"] = {"error": type(exc).__name__}

    # 2. token permission (GET /me/permissions)
    try:
        from services.meta_catalog_onboarding import _catalog_management_granted  # noqa: PLC0415

        granted = _catalog_management_granted(token, client=client)
        section["token_catalog_management"] = {
            "granted": granted,
            "verdict": "granted" if granted is True else ("missing" if granted is False else "unproven"),
        }
        section["reads"].append("GET /me/permissions")
    except Exception as exc:  # noqa: BLE001
        section["token_catalog_management"] = {"granted": None, "verdict": "unproven", "error": type(exc).__name__}

    # 3. catalogs readable (GET /{catalog}?fields=...)
    try:
        from services.meta_catalog_access import probe_catalog_readable  # noqa: PLC0415

        ids: List[str] = []
        if expected_catalog:
            ids.append(expected_catalog)
        for cid in (section.get("waba_link") or {}).get("linked_catalog_ids") or []:
            if cid not in ids:
                ids.append(str(cid))
        probes = []
        for cid in ids:
            probe = probe_catalog_readable(token, cid, client=client)
            probes.append({k: probe.get(k) for k in (
                "ok", "catalog_id", "name", "product_count", "business_id", "error", "error_code", "http_status",
            )})
            section["reads"].append(f"GET /{cid}?fields=id,name,product_count,business")
        section["catalogs"] = probes
        owner_bm = _strip((section.get("waba_owner_business") or {}).get("business_id"))
        section["catalog_business_matches_waba_owner"] = {
            p["catalog_id"]: (bool(owner_bm) and _strip(p.get("business_id")) == owner_bm) if p.get("ok") else None
            for p in probes
        }
    except Exception as exc:  # noqa: BLE001
        section["catalogs"] = [{"error": type(exc).__name__}]

    # 4. live items for the candidates (GET /{catalog}/products)
    if not expected_catalog:
        section["live_items"] = {"skipped": "catalog_id_missing"}
        return section
    try:
        from services.meta_catalog_readiness import build_meta_catalog_readiness_report  # noqa: PLC0415

        report = build_meta_catalog_readiness_report(
            db, tid, include_meta_live_read=True, client=client,
        )
        data = report.to_dict()
        wanted = set(int(x) for x in chosen_ids)
        plan: Dict[str, List[Dict[str, Any]]] = {"create": [], "update": [], "noop": [], "skip": []}
        for item in data.get("items") or []:
            if int(item.get("product_id") or 0) not in wanted:
                continue
            action = str(item.get("action_needed") or "skip")
            plan.setdefault(action, []).append({
                "product_id": item.get("product_id"),
                "retailer_id": item.get("retailer_id"),
                "status": item.get("status"),
                "reasons": item.get("reasons"),
                "meta_product_id": item.get("meta_product_id"),
            })
        section["live_items"] = {
            "counts": data.get("counts"),
            "meta_fetch": data.get("meta_fetch"),
            "error": data.get("error"),
            "candidate_plan": plan,
            "candidate_create": len(plan["create"]),
            "candidate_update": len(plan["update"]),
            "candidate_noop": len(plan["noop"]),
            "candidate_skip": len(plan.get("skip", [])),
        }
        section["reads"].append(f"GET /{expected_catalog}/products")
    except Exception as exc:  # noqa: BLE001
        section["live_items"] = {"error": type(exc).__name__}
    return section


def _missing_requirements(out: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    conn = out.get("connection") or {}
    if not conn.get("exists"):
        missing.append("whatsapp_connection")
        return missing
    if not conn.get("catalog_enabled"):
        missing.append("catalog_enabled")
    if not conn.get("meta_catalog_id"):
        missing.append("meta_catalog_id")
    if not conn.get("graph_token_source") or conn.get("graph_token_source") in ("none", "unavailable"):
        missing.append("graph_token")
    ent = out.get("entitlement") or {}
    if ent.get("error") or not ent.get("meta_catalog_sync"):
        missing.append("entitlement_meta_catalog_sync")
    if not (out.get("trial_candidates") or {}).get("selection_complete"):
        missing.append("trial_candidates")
    graph = out.get("graph")
    if graph is None:
        missing.append("graph_reads_not_run")
    else:
        perm = (graph.get("token_catalog_management") or {}).get("verdict")
        if perm != "granted":
            missing.append("token_catalog_management")
        link = graph.get("waba_link") or {}
        if not link.get("expected_catalog_linked"):
            missing.append("waba_catalog_link")
        matches = graph.get("catalog_business_matches_waba_owner") or {}
        expected = conn.get("meta_catalog_id")
        if expected and matches.get(expected) is not True:
            missing.append("catalog_business_ownership")
    return missing


__all__ = ["build_catalog_trial_readout"]
