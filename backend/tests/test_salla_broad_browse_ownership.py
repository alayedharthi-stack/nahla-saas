"""Broad store-wide browse ownership — shared morphological signal.

Phase A: catalog_browse_turn_policy consumes navigation_signals
``message_indicates_catalog_browse`` so stale ordering cannot force llm_reply.
"""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from modules.ai.brain.catalog.catalog_browse_turn_policy import (  # noqa: E402
    is_catalog_browse_message,
    maybe_suspend_stale_checkout_for_turn,
)
from modules.ai.brain.catalog.navigation_signals import (  # noqa: E402
    message_indicates_catalog_browse,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_CATALOG_NAVIGATE,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.turn.contract import OWNER_DISCOVERY  # noqa: E402
from modules.ai.brain.turn.ownership import (  # noqa: E402
    has_explicit_catalog_browse_intent,
    resolve_conversation_turn_ownership,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

MSG_PRODUCTS = "وش منتجاتكم؟"
MSG_AINDAKOM = "وش عندكم؟"
MSG_SELL = "ايش تبيعون؟"
MSG_ENGLISH = "What products do you have?"
MSG_CONTINUE = "أبيه"
MSG_ORDER_IT = "اطلبه"
MSG_PRICE = "كم سعره؟"
MSG_CATEGORY = "ورني الجاكيتات"

_STRUCTURED_BROWSE_ACTIONS = frozenset({
    ACTION_CATALOG_NAVIGATE,
    ACTION_SEARCH_PRODUCTS,
})

COLLECTIONS = [
    {
        "id": 1,
        "slug": "jackets",
        "label": "جاكيتات",
        "is_active": True,
        "priority": 1,
        "product_count": 3,
    },
]


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=12,
        in_stock_count=8,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="متجر تجريبي عام",
        top_products=[
            {"id": 101, "title": "جاكيت", "external_id": "j1", "price": 169},
            {"id": 102, "title": "قميص قطني أزرق", "external_id": "s1", "price": 89},
        ],
    )


def _stale_ordering_state() -> MerchantConversationState:
    return MerchantConversationState(
        greeted=True,
        stage="ordering",
        turn=8,
        order_prep=OrderPreparationState(
            product_id="old-sku",
            missing_fields=["address_location"],
        ),
        current_product_focus={
            "id": 28,
            "title": "جاكيت",
            "external_id": "old-sku",
        },
    )


def _ctx(
    message: str,
    *,
    tenant_id: int = 7,
    intent_name: str = "ask_product",
    state: MerchantConversationState | None = None,
    db: Any = None,
) -> BrainContext:
    ctx = BrainContext(
        tenant_id=tenant_id,
        customer_phone="966500000000",
        message=message,
        intent=Intent(name=intent_name, confidence=0.9, raw_message=message),
        state=state or MerchantConversationState(greeted=True, stage="discovery"),
        facts=_facts(),
    )
    if db is not None:
        ctx._db = db  # type: ignore[attr-defined]
    return ctx


def _ownership_and_decision(ctx: BrainContext):
    ownership = resolve_conversation_turn_ownership(ctx)
    suspended = maybe_suspend_stale_checkout_for_turn(ctx)
    with patch(
        "modules.ai.brain.catalog.navigation._load_catalog_groups",
        return_value=COLLECTIONS,
    ):
        decision = DefaultDecisionEngine().decide(ctx)
    return ownership, suspended, decision


class TestSharedBrowseSignal:
    @pytest.mark.parametrize(
        "message",
        [MSG_PRODUCTS, MSG_AINDAKOM, MSG_SELL, MSG_ENGLISH],
    )
    def test_policy_reuses_navigation_signal(self, message: str) -> None:
        signal = message_indicates_catalog_browse(message, intent_name="ask_product")
        policy = is_catalog_browse_message(message, intent_name="ask_product")
        assert signal is True
        assert policy is True
        assert policy is signal

    @pytest.mark.parametrize(
        "message",
        [MSG_CONTINUE, MSG_ORDER_IT, MSG_PRICE],
    )
    def test_order_and_product_info_are_not_browse(self, message: str) -> None:
        intent = "start_order" if message != MSG_PRICE else "ask_price"
        assert message_indicates_catalog_browse(message, intent_name=intent) is False
        assert is_catalog_browse_message(message, intent_name=intent) is False


class TestExactReproducerOwnership:
    def test_wesh_montajatkom_owns_discovery_over_stale_ordering(self) -> None:
        state = _stale_ordering_state()
        ctx = _ctx(MSG_PRODUCTS, state=state, db=MagicMock())
        assert has_explicit_catalog_browse_intent(ctx) is True
        ownership, suspended, decision = _ownership_and_decision(ctx)
        assert ownership.explicit_browse_intent is True
        assert ownership.turn_owner == OWNER_DISCOVERY
        assert suspended is True
        assert state.stage == "discovery"
        assert state.current_product_focus is None
        assert decision.action in _STRUCTURED_BROWSE_ACTIONS
        assert decision.action not in {ACTION_LLM_REPLY, ACTION_PROPOSE_DRAFT_ORDER}

    def test_wesh_montajatkom_does_not_keep_old_sku_as_owner(self) -> None:
        state = _stale_ordering_state()
        ctx = _ctx(MSG_PRODUCTS, state=state, db=MagicMock())
        maybe_suspend_stale_checkout_for_turn(ctx)
        assert state.order_prep.product_id == ""
        assert state.order_prep.missing_fields == []


class TestBrowseVariants:
    @pytest.mark.parametrize(
        "message",
        [MSG_AINDAKOM, MSG_SELL, MSG_ENGLISH],
    )
    def test_variants_suspend_stale_order_and_route_structured(self, message: str) -> None:
        state = _stale_ordering_state()
        ctx = _ctx(message, state=state, db=MagicMock())
        ownership, suspended, decision = _ownership_and_decision(ctx)
        assert ownership.turn_owner == OWNER_DISCOVERY
        assert suspended is True
        assert decision.action in _STRUCTURED_BROWSE_ACTIONS
        assert decision.action != ACTION_LLM_REPLY


class TestRealOrderContinuation:
    @pytest.mark.parametrize("message", [MSG_CONTINUE, MSG_ORDER_IT])
    def test_deictic_order_continuation_stays_ordering(self, message: str) -> None:
        state = _stale_ordering_state()
        ctx = _ctx(message, state=state, intent_name="start_order", db=MagicMock())
        ownership = resolve_conversation_turn_ownership(ctx)
        suspended = maybe_suspend_stale_checkout_for_turn(ctx)
        assert ownership.explicit_browse_intent is False
        assert ownership.turn_owner != OWNER_DISCOVERY
        assert suspended is False
        assert state.stage == "ordering"
        assert state.current_product_focus is not None
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_CATALOG_NAVIGATE
        assert decision.action != ACTION_SEARCH_PRODUCTS or "browse" not in (
            str(decision.reason or "").lower()
        )


class TestSpecificProductInfo:
    def test_price_followup_does_not_suspend_focus(self) -> None:
        state = _stale_ordering_state()
        ctx = _ctx(MSG_PRICE, state=state, intent_name="ask_price")
        assert is_catalog_browse_message(MSG_PRICE, intent_name="ask_price") is False
        assert maybe_suspend_stale_checkout_for_turn(ctx) is False
        assert state.current_product_focus is not None
        assert state.current_product_focus.get("title") == "جاكيت"


class TestCategoryBrowse:
    def test_category_browse_still_catalog_owned(self) -> None:
        ctx = _ctx(MSG_CATEGORY, db=MagicMock())
        assert is_catalog_browse_message(MSG_CATEGORY, intent_name="ask_product") is True
        ownership = resolve_conversation_turn_ownership(ctx)
        assert ownership.turn_owner == OWNER_DISCOVERY
        assert ownership.explicit_browse_intent is True
        assert maybe_suspend_stale_checkout_for_turn(ctx) is False


class TestTenantIsolation:
    def test_browse_ownership_does_not_cross_tenants(self) -> None:
        seen: list[int] = []

        def _groups_for(ctx):
            seen.append(int(ctx.tenant_id))
            return [
                {
                    **COLLECTIONS[0],
                    "label": f"tenant-{ctx.tenant_id}",
                },
            ]

        ctx_a = _ctx(MSG_PRODUCTS, tenant_id=3, db=MagicMock())
        ctx_b = _ctx(MSG_PRODUCTS, tenant_id=9, db=MagicMock())
        with patch(
            "modules.ai.brain.catalog.navigation._load_catalog_groups",
            side_effect=_groups_for,
        ):
            DefaultDecisionEngine().decide(ctx_a)
            DefaultDecisionEngine().decide(ctx_b)
        assert seen == [3, 9]
        assert resolve_conversation_turn_ownership(ctx_a).turn_owner == OWNER_DISCOVERY
        assert resolve_conversation_turn_ownership(ctx_b).turn_owner == OWNER_DISCOVERY


class TestStructuredPathSkipsLlmGroundingRewrite:
    def test_navigator_chosen_paths_are_guard_allowlisted(self) -> None:
        from modules.ai.brain.postprocess.catalog_product_grounding_guard import (  # noqa: PLC0415
            _DETERMINISTIC_ALLOW_PATHS,
            apply_catalog_product_grounding_guard,
        )

        assert "catalog_navigation_groups" in _DETERMINISTIC_ALLOW_PATHS
        assert "catalog_navigation_top_products_fallback" in _DETERMINISTIC_ALLOW_PATHS
        llm_shape = (
            "عندنا حالياً:\n"
            "- فساتين\n"
            "- جاكيتات\n"
            "- بناطيل\n"
            "\nتحب تشوف أي قسم؟"
        )
        result = apply_catalog_product_grounding_guard(
            reply=llm_shape,
            inbound_text=MSG_PRODUCTS,
            executor_products=[{"title": "جاكيت"}],
            chosen_path="catalog_navigation_top_products_fallback",
        )
        assert result.action == "allowed"
        assert result.reply == llm_shape
        assert "-" in result.reply

