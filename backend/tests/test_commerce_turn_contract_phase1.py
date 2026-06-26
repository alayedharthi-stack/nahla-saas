"""Phase 1 — CommerceTurnContract pre-decide shadow boundary."""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: E402
    is_current_catalog_order_submitted,
)
from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    CommerceTurnContract,
    build_commerce_turn_contract,
    log_commerce_turn_contract_divergence,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


def _catalog_meta(**overrides: Any) -> Dict[str, Any]:
    base = {
        "source_type": "catalog_order",
        "product_items": [
            {
                "product_retailer_id": "86bqzca62a",
                "quantity": 2,
                "item_price": 159.5,
                "currency": "SAR",
            },
        ],
        "product_names": ["1 كيلو عسل سمر"],
        "total_price": 319.0,
        "currency": "SAR",
        "item_count": 1,
        "total_quantity": 2,
    }
    base.update(overrides)
    return base


def _catalog_message() -> str:
    return (
        "[طلب كتالوج من العميل]\n"
        "عدد أسطر الطلب: 2\n"
        "إجمالي الكمية: 2\n"
        "الإجمالي: 319 SAR\n"
        "رمز المنتج (SKU): 86bqzca62a\n"
        "ملاحظة: العميل أرسل طلبًا من كتالوج واتساب."
    )


def _catalog_ctx() -> BrainContext:
    msg = _catalog_message()
    return BrainContext(
        tenant_id=33,
        customer_phone="966542980511",
        message=msg,
        intent=Intent(name="start_order", confidence=0.9, raw_message=msg),
        state=MerchantConversationState(stage="ordering", turn=2),
        facts=CommerceFacts(has_products=True, orderable=True),
        profile={"inbound_metadata": _catalog_meta()},
    )


class TestCommerceTurnContractCatalogOrder:
    def test_catalog_order_creates_contract(self) -> None:
        ctx = _catalog_ctx()
        assert is_current_catalog_order_submitted(ctx)

        contract = build_commerce_turn_contract(ctx, db=None)

        assert contract.commerce_state == "whatsapp_quick_order"
        assert contract.next_goal == "continue_checkout_from_catalog_order"
        assert contract.known_facts.get("catalog_order_current_turn") is True
        assert contract.known_facts.get("line_items_known") is True
        assert contract.known_facts.get("quantity_known") is True
        assert "do_not_browse" in contract.forbidden_actions
        assert "do_not_search_products" in contract.forbidden_actions
        assert "do_not_ask_product" in contract.forbidden_actions
        assert contract.action_to_execute == ACTION_PROPOSE_DRAFT_ORDER
        assert "product" not in contract.missing_fields

    def test_known_phone_is_not_missing(self) -> None:
        ctx = _catalog_ctx()
        contract = build_commerce_turn_contract(ctx, db=None)

        assert contract.known_facts.get("phone_known") is True
        assert "phone" not in contract.missing_fields
        assert "customer_phone" not in contract.missing_fields


class TestCommerceTurnContractDivergenceLog:
    def test_divergence_log_when_contract_forbids_browse_but_decision_searches(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        contract = CommerceTurnContract(
            commerce_state="whatsapp_quick_order",
            known_facts={"catalog_order_current_turn": True, "phone_known": True},
            missing_fields=["customer_first_name"],
            next_goal="continue_checkout_from_catalog_order",
            forbidden_actions=[
                "do_not_browse",
                "do_not_search_products",
                "do_not_ask_product",
            ],
            action_to_execute=ACTION_PROPOSE_DRAFT_ORDER,
            reasons=["test"],
        )
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "عسل"},
            reason="browse drift",
        )

        with caplog.at_level(logging.WARNING, logger="nahla.brain.commerce_turn_contract"):
            keys = log_commerce_turn_contract_divergence(
                contract,
                decision,
                ctx=_catalog_ctx(),
                phase="test",
            )

        assert "contract_forbids_browse_but_decision_is_browse_or_search" in keys
        assert "catalog_order_current_turn_but_decision_is_browse_or_search" in keys
        assert any(
            "COMMERCE_TURN_CONTRACT/divergence" in record.message
            for record in caplog.records
        )

    def test_no_divergence_when_decision_matches_contract(self) -> None:
        contract = build_commerce_turn_contract(_catalog_ctx(), db=None)
        decision = Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            reason="catalog_order_submitted → continue_checkout",
        )
        keys = log_commerce_turn_contract_divergence(
            contract,
            decision,
            ctx=_catalog_ctx(),
            phase="test",
        )
        browse_keys = {
            "contract_forbids_browse_but_decision_is_browse_or_search",
            "catalog_order_current_turn_but_decision_is_browse_or_search",
        }
        assert not browse_keys.intersection(keys)
