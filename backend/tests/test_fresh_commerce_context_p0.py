"""P0 — abandoned draft / zombie commerce context decay."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modules.ai.brain.commerce.conversation_context_reset import (
    maybe_reset_stale_order_context,
)
from modules.ai.brain.commerce.fresh_commerce_context import (
    COMMERCE_CONTEXT_GAP_DAYS,
    detect_explicit_order_resume,
    has_abandoned_unconfirmed_commerce,
    is_fresh_exploratory_product_question,
    should_reset_abandoned_commerce,
)
from modules.ai.brain.order_context_gate import is_fulfillment_session_locked
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _stale_zombie_state(**prep_kwargs) -> MerchantConversationState:
    state = MerchantConversationState(
        stage="ordering",
        current_product_focus={
            "title": "عسل طلح نجد البري إنتاج مناحلنا",
            "external_id": "honey-1",
        },
        draft_order_id="draft-123",
        updated_at=(
            datetime.now(timezone.utc) - timedelta(days=COMMERCE_CONTEXT_GAP_DAYS + 3)
        ).isoformat(),
    )
    state.order_prep = OrderPreparationState(
        product_id="honey-1",
        quantity=1,
        missing_fields=["customer_first_name"],
        **prep_kwargs,
    )
    return state


def _recent_zombie_state(**prep_kwargs) -> MerchantConversationState:
    state = _stale_zombie_state(**prep_kwargs)
    state.updated_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    return state


def _ctx(state: MerchantConversationState, message: str) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        intent=Intent(name="general", confidence=0.9, raw_message=message),
        state=state,
        facts=CommerceFacts(store_name="Test", has_products=True),
    )


class TestFreshExploratoryDetection:
    def test_what_honey_types_matches(self):
        assert is_fresh_exploratory_product_question("وش العسل اللي عندكم") is True

    def test_explicit_resume_does_not_match_exploration(self):
        assert detect_explicit_order_resume("أبغى أكمل الطلب السابق") is True
        assert is_fresh_exploratory_product_question("أبغى أكمل الطلب السابق") is False


class TestAbandonedDraftDecay:
    """Test 1 — old draft ignored on fresh product discovery."""

    def test_stale_draft_cleared_on_catalog_exploration(self):
        state = _stale_zombie_state()
        msg = "وش العسل اللي عندكم"
        assert has_abandoned_unconfirmed_commerce(state) is True
        should, reason = should_reset_abandoned_commerce(message=msg, state=state)
        assert should is True
        assert reason == "abandoned_draft_fresh_exploration"

        reset = maybe_reset_stale_order_context(state, msg)
        assert reset in ("abandoned_draft_fresh_exploration", "ttl_expired_awaiting_address_payment")
        assert state.current_product_focus is None
        assert state.order_prep.product_id == ""
        assert state.draft_order_id is None
        assert state.stage == "discovery"

    def test_missing_updated_at_still_decays_on_fresh_explore(self):
        state = _stale_zombie_state()
        state.updated_at = ""
        msg = "وش العسل اللي عندكم"
        should, reason = should_reset_abandoned_commerce(message=msg, state=state)
        assert should is True
        assert reason == "abandoned_draft_unknown_age_fresh_exploration"
        reset = maybe_reset_stale_order_context(state, msg)
        assert reset == "abandoned_draft_unknown_age_fresh_exploration"
        assert state.current_product_focus is None

    def test_fulfillment_lock_released_after_decay(self):
        state = _stale_zombie_state()
        ctx = _ctx(state, "وش العسل اللي عندكم")
        assert is_fulfillment_session_locked(ctx) is True
        maybe_reset_stale_order_context(state, "وش العسل اللي عندكم")
        ctx_after = _ctx(state, "وش العسل اللي عندكم")
        assert is_fulfillment_session_locked(ctx_after) is False


class TestRecentDraftContinues:
    """Test 2 — recent draft still continues."""

    def test_recent_draft_not_cleared(self):
        state = _recent_zombie_state()
        msg = "تمام كمل"
        should, reason = should_reset_abandoned_commerce(message=msg, state=state)
        assert should is False
        assert reason == "not_fresh_exploration"
        assert maybe_reset_stale_order_context(state, msg) is None
        assert state.current_product_focus is not None
        assert state.order_prep.product_id == "honey-1"


class TestExplicitResume:
    """Test 3 — explicit reference resumes old draft."""

    def test_explicit_resume_not_discarded(self):
        state = _stale_zombie_state()
        msg = "أبغى أكمل الطلب السابق"
        should, reason = should_reset_abandoned_commerce(message=msg, state=state)
        assert should is False
        assert reason == "explicit_order_resume"
        assert maybe_reset_stale_order_context(state, msg) is None
        assert state.current_product_focus is not None
        assert state.order_prep.product_id == "honey-1"


class TestCompletedOrdersPreserved:
    """Test 4 — completed order history fields are not touched."""

    def test_order_history_question_does_not_clear_profile(self):
        state = MerchantConversationState(
            stage="discovery",
            conversation_summary="عميل طلب عسل سابقاً",
            updated_at=(
                datetime.now(timezone.utc) - timedelta(days=COMMERCE_CONTEXT_GAP_DAYS + 5)
            ).isoformat(),
        )
        state.order_prep = OrderPreparationState(order_status="delivered")
        msg = "وش طلباتي السابقة؟"
        assert is_fresh_exploratory_product_question(msg) is False
        reset = maybe_reset_stale_order_context(state, msg)
        assert reset == "order_delivered"
        assert state.conversation_summary == "عميل طلب عسل سابقاً"


class TestSupportCasePreserved:
    """Test 5 — open support case blocks commerce decay."""

    def test_support_case_blocks_decay(self):
        state = _stale_zombie_state()
        state.stage = "support"
        msg = "وش صار على مشكلتي؟"
        should, reason = should_reset_abandoned_commerce(message=msg, state=state)
        assert should is False
        assert reason == "open_support_case"
        assert maybe_reset_stale_order_context(state, msg) is None
        assert state.current_product_focus is not None

    def test_confirmed_checkout_blocks_decay_on_browse(self):
        state = _stale_zombie_state(order_status="pending_payment")
        state.updated_at = (
            datetime.now(timezone.utc) - timedelta(days=COMMERCE_CONTEXT_GAP_DAYS + 5)
        ).isoformat()
        msg = "وش العسل اللي عندكم"
        should, reason = should_reset_abandoned_commerce(message=msg, state=state)
        assert should is False
        assert reason == "no_abandoned_commerce"
