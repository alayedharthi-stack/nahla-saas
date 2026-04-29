"""
brain/execution/executor.py
────────────────────────────
DefaultActionExecutor: dispatcher that routes a Decision to the correct
handler and returns an ActionResult.

Adding a new action:
  1. Import its handler class here.
  2. Register it in _REGISTRY below.
  3. The rest of the pipeline needs no changes.
"""
from __future__ import annotations

import logging
from typing import Dict, Type, Any

from ..types import ActionResult, BrainContext, Decision
from ..decision.actions import (
    ACTION_CLARIFY,
    ACTION_FAQ_REPLY,
    ACTION_GREET,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_NARROW,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_RECOMMEND_ADDON,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_STASH_ADDRESS_PRE_PRODUCT,
    ACTION_SUGGEST_COUPON,
    ACTION_TRACK_ORDER,
    ACTION_WEB_SEARCH,
)

logger = logging.getLogger("nahla.brain.executor")


# ── Inline simple handlers ────────────────────────────────────────────────────

class _GreetHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        return ActionResult(success=True, data={"type": "greet"})


class _HandoffHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        return ActionResult(success=True, data={"type": "handoff"})


class _SendPaymentLinkHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from modules.ai.commerce.runtime import CommerceToolRuntime

        runtime = CommerceToolRuntime(
            ctx._db,  # type: ignore[attr-defined]
            tenant_id=ctx.tenant_id,
            customer_phone=ctx.customer_phone,
            customer_id=ctx.customer_id,
            tenant_context=ctx.tenant_context,
        )
        runtime_result = await runtime.execute(
            "send_payment_link",
            {
                "checkout_url": decision.args.get("checkout_url") or ctx.state.checkout_url or "",
                "order_id": decision.args.get("draft_order_id") or ctx.state.draft_order_id or "",
                "amount": decision.args.get("amount") or 0,
                "description": decision.args.get("description") or "",
            },
        )
        url = str(runtime_result.payload.get("checkout_url") or "").strip()
        return ActionResult(
            success=bool(url),
            data={"checkout_url": url, "type": "payment_link"},
            error=None if url else "no_checkout_url",
        )


class _SuggestCouponHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from modules.ai.commerce.runtime import CommerceToolRuntime  # noqa: PLC0415

        # Pull the product the customer is currently considering so we can
        # pass its price as cart_total to the offer engine (smarter discount).
        product_focus: dict = ctx.state.current_product_focus or {}
        product_title: str  = str(product_focus.get("title") or "").strip()
        product_price: float | None = None
        _raw_price = product_focus.get("price")
        if _raw_price is not None:
            try:
                product_price = float(_raw_price)
            except (TypeError, ValueError):
                pass

        runtime = CommerceToolRuntime(
            ctx._db,  # type: ignore[attr-defined]
            tenant_id=ctx.tenant_id,
            customer_phone=ctx.customer_phone,
            customer_id=ctx.customer_id,
            tenant_context=ctx.tenant_context,
        )
        payload: dict = {
            "discount_pct": (ctx.sales_context.offer_signals or {}).get("recommended_discount_pct", 0)
            if ctx.sales_context
            else 0,
            "segment": (ctx.sales_context.customer_profile or {}).get("segment", "")
            if ctx.sales_context
            else "",
        }
        if product_price:
            payload["cart_total"] = product_price

        runtime_result = await runtime.execute("apply_coupon", payload)
        coupon    = runtime_result.payload.get("coupon") or {}
        raw_dec   = runtime_result.payload.get("decision") or {}
        code      = str(coupon.get("coupon_code") or coupon.get("discount_code") or "").strip()

        # Build a human-readable Arabic coupon block.
        if code:
            # Try to include the discount percentage from the decision object.
            disc_val = raw_dec.get("discount_value")
            disc_type = str(raw_dec.get("discount_type") or "").lower()
            if disc_val and disc_type == "percentage":
                pct_label = f" ({int(disc_val)}% خصم)"
            elif disc_val and disc_type == "fixed":
                pct_label = f" (خصم {disc_val:.0f} ريال)"
            else:
                pct_label = ""

            if product_title:
                block = f"كود خصم{pct_label} خاص بك على *{product_title}*: `{code}`"
            else:
                block = f"كود خصم{pct_label} خاص بك: `{code}`"

            # Validity hint
            validity = raw_dec.get("validity_days")
            if validity:
                block += f"\n⏳ صالح لـ {validity} يوم"
        else:
            block = ""

        return ActionResult(
            success=bool(block),
            data={
                "coupon_block":   block,
                "product":        decision.args.get("product") or (product_focus or None),
                "coupon_payload": coupon,
                "discount_value": raw_dec.get("discount_value"),
                "discount_type":  raw_dec.get("discount_type"),
            },
        )


class _ClarifyHandler:
    """Ask the customer one focused clarifying question."""
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        question = decision.args.get("question", "ما الذي تبحث عنه بالضبط؟")
        return ActionResult(success=True, data={"question": question, "type": "clarify"})


