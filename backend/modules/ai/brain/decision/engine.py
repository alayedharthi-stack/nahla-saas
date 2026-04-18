"""
brain/decision/engine.py
─────────────────────────
DefaultDecisionEngine: rule-based Commerce Decision Engine.

Decides *what action to take* given the full BrainContext (intent, state,
commerce facts). The decision is deterministic — no LLM involved here.

Rule priority (first match wins):
  1. Human handoff request → ACTION_HANDOFF
  2. Resend payment link (customer in checkout stage) → ACTION_SEND_PAYMENT_LINK
  3. Track order → ACTION_TRACK_ORDER
  4. Greeting / first turn → ACTION_GREET (then fallthrough)
  5. Buy / start order → ACTION_PROPOSE_DRAFT_ORDER (if product in focus)
  6. Buy / start order → ACTION_SEARCH_PRODUCTS (no product selected)
  7. Ask about product or price → ACTION_SEARCH_PRODUCTS
  8. Hesitation with product in focus and coupons available → ACTION_SUGGEST_COUPON
  9. Fallback → ACTION_LLM_REPLY
"""
from __future__ import annotations

import logging

from ..types import BrainContext, Decision
from .actions import (
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
from ..types import (
    INTENT_GREETING,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_PRICE,
    INTENT_START_ORDER,
    INTENT_PAY_NOW,
    INTENT_HESITATION,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
)
from ..state.stages import STAGE_CHECKOUT, STAGE_ORDERING

logger = logging.getLogger("nahla.brain.decision")


class DefaultDecisionEngine:
    """Implements DecisionMaker protocol."""

    def decide(self, ctx: BrainContext) -> Decision:
        intent = ctx.intent
        state  = ctx.state
        facts  = ctx.facts

        # ── 1. Handoff ────────────────────────────────────────────────────
        if intent.name == INTENT_TALK_HUMAN:
            return Decision(
                action=ACTION_HANDOFF,
                reason="customer requested human agent",
            )

        # ── 2. Resend payment link ────────────────────────────────────────
        if intent.name == INTENT_PAY_NOW or (
            state.stage == STAGE_CHECKOUT and intent.name in (INTENT_PAY_NOW, INTENT_START_ORDER)
        ):
            if state.checkout_url:
                return Decision(
                    action=ACTION_SEND_PAYMENT_LINK,
                    args={"checkout_url": state.checkout_url},
                    reason="customer in checkout stage — resend payment link",
                )

        # ── 3. Track order ────────────────────────────────────────────────
        if intent.name == INTENT_TRACK_ORDER:
            return Decision(
                action=ACTION_TRACK_ORDER,
                args={"order_id": intent.slots.get("order_id", "")},
                reason="customer asked for order status",
            )

        # ── 4. Greeting (first turn or explicit greeting) ─────────────────
        if intent.name == INTENT_GREETING or not state.greeted:
            # After greeting, if there's a product query in the slots we also
            # want to search — record that as a secondary hint in args
            product_query = intent.slots.get("product_query", "")
            return Decision(
                action=ACTION_GREET,
                args={"product_query": product_query},
                reason="first contact or explicit greeting",
            )

        # ── 5. Start order — product in focus ──────────────────────────────
        if intent.name == INTENT_START_ORDER:
            if state.current_product_focus and facts.has_products:
                # Only propose order if store can actually fulfil it
                if facts.orderable:
                    return Decision(
                        action=ACTION_PROPOSE_DRAFT_ORDER,
                        args={"product": state.current_product_focus},
                        reason="customer wants to buy the product currently in focus",
                        confidence=0.90,
                    )
                else:
                    # Integration missing or all out-of-stock
                    return Decision(
                        action=ACTION_LLM_REPLY,
                        reason="store not orderable (no integration or all out-of-stock)",
                    )
            elif facts.has_products:
                query = intent.slots.get("product_query", "").strip()
                if not query:
                    # Customer said "أبغى أطلب" with no product mentioned
                    return Decision(
                        action=ACTION_CLARIFY,
                        args={"question": "ما المنتج الذي تودّ طلبه؟ يمكنك ذكر الاسم أو الوصف."},
                        reason="start_order with no product query — ask for clarification",
                        confidence=0.85,
                    )
                return Decision(
                    action=ACTION_SEARCH_PRODUCTS,
                    args={"query": query, "after_search": "propose_order"},
                    reason="customer wants to buy but no product focus — search first",
                    confidence=0.80,
                )

        # ── 6. Ask about product or price ─────────────────────────────────
        if intent.name in (INTENT_ASK_PRODUCT, INTENT_ASK_PRICE):
            if facts.has_products:
                query = (
                    intent.slots.get("product_query")
                    or intent.slots.get("product_name")
                    or ctx.message
                )
                return Decision(
                    action=ACTION_SEARCH_PRODUCTS,
                    args={"query": query},
                    reason=f"customer {intent.name} — search catalog",
                )
            else:
                # No products in DB — go to LLM to apologise gracefully
                return Decision(
                    action=ACTION_LLM_REPLY,
                    reason="no products in catalog — LLM apologises",
                )

        # ── 7. Hesitation with product focus & coupons ───────────────────
        if intent.name == INTENT_HESITATION:
            if state.current_product_focus and facts.has_coupons and facts.has_products:
                return Decision(
                    action=ACTION_SUGGEST_COUPON,
                    args={"product": state.current_product_focus},
                    reason="customer hesitating — nudge with a coupon",
                    confidence=0.75,
                )

        # ── 8. Fallback: LLM ─────────────────────────────────────────────
        return Decision(
            action=ACTION_LLM_REPLY,
            reason=f"no rule matched for intent={intent.name} — LLM fallback",
            confidence=0.50,
        )
