"""
brain/compose/responder.py
───────────────────────────
DefaultComposer: maps (Decision, ActionResult, BrainContext) → Arabic reply text.

For deterministic actions (greet, search, order, …) we use templates.
For ACTION_LLM_REPLY we call the existing generate_orchestrate_response
pipeline (full Claude Sonnet with the merchant's system prompt).

This keeps the LLM in a well-defined "composer" role rather than being
the entire brain.
"""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger("nahla.brain.responder")

from ..types import ActionResult, BrainContext, Decision
from ..decision.actions import (
    ACTION_CLARIFY,
    ACTION_GREET,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_NARROW,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_SUGGEST_COUPON,
    ACTION_TRACK_ORDER,
)
from . import templates as T


class DefaultComposer:
    """Implements Composer protocol."""

    async def compose(
        self,
        decision: Decision,
        result: ActionResult,
        ctx: BrainContext,
    ) -> str:
        action = decision.action
        data   = result.data or {}

        # ── Greet ──────────────────────────────────────────────────────────
        if action == ACTION_GREET:
            return T.greeting(store_name=ctx.facts.store_name)

        # ── Search ─────────────────────────────────────────────────────────
        if action == ACTION_SEARCH_PRODUCTS:
            if not result.success or data.get("message") == "no_products_in_catalog":
                return T.no_products()
            # If many results and no specific intent, present as narrow choices
            if data.get("suggest_narrow") and data.get("products"):
                return T.narrow_choices(products=data["products"][:3])
            return T.product_results(
                product_lines=data.get("product_lines", ""),
                query=data.get("query", ""),
                count=data.get("count", 0),
            )

        # ── Draft order ────────────────────────────────────────────────────
        if action == ACTION_PROPOSE_DRAFT_ORDER:
            if not result.success:
                return T.generic_fallback()
            if data.get("intent_only"):
                return T.order_intent_captured(product=data.get("product", {}))
            return T.draft_order_created(
                product=data.get("product", {}),
                reference=str(data.get("reference", "")),
                checkout_url=data.get("checkout_url", ""),
                total=float(data.get("total") or 0),
                currency=data.get("currency", "SAR"),
            )

        # ── Payment link ───────────────────────────────────────────────────
        if action == ACTION_SEND_PAYMENT_LINK:
            return T.payment_link(checkout_url=data.get("checkout_url", ""))

        # ── Track order ────────────────────────────────────────────────────
        if action == ACTION_TRACK_ORDER:
            if not result.success or data.get("message") == "no_orders_found":
                return T.no_orders()
            return T.order_status(
                reference=str(data.get("reference", "")),
                status=data.get("status", ""),
                total=float(data.get("total") or 0),
                currency=data.get("currency", "SAR"),
            )

        # ── Coupon ─────────────────────────────────────────────────────────
        if action == ACTION_SUGGEST_COUPON:
            if not result.success or not data.get("coupon_block"):
                return T.generic_fallback()
            return T.coupon_offer(
                coupon_block=data.get("coupon_block", ""),
                product=data.get("product"),
            )

        # ── Clarify ────────────────────────────────────────────────────────
        if action == ACTION_CLARIFY:
            return T.clarify(question=data.get("question", ""))

        # ── Narrow choices ─────────────────────────────────────────────────
        if action == ACTION_NARROW:
            return T.narrow_choices(products=data.get("products", []))

        # ── Handoff ────────────────────────────────────────────────────────
        if action == ACTION_HANDOFF:
            return T.handoff()

        # ── LLM fallback ───────────────────────────────────────────────────
        if action == ACTION_LLM_REPLY:
            return await self._llm_compose(ctx)

        return T.generic_fallback()

    # ── LLM delegation ───────────────────────────────────────────────────────

    async def _llm_compose(self, ctx: BrainContext) -> str:
        """Delegate to the existing orchestration pipeline for ambiguous turns.

        Hard timeout of 25 seconds — if the LLM provider is slow or down we
        return a friendly fallback immediately rather than hanging the reply.
        """
        import asyncio  # noqa: PLC0415

        _TIMEOUT = 25  # seconds

        try:
            _BACKEND = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "../../../../..")
            )
            if _BACKEND not in sys.path:
                sys.path.insert(0, _BACKEND)

            from modules.ai.orchestrator.adapter import generate_orchestrate_response  # noqa: PLC0415

            result = await asyncio.wait_for(
                generate_orchestrate_response(
                    tenant_id=ctx.tenant_id,
                    customer_phone=ctx.customer_phone,
                    message=ctx.message,
                    conversation_id=ctx.conversation_id,
                ),
                timeout=_TIMEOUT,
            )
            return result.get("reply", "") or T.generic_fallback()
        except asyncio.TimeoutError:
            logger.warning(
                "[Composer._llm_compose] LLM timed out after %ds | tenant=%s",
                _TIMEOUT, ctx.tenant_id,
            )
            return (
                "عذراً، تأخّر الرد قليلاً. "
                "هل يمكنك إعادة سؤالك؟ أو يمكنني مساعدتك في البحث عن منتج أو إنشاء طلب."
            )
        except Exception as exc:
            logger.error("[Composer._llm_compose] error: %s", exc)
            return T.generic_fallback()
