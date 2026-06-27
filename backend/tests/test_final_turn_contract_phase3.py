"""Phase 3.1 — FinalTurnContract shadow + audit violations."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.turn.final_turn_audit import (  # noqa: E402
    audit_final_turn_reply,
    detect_final_turn_violations,
)
from modules.ai.brain.turn.final_turn_contract import (  # noqa: E402
    FinalTurnContract,
    build_final_turn_contract,
)
from modules.ai.brain.turn.flags import (  # noqa: E402
    is_final_turn_contract_enforce_enabled,
    is_final_turn_contract_shadow_enabled,
)
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _minimal_contract(**overrides) -> FinalTurnContract:
    base = dict(
        response_purpose="general",
        turn_owner="persona/social",
        decision_action=ACTION_LLM_REPLY,
        decision_topic="",
        forbidden_question_types=[],
        promises_forbidden=[],
        browse_allowed=False,
        inbound_text="",
        known_facts={},
    )
    base.update(overrides)
    return FinalTurnContract(**base)


def _ctx(
    message: str,
    *,
    phone: str = "966500000000",
    profile: dict | None = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=33,
        customer_phone=phone,
        message=message,
        raw_message=message,
        intent=Intent(name="general", confidence=0.8, raw_message=message),
        state=MerchantConversationState(stage="discovery", greeted=True),
        facts=CommerceFacts(has_products=True, orderable=True),
        profile=profile or {},
    )


class TestFinalTurnContractFlags:
    def test_shadow_enabled_by_default(self) -> None:
        prev = os.environ.pop("FINAL_TURN_CONTRACT_SHADOW_ENABLED", None)
        try:
            assert is_final_turn_contract_shadow_enabled() is True
        finally:
            if prev is not None:
                os.environ["FINAL_TURN_CONTRACT_SHADOW_ENABLED"] = prev

    def test_enforce_disabled_by_default(self) -> None:
        prev = os.environ.pop("FINAL_TURN_CONTRACT_ENFORCE_ENABLED", None)
        try:
            assert is_final_turn_contract_enforce_enabled() is False
        finally:
            if prev is not None:
                os.environ["FINAL_TURN_CONTRACT_ENFORCE_ENABLED"] = prev


class TestT1CatalogPromiseWithoutAction:
    def test_detects_promise_without_action(self) -> None:
        contract = _minimal_contract(
            decision_action=ACTION_LLM_REPLY,
            browse_allowed=False,
            promises_forbidden=["catalog_promise"],
        )
        violations = detect_final_turn_violations(
            contract,
            "أرسل لك من الكتالوج",
        )
        assert "promise_without_action" in violations
        assert "catalog_promise_without_catalog_action" in violations


class TestT2CourierLogistics:
    COURIER = "معك مندوب سمسا"
    BAD_REPLY = "متوفر معك مندوب سمسا بعدة خيارات. وش خيار تبيه؟"

    def test_contract_forbids_product_questions(self) -> None:
        ctx = _ctx(self.COURIER)
        decision = MagicMock()
        decision.action = ACTION_LLM_REPLY
        decision.args = {"topic": "general"}
        result = ActionResult(success=True, data={})
        contract = build_final_turn_contract(ctx, decision, result)
        assert "product" in contract.forbidden_question_types
        assert "variant" in contract.forbidden_question_types
        assert "catalog_promise" in contract.forbidden_question_types

    def test_bad_reply_triggers_shadow_violations(self) -> None:
        contract = _minimal_contract(
            inbound_text=self.COURIER,
            forbidden_question_types=["product", "variant", "catalog_promise", "availability"],
            promises_forbidden=["catalog_promise", "product_availability"],
        )
        violations = detect_final_turn_violations(contract, self.BAD_REPLY)
        assert "forbidden_variant_followup" in violations
        assert "unsafe_product_availability_claim" in violations


class TestT3KnownName:
    CUSTOMER_NAME = "سلطان القرني"

    def test_contract_forbids_name_question(self) -> None:
        ctx = _ctx(
            "وش المدينة؟",
            profile={"name": self.CUSTOMER_NAME, "customer_name": self.CUSTOMER_NAME},
        )
        ctx.commerce_turn_contract = SimpleNamespace(
            known_facts={"customer_name_known": True, "customer_name": self.CUSTOMER_NAME},
            allowed_actions=[],
            forbidden_actions=[],
            missing_fields=["city"],
            next_goal="collect_missing_city",
            action_to_execute=None,
        )
        decision = MagicMock()
        decision.action = ACTION_PROPOSE_DRAFT_ORDER
        decision.args = {"topic": "checkout"}
        result = ActionResult(success=True, data={})
        contract = build_final_turn_contract(ctx, decision, result)
        assert "name" in contract.forbidden_question_types

    def test_reply_audit_detects_name_question(self) -> None:
        contract = _minimal_contract(
            forbidden_question_types=["name"],
            known_facts={"customer_name_known": True, "customer_name": self.CUSTOMER_NAME},
        )
        violations = detect_final_turn_violations(contract, "ممكن تذكر اسمك الكامل؟")
        assert "forbidden_name_question" in violations


class TestT4KnownPhone:
    def test_contract_forbids_phone_question(self) -> None:
        ctx = _ctx("وش المدينة؟", phone="966542980511")
        ctx.commerce_turn_contract = SimpleNamespace(
            known_facts={"phone_known": True},
            allowed_actions=[],
            forbidden_actions=[],
            missing_fields=["city"],
            next_goal="collect_missing_city",
            action_to_execute=None,
        )
        decision = MagicMock()
        decision.action = ACTION_PROPOSE_DRAFT_ORDER
        decision.args = {}
        result = ActionResult(success=True, data={})
        contract = build_final_turn_contract(ctx, decision, result)
        assert "phone" in contract.forbidden_question_types

    def test_reply_audit_detects_phone_question(self) -> None:
        contract = _minimal_contract(
            forbidden_question_types=["phone"],
            known_facts={"phone_known": True, "customer_phone": "966542980511"},
        )
        violations = detect_final_turn_violations(
            contract,
            "ممكن ترسل لي رقم جوالك؟",
        )
        assert "forbidden_phone_question" in violations


class TestT5ShippingPostOrder:
    SHIPPING_MSG = "اي فرع ارسلتو طلبي في سمسا"

    def test_contract_shipping_purpose(self) -> None:
        ctx = _ctx(self.SHIPPING_MSG)
        decision = MagicMock()
        decision.action = ACTION_LLM_REPLY
        decision.args = {"topic": "shipping_post_order"}
        result = ActionResult(success=True, data={})
        contract = build_final_turn_contract(ctx, decision, result)
        assert contract.response_purpose == "shipping_post_order"
        assert "product" in contract.forbidden_question_types
        assert "browse" in contract.forbidden_question_types
        assert contract.browse_allowed is False

    def test_product_availability_in_shipping_is_violation(self) -> None:
        contract = _minimal_contract(
            response_purpose="shipping_post_order",
            inbound_text=self.SHIPPING_MSG,
            forbidden_question_types=["product", "browse", "availability"],
        )
        violations = detect_final_turn_violations(
            contract,
            "متوفر عسل سدر بعدة خيارات. وش تبيه؟",
        )
        assert "shipping_context_shifted_to_product" in violations


class TestT6ExplicitBrowse:
    BROWSE_MSG = "وش الأنواع المتوفرة؟"

    def test_browse_allowed_on_search_action(self) -> None:
        ctx = _ctx(self.BROWSE_MSG)
        ctx.conversation_turn_ownership = SimpleNamespace(
            turn_owner="discovery",
            explicit_browse_intent=True,
            forbidden_fallbacks=frozenset(),
        )
        decision = MagicMock()
        decision.action = ACTION_SEARCH_PRODUCTS
        decision.args = {"topic": "discovery"}
        result = ActionResult(success=True, data={"products": [{"title": "عسل سدر"}]})
        contract = build_final_turn_contract(ctx, decision, result)
        assert contract.browse_allowed is True

    def test_catalog_promise_without_action_still_violates(self) -> None:
        contract = _minimal_contract(
            inbound_text=self.BROWSE_MSG,
            decision_action=ACTION_LLM_REPLY,
            browse_allowed=True,
            promises_forbidden=["catalog_promise"],
        )
        violations = detect_final_turn_violations(
            contract,
            "أرسل لك من الكتالوج الآن",
        )
        assert "promise_without_action" in violations
        assert "catalog_promise_without_catalog_action" in violations

    def test_search_action_with_products_not_violation(self) -> None:
        contract = _minimal_contract(
            inbound_text=self.BROWSE_MSG,
            decision_action=ACTION_SEARCH_PRODUCTS,
            browse_allowed=True,
        )
        violations = detect_final_turn_violations(
            contract,
            "أرسل لك من الكتالوج",
            result_data={"products": [{"title": "عسل"}]},
        )
        assert "catalog_promise_without_catalog_action" not in violations


class TestShadowDoesNotMutateReply:
    def test_audit_returns_violations_without_changing_reply(self) -> None:
        contract = _minimal_contract(
            promises_forbidden=["catalog_promise"],
            browse_allowed=False,
        )
        original = "أرسل لك من الكتالوج"
        result = audit_final_turn_reply(
            contract,
            original,
            phase="post_compose",
            tenant_id=33,
        )
        assert result.has_violations
        assert "promise_without_action" in result.violations
        # reply is never returned or modified by audit
        assert original == "أرسل لك من الكتالوج"

    def test_shipping_decision_engine_topic(self) -> None:
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY
        from modules.ai.brain.types import INTENT_ASK_SHIPPING

        op = OrderPreparationState()
        op.payment_receipt_received = True
        state = MerchantConversationState()
        state.order_prep = op
        message = "اي فرع ارسلتو طلبي في سمسا"
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="+966500000099",
            message=message,
            intent=Intent(
                name=INTENT_ASK_SHIPPING,
                confidence=0.90,
                slots={},
                raw_message=message,
                extraction_method="rules",
            ),
            state=state,
            facts=CommerceFacts(),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "shipping_post_order"
        contract = build_final_turn_contract(
            ctx,
            decision,
            ActionResult(success=True, data={}),
        )
        assert contract.response_purpose == "shipping_post_order"
        assert "identity_collaboration" not in contract.response_purpose
