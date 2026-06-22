"""
execution/catalog_navigate.py
─────────────────────────────
CatalogNavigateHandler — owned executor for ACTION_CATALOG_NAVIGATE.

Renders groups / group products / top fallback without search fallback or LLM.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..catalog.navigation import (
    PATH_GROUPS,
    PATH_GROUP_PRODUCTS,
    PATH_TOP_FALLBACK,
    STEP_SHOW_GROUP_PRODUCTS,
    STEP_SHOW_GROUPS,
    STEP_TOP_FALLBACK,
    TURN_OWNER,
    owner_reply_hash,
)
from ..types import ActionResult, BrainContext, Decision

logger = logging.getLogger("nahla.brain.execution.catalog_navigate")


class CatalogNavigateHandler:
    """Handles ACTION_CATALOG_NAVIGATE with protected ownership metadata."""

    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        args = getattr(decision, "args", None) or {}
        step = str(args.get("navigator_step") or "").strip().lower()
        chosen_path = str(args.get("chosen_path") or "").strip()
        owner_step = str(args.get("owner_step") or "").strip()

        try:
            if step == STEP_SHOW_GROUPS:
                payload = await self._render_groups(decision, ctx)
            elif step == STEP_SHOW_GROUP_PRODUCTS:
                payload = await self._render_group_products(decision, ctx)
            elif step == STEP_TOP_FALLBACK:
                payload = await self._render_top_fallback(decision, ctx)
            else:
                return ActionResult(success=False, error=f"unknown_navigator_step:{step}")

            reply = str(payload.get("discovery_presentation_text") or payload.get("product_lines") or "")
            payload.update({
                "turn_owner": TURN_OWNER,
                "owner_locked": True,
                "owner_step": owner_step or step,
                "chosen_path": chosen_path or payload.get("chosen_path") or "",
                "owner_replaced": False,
                "navigator_owner": True,
                "owner_reply_hash": owner_reply_hash(reply),
            })
            logger.info(
                "[CATALOG_NAVIGATOR] executed tenant=%s step=%s path=%s kind=%s",
                getattr(ctx, "tenant_id", None),
                step,
                payload.get("chosen_path"),
                payload.get("discovery_output_kind"),
            )
            return ActionResult(success=True, data=payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[CATALOG_NAVIGATOR] failed tenant=%s step=%s err=%s",
                getattr(ctx, "tenant_id", None),
                step,
                exc,
            )
            return ActionResult(success=False, error=str(exc))

    async def _render_groups(self, decision: Decision, ctx: BrainContext) -> Dict[str, Any]:
        plan, strategy, merchant_settings = self._build_plan(
            decision,
            ctx,
            query="",
            source="browse_catalog_groups",
        )
        from ..catalog.discovery_presenter import DiscoveryPresentationComposer  # noqa: PLC0415

        composer = DiscoveryPresentationComposer()
        presentation = composer.compose(
            plan=plan,
            strategy=strategy,
            entry_source="browse_catalog_groups",
            entry_type="global_browse",
            merchant_settings=merchant_settings,
            query="",
        )
        collections = [
            c.to_dict() if hasattr(c, "to_dict") else dict(c)
            for c in list(presentation.collections or plan.collections or [])
        ]
        return {
            "products": [],
            "collections": collections,
            "product_lines": presentation.text,
            "discovery_presentation_text": presentation.text,
            "discovery_output_kind": presentation.output_kind,
            "chosen_path": PATH_GROUPS,
            "count": 0,
            "query": "",
            "discovery_plan": dict(getattr(plan, "evidence", None) or {}),
            "navigation_state_patch": self._navigation_patch(
                decision,
                collections=collections,
                products=[],
                source="groups",
            ),
        }

    async def _render_group_products(self, decision: Decision, ctx: BrainContext) -> Dict[str, Any]:
        args = getattr(decision, "args", None) or {}
        query = str(args.get("query") or "").strip()
        group_id = args.get("catalog_group_id")
        plan, strategy, merchant_settings = self._build_plan(
            decision,
            ctx,
            query=query,
            source="collections_first",
            catalog_group_id=group_id,
        )
        from ..catalog.discovery_presenter import DiscoveryPresentationComposer  # noqa: PLC0415
        from ..catalog.catalog_intelligence import attach_discovery_signals_from_db  # noqa: PLC0415
        from ..catalog.presentation_contract import validate_discovery_products  # noqa: PLC0415

        db = getattr(ctx, "_db", None)
        products = list(plan.products or [])
        if db is not None and products:
            products = validate_discovery_products(
                attach_discovery_signals_from_db(products, db=db, tenant_id=ctx.tenant_id),
            )
            shown = products[: max(1, strategy.initial_count)]
            plan = type(plan)(
                output_kind="products",
                products=shown,
                collections=list(plan.collections or []),
                guided_question=plan.guided_question,
                presentation=plan.presentation,
                evidence={**dict(plan.evidence), "group_products": query},
            )

        composer = DiscoveryPresentationComposer()
        presentation = composer.compose(
            plan=plan,
            strategy=strategy,
            entry_source="collections_first",
            entry_type="global_browse",
            merchant_settings=merchant_settings,
            query=query,
        )
        products_out = list(presentation.products or plan.products or [])
        current_group = (args.get("navigation_state_patch") or {}).get("current_catalog_group")
        if not current_group:
            current_group = {
                "group_id": str(args.get("catalog_group_id") or ""),
                "group_slug": str(args.get("catalog_group_slug") or ""),
                "group_name": query,
            }
        return {
            "products": products_out,
            "collections": [],
            "product_lines": presentation.text,
            "discovery_presentation_text": presentation.text,
            "discovery_output_kind": presentation.output_kind,
            "chosen_path": PATH_GROUP_PRODUCTS,
            "count": len(products_out),
            "query": query,
            "discovery_plan": dict(getattr(plan, "evidence", None) or {}),
            "navigation_state_patch": self._navigation_patch(
                decision,
                collections=[],
                products=products_out,
                source="group_products",
                current_catalog_group=current_group,
                selected_collection=str(args.get("catalog_group_id") or args.get("catalog_group_slug") or ""),
            ),
        }

    async def _render_top_fallback(self, decision: Decision, ctx: BrainContext) -> Dict[str, Any]:
        from ..catalog.catalog_ranking_runtime import load_best_seller_catalog_products  # noqa: PLC0415
        from ..catalog.discovery_presenter import DiscoveryPresentationComposer  # noqa: PLC0415
        from ..commerce.merchant_discovery_settings import parse_merchant_discovery_settings  # noqa: PLC0415

        args = getattr(decision, "args", None) or {}
        strategy = self._resolve_strategy(decision, ctx, collection_count=0)
        merchant_settings = parse_merchant_discovery_settings(
            args.get("discovery_settings") if isinstance(args.get("discovery_settings"), dict) else {}
        )
        products = load_best_seller_catalog_products(
            getattr(ctx, "_db", None),
            ctx.tenant_id,
            message=ctx.message or "",
            query="",
            state=getattr(ctx, "state", None),
            limit=max(12, getattr(strategy, "initial_count", 3) * 4),
        )
        composer = DiscoveryPresentationComposer()
        presentation = composer.compose_products(
            list(products or []),
            strategy=strategy,
            entry_source="top_products",
            entry_type="top_products",
            merchant_settings=merchant_settings,
            query="",
        )
        products_out = list(presentation.products or products or [])
        return {
            "products": products_out,
            "collections": [],
            "product_lines": presentation.text,
            "discovery_presentation_text": presentation.text,
            "discovery_output_kind": presentation.output_kind,
            "chosen_path": PATH_TOP_FALLBACK,
            "count": len(products_out),
            "query": "",
            "navigator_no_groups_fallback": True,
            "navigation_state_patch": self._navigation_patch(
                decision,
                collections=[],
                products=products_out,
                source="top_fallback",
            ),
        }

    def _resolve_strategy(
        self,
        decision: Decision,
        ctx: BrainContext,
        *,
        collection_count: int,
    ):
        from ..commerce.discovery_strategy import (  # noqa: PLC0415
            CatalogContextSnapshot,
            resolve_discovery_strategy,
            strategy_from_decision_args,
            strategy_to_decision_args,
        )
        from ..commerce.commerce_objective import (  # noqa: PLC0415
            COMMERCE_OBJECTIVE_DISCOVERY,
            get_commerce_objective,
        )
        from ..discovery.entry import GLOBAL_BROWSE  # noqa: PLC0415

        args = dict(getattr(decision, "args", None) or {})
        if not args.get("discovery_mode"):
            facts = getattr(ctx, "facts", None)
            resolved = resolve_discovery_strategy(
                commerce_objective=get_commerce_objective(ctx.state) or COMMERCE_OBJECTIVE_DISCOVERY,
                entry_type=GLOBAL_BROWSE,
                catalog_context=CatalogContextSnapshot(
                    product_count=int(getattr(facts, "product_count", 0) or 0),
                    collection_count=max(0, int(collection_count)),
                ),
            )
            args.update(strategy_to_decision_args(resolved))
        return strategy_from_decision_args(args)

    def _build_plan(
        self,
        decision: Decision,
        ctx: BrainContext,
        *,
        query: str,
        source: str,
        catalog_group_id: Any = None,
    ):
        from ..commerce.merchant_discovery_settings import parse_merchant_discovery_settings  # noqa: PLC0415
        from ..catalog.catalog_intelligence import CatalogIntelligence, DiscoveryPlan  # noqa: PLC0415
        from ..catalog.catalog_provider import get_catalog_provider  # noqa: PLC0415
        from ..catalog.catalog_browse_scope_resolver import load_merchant_catalog_groups  # noqa: PLC0415
        from ..catalog.presentation_contract import validate_discovery_products  # noqa: PLC0415
        from ..catalog.catalog_intelligence import attach_discovery_signals_from_db  # noqa: PLC0415

        args = dict(getattr(decision, "args", None) or {})
        db = getattr(ctx, "_db", None)
        catalog_groups = load_merchant_catalog_groups(db, ctx.tenant_id) if db is not None else []
        strategy = self._resolve_strategy(
            decision,
            ctx,
            collection_count=len(catalog_groups),
        )
        merchant_settings = parse_merchant_discovery_settings(
            args.get("discovery_settings") if isinstance(args.get("discovery_settings"), dict) else {}
        )
        platform = str(getattr(getattr(ctx, "facts", None), "integration_platform", "") or "")
        provider = get_catalog_provider(db, ctx.tenant_id, integration_platform=platform)
        intel = CatalogIntelligence(provider)

        if source == "collections_first" and query:
            raw = provider.get_collection_products(
                query,
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
                    collection=merchant_settings.match_collection(query),
                ),
            )
            plan = DiscoveryPlan(
                output_kind="products",
                products=ranked[: max(1, strategy.initial_count)],
                presentation=strategy.presentation,
                evidence={
                    "group_id": str(catalog_group_id or ""),
                    "group_name": query,
                    "source": source,
                },
            )
            return plan, strategy, merchant_settings

        plan = intel.build_discovery_plan(
            strategy=strategy,
            query=query,
            source=source,
            preferred_collections=args.get("discovery_preferred_collections"),
            merchant_settings=merchant_settings,
            merchant_catalog_groups=catalog_groups,
        )
        return plan, strategy, merchant_settings

    def _navigation_patch(
        self,
        decision: Decision,
        *,
        collections: List[Dict[str, Any]],
        products: List[Dict[str, Any]],
        source: str,
        current_catalog_group: Any = None,
        selected_collection: str = "",
    ) -> Dict[str, Any]:
        args_patch = dict((getattr(decision, "args", None) or {}).get("navigation_state_patch") or {})
        patch = {
            "last_presented_collections": collections,
            "last_presented_products": products,
            "last_presented_group_products": products,
            "selected_collection": selected_collection or args_patch.get("selected_collection", ""),
            "current_catalog_group": current_catalog_group or args_patch.get("current_catalog_group"),
            "catalog_navigation_source": source,
            "selection_context_turn": None,
        }
        patch.update({k: v for k, v in args_patch.items() if k not in patch or patch[k] in (None, "", [])})
        return patch


__all__ = ["CatalogNavigateHandler"]
