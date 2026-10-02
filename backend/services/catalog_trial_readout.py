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
ANOMALY_PARENT_IN_STOCK_VARIANTS_OUT = "parent_in_stock_but_all_variants_out_of_stock"
ANOMALY_PARENT_OUT_VARIANT_IN_STOCK = "parent_out_of_stock_but_variant_in_stock"
STOCK_CONSISTENCY_ANOMALIES = frozenset({ANOMALY_PARENT_IN_STOCK_VARIANTS_OUT, ANOMALY_PARENT_OUT_VARIANT_IN_STOCK})

CANDIDATE_ROLES = ("single_variant", "multi_variant", "in_stock_with_media")

_NUMERIC_RE = re.compile(r"^\d+(\.\d+)?$")

# ── compatibility with the deployed code version ─────────────────────────
# The readout is meant to run against production *before* this branch is
# deployed (bundled as a standalone file, see scripts/operators/
# catalog_trial_readout_standalone.py). Helpers that only exist on this
# branch therefore have inline fallbacks; nothing else is imported from it.


def _source_platform_status_fallback(product: Any) -> str:
    meta = product.get("extra_metadata") if isinstance(product, dict) else getattr(product, "extra_metadata", None)
    if not isinstance(meta, dict):
        return ""
    raw = str(meta.get("source_status") or "").strip().lower()
    return raw or str(meta.get("status") or "").strip().lower()


def _source_platform_status(product: Any) -> str:
    try:
        from core.catalog import source_platform_status  # noqa: PLC0415
    except ImportError:
        return _source_platform_status_fallback(product)
    return source_platform_status(product)


def _scope_snapshot(tenant_id: int) -> Dict[str, Any]:
    """Current trial scope as the deployed code sees it (fallback parses the env)."""
    try:
        from services.whatsapp_catalog_sync_scope import (  # noqa: PLC0415
            scope_description,
            tenant_in_sync_scope,
        )

        return dict(scope_description(), tenant_in_scope=tenant_in_sync_scope(tenant_id), source="module")
    except ImportError:
        import os  # noqa: PLC0415

        raw_t = os.environ.get("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", "")
        raw_p = os.environ.get("NAHLA_WHATSAPP_CATALOG_SYNC_PRODUCT_IDS", "")
        tenants = sorted({int(x) for x in raw_t.replace(";", ",").split(",") if x.strip().isdigit()})
        return {
            "active": bool(raw_t.strip()),
            "tenant_ids": tenants,
            "product_ids_raw": raw_p,
            "tenant_env": "NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS",
            "product_env": "NAHLA_WHATSAPP_CATALOG_SYNC_PRODUCT_IDS",
            "tenant_in_scope": (not raw_t.strip()) or int(tenant_id) in tenants,
            "source": "env_fallback_scope_module_not_deployed",
        }


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
    # Stock consistency between the parent row and its variant rows. A parent
    # that says "in stock" while every variant is out of stock cannot be sold
    # (the AI offers variants) and would publish 0 sellable items; the inverse
    # hides a sellable variant. Either way the product is not trial material
    # until the source data is understood.
    if variants:
        in_stock_variants = [v for v in variants if _variant_in_stock(v)]
        parent_in_stock = bool(getattr(product, "in_stock", False))
        if parent_in_stock and not in_stock_variants:
            out.append(ANOMALY_PARENT_IN_STOCK_VARIANTS_OUT)
        if not parent_in_stock and in_stock_variants:
            out.append(ANOMALY_PARENT_OUT_VARIANT_IN_STOCK)
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


def _variant_in_stock(variant: Any) -> bool:
    flag = getattr(variant, "in_stock", None)
    qty = getattr(variant, "stock_quantity", None)
    if flag is False:
        return False
    if qty is not None:
        try:
            return int(qty) > 0
        except (TypeError, ValueError):
            return bool(flag)
    return bool(flag) if flag is not None else True


