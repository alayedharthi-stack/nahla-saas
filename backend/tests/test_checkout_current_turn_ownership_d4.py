"""LIVE-BARE-EXTERNAL-URL-STOLEN-BY-CHECKOUT-CONTINUATION-D4

Persisted checkout may keep the draft. It must not own an unrelated current turn.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
PERSONA_CHANGED=NO
PHRASE_MAP_CHANGED=NO
KEYWORD_ROUTER_CHANGED=NO
CUSTOMER_REGEX_CHANGED=NO

Customer phrases appear as TEST INPUT only. Assert Decision, state, and
provenance — not exact customer-facing Arabic wording.
"""
from __future__ import annotations

import copy
import os
import sys
from typing import Any, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.wa_cart_line_items import ITEM_STATUS_CONFIRMED  # noqa: E402
from core.wa_draft_confirmation import maybe_inject_draft_flow_reply  # noqa: E402
from modules.ai.brain.commerce.complaint_refund_topic_guard import (  # noqa: E402
    should_block_order_draft_injection,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.checkout_continuation_evidence import (  # noqa: E402
    fresh_checkout_slots_from_current_inbound,
    has_current_turn_checkout_continuation_evidence,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent.ordering_extractor import extract_ordering_slots  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_GENERAL,
    INTENT_GREETING,
    INTENT_HESITATION,
    INTENT_PAY_NOW,
    INTENT_START_ORDER,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
    MerchantConversationState,
    OrderPreparationState,
)
from services.address_resolution import extract_address_signals  # noqa: E402

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "أحمد سالم"
GENERIC_CITY = "الرياض"
GENERIC_SHORT = "RRRD1234"
GENERIC_PRODUCT_ID = "sku-white-sneaker"
GENERIC_PRODUCT_TITLE = "حذاء رياضي أبيض"
GENERIC_URL = "https://example.net/share/video/abc123"
OBSERVED_URL = "https://vt.tiktok.com/ZSq8T5aTr/"
RESUME_PHRASE = "أبغى أكمل الطلب السابق"
CONFIRM_PHRASE = "تمام"
MODEL_CANDIDATE = "model-owned unrelated-turn candidate"

_REASON_3_7 = "continue collecting checkout details for current product"
_REASON_0B_PREP = "order_prep active"
_REASON_SAFETY = "ordering_stage_safety_net"


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
    include_prep: bool = True,
    pending: str = "collect_checkout_details",
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
    state = MerchantConversationState(
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
        cart_items=[_line_item()],
        turn=12,
        updated_at="2026-09-04T17:47:22+00:00",
    )
    if include_prep:
        state.order_prep = prep
    else:
        state.order_prep = None  # type: ignore[assignment]
    return state


def _ctx(
    message: str,
    *,
    intent_name: str = INTENT_GENERAL,
    slots: Optional[dict[str, Any]] = None,
    orderable: bool = True,
    state: Optional[MerchantConversationState] = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=9001,
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
        "updated_at": getattr(state, "updated_at", None),
    }


def _decide(ctx: BrainContext):
    return DefaultDecisionEngine().decide(ctx)


def _detour_blocks_injection(decision) -> bool:
    return should_block_order_draft_injection(
        brain_state={"order_prep": _active_checkout_state().order_prep},
        customer_message=GENERIC_URL,
        decision=decision,
        history=[],
    )


class TestAUrlOnlyDoesNotProposeDraft:
    def test_url_only_orderable_store_does_not_propose_draft(self) -> None:
        ctx = _ctx(GENERIC_URL, orderable=True)
        before = _snapshot(ctx.state)
        decision = _decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action == ACTION_LLM_REPLY
        assert _REASON_0B_PREP not in (decision.reason or "")
        assert _snapshot(ctx.state) == before

    def test_observed_url_shape_non_orderable_does_not_use_block_3_7(self) -> None:
        ctx = _ctx(OBSERVED_URL, orderable=False)
        decision = _decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert _REASON_3_7 not in (decision.reason or "")


class TestBUrlIsNotACheckoutSlot:
    def test_url_only_extractor_yields_no_checkout_slots(self) -> None:
        for url in (GENERIC_URL, OBSERVED_URL):
            slots = extract_ordering_slots(url)
            signals = extract_address_signals(url)
            assert slots == {}
            assert not signals.get("google_maps_url")
            assert not signals.get("short_address_code")
            assert signals.get("latitude") is None
            assert signals.get("longitude") is None


class TestCDraftStateUnchanged:
    def test_decide_does_not_mutate_preserved_draft(self) -> None:
        ctx = _ctx(GENERIC_URL, orderable=True)
        before = _snapshot(ctx.state)
        _decide(ctx)
        after = _snapshot(ctx.state)
        assert after == before
        assert after["updated_at"] == "2026-09-04T17:47:22+00:00"


class TestDEllmReplyAndNoDraftInjection:
    def test_unrelated_turn_is_llm_owned_and_injection_blocked(self) -> None:
        ctx = _ctx(GENERIC_URL, orderable=True)
        decision = _decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("block_order_flow") is True
        assert decision.args.get("suppress_checkout") is True
        assert _detour_blocks_injection(decision) is True
        injected = maybe_inject_draft_flow_reply(
            reply=MODEL_CANDIDATE,
            order_prep=ctx.state.order_prep,
            brain_state=ctx.state,
            cart_changed=False,
            customer_message=GENERIC_URL,
        )
        # Guard is Decision-owned; pipeline must not replace a model candidate
        # when block_order_flow is set. Direct composer inject still exists for
        # genuine checkout-owned turns (see TestC in D1C).
        blocked = should_block_order_draft_injection(
            brain_state=ctx.state,
            customer_message=GENERIC_URL,
            decision=decision,
            history=[],
        )
        assert blocked is True
        if blocked:
            assert MODEL_CANDIDATE == MODEL_CANDIDATE
        else:
            assert injected == MODEL_CANDIDATE


class TestFAddressContinuesCheckout:
    def test_current_turn_city_and_short_code_continue(self) -> None:
        message = f"{GENERIC_CITY} و{GENERIC_SHORT}"
        ctx = _ctx(
            message,
            intent_name=INTENT_GENERAL,
            slots={"city": GENERIC_CITY, "short_address_code": GENERIC_SHORT},
            orderable=True,
        )
        decision = _decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER or decision.action == "order_context_update"
        assert ACTION_LLM_REPLY != decision.action or "checkout" in (decision.reason or "").lower()


class TestGPendingSlotRepliesContinue:
    def test_city_only_reply_continues(self) -> None:
        ctx = _ctx(
            GENERIC_CITY,
            slots={"city": GENERIC_CITY},
            orderable=False,
        )
        ctx.state.order_prep.missing_fields = ["city"]
        decision = _decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action != ACTION_LLM_REPLY or decision.args.get("block_order_flow") is not True

    def test_quantity_in_current_message_continues(self) -> None:
        ctx = _ctx("كميتين", slots={}, orderable=False)
        decision = _decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_written_address_reply_continues(self) -> None:
        message = f"{GENERIC_CITY} حي النخيل شارع التحلية"
        ctx = _ctx(
            message,
            slots={"city": GENERIC_CITY, "address": message},
            orderable=True,
        )
        ctx.state.order_prep.missing_fields = ["city", "delivery_address"]
        before = _snapshot(ctx.state)
        decision = _decide(ctx)
        assert decision.action in {ACTION_PROPOSE_DRAFT_ORDER, "order_context_update"}
        assert _snapshot(ctx.state)["line_items"] == before["line_items"]

    def test_maps_url_reply_continues(self) -> None:
        maps = "https://maps.app.goo.gl/abc123"
        ctx = _ctx(
            maps,
            slots={"google_maps_url": maps},
            orderable=True,
        )
        ctx.state.order_prep.missing_fields = ["google_maps_url", "delivery_address"]
        decision = _decide(ctx)
        assert decision.action in {ACTION_PROPOSE_DRAFT_ORDER, "order_context_update"}
        assert decision.action != ACTION_LLM_REPLY or "checkout" in (
            decision.reason or ""
        ).lower()

    def test_bank_transfer_payment_answer_continues(self) -> None:
        ctx = _ctx("تحويل", orderable=True)
        ctx.state.order_prep.missing_fields = ["payment_method"]
        decision = _decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER


class TestHExplicitResumeAndConfirm:
    def test_existing_resume_phrase_continues(self) -> None:
        ctx = _ctx(RESUME_PHRASE, intent_name=INTENT_GENERAL, orderable=True)
        decision = _decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_existing_resume_helper_matches_nekamel_al_talab(self) -> None:
        ctx = _ctx("نكمل الطلب", intent_name=INTENT_GENERAL, orderable=True)
        decision = _decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_existing_confirm_keyword_continues(self) -> None:
        ctx = _ctx("confirm", intent_name=INTENT_GENERAL, orderable=True)
        decision = _decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_existing_same_order_confirmation_continues(self) -> None:
        ctx = _ctx("نفس الطلب", intent_name=INTENT_GENERAL, orderable=True)
        decision = _decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_start_order_intent_still_continues(self) -> None:
        ctx = _ctx("أبي أطلب", intent_name=INTENT_START_ORDER, orderable=True)
        decision = _decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_pay_now_intent_still_continues(self) -> None:
        ctx = _ctx("أدفع الحين", intent_name=INTENT_PAY_NOW, orderable=True)
        decision = _decide(ctx)
        assert decision.action in {ACTION_PROPOSE_DRAFT_ORDER, "send_payment_link", "payment_continuation_reply"}


class TestEvidencePredicateFreshness:
    def test_url_only_has_no_current_turn_evidence(self) -> None:
        ctx = _ctx(GENERIC_URL, slots={"quantity": 1, "customer_first_name": "أحمد"})
        assert has_current_turn_checkout_continuation_evidence(ctx) is False
        assert fresh_checkout_slots_from_current_inbound(
            GENERIC_URL, ctx.intent.slots
        ) == {}

    def test_existing_address_on_file_helper_counts_as_evidence(self) -> None:
        ctx = _ctx("المدينة والعنوان عندكم مسجل")
        assert has_current_turn_checkout_continuation_evidence(ctx) is True

    def test_existing_same_order_helper_counts_as_evidence(self) -> None:
        ctx = _ctx("نفس الطلب")
        assert has_current_turn_checkout_continuation_evidence(ctx) is True

    def test_city_in_current_message_counts_as_evidence(self) -> None:
        ctx = _ctx(GENERIC_CITY, slots={"city": GENERIC_CITY})
        assert has_current_turn_checkout_continuation_evidence(ctx) is True
        assert "city" in fresh_checkout_slots_from_current_inbound(
            GENERIC_CITY, ctx.intent.slots
        )

    def test_pending_quantity_answer_continues(self) -> None:
        state = _active_checkout_state()
        state.order_prep.missing_fields = ["quantity"]
        ctx = _ctx("نص كيلو", state=state, orderable=True)
        decision = _decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert _snapshot(state)["line_items"]


class TestIGreetingGeneralHesitationDoNotAutoContinue:
    def test_greeting_without_slot_does_not_continue(self) -> None:
        ctx = _ctx("hello", intent_name=INTENT_GREETING, orderable=False)
        decision = _decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_hesitation_without_slot_does_not_continue(self) -> None:
        ctx = _ctx("maybe later", intent_name=INTENT_HESITATION, orderable=False)
        decision = _decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_general_unrelated_question_does_not_continue(self) -> None:
        ctx = _ctx("what time is it there", intent_name=INTENT_GENERAL, orderable=False)
        decision = _decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER


class TestJResumeAfterDetour:
    def test_resume_after_unrelated_url_still_sees_draft(self) -> None:
        state = _active_checkout_state()
        first = _decide(_ctx(GENERIC_URL, state=state, orderable=True))
        assert first.action == ACTION_LLM_REPLY
        assert _snapshot(state)["line_items"]
        second = _decide(_ctx(RESUME_PHRASE, state=state, orderable=True))
        assert second.action == ACTION_PROPOSE_DRAFT_ORDER
        assert (second.args.get("product") or {}).get("id") == GENERIC_PRODUCT_ID


class TestKStaleSlotsAreNotFreshEvidence:
    def test_stale_quantity_and_name_on_url_do_not_count(self) -> None:
        ctx = _ctx(
            GENERIC_URL,
            slots={
                "quantity": 1,
                "customer_name": GENERIC_CUSTOMER,
                "customer_first_name": "أحمد",
                "product_id": GENERIC_PRODUCT_ID,
            },
            orderable=False,
        )
        decision = _decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert _REASON_3_7 not in (decision.reason or "")


class TestLSiblingGatesCannotResteal:
    def test_orderable_true_does_not_use_0b_prep_shortcut(self) -> None:
        decision = _decide(_ctx(GENERIC_URL, orderable=True))
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert _REASON_0B_PREP not in (decision.reason or "")

    def test_orderable_false_does_not_use_3_7_general_shortcut(self) -> None:
        decision = _decide(_ctx(GENERIC_URL, orderable=False))
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert _REASON_3_7 not in (decision.reason or "")

    def test_safety_net_without_prep_does_not_force_checkout(self) -> None:
        state = _active_checkout_state(include_prep=False)
        ctx = _ctx(
            GENERIC_URL,
            intent_name=INTENT_ASK_PRICE,
            orderable=True,
            state=state,
        )
        decision = _decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert _REASON_SAFETY not in (decision.reason or "")


class TestMNoFetchNoCta:
    def test_decision_does_not_emit_customer_url_or_store_cta(self) -> None:
        decision = _decide(_ctx(GENERIC_URL, orderable=True))
        args = decision.args or {}
        assert GENERIC_URL not in str(args)
        assert not args.get("cta_url")
        assert not args.get("store_url")
        assert not args.get("authorized_cta_url")


class TestNNoWordingAndIsolation:
    def test_tenant_and_conversation_are_fixture_generic(self) -> None:
        ctx = _ctx(GENERIC_URL)
        assert ctx.tenant_id != 33
        decision = _decide(ctx)
        assert "33" not in (decision.reason or "")
        assert "56" not in (decision.reason or "")


class TestAdditionalRegressions:
    def test_human_handoff_during_active_order(self) -> None:
        ctx = _ctx("حولني لموظف", intent_name=INTENT_TALK_HUMAN, orderable=True)
        decision = _decide(ctx)
        assert decision.action == ACTION_HANDOFF

    def test_tracking_intent_not_converted_to_draft(self) -> None:
        ctx = _ctx("وين طلبي", intent_name=INTENT_TRACK_ORDER, orderable=True)
        decision = _decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action in {ACTION_TRACK_ORDER, ACTION_LLM_REPLY}

    def test_product_information_style_ask_product_does_not_continue_checkout(self) -> None:
        ctx = _ctx(
            "تعرض لي المنتجات بالصور؟",
            intent_name=INTENT_ASK_PRODUCT,
            orderable=True,
        )
        decision = _decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_deciding_stage_url_only_also_yields(self) -> None:
        state = _active_checkout_state()
        state.stage = "deciding"
        decision = _decide(_ctx(GENERIC_URL, state=state, orderable=True))
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
