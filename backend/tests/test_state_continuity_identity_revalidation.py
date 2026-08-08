"""
tests/test_state_continuity_identity_revalidation.py
────────────────────────────────────────────────────
State Continuity / Product Identity Revalidation patch tests.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.state_continuity_identity import (  # noqa: E402
    resolve_product_for_state_continuity,
    suspend_checkout_authority_retain_identity,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_ORDER_CONTEXT_UPDATE,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent_priority.types import (  # noqa: E402
    GOAL_PRICE_INQUIRY,
    IntentPriorityVerdict,
)
from modules.ai.brain.state.state_relevance import StateRelevanceVerdict  # noqa: E402
from modules.ai.brain.turn.enforce import maybe_enforce_turn_decision  # noqa: E402
from modules.ai.brain.turn.mismatch import MISMATCH_CHECKOUT_VS_DISCOVERY  # noqa: E402
from modules.ai.brain.turn.shadow import prepare_turn_arbitration  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _enable_enforce_platform_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURN_ARBITER_ENFORCE_ENABLED", "true")
    monkeypatch.delenv("TURN_ARBITER_ENFORCE_TENANTS", raising=False)
    monkeypatch.setenv(
        "TURN_ARBITER_ENFORCE_MISMATCH_TYPES",
        "checkout_vs_support,checkout_vs_discovery,staff_vs_persona",
    )


def _ctx(
    msg: str,
    *,
    tenant_id: int = 33,
    intent_name: str = "general",
    state: MerchantConversationState | None = None,
    state_relevance: StateRelevanceVerdict | None = None,
    intent_priority: IntentPriorityVerdict | None = None,
) -> BrainContext:
    st = state or MerchantConversationState(turn=3)
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone="+966500000000",
        message=msg,
        raw_message=msg,
        intent=Intent(name=intent_name, confidence=0.92, slots={}),
        state=st,
        facts=CommerceFacts(has_products=True),
        history=[],
        state_relevance=state_relevance,
        intent_priority=intent_priority,
    )


def _state_rel() -> StateRelevanceVerdict:
    return StateRelevanceVerdict(
        safe_to_resume_state=False,
        detected_topic_shift=True,
        active_workflows=("active_fulfillment",),
    )


def _checkout_state_with_focus() -> MerchantConversationState:
    st = MerchantConversationState(turn=5, stage="checkout")
    st.current_product_focus = {
        "id": "501",
        "external_id": "sku-shoe-white",
        "title": "حذاء رياضي أبيض",
        "price": 199,
        "in_stock": True,
        "orderable": True,
        "variants": [{"id": "v1"}],
    }
    st.order_prep = OrderPreparationState(
        product_id="501",
        pending_variant_product_id="501",
        awaiting_variant_choice=True,
        missing_fields=["city"],
    )
    st.last_question_asked = "ما المدينة التي سيصلها الطلب؟"
    st.draft_order_id = "draft-1"
    st.checkout_url = "https://checkout.example/1"
    st.selected_variant = {"variant_id": "v-old", "price": 199}
    st.cart_items = [{"product_id": "501", "qty": 1}]
    return st


class TestFieldScopedSuspend:
    def test_suspend_retains_identity_and_clears_variant_authority(self) -> None:
        state = _checkout_state_with_focus()
        suspend_checkout_authority_retain_identity(state, reason="test")

        focus = state.current_product_focus or {}
        assert focus.get("id") == "501"
        assert focus.get("external_id") == "sku-shoe-white"
        assert focus.get("title") == "حذاء رياضي أبيض"
        assert "price" not in focus
        assert "in_stock" not in focus
        assert "orderable" not in focus
        assert "variants" not in focus

        assert state.order_prep.awaiting_variant_choice is False
        assert state.order_prep.pending_variant_product_id == ""
        assert state.order_prep.missing_fields == []
        assert state.draft_order_id is None
        assert state.checkout_url is None
        assert state.selected_variant is None
        assert state.last_question_asked == ""
        assert state.stage == "discovery"


class TestVariantPickQualification:
    def _variant_state(self) -> MerchantConversationState:
        return MerchantConversationState(
            order_prep=OrderPreparationState(
                awaiting_variant_choice=True,
                pending_variant_product_id="501",
            ),
        )

    def test_ask_product_free_text_does_not_variant_pick(self) -> None:
        msg = "حدثني عن حذاء رياضي أبيض بالتفصيل"
        ctx = _ctx(msg, intent_name=INTENT_ASK_PRODUCT, state=self._variant_state())
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_numeric_variant_pick_still_works(self) -> None:
        for msg in ("44", "2"):
            ctx = _ctx(msg, state=self._variant_state())
            decision = DefaultDecisionEngine().decide(ctx)
            assert decision.action == ACTION_PROPOSE_DRAFT_ORDER, msg
            pick = decision.args.get("variant_pick") or {}
            if msg == "44":
                assert pick.get("label") == "44" or pick.get("index_one_based") == 44
            else:
                assert pick.get("index_one_based") == 2


class TestEnforceDiscoveryReresolve:
    def test_checkout_vs_discovery_yields_search_with_identity(self, monkeypatch) -> None:
        _enable_enforce_platform_wide(monkeypatch)
        ctx = _ctx(
            "حدثني عن حذاء رياضي أبيض",
            intent_name=INTENT_ASK_PRODUCT,
            state=_checkout_state_with_focus(),
            state_relevance=_state_rel(),
        )
        prepare_turn_arbitration(ctx)
        legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="slot_fill")
        new_decision, result = maybe_enforce_turn_decision(ctx, legacy)

        assert result.enforced is True
        assert result.mismatch_type == MISMATCH_CHECKOUT_VS_DISCOVERY
        assert new_decision.action == ACTION_SEARCH_PRODUCTS
        assert new_decision.args.get("source") == "state_continuity_reresolve"
        assert new_decision.args.get("product_id") == "501"
        assert new_decision.args.get("external_id") == "sku-shoe-white"
        assert new_decision.args.get("block_order_flow") is True

        focus = ctx.state.current_product_focus or {}
        assert focus.get("id") == "501"
        assert "price" not in focus
        assert ctx.state.order_prep.awaiting_variant_choice is False

    def test_implicit_price_with_identity_reresolves(self, monkeypatch) -> None:
        _enable_enforce_platform_wide(monkeypatch)
        ctx = _ctx(
            "وش سعره؟",
            intent_name=INTENT_ASK_PRICE,
            state=_checkout_state_with_focus(),
            state_relevance=_state_rel(),
            intent_priority=IntentPriorityVerdict(primary_customer_goal=GOAL_PRICE_INQUIRY),
        )
        prepare_turn_arbitration(ctx)
        legacy = Decision(action=ACTION_PROPOSE_DRAFT_ORDER, reason="variant_gate")
        new_decision, result = maybe_enforce_turn_decision(ctx, legacy)

        assert result.enforced is True
        assert new_decision.action == ACTION_SEARCH_PRODUCTS
        assert new_decision.args.get("product_id") == "501"
        assert new_decision.args.get("source") == "state_continuity_reresolve"

    def test_different_product_inquiry_does_not_keep_checkout_authority(self, monkeypatch) -> None:
        _enable_enforce_platform_wide(monkeypatch)
        state = _checkout_state_with_focus()
        state.current_product_focus = {
            "id": "501",
            "external_id": "sku-shoe-white",
            "title": "حذاء رياضي أبيض",
        }
        state.order_prep.missing_fields = ["city"]
        ctx = _ctx(
            "عندكم عطر ورد 100ml؟",
            intent_name=INTENT_ASK_PRODUCT,
            state=state,
            state_relevance=_state_rel(),
        )
        prepare_turn_arbitration(ctx)
        legacy = Decision(action=ACTION_ORDER_CONTEXT_UPDATE, reason="ask_city")
        new_decision, result = maybe_enforce_turn_decision(ctx, legacy)

        assert result.enforced is True
        assert new_decision.action == ACTION_SEARCH_PRODUCTS
        assert ctx.state.order_prep.missing_fields == []
        assert ctx.state.order_prep.awaiting_variant_choice is False
        assert ctx.state.draft_order_id is None


class TestResolveProductTenantIsolation:
    def test_wrong_tenant_returns_empty(self) -> None:
        db = MagicMock()
        builder = MagicMock()
        builder.get_by_external_id.return_value = None

        from core import store_knowledge  # noqa: PLC0415

        original = store_knowledge.CatalogContextBuilder
        store_knowledge.CatalogContextBuilder = MagicMock(return_value=builder)
        try:
            resolved = resolve_product_for_state_continuity(
                db,
                tenant_id=33,
                external_id="sku-shoe-white",
            )
        finally:
            store_knowledge.CatalogContextBuilder = original

        assert resolved is None
        builder.get_by_external_id.assert_called_once_with("sku-shoe-white")
