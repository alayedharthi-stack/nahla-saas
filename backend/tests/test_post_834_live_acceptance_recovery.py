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


def _ctx(*, intent_name: str, message: str = "مرحبا"):
    return SimpleNamespace(
        intent=SimpleNamespace(name=intent_name),
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


class TestKnowledgeCapability:
    def test_store_story_args_are_structured_not_raw_text(self) -> None:
        facts = SimpleNamespace(store_story_status="KNOWN_PRESENT")
        args = store_story_capability_args(facts)
        assert knowledge_kind_from_args(args) == "store_story"
        assert args["policy_surface"] == "merchant_knowledge_section"
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

    def test_quality_guard_does_not_ask_customer_for_merchant_code(self) -> None:
        result = apply_commerce_reply_quality_guard(
            reply="",
            inbound_text="ابي كوبون خصم",
            intent_name="general",
        )
        assert "أرسل لي كود الخصم اللي عندك" not in (result.reply or "")


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
