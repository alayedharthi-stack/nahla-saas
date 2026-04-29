"""
modules/ai/commerce/runtime.py
──────────────────────────────
Unified deterministic commerce tool runtime shared by MerchantBrain and the
canonical orchestrator compatibility layer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.store_knowledge import CatalogContextBuilder, CustomerContextBuilder, StoreKnowledgeLoader
from modules.ai.commerce.permissions import CommercePermissionSet
from modules.ai.security import (
    TenantContext,
    TenantIsolationLayer,
    TenantIsolationViolation,
)

logger = logging.getLogger("nahla.ai.commerce.runtime")


@dataclass
class ToolExecutionResult:
    ok: bool
    tool_name: str
    payload: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    audit: Dict[str, Any] = field(default_factory=dict)


class CommerceToolRuntime:
    """Deterministic executor for commerce and knowledge tools."""

    def __init__(
        self,
        db: Any,
        *,
        tenant_id: int,
        customer_phone: str = "",
        customer_id: Optional[int] = None,
        permissions: Optional[CommercePermissionSet] = None,
        tenant_context: Optional[TenantContext] = None,
    ) -> None:
        # Build a validated TenantContext eagerly so any caller that forgot
        # to pass tenant_id or passed an invalid value fails fast — before
        # we touch the DB or any external service.
        if tenant_context is None:
            tenant_context = TenantIsolationLayer.make_context(
                tenant_id,
                customer_phone=customer_phone,
                customer_id=customer_id,
            )
        else:
            TenantIsolationLayer.assert_active(tenant_context)
            if tenant_context.tenant_id != int(tenant_id):
                raise TenantIsolationViolation(
                    f"runtime tenant_id={tenant_id} does not match "
                    f"context tenant_id={tenant_context.tenant_id}"
                )

        self.db = db
        self.tenant_context = tenant_context
        self.tenant_id = tenant_context.tenant_id
        self.customer_phone = tenant_context.customer_phone or customer_phone
        self.customer_id = tenant_context.customer_id or customer_id
        self.permissions = permissions or CommercePermissionSet(
            tenant_id=self.tenant_id
        )
        self.catalog = CatalogContextBuilder(db, self.tenant_id)
        self.store_loader = StoreKnowledgeLoader(db, self.tenant_id)

    async def execute(self, tool_name: str, payload: Dict[str, Any]) -> ToolExecutionResult:
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return ToolExecutionResult(
                ok=False,
                tool_name=tool_name,
                error="unknown_tool",
                audit={"tool_name": tool_name},
            )
        # Sanitise the incoming payload through the isolation layer so a
        # hallucinated tenant_id from the LLM cannot redirect a tool call.
        try:
            safe_payload = TenantIsolationLayer.verify_payload(
                payload or {}, self.tenant_context
            )
        except TenantIsolationViolation as exc:
            logger.error("[CommerceToolRuntime] payload rejected: %s", exc)
            return ToolExecutionResult(
                ok=False,
                tool_name=tool_name,
                error="tenant_isolation_violation",
                audit={"tool_name": tool_name, "reason": str(exc)},
            )
        try:
            return await handler(safe_payload)
        except TenantIsolationViolation as exc:
            logger.error(
                "[CommerceToolRuntime] tool=%s isolation breach: %s",
                tool_name, exc,
            )
            return ToolExecutionResult(
                ok=False,
                tool_name=tool_name,
                error="tenant_isolation_violation",
                audit={"tool_name": tool_name, "reason": str(exc)},
            )
        except Exception as exc:
            logger.exception("[CommerceToolRuntime] tool=%s failed: %s", tool_name, exc)
            return ToolExecutionResult(
                ok=False,
                tool_name=tool_name,
                error=str(exc),
                audit={"tool_name": tool_name, "status": "exception"},
            )

    async def _tool_search_products(self, payload: Dict[str, Any]) -> ToolExecutionResult:
        query = str(payload.get("query") or "").strip()
        limit = int(payload.get("limit") or 8)
        products = self.catalog.search_products(query, limit=limit) if query else self.catalog.get_top_products(limit=limit)
        return ToolExecutionResult(
            ok=True,
            tool_name="search_products",
            payload={"products": products, "count": len(products), "query": query},
            audit={"query": query, "count": len(products)},
        )

    async def _tool_get_product_details(self, payload: Dict[str, Any]) -> ToolExecutionResult:
        external_id = str(payload.get("external_id") or "").strip()
        if external_id:
            product = self.catalog.get_by_external_id(external_id)
        else:
            product = None
            query = str(payload.get("query") or "").strip()
            if query:
                matches = self.catalog.search_products(query, limit=1)
                product = matches[0] if matches else None
        return ToolExecutionResult(
            ok=bool(product),
            tool_name="get_product_details",
            payload={"product": product},
            error=None if product else "product_not_found",
            audit={"external_id": external_id},
        )

    async def _tool_check_stock(self, payload: Dict[str, Any]) -> ToolExecutionResult:
        external_id = str(payload.get("external_id") or "").strip()
        if not external_id:
            return ToolExecutionResult(False, "check_stock", error="missing_external_id")
        stock = self.catalog.check_availability(external_id)
        return ToolExecutionResult(
            ok=stock.get("available") is not None,
            tool_name="check_stock",
            payload={"stock": stock},
            error=None if stock.get("available") is not None else "product_not_found",
            audit={"external_id": external_id},
        )

    async def _tool_create_draft_order(self, payload: Dict[str, Any]) -> ToolExecutionResult:
        from store_integration.models import OrderInput, OrderItemInput
        from store_integration.order_service import create_draft_order

        if not self.permissions.is_permitted("create_draft_order"):
            return ToolExecutionResult(False, "create_draft_order", error=self.permissions.denial_reason("create_draft_order"))

        product_id = str(payload.get("product_id") or payload.get("external_id") or "").strip()
        quantity = max(int(payload.get("quantity") or 1), 1)
        customer_name = str(payload.get("customer_name") or "عميل").strip() or "عميل"
        if not product_id:
            return ToolExecutionResult(False, "create_draft_order", error="missing_product_id")

        _raw_sid = payload.get("shipping_company_id")
        _raw_options = payload.get("options") or []
        _item_options = _raw_options if isinstance(_raw_options, list) else []
        # Diagnostic: surface what the runtime actually received from the
        # decision/execution layer. Helps prove that prep.product_options
        # really propagated all the way down to OrderItemInput.
        logger.info(
            "[ORDER FLOW] runtime create_draft_order | tenant=%s product=%s qty=%s options=%s",
            self.tenant_id, product_id, quantity, _item_options,
        )
        order_input = OrderInput(
            customer_name=customer_name,
            customer_phone=self.customer_phone,
            customer_email=str(payload.get("customer_email") or "").strip() or None,
            customer_first_name=str(payload.get("customer_first_name") or "").strip(),
            customer_last_name=str(payload.get("customer_last_name") or "").strip(),
            city=str(payload.get("city") or "").strip(),
            address=str(payload.get("address") or "").strip(),
            street=str(payload.get("street") or "").strip(),
            district=str(payload.get("district") or "").strip(),
            postal_code=str(payload.get("postal_code") or "").strip(),
            building_number=str(payload.get("building_number") or "").strip(),
            additional_number=str(payload.get("additional_number") or "").strip(),
            short_address_code=str(payload.get("short_address_code") or "").strip(),
            google_maps_url=str(payload.get("google_maps_url") or "").strip(),
            latitude=payload.get("latitude"),
            longitude=payload.get("longitude"),
            payment_method=str(payload.get("payment_method") or "online"),
            items=[OrderItemInput(
                product_id=product_id,
                quantity=quantity,
                options=_item_options,
            )],
            notes=str(payload.get("notes") or "").strip() or None,
            shipping_company_id=int(_raw_sid) if _raw_sid else None,
        )
        try:
            order = await create_draft_order(self.tenant_id, order_input)
        except ValueError as _exc:
            # Pre-flight blocker (e.g. required_product_options_missing).
            # Surface the exact reason so the brain can react — typically
            # by asking the customer for product options rather than
            # showing a generic "Salla failed" retry message.
            _reason = str(_exc) or "draft_order_blocked"
            logger.error(
                "[ORDER FLOW] runtime create_draft_order blocked | tenant=%s product=%s reason=%s",
                self.tenant_id, product_id, _reason,
            )
            return ToolExecutionResult(
                ok=False,
                tool_name="create_draft_order",
                payload={"order": None},
                error=_reason,
                audit={"product_id": product_id, "quantity": quantity, "blocked": True},
            )
        return ToolExecutionResult(
            ok=bool(order),
            tool_name="create_draft_order",
            payload={"order": order.model_dump() if order else None},
            error=None if order else "draft_order_failed",
            audit={"product_id": product_id, "quantity": quantity},
        )

    async def _tool_create_checkout(self, payload: Dict[str, Any]) -> ToolExecutionResult:
        from store_integration.payment_service import generate_payment_link

        order_id = str(payload.get("order_id") or "").strip()
        amount = float(payload.get("amount") or 0.0)
        if not order_id:
            return ToolExecutionResult(False, "create_checkout", error="missing_order_id")
        link = await generate_payment_link(
            self.tenant_id,
            order_id,
            amount,
            description=str(payload.get("description") or "").strip(),
        )
        return ToolExecutionResult(
            ok=bool(link),
            tool_name="create_checkout",
            payload={"checkout_url": link, "order_id": order_id},
            error=None if link else "checkout_failed",
            audit={"order_id": order_id},
        )

    async def _tool_send_payment_link(self, payload: Dict[str, Any]) -> ToolExecutionResult:
        from store_integration.payment_service import generate_payment_link

        if not self.permissions.is_permitted("send_payment_link"):
            return ToolExecutionResult(False, "send_payment_link", error=self.permissions.denial_reason("send_payment_link"))
        checkout_url = str(payload.get("checkout_url") or "").strip()
        order_id = str(payload.get("order_id") or "").strip()
        if not checkout_url and order_id:
            amount = float(payload.get("amount") or 0.0)
            checkout_url = await generate_payment_link(
                self.tenant_id,
                order_id,
                amount,
                description=str(payload.get("description") or "").strip(),
            )
        return ToolExecutionResult(
            ok=bool(checkout_url),
            tool_name="send_payment_link",
            payload={"checkout_url": checkout_url, "order_id": order_id},
            error=None if checkout_url else "missing_checkout_url",
            audit={"order_id": order_id},
        )

    async def _tool_apply_coupon(self, payload: Dict[str, Any]) -> ToolExecutionResult:
        from services.offer_decision_service import (
            OfferDecisionContext,
            SURFACE_CHAT,
            apply_decision,
            collect_signals,
            decide,
        )

        if not self.permissions.is_permitted("apply_coupon"):
            return ToolExecutionResult(False, "apply_coupon", error=self.permissions.denial_reason("apply_coupon"))
        ctx = OfferDecisionContext(
            tenant_id=self.tenant_id,
            surface=SURFACE_CHAT,
            customer_id=self.customer_id,
            suggested_source="coupon",
            suggested_discount_pct=(
                int(payload.get("discount_pct")) if payload.get("discount_pct") is not None else None
            ),
            suggested_segment=str(payload.get("segment") or "").strip() or None,
            signals=collect_signals(
                self.db,
                tenant_id=self.tenant_id,
                customer_id=self.customer_id,
                cart_total=float(payload["cart_total"]) if payload.get("cart_total") else None,
            ),
        )
        decision = decide(self.db, ctx)
        extras = await apply_decision(self.db, ctx=ctx, decision=decision, customer=None)
        return ToolExecutionResult(
            ok=bool(extras.get("coupon_code")),
            tool_name="apply_coupon",
            payload={"decision": decision.__dict__, "coupon": extras},
            error=None if extras.get("coupon_code") else "coupon_not_issued",
            audit={"customer_id": self.customer_id, "cart_total": payload.get("cart_total")},
        )

    async def _tool_track_order(self, payload: Dict[str, Any]) -> ToolExecutionResult:
        from store_integration.order_service import get_customer_orders, get_order  # noqa: PLC0415

        order_number = str(payload.get("order_number") or "").strip()

        # Step 1: Try direct lookup if customer mentioned a specific order number
        matched: Dict[str, Any] | None = None
        if order_number:
            direct = await get_order(self.tenant_id, order_number)
            if direct:
                matched = direct.model_dump()

        # Step 2: Fetch customer's recent orders for fallback / list matching
        orders = await get_customer_orders(self.tenant_id, self.customer_phone)

        # Step 3: If we got a direct match, verify it belongs to this customer.
        # If not, search the customer's order list by reference_id / id.
        if not matched and orders:
            if order_number:
                for o in orders:
                    ref = str(o.reference_id or o.id or "").lower()
                    if order_number.lower() in ref or ref in order_number.lower():
                        matched = o.model_dump()
                        break
            if not matched:
                matched = orders[0].model_dump()

        return ToolExecutionResult(
            ok=bool(matched),
            tool_name="track_order",
            payload={
                "order":         matched,
                "orders_count":  len(orders),
                "matched_by_ref": bool(order_number and matched),
            },
            error=None if matched else "no_orders_found",
            audit={"customer_phone": self.customer_phone, "order_number": order_number},
        )

    async def _tool_get_store_info(self, payload: Dict[str, Any]) -> ToolExecutionResult:
        data = {
            "store_profile": self.store_loader.store_profile(),
            "catalog_summary": self.store_loader.catalog_summary(),
            "shipping_summary": self.store_loader.shipping_summary(),
            "policy_summary": self.store_loader.policy_summary(),
            "coupon_summary": self.store_loader.coupon_summary(),
            "snapshot_fresh": self.store_loader.is_fresh(),
        }
        return ToolExecutionResult(
            ok=True,
            tool_name="get_store_info",
            payload=data,
            audit={"tenant_id": self.tenant_id},
        )

    async def _tool_get_customer_history(self, payload: Dict[str, Any]) -> ToolExecutionResult:
        try:
            builder = CustomerContextBuilder(self.db, self.tenant_id)
            context_block = builder.build_context_block(self.customer_phone)
        except Exception:
            context_block = ""
        return ToolExecutionResult(
            ok=bool(context_block),
            tool_name="get_customer_history",
            payload={"history_block": context_block},
            error=None if context_block else "history_not_found",
            audit={"customer_phone": self.customer_phone},
        )

    async def _tool_recommend_addon(self, payload: Dict[str, Any]) -> ToolExecutionResult:
        query = str(payload.get("query") or "").strip()
        base_product_id = payload.get("product_id")
        candidates = self.catalog.search_products(query, limit=5) if query else self.catalog.get_top_products(limit=5)
        recommended = [p for p in candidates if str(p.get("id")) != str(base_product_id)]
        return ToolExecutionResult(
            ok=bool(recommended),
            tool_name="recommend_addon",
            payload={"products": recommended[:3]},
            error=None if recommended else "no_recommendation_found",
            audit={"base_product_id": base_product_id, "query": query},
        )

    async def _tool_web_search(self, payload: Dict[str, Any]) -> ToolExecutionResult:
        from modules.ai.tools.web_search import search_web

        query = str(payload.get("query") or "").strip()
        if not query:
            return ToolExecutionResult(False, "web_search", error="missing_query")
        results = await search_web(query, tenant_id=self.tenant_id)
        return ToolExecutionResult(
            ok=bool(results.get("summary") or results.get("results")),
            tool_name="web_search",
            payload=results,
            error=None if (results.get("summary") or results.get("results")) else "no_web_results",
            audit={"query": query},
        )
