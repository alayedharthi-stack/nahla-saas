"""
brain/suggestion/engine.py
──────────────────────────
DefaultSuggestionEngine — lightweight next-best-step recommender.

This layer does not make decisions or execute actions.  It only explains what
the assistant should try next after the current action/result pair, so that:
  - deterministic template replies can add a natural CTA
  - LLM fallback can receive a short `recommended_next_step`
  - logs / traces can explain coupon consideration and purchase proximity
"""
from __future__ import annotations

from ..decision.actions import (
    ACTION_CLARIFY,
    ACTION_FAQ_REPLY,
    ACTION_GREET,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SEND_PAYMENT_LINK,
    ACTION_SUGGEST_COUPON,
    ACTION_TRACK_ORDER,
)
from ..types import (
    BrainContext,
    SuggestionSnapshot,
    Decision,
    ActionResult,
    INTENT_HESITATION,
)


class DefaultSuggestionEngine:
    """Suggest the next best move after the current action has been resolved."""

    def suggest(
        self,
        ctx: BrainContext,
        decision: Decision,
        result: ActionResult,
    ) -> SuggestionSnapshot:
        suggestion = SuggestionSnapshot()

        if decision.action == ACTION_GREET:
            suggestion.suggested_next_step = "discover_customer_need"
            suggestion.needs_follow_up_question = True
            suggestion.follow_up_question = "وش المنتج أو الخدمة التي تبحث عنها اليوم؟"
            return suggestion

        if decision.action == ACTION_FAQ_REPLY:
            topic = str(result.data.get("topic") or "")
            if topic == "identity":
                suggestion.suggested_next_step = "discover_customer_need"
                suggestion.needs_follow_up_question = True
                suggestion.follow_up_question = "وش أقدر أخدمك فيه اليوم؟"
                return suggestion
            if topic == "store_info":
                suggestion.suggested_next_step = "continue_browsing_store"
                suggestion.needs_follow_up_question = True
                suggestion.follow_up_question = "إذا عندك منتج معيّن في بالك أرسل اسمه وسأبحث لك عنه."
                return suggestion
            if topic == "shipping":
                suggestion.suggested_next_step = "select_product_before_shipping_details"
                suggestion.needs_follow_up_question = True
                suggestion.follow_up_question = "إذا اخترت المنتج أقدر أوضح لك الخطوة التالية للطلب أو الشحن."
                return suggestion
            if topic == "owner_contact":
                suggestion.suggested_next_step = "offer_direct_help_or_contact"
                suggestion.needs_follow_up_question = True
                suggestion.follow_up_question = "إذا تحب أساعدك هنا مباشرة قبل التواصل، أرسل طلبك أو اسم المنتج."
                return suggestion
            return suggestion

        if decision.action == ACTION_CLARIFY:
            suggestion.suggested_next_step = "collect_missing_detail"
            suggestion.needs_follow_up_question = True
            suggestion.follow_up_question = result.data.get("question", "")
            return suggestion

        if decision.action == ACTION_SEARCH_PRODUCTS:
            products = result.data.get("products", []) or []
            count = int(result.data.get("count") or len(products))

            if result.data.get("suggest_narrow"):
                suggestion.suggested_next_step = "narrow_to_one_product"
                suggestion.needs_follow_up_question = True
                suggestion.follow_up_question = "أي خيار منهم نكمل عليه؟ أرسل الاسم أو الرقم."
                return suggestion

            if count == 1:
                suggestion.suggested_next_step = "move_to_order"
                suggestion.close_to_purchase = True
                suggestion.needs_follow_up_question = True
                suggestion.follow_up_question = "إذا ناسبك المنتج أقدر أجهز لك الطلب مباشرة."
                return suggestion

            if count > 1:
                suggestion.suggested_next_step = "select_product"
                suggestion.needs_follow_up_question = True
                suggestion.follow_up_question = "إذا أعجبك منتج معين أرسل اسمه أو رقمه وسأكمل معك."
                return suggestion

            suggestion.suggested_next_step = "wait_for_store_sync_or_human_help"
            return suggestion

        if decision.action == ACTION_PROPOSE_DRAFT_ORDER:
            suggestion.close_to_purchase = True
            if result.data.get("checkout_url"):
                suggestion.suggested_next_step = "complete_checkout"
                suggestion.route_to_checkout = True
                return suggestion

            if result.data.get("needs_collection"):
                suggestion.suggested_next_step = "collect_checkout_details"
                suggestion.needs_follow_up_question = True
                suggestion.follow_up_question = str(result.data.get("question") or "")
                return suggestion

            suggestion.suggested_next_step = "confirm_order_details"
            suggestion.needs_follow_up_question = True
            suggestion.follow_up_question = "إذا تريد أكمل معك، أرسل لي الكمية أو أي ملاحظة على الطلب."
            return suggestion

        if decision.action == ACTION_SEND_PAYMENT_LINK:
            suggestion.suggested_next_step = "complete_payment"
            suggestion.close_to_purchase = True
            suggestion.route_to_checkout = True
            return suggestion

        if decision.action == ACTION_TRACK_ORDER:
            suggestion.suggested_next_step = "offer_additional_help"
            suggestion.needs_follow_up_question = True
            suggestion.follow_up_question = "إذا تريد أساعدك في شيء آخر بخصوص الطلب أنا حاضر."
            return suggestion

        if decision.action == ACTION_SUGGEST_COUPON:
            suggestion.suggested_next_step = "reconsider_purchase_with_discount"
            suggestion.coupon_logic_considered = True
            suggestion.discount_ok_now = True
            suggestion.close_to_purchase = True
            suggestion.needs_follow_up_question = True
            suggestion.follow_up_question = "إذا ناسبك العرض أقدر أجهز لك الطلب مباشرة."
            return suggestion

        if decision.action == ACTION_HANDOFF:
            suggestion.suggested_next_step = "await_human_follow_up"
            return suggestion

        if decision.action == ACTION_LLM_REPLY:
            suggestion.suggested_next_step = "resolve_ambiguous_need"
            suggestion.coupon_logic_considered = self._should_consider_coupon(ctx)
            suggestion.discount_ok_now = (
                suggestion.coupon_logic_considered
                and not bool(decision.args.get("policy_reason"))
            )
            if ctx.intent.name == INTENT_HESITATION and suggestion.discount_ok_now:
                suggestion.suggested_next_step = "soft_discount_nudge"
                suggestion.close_to_purchase = True
            return suggestion

        return suggestion

    def _should_consider_coupon(self, ctx: BrainContext) -> bool:
        return bool(
            ctx.intent.name == INTENT_HESITATION
            and ctx.facts.has_coupons
            and ctx.state.current_product_focus
        )
