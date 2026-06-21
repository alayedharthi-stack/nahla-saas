"""P0 — conversation state isolation (Tenant 33 production regressions)."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_conversation_guard import (  # noqa: E402
    is_delivery_social_thanks,
    is_social_ack_message,
)
from modules.ai.brain.commerce.conversation_state_isolation import (  # noqa: E402
    inbound_breaks_fulfillment_ownership,
    should_replay_pending_question,
)
from modules.ai.brain.commerce.post_purchase_feedback_guard import (  # noqa: E402
    quality_feedback_routing_eligible,
    try_post_purchase_feedback_decision,
)
from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: E402
    MSG_NAME_NOT_CONFIGURED,
    StaffContactRegistry,
    classify_staff_contact_request,
    resolve_staff_contact,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.postprocess.conversation_recovery import (  # noqa: E402
    try_guard_recovery_reply,
)
from modules.ai.brain.postprocess.staff_escalation_truth_guard import (  # noqa: E402
    apply_staff_escalation_truth_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

_UAE_DISCOUNT_MSG = "\u0623\u062e\u0648\u064a \u062a\u0631\u0643\u064a \u0645\u0627 \u0639\u0646\u062f\u0643\u0645 \u0643\u0648\u062f \u062e\u0635\u0645"
_QUALITY_FEEDBACK_MSG = (
    "\u0627\u0644\u0639\u0633\u0644 \u062e\u0641\u064a\u0641 \u0648\u0645\u0648 \u0645\u062b\u0644 \u0623\u0648\u0644"
)
_BARE_LIGHTNESS_MSG = "\u0639\u0633\u0644 \u062e\u0641\u064a\u0641"


def _ctx(
    msg: str,
    *,
    state: MerchantConversationState | None = None,
    history: list | None = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=33,
        customer_phone="+971506669883",
        message=msg,
        intent=Intent(name="general", confidence=0.5, raw_message=msg),
        state=state or MerchantConversationState(greeted=True, stage="discovery"),
        facts=CommerceFacts(
            has_products=True,
            product_count=5,
            orderable=True,
            store_name="test",
        ),
        history=history or [],
    )


class TestStaleCheckoutReplayBlocked:
    _STALE_LAST_NAME_Q = (
        "And your last name (as it should appear on the delivery)?"
    )

    def test_uae_discount_does_not_replay_last_name_question(self) -> None:
        assert inbound_breaks_fulfillment_ownership(_UAE_DISCOUNT_MSG)
        assert not should_replay_pending_question(
            inbound_text=_UAE_DISCOUNT_MSG,
            last_question=self._STALE_LAST_NAME_Q,
        )
        state = MerchantConversationState(
            greeted=True,
            stage="ordering",
            last_question_asked=self._STALE_LAST_NAME_Q,
        )
        rec = try_guard_recovery_reply(
            inbound_text=_UAE_DISCOUNT_MSG,
            state=state,
        )
        assert "last name" not in (rec.reply or "").lower()
        assert rec.source != "last_question_clarify"

    def test_short_name_answer_may_replay_checkout_question(self) -> None:
        assert should_replay_pending_question(
            inbound_text="\u0623\u062d\u0645\u062f",
            last_question=self._STALE_LAST_NAME_Q,
        )


class TestQualityFeedbackBeatsFulfillment:
    def test_comparison_feedback_routes_without_external_outbound(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage="ordering",
            commerce_objective="ordering",
        )
        state.order_prep = OrderPreparationState.from_dict({
            "order_status": "awaiting_address",
            "missing_fields": ["city"],
            "product_id": "honey-1",
        })
        ctx = _ctx(_QUALITY_FEEDBACK_MSG, state=state)
        assert quality_feedback_routing_eligible(ctx, _QUALITY_FEEDBACK_MSG)
        dec = try_post_purchase_feedback_decision(ctx)
        assert dec is not None
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "support_product_feedback"

    def test_engine_routes_feedback_not_city(self) -> None:
        state = MerchantConversationState(greeted=True, stage="ordering")
        state.order_prep = OrderPreparationState.from_dict({
            "missing_fields": ["city"],
            "product_id": "honey-1",
        })
        ctx = _ctx(_QUALITY_FEEDBACK_MSG, state=state)
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "support_product_feedback"

    def test_bare_lightness_during_ordering_not_feedback_route(self) -> None:
        state = MerchantConversationState(greeted=True, stage="ordering")
        ctx = _ctx(_BARE_LIGHTNESS_MSG, state=state, history=[])
        assert try_post_purchase_feedback_decision(ctx) is None


class TestDeliverySocialThanks:
    @pytest.mark.parametrize(
        "msg",
        (
            "\u0648\u0635\u0644 \u0648\u0627\u0644\u0644\u0647 \u064a\u0628\u064a\u0636 \u0648\u062c\u0647\u0643",
            "\u0648\u0635\u0644\u062a \u0648\u0627\u0644\u0644\u0647 \u064a\u0628\u0627\u0631\u0643 \u0641\u064a\u0643",
        ),
    )
    def test_delivery_social_thanks_detected(self, msg: str) -> None:
        assert is_delivery_social_thanks(msg)
        assert is_social_ack_message(msg)

    def test_truth_guard_never_returns_empty_for_delivery_thanks(self) -> None:
        result = apply_staff_escalation_truth_guard(
            reply="\u062a\u0648\u0627\u0635\u0644 \u0645\u0639 \u0623\u0645\u064a\u0646 \u0639\u0644\u0649 \u0627\u0644\u0631\u0642\u0645 \u0627\u0644\u062a\u0627\u0644\u064a",
            inbound_text="\u0648\u0635\u0644 \u0648\u0627\u0644\u0644\u0647 \u064a\u0628\u064a\u0636 \u0648\u062c\u0647\u0643",
            tenant_id=33,
        )
        assert (result.reply or "").strip()


class TestExplicitAmeenStaffResolution:
    def _registry_with_showroom(self) -> StaffContactRegistry:
        from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: E402
            StaffContactRecord,
        )

        return StaffContactRegistry(
            records=(
                StaffContactRecord(
                    lookup_name="\u0623\u0645\u064a\u0646",
                    phone="966541690226",
                    section_id=5,
                    role="showroom",
                    aliases=("\u0627\u0645\u064a\u0646", "\u0623\u0645\u064a\u0646"),
                    is_owner=False,
                    chain_index=0,
                    source="test",
                ),
            ),
        )

    def test_send_ameen_number_is_generic_staff_not_unknown_name(self) -> None:
        msg = "\u0627\u0631\u0633\u0644 \u0644\u064a \u0631\u0642\u0645 \u0623\u0645\u064a\u0646"
        req = classify_staff_contact_request(msg, registry=self._registry_with_showroom())
        assert req.kind == "generic_staff"
        res = resolve_staff_contact(
            self._registry_with_showroom(),
            req,
            message=msg,
        )
        assert res.found is True
        assert res.reason in {
            "general_staff",
            "explicit_ameen_showroom_fallback",
            "named_match",
        }
        assert res.reason != "name_not_configured"
