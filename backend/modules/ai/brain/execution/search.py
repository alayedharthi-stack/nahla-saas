"""
brain/execution/search.py
──────────────────────────
ProductSearchHandler: executes ACTION_SEARCH_PRODUCTS.

Delegates to CatalogContextBuilder (the existing, battle-tested search
layer). Returns structured product dicts AND a formatted Arabic text block
ready for the composer.

Affinity boost
──────────────
After the catalog search, results are re-ranked by each product's
``affinity_score`` from the ``ProductAffinity`` table.  Products the
current customer has viewed or purchased before float to the top.
The boost is best-effort — any DB failure falls back to the original
catalog order and is only logged at DEBUG level.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List
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

        from ..commerce.product_breadth_policy import (  # noqa: PLC0415
            _product_key,
            next_catalog_browse_batch,
            resolve_product_breadth_from_context,
        )
        from ..commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            filter_products_for_browse_turn,
        )
        from ..product_discovery_gate import allows_search_top_products_fallback  # noqa: PLC0415
        breadth = resolve_product_breadth_from_context(ctx, decision)
        fetch_limit = breadth.search_fetch_limit
        source = str(decision.args.get("source") or "").strip().lower()
        state = getattr(ctx, "state", None)

        def _apply_category_scope(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            return filter_products_for_browse_turn(
                products,
                message=ctx.message or "",
                query=str(decision.args.get("query") or ""),
                source=source,
                last_browse_query=str(getattr(state, "last_browse_query", "") or ""),
                state=state,
            )

        def _format_result(
            products: List[Dict[str, Any]],
            *,
            query: str,
            browse_pool: List[Dict[str, Any]] | None = None,
            browse_offset: int | None = None,
        ) -> ActionResult:
            lines = []
            for p in products:
                price_str = f"{p['price']} ريال" if p.get("price") else "السعر غير محدد"
                line = f"• {p['title']} — {price_str}"
                if p.get("sku"):
                    line += f" [SKU: {p['sku']}]"
                lines.append(line)
            after_search = decision.args.get("after_search", "")
            suggest_narrow = len(products) > breadth.display_limit and not after_search
            selected_product = products[0] if len(products) == 1 else None
            payload: Dict[str, Any] = {
                "products": products,
                "product_lines": "\n".join(lines),
                "count": len(products),
                "query": query,
                "suggest_narrow": suggest_narrow,
                "after_search": after_search,
                "product": selected_product,
                "product_breadth": breadth.to_log_dict(),
            }
            if browse_pool is not None:
                payload["browse_pool"] = browse_pool
            if browse_offset is not None:
                payload["browse_offset"] = browse_offset
            return ActionResult(success=True, data=payload)

        # Verbatim repeat — same list the customer already saw.
        replay_candidates = decision.args.get("replay_candidates")
        if replay_candidates:
            from core.catalog import is_catalog_active  # noqa: PLC0415

            active_replay = [
                p for p in list(replay_candidates)
                if is_catalog_active(p)
            ]
            if not active_replay:
                return ActionResult(
                    success=False,
                    error="replay_products_inactive",
                    data={"message": "replay_products_inactive"},
                )
            products = _apply_category_scope(_apply_affinity_boost(active_replay, ctx))
            return _format_result(products, query=str(decision.args.get("query") or ""))

        query = decision.args.get("query", ctx.message)

        # Progressive browse — next unseen slice, never repeat last turn.
        if source == "show_more":
            pool = _apply_category_scope(list(getattr(state, "catalog_browse_pool", None) or []))
            offset = int(getattr(state, "catalog_browse_offset", 0) or 0)
            shown_keys = [
                _product_key(p)
                for p in (getattr(state, "last_search_candidates", None) or [])
            ]
            batch, next_offset = next_catalog_browse_batch(
                pool,
                offset=offset,
                exclude_keys=shown_keys,
                limit=fetch_limit,
            )
            refreshed_pool = pool
            if not batch:
                runtime = CommerceToolRuntime(
                    ctx._db,  # type: ignore[attr-defined]
                    tenant_id=ctx.tenant_id,
                    customer_phone=ctx.customer_phone,
                    customer_id=ctx.customer_id,
                    tenant_context=ctx.tenant_context,
                )
                q = str(query or getattr(state, "last_browse_query", "") or "")
                runtime_result = await runtime.execute(
                    "search_products",
                    {"query": q, "limit": max(fetch_limit, 12)},
                )
                refreshed_pool = list(runtime_result.payload.get("products") or [])
                if not refreshed_pool and not q:
                    if allows_search_top_products_fallback(
                        ctx,
                        query="",
                        source="show_more",
                        message=ctx.message,
                    ):
                        runtime_result = await runtime.execute(
                            "search_products",
                            {"limit": max(fetch_limit, 12)},
                        )
                        refreshed_pool = list(runtime_result.payload.get("products") or [])
                refreshed_pool = _apply_category_scope(_apply_affinity_boost(refreshed_pool, ctx))
                seen = {
                    _product_key(p)
                    for p in (pool + list(getattr(state, "last_search_candidates", None) or []))
                }
                batch, next_offset = next_catalog_browse_batch(
                    refreshed_pool,
                    offset=0,
                    exclude_keys=list(seen),
                    limit=fetch_limit,
                )
            products = _apply_category_scope(_apply_affinity_boost(batch, ctx))
            logger.info(
                "[ORDER FLOW] show_more_batch | tenant=%s shown_before=%d "
                "batch=%d next_offset=%d pool=%d",
                ctx.tenant_id,
                len(shown_keys),
                len(products),
                next_offset,
                len(refreshed_pool),
            )
            if not products:
                return ActionResult(
                    success=False,
                    error="no_more_products",
                    data={"message": "no_more_products"},
                )
            return _format_result(
                products,
                query=str(query or getattr(state, "last_browse_query", "") or ""),
                browse_pool=refreshed_pool,
                browse_offset=next_offset,
            )

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
                {"query": query, "limit": fetch_limit},
            )
            products = list(runtime_result.payload.get("products") or [])

            # Intelligent retry when exact FTS misses but subject is resolved.
            if not products and str(query or "").strip():
                from ..clarification.resolved_product_guard import (  # noqa: PLC0415
                    search_retry_queries,
                )
                for alt_query in search_retry_queries(str(query)):
                    retry_result = await runtime.execute(
                        "search_products",
                        {"query": alt_query, "limit": fetch_limit},
                    )
                    retry_products = list(retry_result.payload.get("products") or [])
                    if retry_products:
                        logger.info(
                            "[SearchHandler] retry_hit | tenant=%s orig=%r "
                            "alt=%r count=%d",
                            ctx.tenant_id, query, alt_query, len(retry_products),
                        )
                        products = retry_products
                        break

            # If search produced nothing but products exist → fallback to top sellers
            if not products:
                if allows_search_top_products_fallback(
                    ctx,
                    query=str(query or ""),
                    source=source,
                    message=ctx.message,
                ):
                    runtime_result = await runtime.execute(
                        "search_products",
                        {"limit": fetch_limit},
                    )
                    products = list(runtime_result.payload.get("products") or [])
                else:
                    return ActionResult(
                        success=False,
                        error="no_search_hits",
                        data={"message": "no_search_hits_no_top_fallback"},
                    )

            if not products:
                return ActionResult(
                    success=False,
                    error="no_products",
                    data={"message": "no_products_in_catalog"},
                )

            products = _apply_category_scope(products)
            strategy_result = _apply_discovery_strategy(
                products,
                decision=decision,
                ctx=ctx,
                query=str(query or ""),
                source=source,
            )
            if strategy_result is not None:
                return strategy_result

            # Re-rank by customer affinity before formatting lines
            products = _apply_affinity_boost(products, ctx)

            return _format_result(
                products,
                query=str(query or ""),
                browse_pool=products,
                browse_offset=0,
            )

        except Exception as exc:
            logger.exception("[SearchHandler] error for tenant=%s query=%r: %s", ctx.tenant_id, query, exc)
            return ActionResult(success=False, error=str(exc))


# ── Affinity boost ─────────────────────────────────────────────────────────────

def _apply_affinity_boost(
    products: List[Dict[str, Any]],
    ctx: BrainContext,
) -> List[Dict[str, Any]]:
    """Re-rank products by the customer's historical affinity score.

    Products the customer has previously viewed or purchased (affinity_score > 0)
    float to the top of the list.  Within each group (affinity vs. no-affinity)
    the original catalog order is preserved (stable sort).

    Each product dict gets an ``affinity_score`` key set (0.0 when unknown),
    so downstream consumers (e.g. DecisionEngine) can read it without a DB call.

    Falls back to the original order on any failure — the result is always a
    valid, non-empty list.
    """
    if not products or not ctx.customer_id:
        return products

    product_ids = [p.get("id") for p in products if p.get("id") is not None]
    if not product_ids:
        return products

    try:
        from database.models import ProductAffinity  # noqa: PLC0415

        rows = (
            ctx._db.query(ProductAffinity)  # type: ignore[attr-defined]
            .filter(
                ProductAffinity.tenant_id   == ctx.tenant_id,
                ProductAffinity.customer_id == ctx.customer_id,
                ProductAffinity.product_id.in_(product_ids),
            )
            .all()
        )

        score_map: Dict[Any, float] = {row.product_id: float(row.affinity_score or 0.0) for row in rows}

        if not score_map:
            # No affinity data — still attach zero scores for consistency
            for p in products:
                p.setdefault("affinity_score", 0.0)
            return products

        # Attach score to each product dict
        for p in products:
            p["affinity_score"] = score_map.get(p.get("id"), 0.0)

        # Stable sort: higher affinity first
        boosted = sorted(products, key=lambda p: p.get("affinity_score", 0.0), reverse=True)

        boosted_count = sum(1 for p in boosted if p.get("affinity_score", 0.0) > 0.0)
        logger.info(
            "[SearchHandler] affinity rerank | customer=%s boosted=%d/%d top=%r score=%.2f tenant=%s",
            ctx.customer_id,
            boosted_count,
            len(boosted),
            (boosted[0].get("title") or "")[:40] if boosted else "",
            (boosted[0].get("affinity_score") or 0.0) if boosted else 0.0,
            ctx.tenant_id,
        )
        return boosted

    except Exception as exc:
        logger.debug(
            "[SearchHandler] affinity boost skipped (non-fatal) | customer=%s error=%s",
            ctx.customer_id, exc,
        )
        # Ensure score key exists even on failure
        for p in products:
            p.setdefault("affinity_score", 0.0)
        return products


def _apply_discovery_strategy(
    products: List[Dict[str, Any]],
    *,
    decision: Decision,
    ctx: BrainContext,
    query: str,
    source: str,
) -> ActionResult | None:
    """Apply Phase 2 catalog intelligence + presentation contract."""
    args = getattr(decision, "args", None) or {}
    mode = str(args.get("discovery_mode") or "").strip().lower()
    if not mode or source == "show_more":
        return None

    try:
        from ..commerce.discovery_strategy import strategy_from_decision_args  # noqa: PLC0415
        from ..catalog.catalog_intelligence import (  # noqa: PLC0415
            CatalogIntelligence,
            attach_discovery_signals_from_db,
        )
        from ..catalog.catalog_provider import get_catalog_provider  # noqa: PLC0415
        from ..catalog.presentation_contract import validate_discovery_products  # noqa: PLC0415

        strategy = strategy_from_decision_args(args)
        settings_raw = args.get("discovery_settings")
        from ..commerce.merchant_discovery_settings import parse_merchant_discovery_settings  # noqa: PLC0415

        merchant_settings = parse_merchant_discovery_settings(
            settings_raw if isinstance(settings_raw, dict) else {}
        )
        db = getattr(ctx, "_db", None)
        platform = str(getattr(getattr(ctx, "facts", None), "integration_platform", "") or "")
        if db is None:
            return None

        provider = get_catalog_provider(
            db,
            ctx.tenant_id,
            integration_platform=platform,
        )
        intel = CatalogIntelligence(provider)
        plan = intel.build_discovery_plan(
            strategy=strategy,
            query=query,
            source=source,
            preferred_collections=args.get("discovery_preferred_collections"),
            merchant_settings=merchant_settings,
        )

        if plan.output_kind == "collections":
            from ..catalog.discovery_presenter import compose_merchant_collections  # noqa: PLC0415

            collections = [group.to_dict() for group in plan.collections]
            presentation = compose_merchant_collections(
                plan.collections,
                merchant_settings=merchant_settings,
            )
            logger.info(
                "[SearchHandler] discovery_collections | tenant=%s count=%d mode=%s",
                ctx.tenant_id,
                len(collections),
                mode,
            )
            return ActionResult(
                success=True,
                data={
                    "products": [],
                    "collections": collections,
                    "product_lines": presentation,
                    "discovery_presentation_text": presentation,
                    "count": 0,
                    "query": query,
                    "suggest_narrow": False,
                    "discovery_output_kind": "collections",
                    "discovery_plan": plan.evidence,
                },
            )

        enriched = attach_discovery_signals_from_db(
            list(products or plan.products),
            db=db,
            tenant_id=ctx.tenant_id,
        )
        matched_collection = merchant_settings.match_collection(query)
        ranked = intel.rank_products(
            enriched,
            strategy=strategy,
            merchant_settings=merchant_settings,
            collection=matched_collection,
        )
        ranked = validate_discovery_products(ranked)
        if not ranked:
            return None

        from ..catalog.discovery_presenter import compose_collection_products  # noqa: PLC0415

        presentation = compose_collection_products(
            ranked[: strategy.initial_count],
            collection=matched_collection,
            merchant_settings=merchant_settings,
            collection_label=matched_collection.label if matched_collection else "",
        )

        logger.info(
            "[SearchHandler] discovery_ranked | tenant=%s mode=%s count=%d top_score=%s",
            ctx.tenant_id,
            mode,
            len(ranked),
            ranked[0].get("discovery_score") if ranked else "-",
        )
        return ActionResult(
            success=True,
            data={
                "products": ranked[: strategy.initial_count],
                "product_lines": presentation,
                "discovery_presentation_text": presentation,
                "count": len(ranked[: strategy.initial_count]),
                "query": query,
                "suggest_narrow": len(ranked) > strategy.initial_count,
                "discovery_output_kind": "products",
                "discovery_plan": plan.evidence,
                "browse_pool": ranked,
                "browse_offset": 0,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[SearchHandler] discovery_strategy_skipped tenant=%s err=%s",
            getattr(ctx, "tenant_id", None),
            exc,
        )
        return None
