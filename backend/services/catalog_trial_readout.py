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
- Salla reads (``include_salla``) never go through the adapter's request
  helper, which may refresh the access token and save it (or mark the
  integration for re-authorisation). They use the stored access token as is;
  when it is expired or Salla rejects it, the read is refused with a clear
  code instead of refreshing anything.
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
        out["candidate_salla_crosscheck"] = _candidate_salla_crosscheck(
            db, tid, products, chosen_ids, adapter=salla_adapter,
        )

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


PAYLOAD_CHECK_MEANING = (
    "local_payload_checks_passed counts variants whose locally built payload passed this "
    "platform's own validation (identity, price, currency, image, url, availability). It is not "
    "Meta's acceptance of the item, not a Graph write, and not visibility in WhatsApp; those are "
    "proven only by the publish response and the owner's visual check."
)


def _candidate_payloads(db: Any, tid: int, chosen_ids: List[int]) -> Dict[str, Any]:
    """Per-variant publish identity and payload preview for the chosen products (no Graph).

    The counts describe *local* payload validation only — see ``PAYLOAD_CHECK_MEANING``.
    """
    empty = {"items": [], "local_payload_checks_passed": 0, "local_payload_checks_blocked": 0,
             "meaning": PAYLOAD_CHECK_MEANING}
    if not chosen_ids:
        return dict(empty)
    try:
        from services.meta_catalog_readiness import build_meta_catalog_readiness_report  # noqa: PLC0415

        report = build_meta_catalog_readiness_report(db, tid)
    except Exception as exc:  # noqa: BLE001
        return dict(empty, error=type(exc).__name__)
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
    passed = sum(1 for i in items if i["status"] in ("ready", "warn"))
    availability = _count_by(items, "availability")
    return {
        "items": items,
        "local_payload_checks_passed": passed,
        "local_payload_checks_blocked": len(items) - passed,
        "meaning": PAYLOAD_CHECK_MEANING,
        "availability_counts": availability,
        "warnings": [
            {"product_id": i["product_id"], "retailer_id": i["retailer_id"], "reasons": i["reasons"]}
            for i in items if i["status"] == "warn"
        ],
        "counts_all_products": report.to_dict().get("counts"),
    }


def _resolve_salla_adapter(db: Any, tid: int, adapter: Any) -> tuple[Any, Optional[str]]:
    if adapter is None:
        try:
            from services.store_sync import StoreSyncService  # noqa: PLC0415

            adapter = StoreSyncService(db, tid)._get_adapter()
        except Exception as exc:  # noqa: BLE001
            return None, f"adapter_unavailable:{type(exc).__name__}"
    if adapter is None or not str(getattr(adapter, "platform", "salla") or "salla").lower() == "salla":
        return None, "adapter_unavailable"
    return adapter, None


