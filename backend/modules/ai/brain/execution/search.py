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
        from modules.ai.commerce.runtime import CommerceToolRuntime

        # Fast path: decision engine already computed alternatives for a
        # rejected (unorderable) product — skip the search entirely.
        if decision.args.get("rejected_product"):
            alts = decision.args.get("alternatives") or []
            return ActionResult(
                success=True,
                data={
                    "products":      alts,
                    "product_lines": "",
                    "count":         len(alts),
                    "query":         "",
                    "suggest_narrow": False,
                    "rejected_product": decision.args["rejected_product"],
                },
            )

        query = decision.args.get("query", ctx.message)

        try:
            runtime = CommerceToolRuntime(
                ctx._db,  # type: ignore[attr-defined]
                tenant_id=ctx.tenant_id,
                customer_phone=ctx.customer_phone,
                customer_id=ctx.customer_id,
                tenant_context=ctx.tenant_context,
            )
            runtime_result = await runtime.execute(
                "search_products",
                {"query": query, "limit": 8},
            )
            products = list(runtime_result.payload.get("products") or [])

            # If search produced nothing but products exist → fallback to top 8
            if not products:
                runtime_result = await runtime.execute("search_products", {"limit": 8})
                products = list(runtime_result.payload.get("products") or [])

            if not products:
                return ActionResult(
                    success=False,
                    error="no_products",
                    data={"message": "no_products_in_catalog"},
                )

            lines = []
            for p in products:
                price_str = f"{p['price']} ريال" if p.get("price") else "السعر غير محدد"
                line = f"• {p['title']} — {price_str}"
                if p.get("sku"):
                    line += f" [SKU: {p['sku']}]"
                lines.append(line)

            after_search = decision.args.get("after_search", "")
            suggest_narrow = len(products) > 3 and not after_search
            selected_product = products[0] if len(products) == 1 else None

            return ActionResult(
                success=True,
                data={
                    "products":      products,
                    "product_lines": "\n".join(lines),
                    "count":         len(products),
                    "query":         query,
                    "suggest_narrow": suggest_narrow,
                    "after_search":   after_search,
                    "product":        selected_product,
                },
            )

        except Exception as exc:
            logger.exception("[SearchHandler] error for tenant=%s query=%r: %s", ctx.tenant_id, query, exc)
            return ActionResult(success=False, error=str(exc))
