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
    ACTION_RECOMMEND_ADDON,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_SUGGEST_COUPON,
    ACTION_TRACK_ORDER,
    ACTION_WEB_SEARCH,
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
    INTENT_PICK_LIST_ITEM,
)
from ..state.stages import STAGE_CHECKOUT, STAGE_DECIDING, STAGE_ORDERING

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

        # ── 2. Resend payment link / retry order ──────────────────────────
        if intent.name == INTENT_PAY_NOW or (
            state.stage == STAGE_CHECKOUT and intent.name in (INTENT_PAY_NOW, INTENT_START_ORDER)
        ):
            if state.checkout_url:
                return Decision(
                    action=ACTION_SEND_PAYMENT_LINK,
                    args={"checkout_url": state.checkout_url},
                    reason="customer in checkout stage — resend payment link",
                )
            # No checkout_url yet but we are in ordering/checkout.
            # If we have a product in focus and the order_prep is complete
            # (the customer already provided name/city/address), try to
            # create the order now instead of falling through to LLM.
            if (
                state.current_product_focus
                and state.stage in (STAGE_ORDERING, STAGE_DECIDING, STAGE_CHECKOUT)
                and facts.orderable
            ):
                return Decision(
                    action=ACTION_PROPOSE_DRAFT_ORDER,
                    args={"product": state.current_product_focus},
                    reason="pay_now with product focus + no checkout_url → retry order creation",
                    confidence=0.92,
                )

        # ── 3. Track order ────────────────────────────────────────────────
        if intent.name == INTENT_TRACK_ORDER:
            return Decision(
                action=ACTION_TRACK_ORDER,
                args={"order_id": intent.slots.get("order_id", "")},
                reason="customer asked for order status",
            )

        # ── 3.5 Pick from numbered list ───────────────────────────────────────
        # CRITICAL bridging logic: when the customer picks "1" / "2" / "3"
        # we MUST commit to a product and start the order flow. Falling
        # through to the LLM here was the production bug that broke the
        # whole sales funnel — the user would type a number, the bot would
        # respond with generic chit-chat, and product_focus stayed null
        # forever.
        if intent.name == INTENT_PICK_LIST_ITEM:
            # Prefer the candidates persisted from the most recent search;
            # fall back to last_recommended_products (set by the suggestion
            # engine) so an old recommendation list is still actionable.
            candidates = (
                list(state.last_search_candidates or [])
                or list(state.last_recommended_products or [])
            )
            if candidates:
                idx = int(intent.slots.get("list_index", 1))
                idx = max(1, min(idx, len(candidates)))
                product = candidates[idx - 1]
                if product:
                    if facts.orderable:
                        return Decision(
                            action=ACTION_PROPOSE_DRAFT_ORDER,
                            args={"product": product},
                            reason=f"customer picked option {idx} from list — start order",
                            confidence=0.95,
                        )
                    # Store not orderable — confirm product focus, show details
                    return Decision(
                        action=ACTION_SEARCH_PRODUCTS,
                        args={"query": product.get("title", ""),
                              "selected_product": product},
                        reason=f"customer picked option {idx} — not orderable, confirm product",
                        confidence=0.90,
                    )
            # We saw a numeric pick but have no list to map it onto. Don't
            # punt to the LLM — ask for clarification so the customer can
            # name the product (or repeat the search).
            return Decision(
                action=ACTION_CLARIFY,
                args={"question": "أي منتج تقصد؟ اكتب اسمه أو اطلب مني عرض المنتجات مرة ثانية."},
                reason="pick_list_item with no remembered candidates — ask for clarification",
                confidence=0.7,
            )

        # ── 3.7 Continue order preparation while collecting checkout details ──
        # While ordering we treat slot-bearing messages and a small set of
        # "neutral" intents as continuation so the funnel doesn't reset.
        #
        # Two rules to keep this from over-firing — the trap that produced
        # "راجياً سجلت اهتمامك بـ فستان" when the customer actually asked
        # "تعرض لي المنتجات بالصور؟":
        #
        #   a) ASK_PRODUCT / ASK_PRICE are NOT continuation intents on their
        #      own. A real product/price question mid-order is a request to
        #      browse, not a slot fill — fall through to SEARCH_PRODUCTS so
        #      the customer can actually compare. (They WILL still match the
        #      slots clause below if the message also contains a city /
        #      name / short_address_code, which is the legitimate
        #      "الرياض ABCD1234" case.)
        #   b) Greeting / general / hesitation stay in the list so a polite
        #      "هلا" or "تمام" doesn't bounce the customer to the greeting
        #      template. The DraftOrderHandler will run
        #      extract_address_signals on the raw text and decide whether
        #      the message actually contained data.
        _CONTINUATION_INTENTS = (
            INTENT_START_ORDER,
            INTENT_GENERAL,
            INTENT_GREETING,
            INTENT_HESITATION,
        )
        if (
            state.stage in (STAGE_ORDERING, STAGE_DECIDING)
            and state.current_product_focus
            and not state.checkout_url
            and (
                intent.name in _CONTINUATION_INTENTS
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
        # Two hard rules to prevent the "bot keeps re-greeting mid-order" bug:
        #   a) NEVER greet if the customer is in a committed sales stage
        #      (deciding/ordering/checkout). The continuation block above
        #      already routes those messages back into the order flow.
        #   b) NEVER greet twice in the same conversation: once `greeted`
        #      is true, only an explicit INTENT_GREETING re-triggers the
        #      template, and even then we only do it when the funnel is
        #      back at discovery (e.g. after an order completed).
        _greet_locked = state.stage in (
            STAGE_DECIDING, STAGE_ORDERING, STAGE_CHECKOUT,
        )
        if not _greet_locked:
            if intent.name == INTENT_GREETING and not state.greeted:
                return Decision(
                    action=ACTION_GREET,
                    reason="explicit greeting on first turn",
                )
            if not state.greeted and intent.name == INTENT_GENERAL:
                return Decision(
                    action=ACTION_GREET,
                    reason="first-turn general help",
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

        # ── 8.5 Upsell / addon recommendation ────────────────────────────
        if (
            state.current_product_focus
            and ctx.sales_context
            and ctx.sales_context.recommendations
            and intent.name in (INTENT_START_ORDER, INTENT_PAY_NOW, INTENT_ASK_PRODUCT)
        ):
            return Decision(
                action=ACTION_RECOMMEND_ADDON,
                args={"query": state.current_product_focus.get("category", "")},
                reason="customer close to purchase with recommendations available",
                confidence=0.68,
            )

        # ── 8.6 Web research when store knowledge likely insufficient ─────
        if (
            intent.name == INTENT_GENERAL
            and ctx.sales_context
            and not ctx.facts.has_products
            and len(ctx.message.split()) >= 4
        ):
            return Decision(
                action=ACTION_WEB_SEARCH,
                args={"query": ctx.message},
                reason="general knowledge question with weak store context",
                confidence=0.55,
            )

        # ── 9. Fallback: LLM ─────────────────────────────────────────────
        return Decision(
            action=ACTION_LLM_REPLY,
            reason=f"no rule matched for intent={intent.name} — LLM fallback",
            confidence=0.50,
        )
