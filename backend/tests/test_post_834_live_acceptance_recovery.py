"""Post-#834 live acceptance recovery — semantic/state fixtures, not phrase tests."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
for _p in [_BACKEND, os.path.join(_BACKEND, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.automation_send_guard import (  # noqa: E402
    REASON_AI_DISABLED,
    REASON_HUMAN_TAKEOVER,
    should_block_automation_for_conversation,
)
from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: E402
    merchant_customer_record_facts,
)
from modules.ai.brain.commerce.inbound_fragment_guard import (  # noqa: E402
    evaluate_duplicate_fragment_turn,
    reset_fragment_cache_for_tests,
    should_block_catalog_grounding_fallback,
)
from modules.ai.brain.commerce.merchant_knowledge_fact_scope import (  # noqa: E402
    knowledge_kind_from_args,
    should_request_store_story_knowledge,
    store_story_capability_args,
)
from modules.ai.brain.commerce.promotion_truth import (  # noqa: E402
    NO_VALID_PROMOTIONS,
    PROMOTION_QUERY_FAILED,
    coupon_policy_for_compose,
)
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (  # noqa: E402
    apply_commerce_reply_quality_guard,
)
from modules.ai.brain.state.state_relevance import (  # noqa: E402
    current_intent_outranks_ordering_safety_net,
)
from services.merchant_document_retrieval import (  # noqa: E402
    retrieve_merchant_documents,
)


def _convo(**kwargs):
    defaults = dict(
        id=26,
        tenant_id=1,
        customer_id=7,
        ai_paused=False,
        ai_paused_reason=None,
        is_human_handoff=False,
        needs_human=False,
        handoff_active=False,
        paused_by_human=False,
        taken_over_at=None,
        taken_over_by=None,
        status="active",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _ctx(*, intent_name: str, message: str = "مرحبا", slots: dict | None = None):
    return SimpleNamespace(
        intent=SimpleNamespace(name=intent_name, slots=dict(slots or {})),
        message=message,
        semantic_interpretation=None,
        state=SimpleNamespace(
            pending_action="collect_checkout_details",
            stage="ordering",
            order_prep=None,
            current_product_focus={"title": "قميص قطني أزرق"},
        ),
    )


class TestSendGuardAdvisoryFlags:
    def test_needs_human_does_not_block_outbound(self) -> None:
        decision = should_block_automation_for_conversation(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            conversation=_convo(needs_human=True),
        )
        assert decision.block is False

    def test_genuine_pause_still_blocks(self) -> None:
        decision = should_block_automation_for_conversation(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            conversation=_convo(ai_paused=True, ai_paused_reason="manual_pause"),
        )
        assert decision.block is True
        assert decision.reason == REASON_AI_DISABLED

    def test_takeover_stamp_still_blocks(self) -> None:
        from datetime import datetime, timezone

        decision = should_block_automation_for_conversation(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            conversation=_convo(taken_over_at=datetime.now(timezone.utc)),
        )
        assert decision.block is True
        assert decision.reason == REASON_HUMAN_TAKEOVER

    def test_status_human_alone_does_not_block(self) -> None:
        from core.ownership_state import (
            OWNERSHIP_HUMAN_REQUESTED,
            conversation_handoff_active,
            resolve_ownership_state,
        )

        decision = should_block_automation_for_conversation(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            conversation=_convo(status="human"),
        )
        assert decision.block is False
        ownership = resolve_ownership_state(MagicMock(), _convo(status="human"))
        assert ownership.state == OWNERSHIP_HUMAN_REQUESTED
        assert conversation_handoff_active(MagicMock(), _convo(status="human")) is False

    def test_advisory_status_does_not_fail_closed_on_gate_error(self) -> None:
        from core.handoff_truth import has_possible_human_ownership_signals

        convo = _convo(status="human", needs_human=True, handoff_active=True)
        assert has_possible_human_ownership_signals(convo) is False

    def test_genuine_takeover_still_fail_closes_on_gate_error(self) -> None:
        from datetime import datetime, timezone

        from core.handoff_truth import has_possible_human_ownership_signals

        convo = _convo(taken_over_at=datetime.now(timezone.utc))
        assert has_possible_human_ownership_signals(convo) is True


class TestKnowledgeCapability:
    def test_store_story_args_are_structured_not_raw_text(self) -> None:
        facts = SimpleNamespace(store_story_status="KNOWN_PRESENT")
        args = store_story_capability_args(facts)
        assert knowledge_kind_from_args(args) == "store_story"
        assert args["policy_surface"] == "merchant_knowledge_section"
        assert "response_goal" not in args
        assert should_request_store_story_knowledge(
            intent_name="ask_store_info", facts=facts,
        ) is True
        assert should_request_store_story_knowledge(
            intent_name="general", facts=facts,
        ) is False
        assert should_request_store_story_knowledge(
            intent_name="start_order", facts=facts,
        ) is False

    def test_raw_customer_text_does_not_retrieve_without_structured_kind(self) -> None:
        result = retrieve_merchant_documents(
            MagicMock(), 1, "طرق الإنتاج عند التاجر",
        )
        assert result.knowledge_query_run is False
        assert tuple(result.sections) == ()

    def test_store_story_goal_outranks_stale_checkout_next_goal(self) -> None:
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
        from modules.ai.brain.pipeline import _compose_base_response_goal
        from modules.ai.brain.types import Decision

        decision = Decision(
            action=ACTION_LLM_REPLY,
            args=store_story_capability_args(
                SimpleNamespace(store_story_status="KNOWN_PRESENT"),
            ),
            reason="test",
        )
        suggestion = SimpleNamespace(
            suggested_next_step="",
            discount_ok_now=False,
            coupon_logic_considered=False,
        )
        goal = _compose_base_response_goal(
            decision,
            suggestion,
            checkout_facts={"next_goal": "collect_city_only"},
        )
        assert "collect_city_only" not in goal
        assert "answer from retrieved tenant knowledge" not in goal

    def test_disabled_story_is_not_requested(self) -> None:
        facts = SimpleNamespace(store_story_status="UNKNOWN")
        assert should_request_store_story_knowledge(
            intent_name="general", facts=facts,
        ) is False


class TestPromotionsContract:
    def test_shareable_native_codes_do_not_require_customer_supplied_code(self) -> None:
        facts = SimpleNamespace(
            has_coupons=True,
            coupon_eligibility="",
            shareable_promotions=[{"code": "SAVE6", "source_type": "manual"}],
            shareable_offers=[],
            promotion_query_outcome="ok",
            promotion_query_failed=False,
            promotion_coupon_source="ok",
            promotion_offer_source="ok",
            promotion_generation_rule_source="ok",
            generation_rules_state="absent",
        )
        policy = coupon_policy_for_compose(facts)
        assert policy["has_coupons"] is True
        assert policy["customer_must_supply_code"] is False
        assert policy["eligibility_guaranteed"] is False
        assert policy["generation_authorized"] is False
        assert policy["invented_codes"] is False

    def test_query_failure_is_not_healthy_empty(self) -> None:
        facts = SimpleNamespace(
            has_coupons=False,
            coupon_eligibility="",
            shareable_promotions=[],
            shareable_offers=[],
            promotion_query_outcome=PROMOTION_QUERY_FAILED,
            promotion_query_failed=True,
            promotion_coupon_source="failed",
            promotion_offer_source="ok",
            promotion_generation_rule_source="ok",
            generation_rules_state="absent",
        )
        policy = coupon_policy_for_compose(facts)
        assert policy["query_failed"] is True
        assert policy["no_valid_promotions"] is False

    def test_healthy_empty_does_not_invent_codes(self) -> None:
        facts = SimpleNamespace(
            has_coupons=False,
            coupon_eligibility="",
            shareable_promotions=[],
            shareable_offers=[],
            promotion_query_outcome=NO_VALID_PROMOTIONS,
            promotion_query_failed=False,
            promotion_coupon_source="ok",
            promotion_offer_source="ok",
            promotion_generation_rule_source="ok",
            generation_rules_state="absent",
        )
        policy = coupon_policy_for_compose(facts)
        assert policy["no_valid_promotions"] is True
        assert policy["shareable_promotions"] == []
        assert policy["invented_codes"] is False
        assert policy["generation_authorized"] is False

    def test_duplicate_coupon_request_still_reaches_brain(self) -> None:
        reset_fragment_cache_for_tests()
        first = evaluate_duplicate_fragment_turn(
            tenant_id=1, customer_phone="966500000001", text="ابي كوبون خصم",
        )
        second = evaluate_duplicate_fragment_turn(
            tenant_id=1, customer_phone="966500000001", text="ابي كوبون خصم",
        )
        assert first.process_turn is True
        assert second.process_turn is True
        reset_fragment_cache_for_tests()

    def test_catalog_miss_blocks_on_promotion_facts_not_raw_text(self) -> None:
        facts = SimpleNamespace(
            has_coupons=True,
            shareable_promotions=[{"code": "SAVE6", "source_type": "manual"}],
        )
        blocked_unrelated, reason_unrelated = should_block_catalog_grounding_fallback(
            inbound_text="قميص قطني أزرق",
            intent=SimpleNamespace(name="general"),
            facts=facts,
        )
        assert blocked_unrelated is False
        assert reason_unrelated != "promotion_facts_present"
        blocked, reason = should_block_catalog_grounding_fallback(
            inbound_text="قميص قطني أزرق",
            decision_topic="promotion_inquiry",
            facts=facts,
        )
        assert blocked is True
        assert reason == "promotion_facts_present"
        browse_blocked, browse_reason = should_block_catalog_grounding_fallback(
            inbound_text="وش عندكم من العسل؟",
            decision_topic="promotion_inquiry",
            facts=facts,
        )
        assert browse_blocked is False
        assert browse_reason == ""

    def test_quality_guard_does_not_ask_customer_for_merchant_code(self) -> None:
        result = apply_commerce_reply_quality_guard(
            reply="",
            inbound_text="ابي كوبون خصم",
            intent_name="general",
        )
        assert "أرسل لي كود الخصم اللي عندك" not in (result.reply or "")

    def test_quality_guard_coupon_wording_without_facts_is_not_containment(self) -> None:
        blocked, reason = should_block_catalog_grounding_fallback(
            inbound_text="ابي كوبون خصم",
        )
        assert blocked is False
        assert reason != "discount_coupon_inquiry"
        result = apply_commerce_reply_quality_guard(
            reply="",
            inbound_text="ابي كوبون خصم",
            intent_name="general",
        )
        assert "catalog_containment_discount_coupon_inquiry" not in (
            result.fallback_kind or ""
        )

    def test_quality_guard_uses_structured_promotion_facts(self) -> None:
        facts = SimpleNamespace(
            has_coupons=True,
            shareable_promotions=[{"code": "SAVE6", "source_type": "manual"}],
        )
        blocked, reason = should_block_catalog_grounding_fallback(
            inbound_text="قميص قطني أزرق",
            decision_topic="promotion_inquiry",
            facts=facts,
        )
        assert blocked is True
        assert reason == "promotion_facts_present"
        result = apply_commerce_reply_quality_guard(
            reply="",
            inbound_text="قميص قطني أزرق",
            intent_name="general",
            decision_topic="promotion_inquiry",
            facts=facts,
        )
        assert "أرسل لي كود الخصم اللي عندك" not in (result.reply or "")
        assert "discount_coupon_inquiry" not in (result.fallback_kind or "")


class TestCustomerHistoryFacts:
    def test_registered_record_does_not_imply_historical_orders(self) -> None:
        identity = SimpleNamespace(
            customer_name_known=True,
            known_facts={
                "customer_id": 8209,
                "customer_name": "أحمد سالم",
                "customer_name_source": "profile",
            },
            prep_patch={},
        )
        facts = merchant_customer_record_facts(identity)
        record = facts["merchant_customer_record"]
        assert record["registered"] is True
        assert record["has_historical_orders"] is False
        assert record["historical_order_details_available"] is False
        assert record["personal_familiarity"] is False


class TestCurrentIntentOutranksStaleCommerce:
    def test_location_intent_outranks_ordering_pending(self) -> None:
        assert current_intent_outranks_ordering_safety_net(
            _ctx(intent_name="ask_location", message="او موقعكم"),
        ) is True

    def test_store_info_outranks_ordering_pending(self) -> None:
        assert current_intent_outranks_ordering_safety_net(
            _ctx(intent_name="ask_store_info", message="رابط المتجر"),
        ) is True

    def test_general_free_text_outranks_stale_product_prep(self) -> None:
        assert current_intent_outranks_ordering_safety_net(
            _ctx(intent_name="general", message="المتجر"),
        ) is True

    def test_general_checkout_name_slot_does_not_yield_funnel(self) -> None:
        assert current_intent_outranks_ordering_safety_net(
            _ctx(
                intent_name="general",
                message="أحمد سالم",
                slots={"customer_name": "أحمد سالم"},
            ),
        ) is False

    def test_general_without_slots_or_awaited_prep_yields(self) -> None:
        assert current_intent_outranks_ordering_safety_net(
            _ctx(intent_name="general", message="أحمد سالم"),
        ) is True

    def test_general_awaited_checkout_slot_keeps_funnel(self) -> None:
        ctx = _ctx(intent_name="general", message="الرياض")
        ctx.state.order_prep = {"missing_fields": ["city"]}
        assert current_intent_outranks_ordering_safety_net(ctx) is False

    def test_start_order_does_not_yield_ordering_funnel(self) -> None:
        assert current_intent_outranks_ordering_safety_net(
            _ctx(intent_name="start_order", message="ابي اشتري"),
        ) is False

    def test_escalation_intent_outranks_ordering(self) -> None:
        assert current_intent_outranks_ordering_safety_net(
            _ctx(intent_name="talk_to_human", message="وصلت ما احد يرد"),
        ) is True
        assert current_intent_outranks_ordering_safety_net(
            _ctx(intent_name="employee_not_responding", message="مالقيت احد"),
        ) is True


class TestNoRawTextKnowledgeRouter:
    def test_customer_retrieval_ignores_message_body(self) -> None:
        db = MagicMock()
        with patch(
            "services.merchant_document_retrieval.detect_document_retrieval_intent",
        ) as detect:
            result = retrieve_merchant_documents(
                db, 7, "قصة المتجر وكيف بدأ", structured_kind=None,
            )
            detect.assert_not_called()
        assert result.knowledge_query_run is False
        assert result.tenant_id == 7


class TestStructuredOverlayVsLongForm:
    def test_custom_short_facts_are_overlay_eligible_not_document_kinds(self) -> None:
        from core.knowledge import is_long_form_document_section
        from services.knowledge_section_kinds import is_behavioral_kind
        from services.merchant_document_retrieval import (
            DOCUMENT_KINDS,
            is_long_form_document_kind,
        )

        assert "custom" not in DOCUMENT_KINDS
        assert is_long_form_document_kind("custom") is False
        assert is_behavioral_kind("custom") is False
        row = SimpleNamespace(kind="custom")
        assert is_long_form_document_section(row) is False

    def test_store_story_stays_retrieval_only(self) -> None:
        from services.merchant_document_retrieval import (
            DOCUMENT_KINDS,
            is_long_form_document_kind,
        )

        assert "store_story" in DOCUMENT_KINDS
        assert is_long_form_document_kind("store_story") is True

    def test_reply_style_is_not_forced_into_document_kinds(self) -> None:
        from services.merchant_document_retrieval import DOCUMENT_KINDS

        assert "reply_style" not in DOCUMENT_KINDS


class TestTruncationTrace:
    def test_provider_length_finish_reason_is_first_layer(self) -> None:
        from modules.ai.orchestrator.ai_usage_ledger import (
            classify_truncation_first_layer,
            extract_provider_completion_telemetry,
        )

        telemetry = extract_provider_completion_telemetry(
            reply_text="نعم، يبدو أنك عميلة سابقة لدينا. إذا كان",
            httpx_data={
                "stop_reason": "max_tokens",
                "usage": {"output_tokens": 18},
            },
        )
        assert telemetry["finish_reason"] == "max_tokens"
        assert telemetry["output_tokens"] == 18
        assert telemetry["raw_char_count"] == 40
        assert classify_truncation_first_layer(
            finish_reason=telemetry["finish_reason"],
            raw_model_text="نعم، يبدو أنك عميلة سابقة لدينا. إذا كان",
            composed_text="نعم، يبدو أنك عميلة سابقة لدينا. إذا كان",
            persisted_text="نعم، يبدو أنك عميلة سابقة لدينا. إذا كان",
            visible_text="نعم، يبدو أنك عميلة سابقة لدينا. إذا كان",
        ) == "provider"

    def test_complete_raw_then_downstream_cut_is_not_provider(self) -> None:
        from modules.ai.orchestrator.ai_usage_ledger import (
            classify_truncation_first_layer,
            extract_provider_completion_telemetry,
        )

        complete = (
            "نعم، يبدو أنك عميلة سابقة لدينا. إذا كان عندك طلب سابق "
            "أقدر أراجع تفاصيله من السجل."
        )
        telemetry = extract_provider_completion_telemetry(
            reply_text=complete,
            httpx_data={
                "stop_reason": "end_turn",
                "usage": {"output_tokens": 40},
            },
        )
        assert telemetry["finish_reason"] == "end_turn"
        truncated = "نعم، يبدو أنك عميلة سابقة لدينا. إذا كان"
        assert classify_truncation_first_layer(
            finish_reason=telemetry["finish_reason"],
            raw_model_text=complete,
            composed_text=complete,
            postprocess_text=truncated,
            persisted_text=truncated,
            visible_text=truncated,
        ) == "postprocess"

    def test_db_equals_visible_rules_out_wire_cut(self) -> None:
        from modules.ai.orchestrator.ai_usage_ledger import (
            classify_truncation_first_layer,
        )

        body = "نعم، يبدو أنك عميلة سابقة لدينا. إذا كان"
        assert classify_truncation_first_layer(
            finish_reason="end_turn",
            raw_model_text=body,
            composed_text=body,
            persisted_text=body,
            visible_text=body,
        ) == "none"
