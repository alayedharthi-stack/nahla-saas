"""
brain/execution/search.py
──────────────────────────
ProductSearchHandler: executes ACTION_SEARCH_PRODUCTS.

Delegates to CatalogContextBuilder (the existing, battle-tested search
layer). Returns structured product dicts AND a formatted Arabic text block
ready for the composer.
"""
from __future__ import annotations

import logging
import os, sys

logger = logging.getLogger("nahla.brain.execution.search")

# Ensure backend root and database root are on sys.path
_THIS = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_THIS, "../../../../.."))   # backend/
_DB      = os.path.abspath(os.path.join(_BACKEND, "../database"))
for _p in (_BACKEND, _DB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ...brain.types import ActionResult, BrainContext, Decision


class ProductSearchHandler:
    """Handles ACTION_SEARCH_PRODUCTS decision."""

    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from core.store_knowledge import CatalogContextBuilder   # lazy import to avoid circulars

        query = decision.args.get("query", ctx.message)

        try:
            builder  = CatalogContextBuilder(ctx._db, ctx.tenant_id)    # type: ignore[attr-defined]
            products = (
                builder.search_products(query, limit=8)
                if query
                else builder.get_top_products(limit=8)
            )

            # If search produced nothing but products exist → fallback to top 8
            if not products:
                products = builder.get_top_products(limit=8)

            if not products:
                return ActionResult(
                    success=False,
                    error="no_products",
                    data={"message": "no_products_in_catalog"},
                )

            # Format the result for the composer
            lines = []
            for p in products:
                in_stock_ar = "متاح" if p.get("in_stock") else "غير متاح"
                price_str   = f"{p['price']} ريال" if p.get("price") else "السعر غير محدد"
                line        = f"• {p['title']} — {price_str} ({in_stock_ar})"
                if p.get("sku"):
                    line += f" [SKU: {p['sku']}]"
                lines.append(line)

            after_search = decision.args.get("after_search", "")
            # Flag narrow when many products found and no specific match was requested
            suggest_narrow = len(products) > 3 and not after_search

            return ActionResult(
                success=True,
                data={
                    "products":      products,
                    "product_lines": "\n".join(lines),
                    "count":         len(products),
                    "query":         query,
                    "suggest_narrow": suggest_narrow,
                    "after_search":   after_search,
                },
            )

        except Exception as exc:
            logger.exception("[SearchHandler] error for tenant=%s query=%r: %s", ctx.tenant_id, query, exc)
            return ActionResult(success=False, error=str(exc))