def _product_entry(product: Any, variants: List[Any], memberships: List[Any]) -> Dict[str, Any]:
    from core.catalog import (  # noqa: PLC0415
        is_whatsapp_channel_publish_eligible,
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
        "source_status": _source_platform_status(product) or None,
        "source_event_at": meta.get("source_event_at"),
        "sync_status": _strip(getattr(product, "sync_status", None)) or None,
        "meta_item_id_present": bool(_strip(getattr(product, "meta_item_id", None))),
        "last_synced_at": getattr(product, "last_synced_at", None).isoformat()
        if getattr(product, "last_synced_at", None) else None,
        "has_image": bool(_strip(meta.get("image_url")) or _strip(getattr(product, "image_url", None))),
        "has_url": bool(_strip(meta.get("product_url")) or _strip(getattr(product, "product_url", None))),
        "variant_count": len(variants),
        "variants_in_stock_count": sum(1 for v in variants if _variant_in_stock(v)),
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


def _candidate_pool(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    pool = [
        p for p in products
        if p["publish_eligible"] and p["in_stock"] and p["has_image"] and p["has_url"]
        and p["external_id"] and not p["anomalies"] and p["variant_count"] > 0
    ]
    pool.sort(key=lambda p: p["product_id"])
    return pool


def _candidate_rejections(p: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    if not p["publish_eligible"]:
        reasons.append(f"not_publish_eligible:{p.get('publish_rejection') or 'unknown'}")
    if not p["in_stock"]:
        reasons.append("out_of_stock")
    if not p["has_image"]:
        reasons.append("missing_image")
    if not p["has_url"]:
        reasons.append("missing_url")
    if not p["external_id"]:
        reasons.append("no_external_id")
    if p["variant_count"] == 0:
        reasons.append("no_variant_rows")
    for a in p["anomalies"]:
        if a not in reasons:
            reasons.append(f"anomaly:{a}")
    return reasons


def _role_of(p: Dict[str, Any]) -> str:
    return "single_variant" if p["variant_count"] == 1 else "multi_variant"


def _select_candidates(
    products: List[Dict[str, Any]],
    count: int,
    *,
    preferred_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Pick trial products by explicit criteria, or evaluate the owner's picks.

    Automatic order: (a) one single-variant product, (b) one multi-variant
    product (proves ``item_group_id``), (c) further in-stock products. Only
    rows that are publish-eligible, in stock, with image and url, from an
    external platform source, with variant rows and with no anomaly qualify.

    ``selection_complete`` says whether *count* products were found;
    ``scenario_coverage`` says, separately, which trial roles the selection
    actually covers — a set with no single-variant product is complete in
    number but does not cover the single-item scenario.
    """
    pool = _candidate_pool(products)
    by_id = {p["product_id"]: p for p in products}
    chosen: List[Dict[str, Any]] = []
    chosen_ids: Set[int] = set()
    reasons: List[str] = []
    preferred_evaluation: List[Dict[str, Any]] = []

    if preferred_ids:
        for pid in preferred_ids:
            p = by_id.get(int(pid))
            if p is None:
                preferred_evaluation.append({"product_id": int(pid), "accepted": False, "reasons": ["not_found_for_tenant"]})
                continue
            rejections = _candidate_rejections(p)
            preferred_evaluation.append({
                "product_id": int(pid), "external_id": p["external_id"], "accepted": not rejections,
                "reasons": rejections, "variant_count": p["variant_count"],
            })
            if not rejections and p["product_id"] not in chosen_ids:
                chosen.append(dict(p, selection_role=_role_of(p)))
                chosen_ids.add(p["product_id"])
    else:
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

    roles_filled = sorted({c["selection_role"] for c in chosen} | (
        {"in_stock_with_media"} if chosen else set()
    ))
    missing_roles = [r for r in CANDIDATE_ROLES if r not in roles_filled]
    return {
        "criteria": [
            "publish_eligible", "in_stock", "has_image", "has_url", "external_platform_source",
            "variant_rows_present", "no_anomalies (incl. parent/variant stock consistency)",
            "roles: single_variant, multi_variant, in_stock_with_media",
        ],
        "mode": "preferred_ids" if preferred_ids else "automatic",
        "eligible_pool_size": len(pool),
        "eligible_pool_product_ids": [p["product_id"] for p in pool],
        "requested": int(count),
        "selected": [
            {
                "product_id": p["product_id"],
                "external_id": p["external_id"],
                "title": p["title"],
                "role": p["selection_role"],
                "variant_count": p["variant_count"],
                "variants_in_stock_count": p["variants_in_stock_count"],
                "retailer_ids": [v["retailer_id"] for v in p["variants"] if v["retailer_id"]],
            }
            for p in chosen
        ],
        "preferred_evaluation": preferred_evaluation,
        "selection_complete": len(chosen) >= count,
        "scenario_coverage": {
            "required_roles": list(CANDIDATE_ROLES),
            "roles_filled": roles_filled,
            "missing_roles": missing_roles,
            "coverage_complete": not missing_roles,
        },
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
            # secret-bearing keys: exact names, or *_token keys whose value is a
            # long opaque string (status words such as "unknown" are kept)
            if low in ("token", "access_token", "refresh_token", "input_token", "app_token", "token_tail") or (
                low.endswith("_token") and isinstance(v, str) and len(v) >= 16 and " " not in v
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
    candidate_ids: Optional[List[int]] = None,
    expected_business_id: Optional[str] = None,
    include_salla: bool = False,
    salla_adapter: Any = None,
) -> Dict[str, Any]:
    """Read-only readout; see module docstring.

    ``candidate_ids`` evaluates the owner's chosen products instead of the
    automatic pick; ``expected_business_id`` is the Business Manager that
    should own the WABA and its catalog; ``include_salla`` re-reads anomalous
    products from the store (GET only) to say whether the inconsistency is in
    Salla's data or in the local copy.
    """
    from models import (  # noqa: PLC0415
        MetaCatalogMembership,
        Product,
        ProductVariant,
        Tenant,
        WhatsAppConnection,
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
    out["sync_scope"] = _scope_snapshot(tid)

    # ── candidates + proposed env + publish payloads ─────────────────────
    selection = _select_candidates(products, int(candidate_count), preferred_ids=candidate_ids)
    chosen_ids = [c["product_id"] for c in selection["selected"]]
    selection["expected_meta_items"] = sum(c["variant_count"] for c in selection["selected"])
    selection["proposed_env"] = {
        "NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS": str(tid),
        "NAHLA_WHATSAPP_CATALOG_SYNC_PRODUCT_IDS": ",".join(f"{tid}:{pid}" for pid in chosen_ids),
    }
    out["trial_candidates"] = selection
    out["candidate_payloads"] = _candidate_payloads(db, tid, chosen_ids)

    # ── Salla re-read of anomalous products (GET only, opt-in) ───────────
    if include_salla:
        anomalous = [p for p in products if any(a in STOCK_CONSISTENCY_ANOMALIES for a in p["anomalies"])]
        out["salla_check"] = _salla_check(db, tid, anomalous, adapter=salla_adapter)

    # ── Graph (GET only, opt-in) ─────────────────────────────────────────
    if include_graph:
        out["graph"] = _graph_section(
            db, tid, conn, token_pick, chosen_ids, client=client,
            expected_business_id=_strip(expected_business_id) or None,
            candidate_retailer_ids=[rid for c in selection["selected"] for rid in c["retailer_ids"]],
        )

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


def _candidate_payloads(db: Any, tid: int, chosen_ids: List[int]) -> Dict[str, Any]:
    """Per-variant publish identity and payload preview for the chosen products (no Graph)."""
    if not chosen_ids:
        return {"items": [], "pushable": 0, "blocked": 0}
    try:
        from services.meta_catalog_readiness import build_meta_catalog_readiness_report  # noqa: PLC0415

        report = build_meta_catalog_readiness_report(db, tid)
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__, "items": [], "pushable": 0, "blocked": 0}
    wanted = {int(x) for x in chosen_ids}
    items: List[Dict[str, Any]] = []
    for it in report.to_dict().get("items") or []:
        if int(it.get("product_id") or 0) not in wanted:
            continue
        preview = it.get("payload_preview") or {}
        items.append({
            "product_id": it.get("product_id"),
            "variant_id": it.get("variant_id"),
            "salla_variant_id": it.get("salla_variant_id"),
            "retailer_id": it.get("retailer_id"),
            "item_group_id": it.get("item_group_id"),
            "status": it.get("status"),
            "reasons": it.get("reasons"),
            "name": preview.get("name") or it.get("generated_name"),
            "price": preview.get("price") or it.get("price"),
            "currency": preview.get("currency") or it.get("currency"),
            "availability": preview.get("availability") or it.get("availability"),
            "image_url_present": bool(preview.get("image_url")) or bool(it.get("image_url_present")),
            "url_present": bool(preview.get("url")) or bool(it.get("url_present")),
            "payload_fields": sorted(preview.keys()),
        })
    pushable = sum(1 for i in items if i["status"] in ("ready", "warn"))
    return {"items": items, "pushable": pushable, "blocked": len(items) - pushable,
            "counts_all_products": report.to_dict().get("counts")}


def _salla_check(db: Any, tid: int, anomalous: List[Dict[str, Any]], *, adapter: Any = None, limit: int = 5) -> Dict[str, Any]:
    """Re-read anomalous products from Salla (GET only) and say where the inconsistency lives."""
    import asyncio  # noqa: PLC0415

    out: Dict[str, Any] = {"checked": [], "skipped": [], "reads": []}
    if not anomalous:
        return out
    if adapter is None:
        try:
            from services.store_sync import StoreSyncService  # noqa: PLC0415

            adapter = StoreSyncService(db, tid)._get_adapter()
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"adapter_unavailable:{type(exc).__name__}"
            return out
    if adapter is None or not hasattr(adapter, "_get"):
        out["error"] = "adapter_unavailable"
        return out

    async def _read(ext: str):
        raw = await adapter._get(f"/products/{ext}")
        data = raw.get("data") if isinstance(raw, dict) else None
        variants = await adapter.get_raw_variants(ext) if hasattr(adapter, "get_raw_variants") else []
        return (data if isinstance(data, dict) else {}), (variants or [])

    for p in anomalous[:limit]:
        ext = p["external_id"]
        if not ext:
            out["skipped"].append({"product_id": p["product_id"], "reason": "no_external_id"})
            continue
        try:
            data, variants = asyncio.run(_read(ext))
        except Exception as exc:  # noqa: BLE001
            out["checked"].append({"product_id": p["product_id"], "external_id": ext, "error": type(exc).__name__})
            continue
        out["reads"].append(f"GET /products/{ext}")
        out["reads"].append(f"GET /products/{ext}/variants")
        salla_qty = data.get("quantity")
        salla_unlimited = bool(data.get("unlimited_quantity"))
        status = data.get("status")
        if isinstance(status, dict):
            status = status.get("slug") or status.get("name")
        var_qty = []
        for v in variants:
            if not isinstance(v, dict):
                continue
            q = v.get("quantity")
            if q is None:
                q = v.get("stock_quantity")
            var_qty.append({"id": str(v.get("id")), "quantity": q, "available": v.get("available")})
        salla_variants_in_stock = sum(1 for v in var_qty if (v["available"] is True) or (
            v["available"] is None and isinstance(v["quantity"], (int, float)) and v["quantity"] > 0))
        try:
            salla_parent_in_stock = salla_unlimited or (salla_qty is not None and int(salla_qty) > 0)
        except (TypeError, ValueError):
            salla_parent_in_stock = None
        local_variants_in_stock = p["variants_in_stock_count"]
        if salla_parent_in_stock and var_qty and salla_variants_in_stock == 0:
            verdict = "salla_parent_quantity_inconsistent_with_its_variants"
        elif salla_variants_in_stock > 0 and local_variants_in_stock == 0:
            verdict = "local_variant_stock_stale"
        elif (salla_parent_in_stock is False) and p["in_stock"]:
            verdict = "local_parent_stock_stale"
        elif not var_qty and p["variant_count"] > 0:
            verdict = "salla_reports_no_variants_for_a_local_variant_product"
        else:
            verdict = "consistent_with_salla"
        out["checked"].append({
            "product_id": p["product_id"], "external_id": ext,
            "local": {"in_stock": p["in_stock"], "stock_quantity": p["stock_quantity"],
                      "variant_count": p["variant_count"], "variants_in_stock_count": local_variants_in_stock},
            "salla": {"quantity": salla_qty, "unlimited_quantity": salla_unlimited, "status": status,
                      "variant_count": len(var_qty), "variants_in_stock": salla_variants_in_stock,
                      "variant_quantities": var_qty[:50]},
            "verdict": verdict,
        })
    return out


def _debug_token(token: str, *, client: Any) -> Dict[str, Any]:
    """GET /debug_token with the app token (from the container env): token type, app and scopes.

    Read-only. Returns ``available=False`` when the app credentials are not in
    the environment or the call fails; never includes the tokens themselves.
    """
    import os  # noqa: PLC0415

    app_id = _strip(os.environ.get("META_APP_ID"))
    app_secret = _strip(os.environ.get("META_APP_SECRET"))
    out: Dict[str, Any] = {"available": False}
    if not (token and app_id and app_secret):
        out["reason"] = "app_credentials_not_in_environment" if token else "no_token"
        return out
    try:
        import httpx  # noqa: PLC0415

        version = _strip(os.environ.get("META_GRAPH_API_VERSION")) or "v21.0"
        url = f"https://graph.facebook.com/{version}/debug_token"
        params = {"input_token": token, "access_token": f"{app_id}|{app_secret}"}
        if client is not None:
            resp = client.get(url, params=params)
        else:
            with httpx.Client(timeout=20) as owned:
                resp = owned.get(url, params=params)
        body = resp.json() if getattr(resp, "content", b"") else {}
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"transport:{type(exc).__name__}"
        return out
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        out["reason"] = "unexpected_response"
        out["http_status"] = getattr(resp, "status_code", None)
        return out
    scopes = [str(x) for x in (data.get("scopes") or [])]
    granular = [str(g.get("scope")) for g in (data.get("granular_scopes") or []) if isinstance(g, dict)]
    return {
        "available": True,
        "is_valid": data.get("is_valid"),
        "type": data.get("type"),
        "app_id_matches_configured_app": (str(data.get("app_id") or "") == app_id) if data.get("app_id") else None,
        "scopes": scopes,
        "granular_scopes": granular,
        "catalog_management_in_scopes": ("catalog_management" in scopes) if scopes else None,
        "expires_at": data.get("expires_at"),
    }


def _explicit_oauth_scope_requests_catalog_management() -> Dict[str, Any]:
    """Does the deployed connect flow ask for catalog_management in its explicit OAuth scope?

    Read from the deployed source file (never from this branch's copy), so the
    answer describes the code the merchants actually authorized against.
    """
    import re  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    for root in (Path("/app"), Path.cwd(), Path(__file__).resolve().parents[2] if len(Path(__file__).resolve().parents) > 2 else Path.cwd()):
        f = root / "backend" / "routers" / "whatsapp_embedded.py"
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            m = re.search(r'"scope":\s*",".join\(\[(.*?)\]\)', text, re.S)
            if not m:
                return {"known": False, "reason": "scope_list_not_found", "source": str(f)}
            scopes = re.findall(r'"([a-z_]+)"', m.group(1))
            return {"known": True, "source": str(f), "explicit_scopes": scopes,
                    "requests_catalog_management": "catalog_management" in scopes}
    return {"known": False, "reason": "source_file_not_found"}


def _permission_status(token: str, *, client: Any) -> Dict[str, Any]:
    """Raw facts about catalog_management on the token, kept apart from any cause.

    ``raw`` holds what Graph returned (``/me/permissions`` status and
    ``/debug_token`` scopes). ``interpretation`` says only whether the token
    carries the permission now; the *reason* it does not is undetermined from
    the token alone — the Embedded Signup configuration (``config_id``) and the
    app's access level for the permission are read in the Meta App Dashboard,
    not through Graph with a merchant token.
    """
    import os  # noqa: PLC0415

    raw: Dict[str, Any] = {}
    try:
        from services.meta_catalog_linking import _graph_json  # noqa: PLC0415

        resp = _graph_json("GET", "me/permissions", token, params={}, client=client)
        if resp.get("ok"):
            rows = (resp.get("body") or {}).get("data") or []
            statuses = {str(r.get("permission")): str(r.get("status") or "") for r in rows if isinstance(r, dict)}
            cm = statuses.get("catalog_management")
            raw["me_permissions"] = {
                "ok": True, "listed_permissions": statuses,
                "catalog_management_status": cm if cm in ("granted", "declined") else "absent",
            }
        else:
            err = resp.get("error")
            raw["me_permissions"] = {"ok": False, "http_status": resp.get("http_status"),
                                     "error": (err.get("message") if isinstance(err, dict) else err),
                                     "catalog_management_status": "unknown"}
    except Exception as exc:  # noqa: BLE001
        raw["me_permissions"] = {"ok": False, "error": type(exc).__name__, "catalog_management_status": "unknown"}
    raw["debug_token"] = _debug_token(token, client=client)
    raw["explicit_oauth_scope_in_deployed_code"] = _explicit_oauth_scope_requests_catalog_management()
    cfg = _strip(os.environ.get("META_EMBEDDED_SIGNUP_CONFIG_ID")) or _strip(os.environ.get("META_WA_CONFIG_ID"))
    raw["embedded_signup_config"] = {
        "config_id_present": bool(cfg),
        "config_id_tail": cfg[-4:] if cfg else None,
        "requested_permissions": "not_readable_via_graph_with_a_merchant_token; read in Meta App Dashboard -> WhatsApp -> Embedded Signup configurations",
    }
    raw["app_access_level_for_catalog_management"] = "not_readable_via_graph; read in Meta App Dashboard -> App Review -> Permissions and Features"

    status = raw["me_permissions"]["catalog_management_status"]
    in_scopes = raw["debug_token"].get("catalog_management_in_scopes")
    if status == "granted" or in_scopes is True:
        on_token = "granted"
    elif status in ("declined", "absent") or in_scopes is False:
        on_token = "not_on_token"
    else:
        on_token = "unknown"
    explicit = raw["explicit_oauth_scope_in_deployed_code"]
    possible_causes: List[Dict[str, Any]] = []
    if on_token != "granted":
        possible_causes = [
            {"cause": "explicit_oauth_scope_omits_catalog_management",
             "evidence": ("confirmed" if explicit.get("known") and explicit.get("requests_catalog_management") is False
                          else "ruled_out" if explicit.get("known") and explicit.get("requests_catalog_management") else "unknown"),
             "note": "the connect flow's explicit scope list; Meta applies the config_id's permissions on top, so this alone does not prove the merchant was never asked"},
            {"cause": "embedded_signup_config_does_not_include_catalog_management",
             "evidence": "unknown", "how_to_verify": "Meta App Dashboard -> WhatsApp -> Embedded Signup -> configuration for the config_id in META_EMBEDDED_SIGNUP_CONFIG_ID/META_WA_CONFIG_ID -> permissions/assets requested"},
            {"cause": "app_lacks_access_level_for_catalog_management",
             "evidence": "unknown", "how_to_verify": "Meta App Dashboard -> App Review -> Permissions and Features -> catalog_management: Standard vs Advanced access, review status"},
            {"cause": "merchant_declined_at_authorization",
             "evidence": "confirmed" if status == "declined" else ("ruled_out" if status in ("granted", "absent") else "unknown")},
            {"cause": "token_type_or_app_mismatch",
             "evidence": ("unknown" if not raw["debug_token"].get("available")
                          else ("suspect" if raw["debug_token"].get("app_id_matches_configured_app") is False else "ruled_out")),
             "note": f"debug_token type={raw['debug_token'].get('type')}"},
        ]
    return {
        "raw": raw,
        "interpretation": {
            "catalog_management_on_token": on_token,
            "cause": "n/a" if on_token == "granted" else "undetermined_from_token_alone",
            "possible_causes": possible_causes,
            "needs_manual_reads": [] if on_token == "granted" else [
                "embedded_signup_configuration_permissions", "app_access_level_catalog_management", "app_review_status",
            ],
        },
    }


def _graph_section(
    db: Any,
    tid: int,
    conn: Any,
    token_pick: Dict[str, Any],
    chosen_ids: List[int],
    *,
    client: Any,
    expected_business_id: Optional[str] = None,
    candidate_retailer_ids: Optional[List[str]] = None,
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
    section["expected_business_id"] = expected_business_id

    # 1. catalogs actually linked to the WABA — independent of any local catalog id
    linked_ids: List[str] = []
    try:
        from services.meta_catalog_linking import (  # noqa: PLC0415
            _fetch_waba_product_catalogs,
            fetch_waba_owner_business_id,
            get_waba_catalog_link_status,
        )

        catalogs, http_status, err = _fetch_waba_product_catalogs(waba_id, token, client=client)
        section["reads"].append(f"GET /{waba_id}/product_catalogs")
        linked_ids = [c["id"] for c in catalogs]
        section["waba_catalogs"] = {
            "ok": err is None,
            "http_status": http_status,
            "catalogs": catalogs,
            "count": len(catalogs),
            "error": err,
            "verdict": (
                "unproven_graph_error" if err is not None
                else ("no_catalog_linked_to_waba" if not catalogs else "catalog_linked_to_waba")
            ),
            "stamped_catalog_id": expected_catalog or None,
            "stamped_catalog_is_linked": (expected_catalog in linked_ids) if expected_catalog else None,
        }
        owner = fetch_waba_owner_business_id(waba_id, token, client=client)
        section["reads"].append(f"GET /{waba_id}?fields=owner_business_info")
        section["waba_owner_business"] = {
            k: owner.get(k) for k in ("ok", "business_id", "business_name", "error") if k in owner
        }
        if expected_business_id:
            section["waba_owner_business"]["matches_expected"] = (
                _strip(owner.get("business_id")) == expected_business_id if owner.get("ok") else None
            )
        # legacy view (needs the stamped id) kept for comparison
        link = get_waba_catalog_link_status(db, tid)
        section["waba_link_legacy"] = {
            k: link.get(k) for k in ("link_status", "expected_catalog_linked", "error", "missing") if k in link
        }
    except Exception as exc:  # noqa: BLE001
        section["waba_catalogs"] = {"ok": False, "error": type(exc).__name__, "verdict": "unproven_exception"}

    # 2. token permission (raw status, absent vs declined vs granted)
    section["token_catalog_management"] = _permission_status(token, client=client)
    section["reads"].append("GET /me/permissions")

    # 3. catalogs readable + owner match (stamped id and every WABA-linked id)
    try:
        from services.meta_catalog_access import probe_catalog_readable  # noqa: PLC0415

        ids: List[str] = []
        if expected_catalog:
            ids.append(expected_catalog)
        for cid in linked_ids:
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
        reference_bm = expected_business_id or owner_bm
        section["catalog_business_matches_waba_owner"] = {
            p["catalog_id"]: (bool(reference_bm) and _strip(p.get("business_id")) == reference_bm) if p.get("ok") else None
            for p in probes
        }
    except Exception as exc:  # noqa: BLE001
        section["catalogs"] = [{"error": type(exc).__name__}]

    # 4. live items: against the stamped catalog (full readiness classification) or,
    #    when nothing is stamped, presence of the candidate retailer ids in each WABA-linked catalog
    if expected_catalog:
        try:
            from services.meta_catalog_readiness import build_meta_catalog_readiness_report  # noqa: PLC0415

            report = build_meta_catalog_readiness_report(db, tid, include_meta_live_read=True, client=client)
            data = report.to_dict()
            wanted = set(int(x) for x in chosen_ids)
            plan: Dict[str, List[Dict[str, Any]]] = {"create": [], "update": [], "noop": [], "skip": []}
            for item in data.get("items") or []:
                if int(item.get("product_id") or 0) not in wanted:
                    continue
                action = str(item.get("action_needed") or "skip")
                plan.setdefault(action, []).append({
                    "product_id": item.get("product_id"), "retailer_id": item.get("retailer_id"),
                    "status": item.get("status"), "reasons": item.get("reasons"), "meta_product_id": item.get("meta_product_id"),
                })
            section["live_items"] = {
                "catalog_id": expected_catalog, "counts": data.get("counts"), "meta_fetch": data.get("meta_fetch"),
                "error": data.get("error"), "candidate_plan": plan,
                "candidate_create": len(plan["create"]), "candidate_update": len(plan["update"]),
                "candidate_noop": len(plan["noop"]), "candidate_skip": len(plan.get("skip", [])),
            }
            section["reads"].append(f"GET /{expected_catalog}/products")
        except Exception as exc:  # noqa: BLE001
            section["live_items"] = {"error": type(exc).__name__}
    elif linked_ids and candidate_retailer_ids:
        presence: Dict[str, Any] = {}
        try:
            from services.meta_catalog_reconcile import fetch_meta_catalog_live_products  # noqa: PLC0415

            for cid in linked_ids[:3]:
                live, meta_fetch = fetch_meta_catalog_live_products(conn, cid, client=client)
                section["reads"].append(f"GET /{cid}/products")
                present = [rid for rid in candidate_retailer_ids if rid in live]
                presence[cid] = {
                    "live_item_count": len(live), "fetch": meta_fetch,
                    "candidates_present": present,
                    "candidates_absent": [rid for rid in candidate_retailer_ids if rid not in live],
                    "expected_actions": {"create": len(candidate_retailer_ids) - len(present), "update_or_noop": len(present)},
                }
        except Exception as exc:  # noqa: BLE001
            presence["error"] = type(exc).__name__
        section["live_items"] = {"catalog_id": None, "note": "no catalog stamped locally; presence checked against WABA-linked catalogs",
                                 "against_linked_catalogs": presence}
    else:
        section["live_items"] = {
            "skipped": "no_catalog_stamped_and_none_linked" if not linked_ids else "no_candidates",
            "expected_actions_if_new_catalog": {"create": len(candidate_retailer_ids or []), "update": 0, "noop": 0},
        }
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
        perm = ((graph.get("token_catalog_management") or {}).get("interpretation") or {}).get("catalog_management_on_token")
        if perm != "granted":
            missing.append(f"token_catalog_management:{perm or 'unknown'}")
        wc = graph.get("waba_catalogs") or {}
        verdict = wc.get("verdict")
        if verdict == "no_catalog_linked_to_waba":
            missing.append("waba_catalog_link:none_linked")
        elif verdict != "catalog_linked_to_waba":
            missing.append("waba_catalog_link:unproven")
        elif conn.get("meta_catalog_id") and wc.get("stamped_catalog_is_linked") is False:
            missing.append("waba_catalog_link:stamped_id_not_linked")
        matches = graph.get("catalog_business_matches_waba_owner") or {}
        for cid in (wc.get("catalogs") or []):
            if matches.get(cid.get("id")) is not True:
                missing.append(f"catalog_business_ownership:{cid.get('id')}")
        owner = graph.get("waba_owner_business") or {}
        if owner.get("matches_expected") is False:
            missing.append("waba_owner_business_mismatch")
    cov = (out.get("trial_candidates") or {}).get("scenario_coverage") or {}
    if cov and not cov.get("coverage_complete"):
        missing.append("trial_scenario_coverage:" + ",".join(cov.get("missing_roles") or []))
    return missing


__all__ = ["build_catalog_trial_readout"]
