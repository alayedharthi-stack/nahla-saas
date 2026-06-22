"""
catalog/catalog_ranking_runtime.py
──────────────────────────────────
Phase 5 — runtime consumption of merchant best-seller flags and product relations.

Uses Phase 1 read helpers + local catalog hydration. No dashboard wiring.
Operational evidence only.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger("nahla.brain.catalog.ranking_runtime")


def _is_orderable(product: Mapping[str, Any]) -> bool:
    return bool(
        product.get("can_checkout", product.get("orderable", True))
        and str(product.get("external_id") or "").strip()
    )


def hydrate_catalog_products_by_ids(
    db: Any,
    tenant_id: int,
    product_ids: Sequence[int],
    *,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    if db is None or tenant_id is None:
        return []
    ids = [int(pid) for pid in product_ids if pid is not None]
    if not ids:
        return []
    try:
        from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415
        from .catalog_browse_scope_resolver import hydrate_group_products  # noqa: PLC0415

        builder = CatalogContextBuilder(db, int(tenant_id))
        return hydrate_group_products(builder, ids, limit=limit)
    except Exception:  # noqa: BLE001
        logger.exception(
            "[CATALOG_RANKING_RUNTIME] hydrate_failed tenant=%s",
            tenant_id,
        )
        return []


def _resolve_group_id_for_browse(
    db: Any,
    tenant_id: int,
    *,
    message: str = "",
    query: str = "",
    state: Any = None,
) -> Optional[int]:
    from .catalog_browse_scope_resolver import (  # noqa: PLC0415
        active_catalog_group_slug_from_state,
        resolve_browse_scope,
    )
    from ..commerce.commerce_browse_category_guard import active_category_from_state  # noqa: PLC0415

    resolution = resolve_browse_scope(
        db,
        int(tenant_id),
        message or "",
        str(query or ""),
        active_group_slug=active_catalog_group_slug_from_state(state),
        active_category=active_category_from_state(state),
    )
    if resolution.matched and resolution.group_id is not None:
        return int(resolution.group_id)
    return None


def load_best_seller_catalog_products(
    db: Any,
    tenant_id: int,
    *,
    message: str = "",
    query: str = "",
    state: Any = None,
    group_id: Optional[int] = None,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    """Merchant-flagged best sellers for browse/top-products turns."""
    if db is None or tenant_id is None:
        return []
    try:
        from services.catalog_intelligence_service import (  # noqa: PLC0415
            get_catalog_settings,
            read_best_sellers,
        )
        from ..commerce.merchant_catalog_settings import parse_merchant_catalog_settings  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_RANKING_RUNTIME] import_failed")
        return []

    settings = parse_merchant_catalog_settings(get_catalog_settings(db, int(tenant_id)))
    if settings.best_seller_mode == "auto":
        return []

    effective_group = group_id
    if effective_group is None:
        effective_group = _resolve_group_id_for_browse(
            db,
            int(tenant_id),
            message=message,
            query=query,
            state=state,
        )

    rankings = read_best_sellers(
        db,
        int(tenant_id),
        group_id=effective_group,
        limit=max(1, int(limit or 12)),
    )
    if not rankings:
        return []

    product_ids = [int(row["product_id"]) for row in rankings if row.get("product_id") is not None]
    products = hydrate_catalog_products_by_ids(
        db,
        int(tenant_id),
        product_ids,
        limit=limit,
    )
    if products:
        logger.info(
            "[CATALOG_RANKING_RUNTIME] best_sellers tenant=%s group=%s count=%d mode=%s",
            tenant_id,
            effective_group,
            len(products),
            settings.best_seller_mode,
        )
        try:
            from modules.ai.brain.catalog.catalog_intelligence_telemetry import (  # noqa: PLC0415
                emit_catalog_intelligence_event,
            )

            emit_catalog_intelligence_event(
                "best_sellers",
                tenant_id=tenant_id,
                group=effective_group,
                count=len(products),
                mode=settings.best_seller_mode,
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry must not break best-seller runtime
            pass
    return products


def load_merchant_alternative_products(
    db: Any,
    tenant_id: int,
    source_product_id: int,
    *,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """Alternative relations configured by the merchant for a source product."""
    if db is None or tenant_id is None or source_product_id is None:
        return []
    try:
        from services.catalog_intelligence_service import read_product_relations  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []

    relations = read_product_relations(
        db,
        int(tenant_id),
        int(source_product_id),
        relation_type="alternative",
    ) or []
    if not relations:
        return []

    ordered_ids: List[int] = []
    seen: set[int] = set()
    for rel in sorted(relations, key=lambda r: (r.get("priority", 0), r.get("id", 0))):
        try:
            pid = int(rel.get("target_product_id"))
        except (TypeError, ValueError):
            continue
        if pid in seen:
            continue
        seen.add(pid)
        ordered_ids.append(pid)
        if len(ordered_ids) >= max(1, int(limit or 8)):
            break

    products = hydrate_catalog_products_by_ids(
        db,
        int(tenant_id),
        ordered_ids,
        limit=limit,
    )
    return [p for p in products if _is_orderable(p)]


def resolve_orderable_alternatives(
    db: Any,
    tenant_id: int,
    *,
    source_product_id: Optional[int],
    fallback_candidates: Sequence[Mapping[str, Any]],
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Prefer merchant alternative relations; supplement from fallback list."""
    cap = max(1, int(limit or 3))
    alts: List[Dict[str, Any]] = []
    seen_ids: set[int] = set()

    merchant_count = 0
    if source_product_id is not None and db is not None and tenant_id is not None:
        merchant_alts = load_merchant_alternative_products(
            db,
            int(tenant_id),
            int(source_product_id),
            limit=cap,
        )
        merchant_count = len(merchant_alts)
        for product in merchant_alts:
            try:
                pid = int(product.get("id"))
            except (TypeError, ValueError):
                continue
            if pid == int(source_product_id) or pid in seen_ids:
                continue
            if not _is_orderable(product):
                continue
            seen_ids.add(pid)
            alts.append(dict(product))
            if len(alts) >= cap:
                return alts

    for candidate in fallback_candidates or []:
        if not isinstance(candidate, Mapping):
            continue
        if not _is_orderable(candidate):
            continue
        try:
            pid = int(candidate.get("id"))
        except (TypeError, ValueError):
            continue
        if source_product_id is not None and pid == int(source_product_id):
            continue
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        alts.append(dict(candidate))
        if len(alts) >= cap:
            break

    if alts and source_product_id is not None:
        logger.info(
            "[CATALOG_RANKING_RUNTIME] alternatives tenant=%s source=%s count=%d merchant_first=%s",
            tenant_id,
            source_product_id,
            len(alts),
            merchant_count > 0,
        )
        try:
            from modules.ai.brain.catalog.catalog_intelligence_telemetry import (  # noqa: PLC0415
                emit_catalog_intelligence_event,
            )

            emit_catalog_intelligence_event(
                "alternatives",
                tenant_id=tenant_id,
                source=source_product_id,
                count=len(alts),
                merchant_first=merchant_count > 0,
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry must not break best-seller runtime
            pass
    return alts


__all__ = [
    "hydrate_catalog_products_by_ids",
    "load_best_seller_catalog_products",
    "load_merchant_alternative_products",
    "resolve_orderable_alternatives",
]
