"""LIVE-BARE-EXTERNAL-URL-STOLEN-BY-CHECKOUT-CONTINUATION-D4

Post-decision: a non-checkout Decision must remain authoritative after compose.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO

Customer phrases appear as TEST INPUT only. Assert action, ownership,
evidence, state deltas, model provenance, and injection absence/presence.
Do not assert exact Arabic model wording.
"""
from __future__ import annotations

import copy
import os
import sys
from typing import Any, List, Optional
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.inbound_url_spans import (  # noqa: E402
    is_url_only_inbound,
    semantic_text_excluding_url_spans,
)
from core.order_flow import context_aware_dedup_fallback  # noqa: E402
from core.wa_cart_line_items import ITEM_STATUS_CONFIRMED  # noqa: E402
from core.wa_draft_confirmation import (  # noqa: E402
    compose_wa_order_flow_reply,
    maybe_inject_draft_flow_reply,
)
from modules.ai.brain.commerce.complaint_refund_topic_guard import (  # noqa: E402
    should_block_order_draft_injection,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from modules.ai.brain.decision.checkout_continuation_evidence import (  # noqa: E402
    has_current_turn_checkout_continuation_evidence,
    has_positive_checkout_ownership,
    select_last_question,
    select_pending_action,
    should_stamp_checkout_progress,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    INTENT_GENERAL,
    INTENT_PAY_NOW,
    INTENT_START_ORDER,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CITY = "الرياض"
GENERIC_SHORT = "RRRD1234"
GENERIC_PRODUCT_ID = "sku-white-sneaker"
GENERIC_PRODUCT_TITLE = "حذاء رياضي أبيض"
GENERIC_URL = "https://example.net/share/video/abc123"
RESUME_PHRASE = "أبغى أكمل الطلب السابق"
MODEL_CANDIDATE = "model-owned unrelated-turn candidate"
PREVIOUS_CHECKOUT_QUESTION = "أرسل لي التفاصيل الناقصة لإكمال الطلب."
NUDGE_MARKER = "تأمر بشيء أكمّل لك فيه؟"
RULE_E_MARKER = "اختياراتك محفوظة في هذه المحادثة مبدئيًا"


def _line_item() -> dict[str, Any]:
    return {
        "product_id": GENERIC_PRODUCT_ID,
        "title": GENERIC_PRODUCT_TITLE,
        "quantity": 1,
        "unit_price": 199.0,
        "match_status": ITEM_STATUS_CONFIRMED,
        "last_updated_at": "2026-09-03T09:27:46.921840+00:00",
    }


def _active_checkout_state(
    *,
    pending: str = "collect_checkout_details",
    last_question: str = PREVIOUS_CHECKOUT_QUESTION,
) -> MerchantConversationState:
    prep = OrderPreparationState(
        product_id=GENERIC_PRODUCT_ID,
        quantity=1,
        customer_first_name="أحمد",
        customer_last_name="سالم",
        missing_fields=["payment_method"],
        order_status="awaiting_address",
        line_items=[_line_item()],
        catalog_line_items_authoritative=True,
    )
    return MerchantConversationState(
        stage="ordering",
        greeted=True,
        current_product_focus={
            "id": GENERIC_PRODUCT_ID,
            "external_id": GENERIC_PRODUCT_ID,
            "title": GENERIC_PRODUCT_TITLE,
            "price": 199.0,
        },
        selected_product_id=GENERIC_PRODUCT_ID,
        checkout_url=None,
        draft_order_id=None,
        last_search_candidates=[],
        pending_action=pending,
        last_question_asked=last_question,
        last_question_answered=False,
        recommended_next_step=pending,
        cart_items=[_line_item()],
        turn=12,
        updated_at="2026-09-04T17:47:22+00:00",
        order_prep=prep,
        last_action="propose_draft_order",
        last_intent="start_order",
    )


def _ctx(
    message: str,
    *,
    intent_name: str = INTENT_GENERAL,
    slots: Optional[dict[str, Any]] = None,
    orderable: bool = True,
    state: Optional[MerchantConversationState] = None,
    tenant_id: int = 9001,
) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000099",
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=0.5,
            raw_message=message,
            slots=dict(slots or {}),
            extraction_method="rules",
        ),
        state=state or _active_checkout_state(),
        facts=CommerceFacts(
            store_name=GENERIC_MERCHANT,
            has_products=True,
            product_count=4,
            in_stock_count=4,
            orderable=orderable,
            snapshot_fresh=True,
        ),
    )