class SallaReadRefused(RuntimeError):
    """A Salla read that could only proceed by changing stored credentials."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


SALLA_READ_REFUSED_TOKEN_MISSING = "salla_read_refused_access_token_missing"
SALLA_READ_REFUSED_TOKEN_EXPIRED = "salla_read_refused_token_expired_refresh_required"
SALLA_READ_REFUSED_TOKEN_REJECTED = "salla_read_refused_token_rejected_refresh_required"


def _salla_read_refusal(adapter: Any, now: Optional[datetime] = None) -> Optional[str]:
    """Why a read with the stored token cannot proceed without a mutation, or None."""
    if not str(getattr(adapter, "api_key", "") or "").strip():
        return SALLA_READ_REFUSED_TOKEN_MISSING
    raw = str(getattr(adapter, "_expires_at", "") or "").strip()
    if raw:
        try:
            exp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= (now or datetime.now(timezone.utc)):
            return SALLA_READ_REFUSED_TOKEN_EXPIRED
    return None


async def _salla_http_get(url: str, headers: Dict[str, str], params: Optional[Dict[str, Any]] = None) -> Any:
    import httpx  # noqa: PLC0415

    async with httpx.AsyncClient(timeout=20.0) as client:
        return await client.get(url, headers=headers, params=params or {})


async def _salla_readonly_get(adapter: Any, path: str) -> Dict[str, Any]:
    """One Salla GET with the stored token; never refreshes, saves or invalidates it."""
    from core.acceptance_execution_context import deny_external_egress  # noqa: PLC0415
    from store_adapters.salla_adapter import SALLA_API_BASE  # noqa: PLC0415

    refusal = _salla_read_refusal(adapter)
    if refusal:
        raise SallaReadRefused(refusal)
    deny_external_egress(egress_kind="salla_integration", operation="get",
                         tenant_id=getattr(adapter, "_tenant_id", None))
    headers = {"Authorization": f"Bearer {adapter.api_key}", "Accept": "application/json"}
    resp = await _salla_http_get(f"{SALLA_API_BASE}{path}", headers)
    if resp.status_code == 401:
        raise SallaReadRefused(SALLA_READ_REFUSED_TOKEN_REJECTED)
    resp.raise_for_status()
    return resp.json()


def _salla_product_read(adapter: Any, ext: str) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """GET /products/{ext} and its variants with the stored token (read only).

    Never uses the adapter's request helper (``_get``), so the token is not
    refreshed, saved or invalidated by this read. ``SallaReadRefused`` when the
    read could only proceed with such a change.
    """
    import asyncio  # noqa: PLC0415

    async def _read():
        raw = await _salla_readonly_get(adapter, f"/products/{ext}")
        data = raw.get("data") if isinstance(raw, dict) else None
        try:
            vraw = await _salla_readonly_get(adapter, f"/products/{ext}/variants")
        except SallaReadRefused:
            raise
        except Exception:  # noqa: silent-ok — same contract as get_raw_variants: a variant read error yields no variants
            vraw = {}
        variants = vraw.get("data") if isinstance(vraw, dict) else None
        variants = variants if isinstance(variants, list) else []
        return (data if isinstance(data, dict) else {}), [v for v in variants if isinstance(v, dict)]

    return asyncio.run(_read())


def _salla_price(raw: Any) -> Optional[float]:
    if isinstance(raw, dict):
        raw = raw.get("amount")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _salla_variant_label(v: Dict[str, Any]) -> Optional[str]:
    """Human label of a Salla variant from its option values or name (read only)."""
    opts = v.get("related_option_values") or v.get("option_values") or v.get("options")
    labels: List[str] = []
    if isinstance(opts, list):
        for o in opts:
            if isinstance(o, dict):
                lab = o.get("name") or o.get("value") or o.get("label")
                if isinstance(lab, dict):
                    lab = lab.get("name") or lab.get("value")
                if lab:
                    labels.append(str(lab))
            elif isinstance(o, str):
                labels.append(o)
    if labels:
        return " / ".join(labels)
    name = v.get("name")
    return str(name) if name else None


def _candidate_salla_crosscheck(
    db: Any, tid: int, products: List[Dict[str, Any]], chosen_ids: List[int], *, adapter: Any = None,
) -> Dict[str, Any]:
    """Compare every chosen product's local variants with Salla's current variants (GET only).

    Per local variant: found in Salla, price match, stock match, option label present.
    Per product: Salla variants that have no local row. Nothing is written anywhere.
    """
    out: Dict[str, Any] = {"products": [], "reads": [], "variants_checked": 0,
                           "variants_matching": 0, "mismatches": 0}
    if not chosen_ids:
        return out
    adapter, err = _resolve_salla_adapter(db, tid, adapter)
    if err:
        out["error"] = err
        return out
    by_id = {p["product_id"]: p for p in products}
    for pid in chosen_ids:
        p = by_id.get(pid)
        if p is None or not p.get("external_id"):
            out["products"].append({"product_id": pid, "error": "not_found_or_no_external_id"})
            continue
        ext = p["external_id"]
        try:
            data, variants = _salla_product_read(adapter, ext)
        except SallaReadRefused as exc:
            out["products"].append({"product_id": pid, "external_id": ext, "error": exc.code})
            continue
        except Exception as exc:  # noqa: BLE001
            out["products"].append({"product_id": pid, "external_id": ext, "error": type(exc).__name__})
            continue
        out["reads"].extend([f"GET /products/{ext}", f"GET /products/{ext}/variants"])
        salla_by_id = {str(v.get("id")): v for v in variants if v.get("id") is not None}
        rows: List[Dict[str, Any]] = []
        for lv in p["variants"]:
            sid = lv.get("salla_variant_id")
            sv = salla_by_id.get(str(sid)) if sid else None
            row: Dict[str, Any] = {"variant_id": lv["variant_id"], "salla_variant_id": sid,
                                   "retailer_id": lv.get("retailer_id"), "local_price": lv.get("price"),
                                   "local_in_stock": lv.get("in_stock")}
            if sv is None:
                row.update({"verdict": "variant_missing_in_salla", "salla_price": None})
            else:
                sprice = _salla_price(sv.get("sale_price")) if _salla_price(sv.get("sale_price")) else _salla_price(sv.get("price"))
                try:
                    lprice = float(lv.get("price")) if lv.get("price") not in (None, "") else None
                except (TypeError, ValueError):
                    lprice = None
                price_match = (lprice is not None and sprice is not None and abs(lprice - sprice) < 0.005)
                qty = sv.get("quantity", sv.get("stock_quantity"))
                avail = sv.get("available")
                if avail is None and isinstance(qty, (int, float)):
                    avail = qty > 0
                stock_match = (avail is None) or (bool(avail) == bool(lv.get("in_stock")))
                label = _salla_variant_label(sv)
                problems = []
                if not price_match:
                    problems.append("price_mismatch")
                if not stock_match:
                    problems.append("stock_mismatch")
                if not label:
                    problems.append("salla_variant_has_no_option_label")
                row.update({
                    "salla_price": sprice, "salla_available": avail, "salla_quantity": qty,
                    "salla_option_label": label, "salla_sku": sv.get("sku"),
                    "verdict": "matches_salla" if not problems else ",".join(problems),
                })
            rows.append(row)
        local_ids = {str(lv.get("salla_variant_id")) for lv in p["variants"] if lv.get("salla_variant_id")}
        missing_locally = [sid for sid in salla_by_id if sid not in local_ids]
        matching = sum(1 for r in rows if r["verdict"] == "matches_salla")
        out["variants_checked"] += len(rows)
        out["variants_matching"] += matching
        out["mismatches"] += len(rows) - matching + len(missing_locally)
        pname = data.get("name")
        out["products"].append({
            "product_id": pid, "external_id": ext,
            "salla_name": pname if isinstance(pname, str) else None,
            "salla_status": (data.get("status") or {}).get("slug") if isinstance(data.get("status"), dict) else data.get("status"),
            "salla_variant_count": len(salla_by_id), "local_variant_count": len(rows),
            "salla_variants_missing_locally": missing_locally,
            "variants": rows,
            "verdict": "consistent_with_salla" if (matching == len(rows) and not missing_locally) else "differences_found",
        })
    out["meaning"] = ("local copy compared with Salla's current product and variant reads; a match here "
                      "is a precondition for a truthful payload, not Meta acceptance")
    return out


def _salla_check(db: Any, tid: int, anomalous: List[Dict[str, Any]], *, adapter: Any = None, limit: int = 5) -> Dict[str, Any]:
    """Re-read anomalous products from Salla (GET only) and say where the inconsistency lives."""
    out: Dict[str, Any] = {"checked": [], "skipped": [], "reads": []}
    if not anomalous:
        return out
    adapter, err = _resolve_salla_adapter(db, tid, adapter)
    if err:
        out["error"] = err
        return out

    def _read(ext: str):
        return _salla_product_read(adapter, ext)

    for p in anomalous[:limit]:
        ext = p["external_id"]
        if not ext:
            out["skipped"].append({"product_id": p["product_id"], "reason": "no_external_id"})
            continue
        try:
            data, variants = _read(ext)
        except SallaReadRefused as exc:
            out["checked"].append({"product_id": p["product_id"], "external_id": ext, "error": exc.code})
            continue
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
    granular_rows = [g for g in (data.get("granular_scopes") or []) if isinstance(g, dict)]
    granular = [str(g.get("scope")) for g in granular_rows]
    # scope -> asset ids the token is scoped to (business / WABA ids; ids only, never tokens)
    granular_targets = {
        str(g.get("scope")): [str(x) for x in (g.get("target_ids") or [])]
        for g in granular_rows if g.get("scope")
    }
    token_type = str(data.get("type") or "") or None
    principal_id = _strip(data.get("user_id") or data.get("profile_id"))
    return {
        "available": True,
        "is_valid": data.get("is_valid"),
        "type": token_type,
        # SYSTEM_USER = a business-integration system user token (Embedded Signup); its
        # principal is a system user of the merchant's business, not a human app-role
        # holder, so Standard-access eligibility cannot be settled by comparing a
        # user id with the app's human roles.
        "token_type_class": ("system_user" if (token_type or "").upper() == "SYSTEM_USER"
                             else "user" if (token_type or "").upper() == "USER" else "other" if token_type else None),
        "principal_id": principal_id or None,
        "app_id_matches_configured_app": (str(data.get("app_id") or "") == app_id) if data.get("app_id") else None,
        "scopes": scopes,
        "granular_scopes": granular,
        "granular_scope_targets": granular_targets,
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
             "evidence": "confirmed" if status == "declined" else ("ruled_out" if status == "granted" else "unknown"),
             "note": "an absent /me/permissions row does not distinguish never-asked from declined-and-unlisted; only an explicit declined/granted row settles it"},
            {"cause": "token_type_or_app_mismatch",
             "evidence": ("unknown" if not raw["debug_token"].get("available")
                          else ("suspect" if raw["debug_token"].get("app_id_matches_configured_app") is False else "ruled_out")),
             "note": f"debug_token type={raw['debug_token'].get('type')}"},
            {"cause": "standard_access_not_usable_for_this_token_principal",
             "evidence": "unknown",
             "note": (
                 "token_type_class=" + str(raw["debug_token"].get("token_type_class")) + "; "
                 "a SYSTEM_USER (business-integration) token's principal is a system user of the merchant's business: "
                 "human app roles do not apply, and whether Standard access covers a permission on a business that does not "
                 "own the app is settled only by Meta's official text (not readable via Graph) and the app's access level per permission"
             ),
             "how_to_verify": "Meta docs: access levels for business-integration system user tokens; App Dashboard: access level of each permission; granular_scope_targets vs the merchant business id"},
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


GRAPH_ERROR_BUSINESS_TYPE_RESTRICTION = "business_type_restriction"

# Documentation facts carried with their attribution. ``developers.facebook.com`` could not be
# fetched from the readout author's environment (egress blocked); every excerpt below reached
# us through a search engine and must be transcribed verbatim from the page by the operator.
COEXISTENCE_DOC_REFERENCES = [
    {
        "title": "Onboard WhatsApp Business app users (Embedded Signup → coexistence) — feature comparison table",
        "url": "https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users/",
        "kind": "official_page_table",
        "columns_per_excerpt": [
            "Feature",
            "Changes to the WhatsApp Business app feature after onboarding to Cloud API",
            "WhatsApp Business app feature supported on Cloud API?",
        ],
        "row_business_tools_per_excerpt": {
            "feature": "Business tools (catalog, orders, status)",
            "change_to_business_app_feature_after_onboarding": "No change",
            "supported_on_cloud_api": "Not supported",
        },
        "reading": {
            "business_app_catalog_persists_after_onboarding": "yes per 'No change' (app-side; the merchant keeps the app catalog)",
            "business_app_catalog_usable_or_syncable_via_cloud_api": "'Not supported' per the excerpt — a separate question from persistence",
        },
        "attribution": "official page; first reached as a search-engine excerpt, then read on the page by the operator (2026-10-02): the table separates 'No change' for the in-app catalog from 'Not supported' via Cloud API",
        "verification": "verified on page by operator read 2026-10-02; verbatim transcription still to be filed with the dashboard reads",
    },
    {
        "title": "Sentence circulating in search results and partner documentation",
        "text": ("Group chats, disappearing messages, view-once messages, live location messages, broadcast lists, "
                 "voice and video calls, and business tools such as the catalog are not supported once a number is "
                 "running Coexistence."),
        "kind": "unattributed_excerpt",
        "attribution": ("wording appears in partner documentation; the operator read Meta's Limitations section on 2026-10-02 "
                        "and this sentence is NOT present there verbatim. NOT to be cited as Meta's Limitations section"),
        "verification": "verified absent by operator read 2026-10-02 (Limitations section); keep only as a partner-documentation phrase",
    },
    {
        "title": "Onboard WhatsApp Business app users — detecting coexistence",
        "url": "https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-business-app-users/",
        "kind": "official_page_excerpt",
        "text": "GET /{phone_number_id}?fields=is_on_biz_app,platform_type — is_on_biz_app=true with platform_type=CLOUD_API means the number runs on both.",
        "attribution": "matches this repository's own coexistence verification (services/meta_coexistence.verify_coexistence_phone)",
        "verification": "verify on page",
    },
    {
        "title": "Sell products and services (Cloud API) — catalog connected to the WABA, catalog_management",
        "url": "https://developers.facebook.com/docs/whatsapp/cloud-api/guides/sell-products-and-services/",
        "kind": "official_page_not_fetched",
        "attribution": "not fetched from this environment",
        "verification": "verify on page (operator Part ب)",
    },
]


def _classify_graph_error(err: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Name what a Graph error body says, without inferring more than it says."""
    if not err:
        return None
    code = err.get("meta_code")
    msg = str(err.get("meta_message") or "")
    low = msg.lower()
    if code == 10 and "smb business type" in low:
        klass = GRAPH_ERROR_BUSINESS_TYPE_RESTRICTION
        note = ("Graph refused THIS call and its message names the WABA's owning business type, not a missing "
                "permission. It proves the refusal of this operation only: not the final cause, not whether other "
                "paths (Commerce Manager / WhatsApp Manager UI, a catalog shared to the portfolio) are also closed, "
                "not that a catalog is absent, and not that catalog_management would change the answer")
    elif code in (190, 102):
        klass, note = "access_token_invalid", "token rejected"
    elif code in (10, 200, 294) or err.get("meta_subcode") == 2388100:
        klass, note = "permission_or_restriction", "code reused by Graph for permissions and for business restrictions"
    else:
        klass, note = "graph_error", "unclassified"
    return {"class": klass, "meta_code": code, "meta_subcode": err.get("meta_subcode"), "message": msg, "note": note}


