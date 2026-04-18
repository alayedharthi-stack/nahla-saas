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
  4. Simple FAQ (identity / shipping / store / contact) → ACTION_FAQ_REPLY
  5. Greeting / first-turn general help → ACTION_GREET
  6. Buy / start order → ACTION_PROPOSE_DRAFT_ORDER (if product in focus)
  7. Buy / start order → ACTION_SEARCH_PRODUCTS (no product selected)
  8. Ask about product or price → ACTION_SEARCH_PRODUCTS
  9. Hesitation with product in focus and coupons available → ACTION_SUGGEST_COUPON
 10. Fallback → ACTION_LLM_REPLY
"""
from __future__ import annotations

import logging

from ..types import BrainContext, Decision
from .actions import (
    ACTION_CLARIFY,
    ACTION_FAQ_REPLY,
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
    INTENT_ASK_OWNER_CONTACT,
    INTENT_GREETING,
    INTENT_ASK_PRODUCT,
    INTENT_ASK_PRICE,
    INTENT_ASK_SHIPPING,
    INTENT_ASK_STORE_INFO,
    INTENT_START_ORDER,
    INTENT_PAY_NOW,
    INTENT_HESITATION,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
    INTENT_GENERAL,
    INTENT_WHO_ARE_YOU,
)
from ..state.stages import STAGE_CHECKOUT, STAGE_ORDERING

logger = logging.getLogger("nahla.brain.decision")


class DefaultDecisionEngine:
    """Implements DecisionMaker protocol."""

    def decide(self, ctx: BrainContext) -> Decision:
        intent = ctx.intent
        state  = ctx.state
        facts  = ctx.facts
        checkout_slots = {
            "customer_first_name",
            "customer_last_name",
            "customer_name",
            "full_name",
            "city",
            "short_address_code",
            "google_maps_url",
            "location_url",
            "address",
            "address_line",
            "street",
            "district",
            "postal_code",
            "zip_code",
            "building_number",
            "additional_number",
            "latitude",
            "longitude",
        }

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

        # ── 3.5 Continue order preparation while collecting checkout details ──
        if (
            state.stage == STAGE_ORDERING
            and state.current_product_focus
            and not state.checkout_url
            and (
                intent.name in (INTENT_START_ORDER, INTENT_GENERAL)
                or any(slot in intent.slots for slot in checkout_slots)
            )
        ):
            return Decision(
                action=ACTION_PROPOSE_DRAFT_ORDER,
                args={"product": state.current_product_focus},
                reason="continue collecting checkout details for current product",
                confidence=0.88,
            )

        # ── 4. Simple FAQ / identity / shipping / contact ──────────────────
        if intent.name == INTENT_WHO_ARE_YOU:
            return Decision(
                action=ACTION_FAQ_REPLY,
                args={"topic": "identity"},
                reason="customer asked who the assistant is",
            )

        if intent.name == INTENT_ASK_SHIPPING:
            return Decision(
                action=ACTION_FAQ_REPLY,
                args={"topic": "shipping"},
                reason="customer asked about shipping / delivery",
            )

        if intent.name == INTENT_ASK_STORE_INFO:
            return Decision(
                action=ACTION_FAQ_REPLY,
                args={"topic": "store_info"},
                reason="customer asked for store info / link / location",
            )

        if intent.name == INTENT_ASK_OWNER_CONTACT:
            return Decision(
                action=ACTION_FAQ_REPLY,
                args={"topic": "owner_contact"},
                reason="customer asked for contact details",
            )

        # ── 5. Greeting (explicit greeting or first-turn generic help) ─────
        if intent.name == INTENT_GREETING or (not state.greeted and intent.name == INTENT_GENERAL):
            return Decision(
                action=ACTION_GREET,
                reason="explicit greeting or first-turn general help",
            )

        # ── 6. Start order — product in focus ──────────────────────────────
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

        # ── 7. Ask about product or price ─────────────────────────────────
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

        # ── 8. Hesitation with product focus & coupons ───────────────────
        if intent.name == INTENT_HESITATION:
            if state.current_product_focus and facts.has_coupons and facts.has_products:
                return Decision(
                    action=ACTION_SUGGEST_COUPON,
                    args={"product": state.current_product_focus},
                    reason="customer hesitating — nudge with a coupon",
                    confidence=0.75,
                )

        # ── 9. Fallback: LLM ─────────────────────────────────────────────
        return Decision(
            action=ACTION_LLM_REPLY,
            reason=f"no rule matched for intent={intent.name} — LLM fallback",
            confidence=0.50,
        )