def _snapshot(state: MerchantConversationState) -> dict[str, Any]:
    prep = getattr(state, "order_prep", None)
    items = list(getattr(prep, "line_items", None) or [])
    return {
        "stage": state.stage,
        "focus_id": (state.current_product_focus or {}).get("id"),
        "focus_title": (state.current_product_focus or {}).get("title"),
        "checkout_url": state.checkout_url,
        "draft_order_id": state.draft_order_id,
        "pending_action": state.pending_action,
        "product_id": getattr(prep, "product_id", None),
        "quantity": getattr(prep, "quantity", None),
        "first_name": getattr(prep, "customer_first_name", None),
        "city": getattr(prep, "city", None),
        "short_code": getattr(prep, "short_address_code", None),
        "maps_url": getattr(prep, "google_maps_url", None),
        "payment_method": getattr(prep, "payment_method", None),
        "line_items": copy.deepcopy(items),
        "cart_items": copy.deepcopy(list(state.cart_items or [])),
        "line_item_updated_at": (items[0].get("last_updated_at") if items else None),
    }


def _llm_decision() -> Decision:
    return Decision(
        action=ACTION_LLM_REPLY,
        args={"topic": "commerce_ambiguous"},
        reason="url-only catalog turn",
        confidence=0.7,
    )


def _checkout_decision() -> Decision:
    return Decision(
        action=ACTION_PROPOSE_DRAFT_ORDER,
        args={},
        reason="authorized checkout",
        confidence=0.9,
    )


def _pipeline_post_compose_draft_inject(
    *,
    reply: str,
    order_prep: Any,
    brain_state: Any,
    decision: Any,
    customer_message: str = "",
    cart_changed: bool = False,
    history: Optional[List[Any]] = None,
    ctx: Any = None,
) -> str:
    if should_block_order_draft_injection(
        brain_state=brain_state,
        customer_message=customer_message or "",
        decision=decision,
        history=list(history or []),
        ctx=ctx,
    ):
        return reply or ""
    return maybe_inject_draft_flow_reply(
        reply=reply or "",
        order_prep=order_prep,
        brain_state=brain_state,
        cart_changed=cart_changed,
        customer_message=customer_message or "",
        history=list(history or []),
    )


def _stale_brain_dict(state: MerchantConversationState) -> dict[str, Any]:
    return {
        "stage": state.stage,
        "current_product_focus": dict(state.current_product_focus or {}),
        "pending_action": state.pending_action,
        "cart_items": copy.deepcopy(list(state.cart_items or [])),
        "order_prep": state.order_prep.to_dict() if hasattr(state.order_prep, "to_dict") else {},
        "updated_at": state.updated_at,
    }


def _dedup_with_state(
    *,
    inbound: str,
    decision: Any = None,
    decision_action: str = "",
    default_fallback: str = "",
    state: Optional[MerchantConversationState] = None,
) -> str:
    st = state or _active_checkout_state()
    bs = _stale_brain_dict(st)
    import core.order_flow as of

    def _fake_load(db, tenant_id, phone):
        return None, bs

    orig_load = of._load_brain_state
    try:
        of._load_brain_state = _fake_load
        return context_aware_dedup_fallback(
            object(),
            tenant_id=9001,
            phone="966500000099",
            history=[],
            default_fallback=default_fallback,
            inbound_text=inbound,
            decision=decision,
            decision_action=decision_action or str(getattr(decision, "action", "") or ""),
            decision_args=dict(getattr(decision, "args", None) or {}),
        )
    finally:
        of._load_brain_state = orig_load