def _coexistence_facts(conn: Any, token: str, *, client: Any) -> tuple[Dict[str, Any], List[str]]:
    """Local connection mode + Graph phone facts (GET only): is the number on the Business app?"""
    reads: List[str] = []
    out: Dict[str, Any] = {"local_connection_mode": None, "is_on_biz_app": None, "platform_type": None}
    meta = getattr(conn, "extra_metadata", None) or {}
    try:
        from services.meta_coexistence import is_coexistence_mode  # noqa: PLC0415

        out["local_connection_mode"] = "coexistence" if is_coexistence_mode(conn) else (
            _strip(meta.get("connection_mode")) or "cloud_api_only_or_unset")
    except Exception:  # noqa: BLE001
        out["local_connection_mode"] = _strip(meta.get("connection_mode")) or "unknown"
    phone_id = _strip(getattr(conn, "phone_number_id", None))
    if phone_id and token:
        try:
            from services.meta_catalog_linking import _graph_json  # noqa: PLC0415

            resp = _graph_json("GET", phone_id, token, params={"fields": "is_on_biz_app,platform_type"}, client=client)
            reads.append(f"GET /{phone_id}?fields=is_on_biz_app,platform_type")
            body = resp.get("body") or {}
            if resp.get("ok"):
                out["is_on_biz_app"] = body.get("is_on_biz_app") if isinstance(body.get("is_on_biz_app"), bool) else None
                out["platform_type"] = _strip(body.get("platform_type")) or None
                out["graph_ok"] = True
            else:
                e = resp.get("error")
                out["graph_ok"] = False
                out["error"] = (e.get("message") if isinstance(e, dict) else e) or resp.get("http_status")
        except Exception as exc:  # noqa: BLE001
            out["graph_ok"] = False
            out["error"] = type(exc).__name__
    if out.get("is_on_biz_app") is True:
        out["verdict"] = "coexistence_confirmed_by_graph"
    elif out.get("is_on_biz_app") is False:
        out["verdict"] = "not_on_business_app_per_graph"
    else:
        out["verdict"] = "unproven"
    return out, reads


