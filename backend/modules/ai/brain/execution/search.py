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
from typing import Any, Dict, List, Optional
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
        from modules.ai.brain.execution.runtime_factory import build_commerce_runtime

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

        source = str(decision.args.get("source") or "").strip().lower()
        if source.startswith("selection_context") and decision.args.get("products"):
            products = list(decision.args.get("products") or [])
            presentation = str(decision.args.get("selection_presentation_text") or "").strip()
            return ActionResult(
                success=True,
                data={
                    "products": products,
                    "product_lines": presentation,
                    "count": len(products),
                    "query": str(decision.args.get("query") or ""),
                    "suggest_narrow": False,
                    "selection_presentation_text": presentation,
                    "discovery_output_kind": "products",
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

        try:
            from ..catalog.catalog_browse_turn_policy import (  # noqa: PLC0415
                stamp_catalog_browse_scope_for_turn,
            )

            stamp_catalog_browse_scope_for_turn(
                ctx,
                query=str(decision.args.get("query") or ""),
            )
        except Exception:  # noqa: BLE001
            logger.exception("[PRODUCT_SEARCH] early_browse_scope_failed")

        def _apply_category_scope(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            return filter_products_for_browse_turn(
                products,
                message=ctx.message or "",
                query=str(decision.args.get("query") or ""),
                source=source,
                last_browse_query=str(getattr(state, "last_browse_query", "") or ""),
                state=state,
                db=getattr(ctx, "_db", None),
                tenant_id=getattr(ctx, "tenant_id", None),
            )

        def _format_result(
            products: List[Dict[str, Any]],
            *,
            query: str,
            browse_pool: List[Dict[str, Any]] | None = None,
            browse_offset: int | None = None,
            attach_discovery_presentation: bool = False,
            catalog_fact_products: List[Dict[str, Any]] | None = None,
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
            selected_product = resolve_search_result_product_for_focus(
                products=products,
                catalog_fact_products=catalog_fact_products,
                query=query,
            )
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
            if catalog_fact_products:
                payload["catalog_fact_products"] = list(catalog_fact_products)
            if attach_discovery_presentation:
                payload = _attach_discovery_presentation(
                    payload,
                    decision=decision,
                    ctx=ctx,
                    source=source,
                )
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

        source = str(decision.args.get("source") or "").strip().lower()
        entry_type = str((decision.args or {}).get("discovery_entry_type") or "").strip().lower()
        discovery_mode = str((decision.args or {}).get("discovery_mode") or "").strip().lower()
        query_s = str(query or "").strip()

        if discovery_mode == "collections_first" and source != "show_more":
            strategy_result = _apply_discovery_strategy(
                [],
                decision=decision,
                ctx=ctx,
                query=query_s,
                source=source or "browse_catalog_groups",
            )
            if strategy_result is not None:
                return strategy_result

        if (
            source in {
                "top_products",
                "top_products_start_order",
                "top_products_replay_fallback",
                "top_products_numeric_fallback",
            }
            or entry_type == "top_products"
        ):
            from ..catalog.catalog_ranking_runtime import load_best_seller_catalog_products  # noqa: PLC0415

            best_sellers = load_best_seller_catalog_products(
                getattr(ctx, "_db", None),
                ctx.tenant_id,
                message=ctx.message or "",
                query=str(query or ""),
                state=state,
                group_id=decision.args.get("catalog_group_id"),
                limit=fetch_limit,
            )
            if best_sellers:
                products = _apply_category_scope(_apply_affinity_boost(best_sellers, ctx))
                return _format_result(
                    products,
                    query=str(query or ""),
                    attach_discovery_presentation=bool(
                        getattr(state, "last_discovery_mode", None)
                        or (decision.args or {}).get("discovery_mode")
                    ),
                )

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
                runtime = build_commerce_runtime(ctx)
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
                attach_discovery_presentation=bool(
                    getattr(state, "last_discovery_mode", None)
                    or (decision.args or {}).get("discovery_mode")
                ),
            )

        try:
            from ..persona.catalog_product_answer import classify_catalog_question_kind  # noqa: PLC0415

            _qa_kind = classify_catalog_question_kind(
                str(ctx.message or ""),
                query=str(query or (decision.args or {}).get("query") or ""),
                decision_args=dict(decision.args or {}),
            )
            _include_catalog_facts = _qa_kind in {"price", "availability"}

            runtime = build_commerce_runtime(ctx)

            async def _run_catalog_search(search_query: str) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
                tool_payload: Dict[str, Any] = {
                    "query": search_query,
                    "limit": fetch_limit,
                }
                if _include_catalog_facts:
                    tool_payload["include_non_orderable_facts"] = True
                runtime_result = await runtime.execute("search_products", tool_payload)
                return (
                    list(runtime_result.payload.get("products") or []),
                    list(runtime_result.payload.get("catalog_fact_products") or []),
                )

            products, catalog_fact_products = await _run_catalog_search(str(query or ""))

            # Intelligent retry when exact FTS misses but subject is resolved.
            if not products and not catalog_fact_products and str(query or "").strip():
                from ..clarification.resolved_product_guard import (  # noqa: PLC0415
                    search_retry_queries,
                )
                for alt_query in search_retry_queries(str(query)):
                    retry_products, retry_facts = await _run_catalog_search(alt_query)
                    if retry_products or retry_facts:
                        logger.info(
                            "[SearchHandler] retry_hit | tenant=%s orig=%r "
                            "alt=%r count=%d facts=%d",
                            ctx.tenant_id,
                            query,
                            alt_query,
                            len(retry_products),
                            len(retry_facts),
                        )
                        products = retry_products
                        catalog_fact_products = retry_facts
                        break

            # If search produced nothing but products exist → fallback to top sellers
            if not products and not catalog_fact_products:
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

            if not products and not catalog_fact_products:
                return ActionResult(
                    success=False,
                    error="no_products",
                    data={"message": "no_products_in_catalog"},
                )

            products = _apply_category_scope(products)
            had_catalog_fact_products = bool(catalog_fact_products)
            if catalog_fact_products:
                catalog_fact_products = _apply_category_scope(catalog_fact_products)
            # Fact-bearing price/availability searches keep structured facts and
            # focus identity via _format_result — discovery presentation must not
            # short-circuit with an empty browse payload.
            if not had_catalog_fact_products:
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
                catalog_fact_products=catalog_fact_products,
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


def resolve_search_result_product_for_focus(
    *,
    products: List[Dict[str, Any]],
    catalog_fact_products: List[Dict[str, Any]] | None,
    query: str,
) -> Optional[Dict[str, Any]]:
    """Pick a singular catalog identity for pipeline focus from search results.

    Orderable-only singularity, fact-only singularity, or no focus when channels
    are mixed or ambiguous. Availability truth stays in facts/guards, not here.
    """
    orderable = list(products or [])
    facts = list(catalog_fact_products or [])

    if orderable and facts:
        return None

    if len(orderable) == 1:
        return orderable[0]
    if orderable:
        return None

    if len(facts) != 1:
        return None

    from ..product_discovery_gate import is_generic_category_noun  # noqa: PLC0415

    if is_generic_category_noun(str(query or "")):
        return None

    product = facts[0]
    if not isinstance(product, dict):
        return None

    has_catalog_identity = any(
        str(product.get(key) or "").strip()
        for key in ("external_id", "id", "product_id", "sku")
    )
    if not has_catalog_identity:
        return None

    return product


def resolve_confirmed_discovery_product(
    *,
    entry_type: str,
    discovery_output_kind: str,
    presented_products: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Return a singular discovery product for focus pinning, else None.

    Only ``product_specific`` discovery with exactly one presented product
    carrying a canonical catalog identity qualifies. Category/global browse,
    collections, multi-product lists, and title-only rows never pin.
    """
    from ..discovery.entry import PRODUCT_SPECIFIC  # noqa: PLC0415

    if str(entry_type or "").strip().lower() != PRODUCT_SPECIFIC:
        return None
    if str(discovery_output_kind or "").strip().lower() != "products":
        return None
    if len(presented_products or []) != 1:
        return None

    product = presented_products[0]
    if not isinstance(product, dict):
        return None

    has_catalog_identity = any(
        str(product.get(key) or "").strip()
        for key in ("external_id", "id", "product_id", "sku")
    )
    if not has_catalog_identity:
        return None

    return product


def _attach_discovery_presentation(
    payload: Dict[str, Any],
    *,
    decision: Decision,
    ctx: BrainContext,
    source: str,
    plan: Any | None = None,
    strategy: Any | None = None,
) -> Dict[str, Any]:
    """Phase 3 — evidence-based discovery reply text on executor payload."""
    from ..catalog.discovery_presenter import (  # noqa: PLC0415
        DiscoveryPresentationComposer,
        resolve_strategy_for_presentation,
    )
    from ..commerce.merchant_discovery_settings import parse_merchant_discovery_settings  # noqa: PLC0415

    args = getattr(decision, "args", None) or {}
    settings_raw = args.get("discovery_settings")
    merchant_settings = parse_merchant_discovery_settings(
        settings_raw if isinstance(settings_raw, dict) else {}
    )
    composer = DiscoveryPresentationComposer()
    resolved_strategy = strategy or resolve_strategy_for_presentation(
        args,
        state=getattr(ctx, "state", None),
    )
    entry_source = str(source or args.get("source") or "").strip().lower()
    entry_type = str(args.get("discovery_entry_type") or "").strip().lower()
    query = str(payload.get("query") or args.get("query") or ctx.message or "")

    if plan is not None:
        presentation = composer.compose(
            plan=plan,
            strategy=resolved_strategy,
            entry_source=entry_source,
            entry_type=entry_type,
            merchant_settings=merchant_settings,
            query=query,
        )
    else:
        presentation = composer.compose_products(
            list(payload.get("products") or []),
            strategy=resolved_strategy,
            entry_source=entry_source,
            entry_type=entry_type,
            merchant_settings=merchant_settings,
            query=query,
        )

    out = dict(payload)
    out["discovery_presentation_text"] = presentation.text
    out["discovery_output_kind"] = presentation.output_kind
    out["product_lines"] = presentation.text
    if presentation.products:
        out["products"] = presentation.products
        out["count"] = len(presentation.products)
    if presentation.collections:
        out["collections"] = presentation.collections
    if plan is not None and getattr(plan, "evidence", None):
        out["discovery_plan"] = dict(plan.evidence)
    return out


def _apply_discovery_strategy(
    products: List[Dict[str, Any]],
    *,
    decision: Decision,
    ctx: BrainContext,
    query: str,
    source: str,
) -> ActionResult | None:
    """Apply Phase 2 catalog intelligence + Phase 3/4A presentation composer."""
    args = getattr(decision, "args", None) or {}
    mode = str(args.get("discovery_mode") or "").strip().lower()
    if not mode or source == "show_more":
        return None

    try:
        from ..commerce.discovery_strategy import strategy_from_decision_args  # noqa: PLC0415
        from ..commerce.merchant_discovery_settings import parse_merchant_discovery_settings  # noqa: PLC0415
        from ..catalog.catalog_intelligence import (  # noqa: PLC0415
            CatalogIntelligence,
            DiscoveryPlan,
            attach_discovery_signals_from_db,
        )
        from ..catalog.catalog_provider import get_catalog_provider  # noqa: PLC0415
        from ..catalog.discovery_presenter import DiscoveryPresentationComposer  # noqa: PLC0415
        from ..catalog.presentation_contract import validate_discovery_products  # noqa: PLC0415

        strategy = strategy_from_decision_args(args)
        merchant_settings = parse_merchant_discovery_settings(
            args.get("discovery_settings") if isinstance(args.get("discovery_settings"), dict) else {}
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
        from ..catalog.catalog_browse_scope_resolver import load_merchant_catalog_groups  # noqa: PLC0415
        intel = CatalogIntelligence(provider)
        catalog_groups = load_merchant_catalog_groups(db, ctx.tenant_id)
        plan = intel.build_discovery_plan(
            strategy=strategy,
            query=query,
            source=source,
            preferred_collections=args.get("discovery_preferred_collections"),
            merchant_settings=merchant_settings,
            merchant_catalog_groups=catalog_groups,
        )
        composer = DiscoveryPresentationComposer()
        entry_type = str(args.get("discovery_entry_type") or "").strip().lower()

        if plan.output_kind == "collections" and len(plan.collections) == 1:
            group = plan.collections[0]
            raw = provider.get_collection_products(
                group.group_name,
                limit=max(12, strategy.initial_count * 4),
            )
            enriched = attach_discovery_signals_from_db(
                list(raw or []),
                db=db,
                tenant_id=ctx.tenant_id,
            )
            ranked = validate_discovery_products(
                intel.rank_products(
                    enriched,
                    strategy=strategy,
                    merchant_settings=merchant_settings,
                    collection=merchant_settings.match_collection(group.group_name),
                ),
            )
            if ranked:
                plan = DiscoveryPlan(
                    output_kind="products",
                    products=ranked[: max(1, strategy.initial_count)],
                    presentation=strategy.presentation,
                    evidence={
                        **dict(plan.evidence),
                        "single_collection_pivot": group.group_name,
                    },
                )

        if plan.output_kind == "products":
            enriched = attach_discovery_signals_from_db(
                list(products or plan.products),
                db=db,
                tenant_id=ctx.tenant_id,
            )
            matched_collection = merchant_settings.match_collection(query)
            ranked_full = intel.rank_products(
                enriched,
                strategy=strategy,
                merchant_settings=merchant_settings,
                collection=matched_collection,
            )
            ranked = validate_discovery_products(ranked_full)
            if not ranked:
                empty = composer.compose(
                    plan=DiscoveryPlan(output_kind="empty"),
                    strategy=strategy,
                    entry_source=source,
                    entry_type=entry_type,
                    merchant_settings=merchant_settings,
                    query=query,
                )
                logger.info(
                    "[SearchHandler] discovery_empty | tenant=%s mode=%s",
                    ctx.tenant_id,
                    mode,
                )
                return ActionResult(
                    success=True,
                    data={
                        "products": [],
                        "product_lines": empty.text,
                        "count": 0,
                        "query": query,
                        "suggest_narrow": False,
                        "discovery_output_kind": "empty",
                        "discovery_presentation_text": empty.text,
                        "discovery_plan": plan.evidence,
                    },
                )
            shown = ranked[: max(1, strategy.initial_count)]
            plan = DiscoveryPlan(
                output_kind="products",
                products=shown,
                presentation=strategy.presentation,
                evidence={
                    **dict(plan.evidence),
                    "ranked_count": len(ranked),
                },
            )
            browse_pool = ranked
        else:
            browse_pool = []

        presentation = composer.compose(
            plan=plan,
            strategy=strategy,
            entry_source=source,
            entry_type=entry_type,
            merchant_settings=merchant_settings,
            query=query,
        )
        payload: Dict[str, Any] = {
            "products": list(presentation.products or []),
            "collections": list(presentation.collections or []),
            "product_lines": presentation.text,
            "discovery_presentation_text": presentation.text,
            "count": len(presentation.products or []),
            "query": query,
            "suggest_narrow": False,
            "discovery_output_kind": presentation.output_kind,
            "discovery_plan": plan.evidence,
        }
        if presentation.output_kind == "products" and browse_pool:
            payload["browse_pool"] = browse_pool
            payload["browse_offset"] = 0
            payload["suggest_narrow"] = len(browse_pool) > strategy.initial_count

        payload["product"] = resolve_confirmed_discovery_product(
            entry_type=entry_type,
            discovery_output_kind=presentation.output_kind,
            presented_products=list(presentation.products or []),
        )

        logger.info(
            "[SearchHandler] discovery_presented | tenant=%s mode=%s kind=%s count=%d",
            ctx.tenant_id,
            mode,
            presentation.output_kind,
            payload["count"],
        )
        return ActionResult(success=True, data=payload)
    except Exception:
        logger.exception(
            "[SearchHandler] discovery_strategy_failed tenant=%s",
            getattr(ctx, "tenant_id", None),
        )
        return None