class TestALivePairShape:
    def test_url_only_stale_ordering_no_fresh_slots(self) -> None:
        ctx = _ctx(GENERIC_URL)
        assert is_url_only_inbound(GENERIC_URL) is True
        assert semantic_text_excluding_url_spans(GENERIC_URL) == ""
        assert has_current_turn_checkout_continuation_evidence(ctx) is False
        snap = _snapshot(ctx.state)
        assert snap["quantity"] == 1
        assert snap["focus_title"] == GENERIC_PRODUCT_TITLE
        assert snap["city"] is None or snap["city"] == ""
        assert snap["payment_method"] is None or snap["payment_method"] == ""
        assert snap["line_item_updated_at"] == "2026-09-03T09:27:46.921840+00:00"


class TestBDecisionRemainsNonCheckout:
    def test_url_only_stays_llm_reply(self) -> None:
        ctx = _ctx(GENERIC_URL)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert has_positive_checkout_ownership(decision=decision, ctx=ctx) is False


class TestCNoDraftInjection:
    def test_maybe_inject_does_not_replace_model_candidate(self) -> None:
        ctx = _ctx(GENERIC_URL)
        decision = DefaultDecisionEngine().decide(ctx)
        assert should_block_order_draft_injection(
            brain_state=ctx.state,
            customer_message=GENERIC_URL,
            decision=decision,
            history=[],
            ctx=ctx,
        ) is True
        out = _pipeline_post_compose_draft_inject(
            reply=MODEL_CANDIDATE,
            order_prep=ctx.state.order_prep,
            brain_state=ctx.state,
            decision=decision,
            customer_message=GENERIC_URL,
            ctx=ctx,
        )
        assert out == MODEL_CANDIDATE


class TestDRuleENotInvoked:
    def test_compose_wa_order_flow_reply_not_called_on_blocked_path(self) -> None:
        ctx = _ctx(GENERIC_URL)
        decision = DefaultDecisionEngine().decide(ctx)
        with patch(
            "core.wa_draft_confirmation.compose_wa_order_flow_reply",
            wraps=compose_wa_order_flow_reply,
        ) as spy:
            out = _pipeline_post_compose_draft_inject(
                reply=MODEL_CANDIDATE,
                order_prep=ctx.state.order_prep,
                brain_state=ctx.state,
                decision=decision,
                customer_message=GENERIC_URL,
                ctx=ctx,
            )
            spy.assert_not_called()
        assert out == MODEL_CANDIDATE
        assert RULE_E_MARKER not in out


class TestEDedupCannotNudge:
    def test_active_order_nudge_not_substituted(self) -> None:
        decision = _llm_decision()
        out = _dedup_with_state(inbound=GENERIC_URL, decision=decision)
        assert NUDGE_MARKER not in (out or "")
        assert GENERIC_PRODUCT_TITLE not in (out or "")
        assert out == ""


class TestFModelCandidatePreserved:
    def test_final_body_equals_model_candidate(self) -> None:
        ctx = _ctx(GENERIC_URL)
        decision = DefaultDecisionEngine().decide(ctx)
        injected = _pipeline_post_compose_draft_inject(
            reply=MODEL_CANDIDATE,
            order_prep=ctx.state.order_prep,
            brain_state=ctx.state,
            decision=decision,
            customer_message=GENERIC_URL,
            ctx=ctx,
        )
        nudged = _dedup_with_state(inbound=GENERIC_URL, decision=decision)
        assert injected == MODEL_CANDIDATE
        assert not (nudged or "").strip()