def _catalog_path_assessment(section: Dict[str, Any]) -> Dict[str, Any]:
    """Interpretation of the raw Graph facts: can this WABA take an API-linked catalog?

    Keeps three things apart: what Graph answered to *this* call, what the documentation
    says (with attribution), and what is still unknown. The tenant verdict stays
    conditional on the coexistence read and the manual dashboard reads; a refusal of one
    operation is never promoted to a final cause or to "all paths are closed".
    """
    wc = section.get("waba_catalogs") or {}
    ec = (wc.get("error_class") or {}).get("class")
    coex = section.get("coexistence") or {}
    facts = {
        "waba_product_catalogs_read": wc.get("verdict"),
        "waba_product_catalogs_error_class": ec,
        "coexistence": coex.get("verdict"),
        "catalog_management_on_token": ((section.get("token_catalog_management") or {}).get("interpretation") or {}).get(
            "catalog_management_on_token"),
    }
    if wc.get("verdict") in ("catalog_linked_to_waba", "no_catalog_linked_to_waba"):
        state = "api_catalog_link_readable_for_this_waba"
    elif ec == GRAPH_ERROR_BUSINESS_TYPE_RESTRICTION:
        state = "current_read_refused_with_business_type_message"
    else:
        state = "unproven"
    refused = state == "current_read_refused_with_business_type_message"
    return {
        "api_catalog_link_for_this_waba": state,
        "catalog_exists_for_this_waba": ("yes" if wc.get("verdict") == "catalog_linked_to_waba"
                                         else "no" if wc.get("verdict") == "no_catalog_linked_to_waba" else "unproven"),
        "final_cause_of_refusal": "unknown" if refused else "n/a",
        "alternative_paths": "unknown_until_manual_reads" if refused else "n/a",
        "would_catalog_management_alone_lift_the_block": "unproven" if refused else "n/a",
        "tenant_verdict": "conditional" if state != "api_catalog_link_readable_for_this_waba" else "readable",
        "conditional_on": ([
            "coexistence.is_on_biz_app (Graph read of the phone number)",
            "meta_dashboard_reads (embedded signup config, access level, app review)",
            "whatsapp_manager_reads (owning portfolio and its type, catalog tab offering)",
            "official_comparison_table_transcribed_verbatim",
        ] if state != "api_catalog_link_readable_for_this_waba" else []),
        "facts": facts,
        "documentation": COEXISTENCE_DOC_REFERENCES if (coex.get("verdict") == "coexistence_confirmed_by_graph" or refused) else [],
        "needs_manual_reads": ([
            "whatsapp_manager:business_portfolio_that_owns_the_waba_and_its_type",
            "whatsapp_manager:catalog_tab_for_this_waba (does it offer connecting a Commerce Manager catalog?)",
            "commerce_manager:catalogs_owned_by_the_expected_business_and_their_whatsapp_connection",
            "meta_docs:coexistence_page_feature_comparison_table_transcribed_with_its_columns",
        ] if state != "api_catalog_link_readable_for_this_waba" else []),
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
            "count": len(catalogs) if err is None else None,
            "error": err,
            "error_class": _classify_graph_error(err),
            "verdict": (
                "unproven_graph_error" if err is not None
                else ("no_catalog_linked_to_waba" if not catalogs else "catalog_linked_to_waba")
            ),
            "stamped_catalog_id": expected_catalog or None,
            "stamped_catalog_is_linked": (expected_catalog in linked_ids) if (expected_catalog and err is None) else None,
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

    # 1b. coexistence facts: local mode + GET phone?fields=is_on_biz_app,platform_type
    section["coexistence"], coex_reads = _coexistence_facts(conn, token, client=client)
    section["reads"].extend(coex_reads)

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
                    "classification": {
                        "create_if_absent": len(candidate_retailer_ids) - len(present),
                        "match_needs_verification": len(present),
                    },
                    "note": ("a retailer_id present in the catalog is a match that needs verification, not an "
                             "item this path may update: ownership requires a membership bound to that Graph "
                             "item with a publication provenance (or the legacy product stamp); noop requires "
                             "every synced field to be read and equal"),
                }
        except Exception as exc:  # noqa: BLE001
            presence["error"] = type(exc).__name__
        section["live_items"] = {"catalog_id": None, "note": "no catalog stamped locally; presence checked against WABA-linked catalogs",
                                 "against_linked_catalogs": presence}
    else:
        link_verdict = (section.get("waba_catalogs") or {}).get("verdict")
        n = len(candidate_retailer_ids or [])
        if not candidate_retailer_ids:
            section["live_items"] = {"skipped": "no_candidates"}
        elif link_verdict == "no_catalog_linked_to_waba":
            section["live_items"] = {
                "skipped": "no_catalog_stamped_and_none_linked",
                "expected_actions_if_new_catalog": {"create": n, "update": 0, "noop": 0},
            }
        else:
            # the WABA read failed: the link is unproven, so no create/update split can be claimed
            section["live_items"] = {
                "skipped": "waba_catalog_link_unproven_graph_error",
                "note": "GET /{waba}/product_catalogs did not answer; a catalog may or may not be linked",
                "conditional_actions": {
                    "if_no_catalog_is_linked": {"create": n, "update": 0, "noop": 0},
                    "if_a_catalog_is_linked": "per_identity_presence_read_required",
                },
            }
    section["catalog_path_assessment"] = _catalog_path_assessment(section)
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
        if (wc.get("error_class") or {}).get("class") == GRAPH_ERROR_BUSINESS_TYPE_RESTRICTION:
            missing.append("graph_refused_product_catalogs:business_type_message")
        coex = graph.get("coexistence") or {}
        if coex.get("verdict") == "coexistence_confirmed_by_graph" and verdict != "catalog_linked_to_waba":
            missing.append("coexistence:api_catalog_path_unproven")
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