class _NarrowHandler:
    """Present a short list of product choices to help the customer decide."""
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        products = decision.args.get("products", [])
        return ActionResult(success=True, data={"products": products, "type": "narrow"})


class _RecommendAddonHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from modules.ai.commerce.runtime import CommerceToolRuntime

        runtime = CommerceToolRuntime(
            ctx._db,  # type: ignore[attr-defined]
            tenant_id=ctx.tenant_id,
            customer_phone=ctx.customer_phone,
            customer_id=ctx.customer_id,
            tenant_context=ctx.tenant_context,
        )
        result = await runtime.execute(
            "recommend_addon",
            {
                "product_id": (
                    (ctx.state.current_product_focus or {}).get("external_id")
                    or (ctx.state.current_product_focus or {}).get("id")
                ),
                "query": decision.args.get("query") or "",
            },
        )
        return ActionResult(
            success=result.ok,
            data={
                "products": result.payload.get("products", []),
                "recommended_products": result.payload.get("products", []),
                "type": "recommend_addon",
            },
            error=result.error,
        )


class _WebSearchHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        from modules.ai.commerce.runtime import CommerceToolRuntime

        runtime = CommerceToolRuntime(
            ctx._db,  # type: ignore[attr-defined]
            tenant_id=ctx.tenant_id,
            customer_phone=ctx.customer_phone,
            customer_id=ctx.customer_id,
            tenant_context=ctx.tenant_context,
        )
        result = await runtime.execute(
            "web_search",
            {"query": decision.args.get("query") or ctx.message},
        )
        return ActionResult(
            success=result.ok,
            data={
                "summary": result.payload.get("summary", ""),
                "results": result.payload.get("results", []),
                "citations": result.payload.get("citations", []),
                "type": "web_search",
            },
            error=result.error,
        )


class _LLMReplyHandler:
    """Route to the existing generate_orchestrate_response pipeline."""
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        policy_reason = decision.args.get("policy_reason", "")
        return ActionResult(
            success=True,
            data={"type": "llm_fallback", "policy_reason": policy_reason},
        )


class _StashAddressPreProductHandler:
    """Stash address signals captured BEFORE a product was picked.

    The customer typed e.g. "TAPA7401" while still browsing. We persist
    the values onto state.pending_* (the pipeline projects this onto the
    next state). On the next turn — when the customer actually picks a
    product — DraftOrderHandler reads `state.pending_*`, merges the
    values into `order_prep`, and clears the pending fields so we never
    ask for the address again.
    """
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        return ActionResult(
            success=True,
            data={
                "type": "stash_address_pre_product",
                "stash_address": {
                    "short_address_code": str(decision.args.get("short_address_code") or "").strip(),
                    "google_maps_url":   str(decision.args.get("google_maps_url") or "").strip(),
                    "city":              str(decision.args.get("city") or "").strip(),
                },
            },
        )


# ── Registry ──────────────────────────────────────────────────────────────────

class DefaultActionExecutor:
    """Implements ActionExecutor protocol."""

    def __init__(self) -> None:
        from .faq import FAQReplyHandler
        from .search import ProductSearchHandler
        from .orders import DraftOrderHandler, TrackOrderHandler

        self._handlers: Dict[str, Any] = {
            ACTION_GREET:               _GreetHandler(),
            ACTION_FAQ_REPLY:           FAQReplyHandler(),
            ACTION_SEARCH_PRODUCTS:     ProductSearchHandler(),
            ACTION_PROPOSE_DRAFT_ORDER: DraftOrderHandler(),
            ACTION_SEND_PAYMENT_LINK:   _SendPaymentLinkHandler(),
            ACTION_SUGGEST_COUPON:      _SuggestCouponHandler(),
            ACTION_TRACK_ORDER:         TrackOrderHandler(),
            ACTION_HANDOFF:             _HandoffHandler(),
            ACTION_CLARIFY:             _ClarifyHandler(),
            ACTION_NARROW:              _NarrowHandler(),
            ACTION_RECOMMEND_ADDON:     _RecommendAddonHandler(),
            ACTION_WEB_SEARCH:          _WebSearchHandler(),
            ACTION_LLM_REPLY:           _LLMReplyHandler(),
            ACTION_STASH_ADDRESS_PRE_PRODUCT: _StashAddressPreProductHandler(),
        }

    async def execute(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        handler = self._handlers.get(decision.action)
        if not handler:
            logger.error("[Executor] unknown action: %s — falling back to LLM", decision.action)
            handler = self._handlers[ACTION_LLM_REPLY]

        logger.debug(
            "[Executor] tenant=%s action=%s args=%s",
            ctx.tenant_id, decision.action, decision.args,
        )
        try:
            return await handler.handle(decision, ctx)
        except Exception as exc:
            logger.exception("[Executor] handler %s failed: %s", decision.action, exc)
            return ActionResult(success=False, error=str(exc))
