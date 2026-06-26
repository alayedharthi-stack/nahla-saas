"""Phase 2 — enforce catalog_order contract at decide-time."""
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

from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    build_commerce_turn_contract,
    maybe_enforce_commerce_turn_contract_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.discovery.entry import resolve_discovery_entry  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
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


def _catalog_ctx(*, prep: OrderPreparationState | None = None) -> BrainContext:
    state = MerchantConversationState(stage="ordering", turn=2)
    if prep is not None:
        state.order_prep = prep
    msg = _catalog_message()
    return BrainContext(
        tenant_id=33,
        customer_phone="966542980511",
        message=msg,
        intent=Intent(name="start_order", confidence=0.9, raw_message=msg),
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True),
        profile={"inbound_metadata": _catalog_meta()},
    )


class TestCommerceTurnContractPhase2Enforce:
    def test_enforce_overrides_search_products_to_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _catalog_ctx()
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "عسل", "source": "top_products"},
            reason="browse drift",
        )
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_PROPOSE_DRAFT_ORDER
        assert enforced.args.get("catalog_order_submitted") is True
        assert enforced.args.get("skip_product_discovery") is True
        assert enforced.args.get("product") is not None

    def test_enforce_overrides_catalog_navigate_and_llm_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _catalog_ctx()
        contract = build_commerce_turn_contract(ctx, db=None)
        for raw_action in (ACTION_CATALOG_NAVIGATE, ACTION_LLM_REPLY):
            raw = Decision(action=raw_action, args={"topic": "browse"}, reason="fallback")
            enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
            assert enforced.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_enforce_logs_telemetry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _catalog_ctx()
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(action=ACTION_SEARCH_PRODUCTS, args={"query": ""}, reason="browse")
        with caplog.at_level(logging.INFO, logger="nahla.brain.commerce_turn_contract"):
            maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert any(
            "contract_enforced_catalog_order_over_browse" in record.message
            for record in caplog.records
        )
        assert any("raw_action=search_products" in record.message for record in caplog.records)
        assert any("enforced_action=propose_draft_order" in record.message for record in caplog.records)

    def test_enforce_no_op_without_catalog_order_current_turn(self) -> None:
        ctx = BrainContext(
            tenant_id=33,
            customer_phone="966542980511",
            message="وش عندكم",
            intent=Intent(name="general", confidence=0.5, raw_message="وش عندكم"),
            state=MerchantConversationState(),
            facts=CommerceFacts(has_products=True),
            profile={"inbound_metadata": {}},
        )
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(action=ACTION_SEARCH_PRODUCTS, args={"query": ""}, reason="browse")
        assert maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw) is raw

    def test_catalog_order_does_not_enter_discovery(self) -> None:
        ctx = _catalog_ctx()
        entry = resolve_discovery_entry(ctx)
        assert entry.matched is False
        assert entry.reason == "catalog_order"

    def test_catalog_order_preserves_line_items_in_enforced_decision(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _catalog_ctx()
        contract = build_commerce_turn_contract(ctx, db=None)
        enforced = maybe_enforce_commerce_turn_contract_decision(
            ctx,
            contract,
            Decision(action=ACTION_SEARCH_PRODUCTS, args={"source": "top_products"}),
        )
        product = enforced.args.get("product") or {}
        assert product.get("line_items_count") == 1
        assert product.get("price") == 319.0
        assert product.get("product_retailer_id") == "86bqzca62a"

    def test_phone_known_not_in_contract_missing_fields(self) -> None:
        contract = build_commerce_turn_contract(_catalog_ctx(), db=None)
        assert contract.known_facts.get("phone_known") is True
        assert "phone" not in contract.missing_fields
        assert "do_not_ask_product" in contract.forbidden_actions

    def test_pipeline_sequence_enforces_browse_to_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Simulates MerchantBrain post-decide path: contract build → enforce → checkout."""
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: PLC0415
            maybe_enforce_catalog_order_continue_checkout,
        )

        ctx = _catalog_ctx()
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "", "source": "top_products"},
            reason="mocked browse",
        )
        after_contract = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        final = maybe_enforce_catalog_order_continue_checkout(ctx, after_contract)

        assert raw.action == ACTION_SEARCH_PRODUCTS
        assert after_contract.action == ACTION_PROPOSE_DRAFT_ORDER
        assert final.action == ACTION_PROPOSE_DRAFT_ORDER
        assert final.args.get("skip_product_discovery") is True
        assert "product" not in contract.missing_fields
        assert contract.next_goal == "continue_checkout_from_catalog_order"
