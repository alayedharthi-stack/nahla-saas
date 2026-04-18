"""
brain/execution/orders.py
──────────────────────────
DraftOrderHandler: executes ACTION_PROPOSE_DRAFT_ORDER.

Creates a draft order in the merchant's store (via order_service) and
returns the checkout URL. Falls back to a WhatsApp-friendly "intent
captured" message when no store adapter is available (e.g. store not
connected or adapter doesn't support draft orders).
"""
from __future__ import annotations

import logging
import os, sys

logger = logging.getLogger("nahla.brain.execution.orders")

_THIS    = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_THIS, "../../../../.."))
_DB      = os.path.abspath(os.path.join(_BACKEND, "../database"))
for _p in (_BACKEND, _DB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ...brain.types import ActionResult, BrainContext, Decision


class DraftOrderHandler:
    """Handles ACTION_PROPOSE_DRAFT_ORDER."""

    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from store_integration.models import OrderInput, OrderItemInput
        from store_integration.order_service import create_draft_order

        product_info = decision.args.get("product") or ctx.state.current_product_focus
        if not product_info:
            return ActionResult(
                success=False,
                error="no_product_focus",
                data={"message": "no_product_selected"},
            )

        # Build a minimal OrderInput — we don't have the full address yet,
        # so we create a draft with COD and empty address fields.
        external_id = product_info.get("external_id") or str(product_info.get("id", ""))
        if not external_id:
            return ActionResult(
                success=False,
                error="missing_product_id",
                data={"message": "product_has_no_external_id"},
            )

        order_input = OrderInput(
            customer_name=ctx.profile.get("name", "عميل"),
            customer_phone=ctx.customer_phone,
            customer_email=ctx.profile.get("email"),
            payment_method="cod",
            items=[OrderItemInput(product_id=external_id, quantity=1)],
            notes="طلب أنشأه نظام نحلة الذكي عبر واتساب",
        )

        try:
            order = await create_draft_order(ctx.tenant_id, order_input)
        except Exception as exc:
            logger.warning("[DraftOrderHandler] create_draft_order error: %s", exc)
            order = None

        if order:
            return ActionResult(
                success=True,
                data={
                    "order_id":    order.id,
                    "reference":   order.reference_id or order.id,
                    "checkout_url": order.payment_link or "",
                    "total":       order.total,
                    "currency":    order.currency,
                    "product":     product_info,
                },
            )

        # No adapter / adapter failed — record intent for follow-up
        logger.info(
            "[DraftOrderHandler] tenant=%s — no adapter or draft failed, recording intent",
            ctx.tenant_id,
        )
        return ActionResult(
            success=True,   # success=True so composer produces a friendly reply
            data={
                "order_id":    None,
                "checkout_url": "",
                "product":     product_info,
                "intent_only": True,
            },
        )


class TrackOrderHandler:
    """Handles ACTION_TRACK_ORDER."""

    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from store_integration.order_service import get_customer_orders

        try:
            orders = await get_customer_orders(ctx.tenant_id, ctx.customer_phone)
        except Exception as exc:
            logger.warning("[TrackOrderHandler] error: %s", exc)
            orders = []

        if not orders:
            return ActionResult(
                success=False,
                error="no_orders",
                data={"message": "no_orders_found"},
            )

        latest = orders[0]
        return ActionResult(
            success=True,
            data={
                "order_id":  latest.id,
                "reference": latest.reference_id or latest.id,
                "status":    latest.status,
                "total":     latest.total,
                "currency":  latest.currency,
            },
        )
