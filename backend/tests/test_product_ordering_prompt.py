"""Tests for context-aware product ordering prompts and context reset rules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modules.ai.brain.commerce.conversation_context_reset import (
    STAGE_BROWSING,
    STAGE_ORDER_DRAFT,
    STAGE_PAID_ORDER,
    STAGE_PRODUCT_SELECTED,
    clear_active_order_context,
    infer_order_context_stage,
    is_active_order_context,
    is_context_expired,
    maybe_reset_stale_order_context,
)
from modules.ai.brain.commerce.product_ordering_prompt import (
    LEGACY_ROBOTIC_PRODUCT_PROMPT,
    build_product_ordering_prompt,
    resolve_product_clarify_question,
)
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    INTENT_START_ORDER,
    MerchantConversationState,
    OrderPreparationState,
)


def _ctx(
    message: str,
    *,
    intent_name: str = "general",
    focus: dict | None = None,
    candidates: list | None = None,
    order_prep: OrderPreparationState | None = None,
) -> BrainContext:
    state = MerchantConversationState(greeted=True, stage="discovery")
    if focus:
        state.current_product_focus = focus
    if candidates:
        state.last_search_candidates = candidates
    if order_prep is not None:
        state.order_prep = order_prep
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        intent=Intent(name=intent_name, confidence=0.9, raw_message=message),
        state=state,
        facts=CommerceFacts(
            store_name="Test",
            has_products=True,
            product_count=3,
            in_stock_count=3,
            orderable=True,
            snapshot_fresh=True,
            top_products=candidates or [],
        ),
    )


class TestProductOrderingPrompt:
    def test_legacy_phrase_never_returned(self):
        ctx = _ctx("أبغى أطلب", intent_name=INTENT_START_ORDER)
        prompt = build_product_ordering_prompt(ctx)
        assert LEGACY_ROBOTIC_PRODUCT_PROMPT not in prompt
        assert "ما المنتج الذي تود" not in prompt

    def test_generic_order_intent_warm_prompt(self):
        ctx = _ctx("أبغى أطلب", intent_name=INTENT_START_ORDER)
        prompt = build_product_ordering_prompt(ctx)
        assert "أبشر" in prompt
        assert "وش" in prompt or "المتوفر" in prompt

    def test_honey_order_offers_catalog_options(self):
        ctx = _ctx(
            "أبي عسل",
            intent_name=INTENT_START_ORDER,
            candidates=[
                {"title": "طلح نجد", "external_id": "1"},
                {"title": "سمر الحجاز", "external_id": "2"},
            ],
        )
        prompt = build_product_ordering_prompt(ctx)
        assert "طلح نجد" in prompt
        assert "سمر الحجاز" in prompt
        assert LEGACY_ROBOTIC_PRODUCT_PROMPT not in prompt

    def test_active_product_context_asks_quantity_not_product(self):
        ctx = _ctx(
            "تمام",
            focus={"title": "طلح نجد", "external_id": "1"},
            order_prep=OrderPreparationState(
                product_id="1",
                quantity=1,
                product_variants_raw=[{"name": "500g"}, {"name": "1kg"}],
                product_options={"size": {"value_name": "500g"}},
            ),
        )
        prompt = build_product_ordering_prompt(ctx)
        assert "كم" in prompt or "500g" in prompt or "1kg" in prompt
        assert "ما المنتج" not in prompt

    def test_best_seller_request_natural_reply(self):
        ctx = _ctx(
            "الأكثر مبيعاً",
            candidates=[
                {"title": "طلح نجد", "external_id": "1"},
                {"title": "سمر الحجاز", "external_id": "2"},
            ],
        )
        prompt = build_product_ordering_prompt(ctx)
        assert "الأكثر" in prompt or "طلب" in prompt
        assert "أكثر مبيعاً" not in prompt or "غالبًا" in prompt

    def test_resolve_replaces_legacy_copy(self):
        ctx = _ctx("أبغى أطلب", intent_name=INTENT_START_ORDER)
        out = resolve_product_clarify_question(ctx, LEGACY_ROBOTIC_PRODUCT_PROMPT)
        assert out != LEGACY_ROBOTIC_PRODUCT_PROMPT

    def test_honey_order_without_catalog_never_invents_types(self):
        ctx = _ctx("أبي عسل", intent_name=INTENT_START_ORDER)
        prompt = build_product_ordering_prompt(ctx)
        assert "طلح نجد" not in prompt
        assert "سمر الحجاز" not in prompt
        assert "خليني أتأكد" in prompt or "ما ظهرت" in prompt


class TestConversationContextReset:
    def _state_with_prep(self, **kwargs) -> MerchantConversationState:
        state = MerchantConversationState(
            stage="ordering",
            current_product_focus={"title": "طلح نجد", "external_id": "1"},
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        state.order_prep = OrderPreparationState(product_id="1", **kwargs)
        return state

    def test_product_context_within_ttl_active(self):
        state = self._state_with_prep(awaiting_variant_choice=True)
        assert is_active_order_context(state) is True
        assert infer_order_context_stage(state) == STAGE_PRODUCT_SELECTED

    def test_expired_browsing_context_can_reset(self):
        state = MerchantConversationState(
            stage="discovery",
            last_search_candidates=[{"title": "طلح نجد"}],
            updated_at=(datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(),
        )
        assert infer_order_context_stage(state) == STAGE_BROWSING
        assert is_context_expired(state) is True
        reason = maybe_reset_stale_order_context(state, "أبغى أطلب")
        assert reason == "ttl_expired_browsing"
        assert state.last_search_candidates == []

    def test_delivered_order_clears_context(self):
        state = self._state_with_prep(order_status="delivered")
        reason = maybe_reset_stale_order_context(state, "شكراً")
        assert reason == "order_delivered"
        assert state.current_product_focus is None
        assert state.order_prep.product_id == ""

    def test_cancellation_clears_active_order(self):
        state = self._state_with_prep(city="الرياض")
        reason = maybe_reset_stale_order_context(state, "ألغي الطلب")
        assert reason == "customer_cancelled"
        assert state.order_prep.product_id == ""

    def test_paid_order_does_not_expire_by_ttl(self):
        state = self._state_with_prep(
            payment_receipt_received=True,
            order_status="under_review",
        )
        state.updated_at = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        assert infer_order_context_stage(state) == STAGE_PAID_ORDER
        assert is_context_expired(state) is False
        assert maybe_reset_stale_order_context(state, "متى يوصل؟") is None

    def test_clear_keeps_conversation_summary(self):
        state = MerchantConversationState(
            conversation_summary="عميل يفضل التوصيل",
            order_prep=OrderPreparationState(product_id="1"),
        )
        clear_active_order_context(state, reason="test")
        assert state.conversation_summary == "عميل يفضل التوصيل"
        assert state.order_prep.product_id == ""