class TestGExpressionOwner:
    def test_inject_and_dedup_do_not_claim_guard_or_template_ownership(self) -> None:
        ctx = _ctx(GENERIC_URL)
        decision = DefaultDecisionEngine().decide(ctx)
        assert has_positive_checkout_ownership(decision=decision, ctx=ctx) is False
        blocked = should_block_order_draft_injection(
            brain_state=ctx.state,
            customer_message=GENERIC_URL,
            decision=decision,
            history=[],
            ctx=ctx,
        )
        assert blocked is True
        out = _dedup_with_state(inbound=GENERIC_URL, decision=decision)
        assert out == ""


class TestHCartPreserved:
    def test_line_item_quantity_price_untouched(self) -> None:
        ctx = _ctx(GENERIC_URL)
        before = _snapshot(ctx.state)
        DefaultDecisionEngine().decide(ctx)
        after = _snapshot(ctx.state)
        assert after["line_items"] == before["line_items"]
        assert after["cart_items"] == before["cart_items"]
        assert after["quantity"] == 1
        assert after["line_item_updated_at"] == before["line_item_updated_at"]


class TestIAddressPaymentUnchanged:
    def test_checkout_fields_stay_as_stored(self) -> None:
        ctx = _ctx(GENERIC_URL)
        before = _snapshot(ctx.state)
        DefaultDecisionEngine().decide(ctx)
        after = _snapshot(ctx.state)
        assert after["city"] == before["city"]
        assert after["short_code"] == before["short_code"]
        assert after["maps_url"] == before["maps_url"]
        assert after["payment_method"] == before["payment_method"]


class TestJPendingAndLastQuestion:
    def test_checkout_progress_prompts_are_not_restamped(self) -> None:
        ctx = _ctx(GENERIC_URL)
        decision = DefaultDecisionEngine().decide(ctx)
        pending = select_pending_action(
            previous="collect_checkout_details",
            suggested="collect_checkout_details",
            decision=decision,
            ctx=ctx,
        )
        question, answered = select_last_question(
            previous_question=PREVIOUS_CHECKOUT_QUESTION,
            previous_answered=False,
            asked_now=PREVIOUS_CHECKOUT_QUESTION,
            suggested_next_step="collect_checkout_details",
            decision=decision,
            ctx=ctx,
        )
        assert pending == "collect_checkout_details"
        assert question == PREVIOUS_CHECKOUT_QUESTION
        assert answered is False
        non_checkout = select_pending_action(
            previous="collect_checkout_details",
            suggested="resolve_ambiguous_need",
            decision=decision,
            ctx=ctx,
        )
        assert non_checkout == "resolve_ambiguous_need"


class TestKTimestampSeparation:
    def test_stamp_gate_false_does_not_require_freezing_conversation_updated_at(self) -> None:
        ctx = _ctx(GENERIC_URL)
        decision = DefaultDecisionEngine().decide(ctx)
        assert should_stamp_checkout_progress(decision=decision, ctx=ctx) is False
        assert ctx.state.updated_at == "2026-09-04T17:47:22+00:00"
        assert _snapshot(ctx.state)["line_item_updated_at"] == (
            "2026-09-03T09:27:46.921840+00:00"
        )


class TestLExplicitResumeContinues:
    def test_resume_request_owns_checkout(self) -> None:
        ctx = _ctx(RESUME_PHRASE)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert has_positive_checkout_ownership(decision=decision, ctx=ctx) is True
        assert should_block_order_draft_injection(
            brain_state=ctx.state,
            customer_message=RESUME_PHRASE,
            decision=decision,
            history=[],
            ctx=ctx,
        ) is False


