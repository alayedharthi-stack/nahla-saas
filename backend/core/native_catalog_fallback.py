"""
core/native_catalog_fallback.py
─────────────────────────────────
Operational fallback when WhatsApp ``catalog_message`` fails at send time.

Produces an honest reply plus a deterministic product list — no claim that
the native catalog appeared.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("nahla.native_catalog")

_NATIVE_CATALOG_FAILURE_INTRO_AR = (
    "الكتالوج ما ظهر هنا، هذي أبرز المنتجات المتوفرة:"
)


def compose_native_catalog_failure_reply(
    db: Any,
    tenant_id: Optional[int],
    *,
    customer_message: str = "",
) -> str:
    """Return an operationally honest browse fallback after catalog send failure."""
    if db is None or not tenant_id:
        return _NATIVE_CATALOG_FAILURE_INTRO_AR
    try:
        from modules.ai.brain.catalog.catalog_ranking_runtime import (  # noqa: PLC0415
            load_best_seller_catalog_products,
        )
        from modules.ai.brain.catalog.discovery_presenter import (  # noqa: PLC0415
            DiscoveryPresentationComposer,
        )
        from modules.ai.brain.commerce.discovery_strategy import (  # noqa: PLC0415
            CatalogContextSnapshot,
            resolve_discovery_strategy,
        )
        from modules.ai.brain.commerce.merchant_discovery_settings import (  # noqa: PLC0415
            parse_merchant_discovery_settings,
        )
        from modules.ai.brain.discovery.entry import GLOBAL_BROWSE  # noqa: PLC0415

        strategy = resolve_discovery_strategy(
            CatalogContextSnapshot(
                entry_source=GLOBAL_BROWSE,
                entry_type="global_browse",
                collection_count=0,
            )
        )
        products = load_best_seller_catalog_products(
            db,
            int(tenant_id),
            message=customer_message or "",
            query="",
            state=None,
            limit=max(12, getattr(strategy, "initial_count", 3) * 4),
        )
        if not products:
            return _NATIVE_CATALOG_FAILURE_INTRO_AR

        composer = DiscoveryPresentationComposer()
        presentation = composer.compose_products(
            list(products or []),
            strategy=strategy,
            entry_source="top_products",
            entry_type="top_products",
            merchant_settings=parse_merchant_discovery_settings({}),
            query="",
        )
        body = str(getattr(presentation, "text", "") or "").strip()
        if body:
            return f"{_NATIVE_CATALOG_FAILURE_INTRO_AR}\n\n{body}"
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — fallback compose must not block send
        logger.debug(
            "[NATIVE_CATALOG] failure_reply_compose_failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
    return _NATIVE_CATALOG_FAILURE_INTRO_AR


__all__ = ["compose_native_catalog_failure_reply"]
