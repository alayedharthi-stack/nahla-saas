"""Phase 2.5 — active catalog checkout continuity after catalog_order turn."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: E402
    is_active_catalog_checkout,
    maybe_enforce_catalog_order_continue_checkout,
    try_active_catalog_checkout_continue_decision,
)
from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    build_commerce_turn_contract,
    maybe_enforce_commerce_turn_contract_decision,
)
from modules.ai.brain.commerce.current_order_amount import (  # noqa: E402
    is_current_order_inquiry,
    should_route_current_order_inquiry_over_tracking,
)
from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _catalog_prep(**extra: Any) -> OrderPreparationState:
    base = dict(
        catalog_line_items_authoritative=True,
        catalog_checkout_total=319.0,
        catalog_checkout_currency="SAR",
        missing_fields=["city", "delivery_address"],
        order_status="awaiting_address",
        line_items=[
            {
                "product_retailer_id": "86bqzca62a",
                "product_name": "1 كيلو عسل سمر",
                "quantity": 2,
                "unit_price": 159.5,
                "currency": "SAR",
                "from_native_catalog_order": True,
                "source": "whatsapp_native_catalog_order",
            },
        ],
    )
    base.update(extra)
    return OrderPreparationState(**base)


def _followup_ctx(
    message: str,
    *,
    intent_name: str = "general",
    prep: OrderPreparationState | None = None,
) -> BrainContext:
    prep = prep or _catalog_prep()
    state = MerchantConversationState(stage="ordering", turn=5, greeted=True)
    state.order_prep = prep
    return BrainContext(
        tenant_id=33,
        customer_phone="966542980511",
        message=message,
        intent=Intent(name=intent_name, confidence=0.85, raw_message=message),
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True),
        profile={"inbound_metadata": {}},
    )


class TestActiveCatalogCheckoutDetection:
    def test_followup_turn_is_active_catalog_checkout(self) -> None:
        ctx = _followup_ctx("وش هو طلبي؟")
        assert is_active_catalog_checkout(ctx)
        assert is_current_order_inquiry("وش هو طلبي؟")

    def test_browse_without_checkout_is_not_active(self) -> None:
        ctx = BrainContext(
            tenant_id=33,
            customer_phone="966542980511",
            message="وش عندكم",
            intent=Intent(name="general", confidence=0.5, raw_message="وش عندكم"),
            state=MerchantConversationState(),
            facts=CommerceFacts(has_products=True),
            profile={},
        )
        assert not is_active_catalog_checkout(ctx)


class TestWhatIsMyOrderInquiry:
    def test_inquiry_routes_over_tracking_not_no_orders_template(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _followup_ctx("وش هو طلبي؟")
        assert should_route_current_order_inquiry_over_tracking(
            ctx.message,
            state=ctx.state,
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_TRACK_ORDER
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "current_order_inquiry"
        assert T.no_orders() not in str(decision.reason)

    def test_contract_summarize_goal_on_inquiry(self) -> None:
        contract = build_commerce_turn_contract(_followup_ctx("وش هو طلبي؟"), db=None)
        assert contract.known_facts.get("active_catalog_checkout") is True
        assert contract.next_goal == "summarize_active_draft_order"
        assert "do_not_browse" in contract.forbidden_actions


class TestSameOrderConfirmation:
    def test_same_order_stays_in_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _followup_ctx("نفس الطلب")
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args.get("continue_checkout") is True
        assert decision.args.get("skip_product_discovery") is True

    def test_contract_next_goal_continue_on_same_order(self) -> None:
        contract = build_commerce_turn_contract(_followup_ctx("نفس الطلب"), db=None)
        assert contract.next_goal == "continue_checkout"
        assert "product" not in contract.missing_fields


class TestAddressOnFileClaim:
    def test_address_claim_does_not_open_browse(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _followup_ctx("المدينة والعنوان عندكم مسجل")
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_contract_confirm_address_goal(self) -> None:
        contract = build_commerce_turn_contract(
            _followup_ctx("المدينة والعنوان عندكم مسجل"),
            db=None,
        )
        assert contract.next_goal == "confirm_known_address"
        assert contract.known_facts.get("line_items_known") is True


class TestActiveCheckoutContractEnforce:
    def test_enforce_blocks_browse_without_current_turn_catalog_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _followup_ctx("المدينة والعنوان عندكم مسجل")
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(action=ACTION_SEARCH_PRODUCTS, args={"source": "top_products"})
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_PROPOSE_DRAFT_ORDER
        assert contract.known_facts.get("catalog_order_current_turn") is not True
        assert contract.known_facts.get("active_catalog_checkout") is True

    def test_unrelated_browse_followup_is_not_forced_into_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _followup_ctx("وش المتوفر")
        enforced = maybe_enforce_catalog_order_continue_checkout(
            ctx,
            Decision(action=ACTION_SEARCH_PRODUCTS, args={}),
        )
        assert enforced.action == ACTION_SEARCH_PRODUCTS
        assert try_active_catalog_checkout_continue_decision(ctx) is None

    def test_normal_browse_unaffected_without_active_checkout(self) -> None:
        ctx = BrainContext(
            tenant_id=33,
            customer_phone="966542980511",
            message="وش عندكم منتجات",
            intent=Intent(name="general", confidence=0.5, raw_message="وش عندكم منتجات"),
            state=MerchantConversationState(),
            facts=CommerceFacts(has_products=True),
            profile={},
        )
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(action=ACTION_SEARCH_PRODUCTS, args={"query": "عسل"})
        assert maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw) is raw
        assert try_active_catalog_checkout_continue_decision(ctx) is None
