"""
execution/catalog_navigate.py
─────────────────────────────
CatalogNavigateHandler — owned executor for ACTION_CATALOG_NAVIGATE.

Renders groups / group products / top fallback without search fallback or LLM.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..catalog.navigation import (
    OWNER_STEP_TOP_FALLBACK,
    PATH_GROUPS,
    PATH_GROUP_PRODUCTS,
    PATH_NATIVE_CATALOG,
    PATH_TOP_FALLBACK,
    STEP_NATIVE_CATALOG_ENTRY,
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

        try:
            if step == STEP_NATIVE_CATALOG_ENTRY:
                payload = await self._render_native_catalog_entry(decision, ctx)
            elif step == STEP_SHOW_GROUPS:
                payload = await self._render_groups(decision, ctx)
            elif step == STEP_SHOW_GROUP_PRODUCTS:
                payload = await self._render_group_products(decision, ctx)
            elif step == STEP_TOP_FALLBACK:
                payload = await self._render_top_fallback(decision, ctx)
            else:
                return ActionResult(success=False, error=f"unknown_navigator_step:{step}")

            # Provenance is read AFTER rendering: a render step may legitimately
            # re-own the navigator step (native catalog falling back to the
            # top-products path), and downstream compose keys off chosen_path.
            # Priority is unchanged (decision args still win over payload) —
            # only the read happens at stamp time. Every other render step
            # leaves args untouched, so their stamping is byte-identical.
            stamped_args = getattr(decision, "args", None) or {}
            stamped_chosen_path = str(stamped_args.get("chosen_path") or "").strip()
            stamped_owner_step = str(stamped_args.get("owner_step") or "").strip()

            reply = str(payload.get("discovery_presentation_text") or payload.get("product_lines") or "")
            payload.update({
                "turn_owner": TURN_OWNER,
                "owner_locked": True,
                "owner_step": stamped_owner_step or step,
                "chosen_path": stamped_chosen_path or payload.get("chosen_path") or "",
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

    async def _render_native_catalog_entry(
        self,
        decision: Decision,
        ctx: BrainContext,
    ) -> Dict[str, Any]:
        args = getattr(decision, "args", None) or {}
        entry = dict(args.get("native_catalog_entry") or {})
        db = getattr(ctx, "_db", None)
        thumbnail = str(entry.get("thumbnail_product_retailer_id") or "").strip()
        if not thumbnail and db is not None:
            try:
                from core.native_catalog_capability import pick_thumbnail_retailer_id  # noqa: PLC0415

                thumbnail = pick_thumbnail_retailer_id(db, ctx.tenant_id)
            except Exception:  # noqa: BLE001
                thumbnail = ""

        if not thumbnail:
            # Native/Meta catalog isn't actually deliverable here (e.g.
            # unpublished catalog, synthetic/sku-only retailer ids, missing
            # connection...). Rendering the bare native-catalog body text
            # anyway told the customer to "choose from the list" while
            # attaching no list at all (production regression 2026-07-26:
            # customer_facing_text_debt=True, catalog_sent=False).
            logger.info(
                "[CATALOG_NAVIGATOR] native_catalog_thumbnail_unavailable_text_fallback "
                "tenant=%s",
                getattr(ctx, "tenant_id", None),
            )
            return await self._render_native_catalog_text_fallback(decision, ctx)

        from modules.ai.brain.commerce.catalog_body_policy import (  # noqa: PLC0415
            resolve_native_catalog_body_text,
        )

        return {
            "products": [],
            "collections": [],
            "product_lines": "",
            "discovery_presentation_text": "",
            "discovery_output_kind": "native_catalog",
            "chosen_path": PATH_NATIVE_CATALOG,
            "count": 0,
            "query": "",
            "native_catalog_entry": {
                "thumbnail_product_retailer_id": thumbnail,
                "body_text": resolve_native_catalog_body_text(
                    context_reply=str(args.get("catalog_context_reply") or ""),
                    inbound_customer_message=str(getattr(ctx, "message", "") or ""),
                ),
            },
            "navigation_state_patch": self._navigation_patch(
                decision,
                collections=[],
                products=[],
                source="native_catalog",
            ),
        }

    async def _render_native_catalog_text_fallback(
        self,
        decision: Decision,
        ctx: BrainContext,
    ) -> Dict[str, Any]:
        """Native/Meta catalog attachment is unavailable — reuse the general
        product search (empty query → ``CatalogContextBuilder.get_top_
        products``) so the reply is a real numbered list of currently
        orderable products. Deliberately bypasses merchant-curated
        best-seller ranking (a separate, opt-in feature unrelated to native
        catalog eligibility) and goes straight to the same synced-catalog
        query every plain "عندكم إيه؟" answer already uses.

        The navigator step is re-owned as ``STEP_TOP_FALLBACK`` /
        ``PATH_TOP_FALLBACK`` **on the decision args**, because ``handle()``
        and ``compose/responder.py`` both key provenance off
        ``decision.args["chosen_path"]``: the production decision carries
        ``chosen_path="commerce_entry_send_catalog"``, which would otherwise
        keep the response outside the ``PATH_TOP_FALLBACK`` branch and leave
        no persona surface for the reply.

        This method deliberately returns **no customer-facing text** — only
        the trusted ``products`` facts. ``compose/responder.py`` routes
        ``PATH_TOP_FALLBACK`` to ``try_compose_catalog_navigation_browse_
        answer`` (with ``build_catalog_navigation_emergency_outcome`` as the
        audited fail-closed path), so the wording — including how the list is
        presented and how an empty catalog is explained — stays LLM/persona
        owned per the natural-language rule in AGENTS.md.
        """
        from modules.ai.brain.execution.runtime_factory import build_commerce_runtime

        db = getattr(ctx, "_db", None)
        products: List[Dict[str, Any]] = []
        if db is not None and getattr(ctx, "tenant_id", None):
            try:
                runtime = build_commerce_runtime(ctx)
                tool_result = await runtime.execute("search_products", {"query": "", "limit": 12})
                if tool_result.ok:
                    products = list(tool_result.payload.get("products") or [])
            except Exception:  # noqa: BLE001  # noqa: silent-ok — text fallback is best-effort
                logger.exception(
                    "[CATALOG_NAVIGATOR] native_catalog_text_fallback_search_failed tenant=%s",
                    getattr(ctx, "tenant_id", None),
                )

        args = getattr(decision, "args", None)
        if isinstance(args, dict):
            args["navigator_step"] = STEP_TOP_FALLBACK
            args["owner_step"] = OWNER_STEP_TOP_FALLBACK
            args["chosen_path"] = PATH_TOP_FALLBACK

        return {
            "products": products,
            "collections": [],
            "product_lines": "",
            "discovery_presentation_text": "",
            "discovery_output_kind": "products" if products else "empty",
            "chosen_path": PATH_TOP_FALLBACK,
            "count": len(products),
            "query": "",
            "navigator_no_groups_fallback": False,
            "native_catalog_thumbnail_unavailable": True,
            "navigation_state_patch": self._navigation_patch(
                decision,
                collections=[],
                products=products,
                source="native_catalog_text_fallback",
            ),
        }

    async def _render_groups(self, decision: Decision, ctx: BrainContext) -> Dict[str, Any]:
        from ..catalog.collections_pagination import (  # noqa: PLC0415
            COLLECTIONS_BUTTON_PAGE_SIZE,
            normalize_collections_page,
        )
        from ..catalog.catalog_intelligence import DiscoveryPlan  # noqa: PLC0415

        args = getattr(decision, "args", None) or {}
        state = ctx.state
        offset = int(
            args.get("collections_offset")
            if args.get("collections_offset") is not None
            else getattr(state, "collections_offset", 0)
            or 0
        )
        reuse_pool = bool(args.get("reuse_collections_pool"))
        pool = list(
            args.get("collections_pool")
            or (getattr(state, "collections_pool", None) if reuse_pool else [])
            or []
        )
        collections_at_end = bool(args.get("collections_at_end"))

        plan, strategy, merchant_settings = self._build_plan(
            decision,
            ctx,
            query="",
            source="browse_catalog_groups",
        )
        full_collections = [
            c.to_dict() if hasattr(c, "to_dict") else dict(c)
            for c in list(plan.collections or [])
        ]
        if not pool:
            pool = full_collections

        page_size = COLLECTIONS_BUTTON_PAGE_SIZE
        shown_raw = pool[offset: offset + page_size]
        shown = normalize_collections_page(shown_raw, offset=offset)
        collections_next_available = (offset + page_size) < len(pool)

        page_plan = DiscoveryPlan(
            output_kind="collections",
            collections=shown,
            presentation=plan.presentation,
            evidence={**dict(plan.evidence or {}), "collections_offset": offset},
        )
        from ..catalog.discovery_presenter import DiscoveryPresentationComposer  # noqa: PLC0415

        composer = DiscoveryPresentationComposer()
        presentation = composer.compose(
            plan=page_plan,
            strategy=strategy,
            entry_source="browse_catalog_groups",
            entry_type="global_browse",
            merchant_settings=merchant_settings,
            query="",
        )
        discovery_text = str(presentation.text or "")
        if collections_at_end:
            discovery_text = f"{discovery_text}\n\nهذي آخر الأقسام."

        return {
            "products": [],
            "collections": shown,
            "product_lines": discovery_text,
            "discovery_presentation_text": discovery_text,
            "discovery_output_kind": presentation.output_kind,
            "chosen_path": PATH_GROUPS,
            "count": len(shown),
            "query": "",
            "collections_next_available": collections_next_available,
            "collections_at_end": collections_at_end or (
                not collections_next_available and offset > 0
            ),
            "discovery_plan": dict(getattr(plan, "evidence", None) or {}),
            "navigation_state_patch": self._navigation_patch(
                decision,
                collections=shown,
                products=[],
                source="groups",
                collections_pool=pool,
                collections_offset=offset,
                collections_page_size=page_size,
                collections_next_available=collections_next_available,
            ),
        }

    async def _render_group_products(self, decision: Decision, ctx: BrainContext) -> Dict[str, Any]:
        args = getattr(decision, "args", None) or {}
        query = str(args.get("query") or "").strip()
        group_id = args.get("catalog_group_id")
        state = ctx.state
        plan, strategy, merchant_settings = self._build_plan(
            decision,
            ctx,
            query=query,
            source="collections_first",
            catalog_group_id=group_id,
            catalog_group_db_id=args.get("catalog_group_db_id"),
        )
        from ..catalog.discovery_presenter import DiscoveryPresentationComposer  # noqa: PLC0415
        from ..catalog.catalog_intelligence import attach_discovery_signals_from_db  # noqa: PLC0415
        from ..catalog.presentation_contract import validate_discovery_products  # noqa: PLC0415
        from ..commerce.selection_context import normalize_presented_product  # noqa: PLC0415

        db = getattr(ctx, "_db", None)
        page_size = int(
            args.get("group_products_page_size")
            or getattr(state, "group_products_page_size", 0)
            or max(1, strategy.initial_count)
        )
        offset = int(
            args.get("group_products_offset")
            if args.get("group_products_offset") is not None
            else getattr(state, "group_products_offset", 0)
            or 0
        )
        reuse_pool = bool(args.get("reuse_group_pool"))
        pool = list(
            args.get("group_products_pool")
            or (getattr(state, "group_products_pool", None) if reuse_pool else [])
            or []
        )

        products = list(plan.products or [])
        if not pool:
            if db is not None and products:
                products = validate_discovery_products(
                    attach_discovery_signals_from_db(products, db=db, tenant_id=ctx.tenant_id),
                )
            ranked = list(products)
            if db is not None and not ranked:
                ranked = list(plan.products or [])
            pool_limit = max(12, page_size * 4)
            pool = ranked[:pool_limit] if ranked else []

        shown = pool[offset: offset + page_size] if pool else []
        next_page_available = (offset + page_size) < len(pool)
        shown = [
            normalize_presented_product(p, list_index=i)
            for i, p in enumerate(shown, start=1)
        ]

        plan = type(plan)(
            output_kind="products",
            products=shown,
            collections=list(plan.collections or []),
            guided_question=plan.guided_question,
            presentation=plan.presentation,
            evidence={**dict(plan.evidence), "group_products": query, "offset": offset},
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
        products_out = list(presentation.products or shown or [])
        current_group = (args.get("navigation_state_patch") or {}).get("current_catalog_group")
        if not current_group:
            current_group = {
                "group_db_id": args.get("catalog_group_db_id"),
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
            "next_page_available": next_page_available,
            "discovery_plan": dict(getattr(plan, "evidence", None) or {}),
            "navigation_state_patch": self._navigation_patch(
                decision,
                collections=[],
                products=products_out,
                source="group_products",
                current_catalog_group=current_group,
                selected_collection=str(args.get("catalog_group_id") or args.get("catalog_group_slug") or ""),
                group_products_pool=pool,
                group_products_offset=offset,
                group_products_page_size=page_size,
                next_page_available=next_page_available,
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
        conversation_focus = str(getattr(getattr(ctx, "state", None), "conversation_focus", "") or "")
        ranking_state = getattr(ctx, "state", None)
        if conversation_focus in {"order_tracking", "shipping_policy"}:
            ranking_state = None
        products = load_best_seller_catalog_products(
            getattr(ctx, "_db", None),
            ctx.tenant_id,
            message=ctx.message or "",
            query="",
            state=ranking_state,
            limit=max(12, getattr(strategy, "initial_count", 3) * 4),
        )
        if not products:
            products = self._fallback_top_products(
                ctx,
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

    def _fallback_top_products(self, ctx: BrainContext, *, limit: int) -> List[Dict[str, Any]]:
        """Global catalog fallback when best-seller / group ranking is empty."""
        facts = getattr(ctx, "facts", None)
        for rows in (
            list(getattr(facts, "discovery_products", None) or []),
            list(getattr(facts, "top_products", None) or []),
        ):
            if rows:
                return list(rows)[: max(1, int(limit or 12))]
        db = getattr(ctx, "_db", None)
        tenant_id = getattr(ctx, "tenant_id", None)
        if db is None or tenant_id is None:
            return []
        try:
            from core.store_knowledge import CatalogContextBuilder  # noqa: PLC0415

            return list(
                CatalogContextBuilder(db, int(tenant_id)).get_top_products(int(limit or 12)) or []
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — empty catalog must not crash browse
            logger.exception(
                "[CATALOG_NAVIGATOR] top_products_fallback_failed tenant=%s",
                tenant_id,
            )
            return []

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
        catalog_group_db_id: Any = None,
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
            group_db_id = catalog_group_db_id
            if group_db_id is None:
                group_db_id = args.get("catalog_group_db_id")
            group_slug = str(args.get("catalog_group_slug") or catalog_group_id or "").strip()
            fetch = None
            try:
                resolved_db_id = int(group_db_id) if group_db_id is not None else None
            except (TypeError, ValueError):
                resolved_db_id = None

            if resolved_db_id is not None:
                fetch = provider.get_collection_products_by_id(
                    resolved_db_id,
                    limit=max(12, strategy.initial_count * 4),
                    allow_search_fallback=False,
                    group_slug=group_slug,
                    group_name=query,
                )
                raw = list(fetch.products or [])
            else:
                fetch = None
                raw = []
                logger.info(
                    "[CATALOG_NAVIGATOR] group_products tenant=%s product_source=scoped_empty "
                    "group_db_id=%s group_slug=%r group_name=%r membership_count=0 "
                    "orderable_count=0 products_returned=0 empty_reason=missing_group_db_id",
                    getattr(ctx, "tenant_id", None),
                    group_db_id,
                    group_slug,
                    query,
                )

            enriched = attach_discovery_signals_from_db(
                raw,
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
            evidence = {
                "group_id": str(catalog_group_id or ""),
                "group_db_id": resolved_db_id,
                "group_slug": group_slug,
                "group_name": query,
                "source": source,
            }
            if fetch is not None:
                evidence.update({
                    "product_source": fetch.product_source,
                    "membership_count": fetch.membership_count,
                    "orderable_count": fetch.orderable_count,
                    "products_returned": fetch.products_returned,
                    "empty_reason": fetch.empty_reason,
                })
            elif not resolved_db_id:
                evidence.update({
                    "product_source": "scoped_empty",
                    "membership_count": 0,
                    "orderable_count": 0,
                    "products_returned": 0,
                    "empty_reason": "missing_group_db_id",
                })
            plan = DiscoveryPlan(
                output_kind="products",
                products=list(ranked),
                presentation=strategy.presentation,
                evidence=evidence,
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
        group_products_pool: Optional[List[Dict[str, Any]]] = None,
        group_products_offset: int = 0,
        group_products_page_size: int = 0,
        next_page_available: bool = False,
        collections_pool: Optional[List[Dict[str, Any]]] = None,
        collections_offset: int = 0,
        collections_page_size: int = 0,
        collections_next_available: bool = False,
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
        if group_products_pool is not None:
            patch["group_products_pool"] = list(group_products_pool)
            patch["group_products_offset"] = int(group_products_offset or 0)
            patch["group_products_page_size"] = int(group_products_page_size or 0)
            patch["next_page_available"] = bool(next_page_available)
        if collections_pool is not None:
            patch["collections_pool"] = list(collections_pool)
            patch["collections_offset"] = int(collections_offset or 0)
            patch["collections_page_size"] = int(collections_page_size or 0)
            patch["collections_next_available"] = bool(collections_next_available)
        patch.update({k: v for k, v in args_patch.items() if k not in patch or patch[k] in (None, "", [])})
        return patch


__all__ = ["CatalogNavigateHandler"]
