"""Commerce Turn Contract — preserve existing-order support over catalog checkout."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

import pytest

pytestmark = pytest.mark.governance_contract

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    build_commerce_turn_contract,
    maybe_enforce_commerce_turn_contract_decision,
)
from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    build_order_support_follow_up_args,
    compose_order_support_response_goal_for_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.pipeline import _compose_response_goal  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
    SuggestionSnapshot,
)
from modules.ai.brain.state.stages import STAGE_ORDERING  # noqa: E402

GENERIC_ORDER_REF = "284719365"
GENERIC_PRODUCT = "حذاء رياضي أبيض"
GENERIC_MERCHANT = "متجر تجريبي عام"
SHIPPING_DELAY_VOICE = "الطلب متأخر والشحن ما وصل"
SHIPPING_DELAY_TEXT = "الشحن ما وصل والطلب متأخر"


def _not_found_history() -> List[Dict[str, str]]:
    return [
        {"direction": "in", "body": GENERIC_ORDER_REF},
        {"direction": "out", "body": "لم نجد الطلب"},
    ]


def _stale_catalog_prep() -> OrderPreparationState:
    prep = OrderPreparationState()
    prep.draft_order_id = "draft-generic-1"
    prep.draft_order_reference = "NHL-1-000099"
    prep.order_creation_status = "created"
    prep.order_status = "pending_customer_info"
    prep.catalog_line_items_authoritative = True
    prep.catalog_checkout_total = 249.0
    prep.line_items = [
        {
            "name": GENERIC_PRODUCT,
            "qty": 1,
            "from_native_catalog_order": True,
        },
    ]
    return prep


def _catalog_only_prep() -> OrderPreparationState:
    """Active catalog checkout without draft-order evidence that claims support ownership."""
    prep = OrderPreparationState()
    prep.catalog_line_items_authoritative = True
    prep.catalog_checkout_total = 249.0
    prep.line_items = [
        {
            "name": GENERIC_PRODUCT,
            "qty": 1,
            "from_native_catalog_order": True,
        },
    ]
    return prep


def _active_catalog_ctx(
    message: str,
    *,
    history: List[Dict[str, str]] | None = None,
    intent_name: str = "ask_product",
    order_support_history: bool = True,
    catalog_only_prep: bool = False,
) -> BrainContext:
    state = MerchantConversationState(stage=STAGE_ORDERING, turn=3)
    state.current_product_focus = {
        "title": GENERIC_PRODUCT,
        "external_id": "prod-generic-1",
        "from_native_catalog_order": True,
    }
    state.order_prep = _catalog_only_prep() if catalog_only_prep else _stale_catalog_prep()
    resolved_history = history
    if resolved_history is None and order_support_history:
        resolved_history = _not_found_history()
    elif resolved_history is None:
        resolved_history = []
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000099",
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=0.85,
            slots={},
            raw_message=message,
            extraction_method="rules",
        ),
        state=state,
        facts=CommerceFacts(orderable=True, has_products=True),
        history=resolved_history,
        profile={"inbound_metadata": {}},
    )


def _support_decision(
    message: str,
    *,
    history: List[Dict[str, str]] | None = None,
    order_verified: bool = False,
    order_status: str = "",
) -> Decision:
    args = build_order_support_follow_up_args(
        message=message,
        history=history or _not_found_history(),
        order_verified=order_verified,
    )
    if order_status:
        args["order_status"] = order_status
    return Decision(
        action=ACTION_LLM_REPLY,
        args=args,
        reason="existing_order_support_ownership:test",
        confidence=0.94,
    )


def _enforce_support(
    message: str,
    *,
    history: List[Dict[str, str]] | None = None,
    order_verified: bool = False,
    order_status: str = "",
) -> Decision:
    ctx = _active_catalog_ctx(message, history=history)
    contract = build_commerce_turn_contract(ctx, db=None)
    assert contract.known_facts.get("active_catalog_checkout") is True
    raw = _support_decision(
        message,
        history=history,
        order_verified=order_verified,
        order_status=order_status,
    )
    assert raw.action == ACTION_LLM_REPLY
    assert raw.args.get("topic") == "existing_order_support"
    return maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)


class TestCommerceContractPreserveOrderSupport:
    def test_production_shaped_voice_shipping_follow_up_preserved(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        enforced = _enforce_support(SHIPPING_DELAY_VOICE)
        assert enforced.action == ACTION_LLM_REPLY
        assert enforced.args.get("topic") == "existing_order_support"
        assert enforced.args.get("contract_override_applied") is False
        assert enforced.args.get("override_skipped_reason") == "existing_order_support_owned"
        assert enforced.args.get("pre_contract_action") == ACTION_LLM_REPLY
        assert enforced.args.get("post_contract_action") == ACTION_LLM_REPLY
        assert enforced.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_text_shipping_follow_up_preserved(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        enforced = _enforce_support(SHIPPING_DELAY_TEXT)
        assert enforced.action == ACTION_LLM_REPLY
        assert enforced.args.get("topic") == "existing_order_support"

    def test_generic_merchant_fixture_not_category_specific(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _active_catalog_ctx(SHIPPING_DELAY_TEXT)
        contract = build_commerce_turn_contract(ctx, db=None)
        assert contract.known_facts.get("existing_order_support_only") is True
        raw = _support_decision(SHIPPING_DELAY_TEXT)
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_LLM_REPLY
        assert enforced.args.get("topic") == "existing_order_support"
        assert GENERIC_PRODUCT in str(ctx.state.current_product_focus.get("title"))

    def test_catalog_checkout_continues_without_support_ownership(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _active_catalog_ctx(
            "تمام",
            order_support_history=False,
            catalog_only_prep=True,
        )
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "", "source": "top_products"},
            reason="checkout_ack",
        )
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_normal_product_question_still_overrides_to_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _active_catalog_ctx(
            "وش عندكم من أحذية",
            order_support_history=False,
            catalog_only_prep=True,
        )
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "أحذية", "source": "top_products"},
            reason="browse",
        )
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_explicit_new_order_topic_not_preserved(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _active_catalog_ctx(
            "ابي اطلب منتج جديد",
            order_support_history=False,
            catalog_only_prep=True,
        )
        contract = build_commerce_turn_contract(ctx, db=None)
        raw = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "منتج جديد"},
            reason="new_order",
        )
        enforced = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
        assert enforced.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_verified_existing_order_support_preserved(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        enforced = _enforce_support(
            SHIPPING_DELAY_TEXT,
            order_verified=True,
            order_status="shipped",
        )
        assert enforced.action == ACTION_LLM_REPLY
        assert enforced.args.get("topic") == "existing_order_support"
        assert enforced.args.get("order_verified") is True
        assert enforced.args.get("order_status") == "shipped"

    def test_unverified_not_found_reference_preserved_without_fabrication(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        enforced = _enforce_support(SHIPPING_DELAY_TEXT)
        assert enforced.action == ACTION_LLM_REPLY
        assert enforced.args.get("order_verified") is False
        assert enforced.args.get("order_reference") == GENERIC_ORDER_REF
        assert enforced.args.get("order_status") in (None, "")

    def test_pr568_support_channel_ownership_goal_still_reachable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        enforced = _enforce_support(SHIPPING_DELAY_TEXT)
        goal = _compose_response_goal(enforced, SuggestionSnapshot())
        assert "support_channel_ownership" in goal
        assert compose_order_support_response_goal_for_decision(enforced.args).startswith(
            "existing_order_support —"
        )