class TestMSlotAnswersContinue:
    def test_maps_payment_quantity_continue(self) -> None:
        maps = "https://maps.app.goo.gl/abc123"
        maps_ctx = _ctx(maps, slots={"google_maps_url": maps})
        maps_ctx.state.order_prep.missing_fields = ["google_maps_url", "delivery_address"]
        maps_decision = DefaultDecisionEngine().decide(maps_ctx)
        assert maps_decision.action in {ACTION_PROPOSE_DRAFT_ORDER, "order_context_update"}
        assert has_positive_checkout_ownership(decision=maps_decision, ctx=maps_ctx) is True

        pay_ctx = _ctx("تحويل")
        pay_ctx.state.order_prep.missing_fields = ["payment_method"]
        pay_decision = DefaultDecisionEngine().decide(pay_ctx)
        assert pay_decision.action == ACTION_PROPOSE_DRAFT_ORDER

        qty_ctx = _ctx("كميتين")
        qty_decision = DefaultDecisionEngine().decide(qty_ctx)
        assert qty_decision.action == ACTION_PROPOSE_DRAFT_ORDER


class TestNProposeDraftAndRuleERemain:
    def test_authorized_checkout_still_injects_rule_e(self) -> None:
        ctx = _ctx(RESUME_PHRASE)
        decision = _checkout_decision()
        assert should_block_order_draft_injection(
            brain_state=ctx.state,
            customer_message=RESUME_PHRASE,
            decision=decision,
            history=[],
            ctx=ctx,
        ) is False
        with patch(
            "core.wa_draft_confirmation.compose_wa_order_flow_reply",
            wraps=compose_wa_order_flow_reply,
        ) as spy:
            out = _pipeline_post_compose_draft_inject(
                reply="",
                order_prep=ctx.state.order_prep,
                brain_state=ctx.state,
                decision=decision,
                customer_message=RESUME_PHRASE,
                cart_changed=False,
                ctx=ctx,
            )
            spy.assert_called()
        assert out
        assert out != MODEL_CANDIDATE


class TestOOrderAwareDedupWhenOwned:
    def test_authorized_checkout_may_use_order_nudge(self) -> None:
        out = _dedup_with_state(
            inbound=RESUME_PHRASE,
            decision=_checkout_decision(),
        )
        assert GENERIC_PRODUCT_TITLE in out
        assert NUDGE_MARKER in out
        pending = select_pending_action(
            previous="collect_checkout_details",
            suggested="collect_checkout_details",
            decision=_checkout_decision(),
            ctx=_ctx(RESUME_PHRASE),
        )
        assert pending == "collect_checkout_details"


class TestPIsolationAndNoCta:
    def test_tenant_isolation_no_fetch_no_cta(self) -> None:
        ctx_a = _ctx(GENERIC_URL, tenant_id=9001)
        ctx_b = _ctx(GENERIC_URL, tenant_id=9002)
        assert ctx_a.tenant_id != ctx_b.tenant_id
        decision = DefaultDecisionEngine().decide(ctx_a)
        args = dict(decision.args or {})
        assert not args.get("cta_url")
        assert not args.get("store_url")
        assert not args.get("authorized_cta_url")
        assert args.get("cta_url") != GENERIC_URL
        with patch("urllib.request.urlopen") as fetch:
            DefaultDecisionEngine().decide(ctx_a)
            _pipeline_post_compose_draft_inject(
                reply=MODEL_CANDIDATE,
                order_prep=ctx_a.state.order_prep,
                brain_state=ctx_a.state,
                decision=decision,
                customer_message=GENERIC_URL,
                ctx=ctx_a,
            )
            fetch.assert_not_called()


class TestIntentPayNowAndStartOrderStillOwn:
    def test_start_order_and_pay_now_intents_authorize(self) -> None:
        start_ctx = _ctx("أبي أطلب", intent_name=INTENT_START_ORDER)
        assert has_positive_checkout_ownership(
            decision=_llm_decision(),
            ctx=start_ctx,
        ) is True
        pay_ctx = _ctx("أدفع الحين", intent_name=INTENT_PAY_NOW)
        assert has_positive_checkout_ownership(
            decision=_llm_decision(),
            ctx=pay_ctx,
        ) is True
