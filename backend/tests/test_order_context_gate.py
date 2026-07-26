"""
tests/test_order_context_gate.py
────────────────────────────────
Regression: active-order location/map messages must not trigger catalog search.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.decision.actions import (
    ACTION_FAQ_REPLY,
    ACTION_LLM_REPLY,
    ACTION_ORDER_CONTEXT_UPDATE,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.order_context_gate import (
    FULFILLMENT_DELIVERY_SWITCH,
    detect_fulfillment_update,
    has_active_order_context,
    has_explicit_commerce_topic_change,
    is_fulfillment_session_locked,
    is_order_fulfillment_product_query,
    should_block_product_discovery,
    should_skip_catalog_preload,
    should_suppress_product_escalation,
    try_fulfillment_lock_continuation,
    try_order_context_update_decision,
)
from modules.ai.brain.state.stages import STAGE_ORDERING
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)

_MAPS = "https://maps.app.goo.gl/abc123test"
_PRODUCT = {
    "title": "عسل سدر",
    "external_id": "ext-honey-1",
    "id": 101,
    "can_checkout": True,
}


def _active_order_ctx(
    message: str,
    *,
    intent_name: str = "general",
    intent_confidence: float = 0.55,
    slots: dict | None = None,
    with_focus: bool = True,
) -> BrainContext:
    prep = OrderPreparationState(
        product_id="ext-honey-1",
        customer_first_name="محمد",
        customer_phone="966500000001",
        city="الرياض",
        order_status="awaiting_address",
    )
    state = MerchantConversationState(
        stage=STAGE_ORDERING,
        greeted=True,
        order_prep=prep,
        current_product_focus=dict(_PRODUCT) if with_focus else None,
        last_search_candidates=[dict(_PRODUCT)],
    )
    return BrainContext(
        tenant_id=99,
        customer_phone="966500000001",
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=intent_confidence,
            slots=dict(slots or {}),
            raw_message=message,
        ),
        state=state,
        facts=CommerceFacts(has_products=True, orderable=True, store_name="Test"),
    )


class TestOrderContextGateDetection:
    def test_maps_link_during_active_order(self):
        msg = f"{_MAPS}\nأبغى الطلبية تجي الموقع ذا"
        ctx = _active_order_ctx(msg, slots={"google_maps_url": _MAPS})
        assert has_active_order_context(ctx)
        assert detect_fulfillment_update(msg, ctx.intent.slots) is not None
        assert should_block_product_discovery(ctx)

    def test_delivery_phrase_without_maps(self):
        msg = "أبغى الطلبية تجي الموقع ذا"
        assert detect_fulfillment_update(msg, {}) is not None
        assert is_order_fulfillment_product_query("الطلبية تجي الموقع ذا")

    def test_pickup_to_delivery_switch(self):
        msg = "\u063a\u064a\u0631\u062a \u0625\u0644\u0649 \u062a\u0648\u0635\u064a\u0644"
        assert detect_fulfillment_update(msg, {}) == FULFILLMENT_DELIVERY_SWITCH

    def test_no_active_order_map_is_not_blocked(self):
        msg = _MAPS
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(name="general", confidence=0.5, raw_message=msg),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(has_products=True, store_name="Test"),
        )
        assert not has_active_order_context(ctx)
        assert not should_block_product_discovery(ctx)


class TestOrderContextDecisionEngine:
    def test_active_order_maps_routes_order_context_update(self):
        msg = f"{_MAPS}\nأبغى الطلبية تجي الموقع ذا"
        ctx = _active_order_ctx(msg, slots={"google_maps_url": _MAPS})
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_ORDER_CONTEXT_UPDATE
        assert decision.action != ACTION_SEARCH_PRODUCTS
        assert decision.args.get("google_maps_url") == _MAPS

    def test_active_order_delivery_phrase_not_search(self):
        msg = "أبغى الطلبية تجي الموقع ذا"
        ctx = _active_order_ctx(msg, intent_name="start_order", intent_confidence=0.85)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action in {
            ACTION_ORDER_CONTEXT_UPDATE,
            ACTION_PROPOSE_DRAFT_ORDER,
        }
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_pickup_to_delivery_continues_order_flow(self):
        msg = "\u063a\u064a\u0631\u062a \u0625\u0644\u0649 \u062a\u0648\u0635\u064a\u0644"
        ctx = _active_order_ctx(msg)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_ORDER_CONTEXT_UPDATE
        assert decision.args.get("fulfillment_kind") == FULFILLMENT_DELIVERY_SWITCH

    def test_no_active_order_map_routes_store_location_faq(self):
        msg = "وين موقعكم"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(
                name="ask_location",
                confidence=0.92,
                raw_message=msg,
            ),
            state=MerchantConversationState(greeted=True),
            facts=CommerceFacts(
                has_products=True,
                store_name="Test",
                maps_url="https://maps.app.goo.gl/store",
            ),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_LLM_REPLY
        assert decision.args.get("topic") == "location_delivery"

    def test_completed_order_product_ask_still_allowed(self):
        msg = "عسل سدر بكم؟"
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message=msg,
            intent=Intent(name="ask_price", confidence=0.9, raw_message=msg),
            state=MerchantConversationState(greeted=True, stage="discovery"),
            facts=CommerceFacts(has_products=True, orderable=True),
            commerce_bundle={},
        )
        assert not has_active_order_context(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS


class TestOrderContextPipelineSkip:
    def test_skip_catalog_preload_during_fulfillment(self):
        msg = f"{_MAPS} أبغى الطلبية تجي الموقع ذا"
        prep = OrderPreparationState(
            product_id="ext-honey-1",
            customer_first_name="محمد",
        )
        state = MerchantConversationState(
            stage=STAGE_ORDERING,
            order_prep=prep,
            current_product_focus=dict(_PRODUCT),
        )
        intent = Intent(name="general", confidence=0.5, raw_message=msg)
        assert should_skip_catalog_preload(
            message=msg,
            state=state,
            intent=intent,
        )

    def test_try_order_context_update_includes_product(self):
        msg = f"{_MAPS}"
        ctx = _active_order_ctx(msg, slots={"google_maps_url": _MAPS})
        decision = try_order_context_update_decision(ctx)
        assert decision is not None
        assert decision.action == ACTION_ORDER_CONTEXT_UPDATE
        assert decision.args.get("product", {}).get("external_id") == "ext-honey-1"


class TestFulfillmentSessionLock:
    def test_generic_message_blocks_product_discovery(self):
        msg = "تمام"
        ctx = _active_order_ctx(msg, intent_name="general")
        assert is_fulfillment_session_locked(ctx)
        assert should_block_product_discovery(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_explicit_commerce_topic_change_unlocks_discovery(self):
        msg = "أبي منتج ثاني"
        ctx = _active_order_ctx(msg, intent_name="ask_product", intent_confidence=0.9)
        assert has_explicit_commerce_topic_change(msg)
        assert not should_block_product_discovery(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS

    def test_quiet_gap_order_prep_only_still_locked(self):
        """Persisted order_prep survives stage=discovery after a quiet gap."""
        prep = OrderPreparationState(
            product_id="ext-honey-1",
            customer_first_name="محمد",
            order_status="awaiting_payment",
        )
        state = MerchantConversationState(
            stage="discovery",
            greeted=True,
            order_prep=prep,
        )
        ctx = BrainContext(
            tenant_id=99,
            customer_phone="966500000001",
            message="أيوه",
            intent=Intent(name="general", confidence=0.5, raw_message="أيوه"),
            state=state,
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        assert is_fulfillment_session_locked(ctx)
        assert should_block_product_discovery(ctx)
        assert should_skip_catalog_preload(
            message="أيوه",
            state=state,
            intent=ctx.intent,
        )

    def test_fulfillment_lock_continuation_on_generic_turn(self):
        msg = "حسنا"
        ctx = _active_order_ctx(msg, intent_name="general")
        decision = try_fulfillment_lock_continuation(ctx)
        assert decision is not None
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.args.get("fulfillment_lock") is True

    def test_stale_city_and_short_address_code_do_not_block_new_product_question(self):
        """Root-cause regression (2026-07-26) — reproduces the EXACT
        production field combination that caused the live FAIL 1 incident,
        not a simplified stand-in.

        The abandoned May checkout attempt (conversation_id=13, tenant_id=1)
        left ``city="الطايف"`` and ``short_address_code="TAPA7401"``
        persisted in ``order_prep`` for months. With ``stage`` already back
        to ``discovery`` and no strong signal (no product_id, no
        order_status, no payment/variant flags), a brand-new, unrelated
        product question must NOT be blocked."""
        msg = "عندكم أحذية رياضية بيضاء؟"
        prep = OrderPreparationState(
            city="الطايف",
            short_address_code="TAPA7401",
        )
        state = MerchantConversationState(
            stage="discovery",
            greeted=True,
            order_prep=prep,
        )
        intent = Intent(name="ask_product", confidence=0.82, raw_message=msg)
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966542980511",
            message=msg,
            intent=intent,
            state=state,
            facts=CommerceFacts(has_products=True, orderable=True, store_name="Test"),
        )
        assert not is_fulfillment_session_locked(ctx)
        assert not should_block_product_discovery(ctx)
        assert not should_skip_catalog_preload(message=msg, state=state, intent=intent)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS

    def test_stale_name_and_address_together_do_not_block_new_product_question(self):
        """Same regression with the full residual field set actually
        observed live in production at incident time (customer name AND
        address fragments both persisted, no strong signal)."""
        msg = "عندكم أحذية رياضية بيضاء؟"
        prep = OrderPreparationState(
            customer_first_name="هيثم",
            customer_last_name="الحارثي",
            city="الطايف",
            short_address_code="TAPA7401",
        )
        state = MerchantConversationState(
            stage="discovery",
            greeted=True,
            order_prep=prep,
        )
        intent = Intent(name="ask_product", confidence=0.82, raw_message=msg)
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966542980511",
            message=msg,
            intent=intent,
            state=state,
            facts=CommerceFacts(has_products=True, orderable=True, store_name="Test"),
        )
        assert not is_fulfillment_session_locked(ctx)
        assert not should_block_product_discovery(ctx)
        assert not should_skip_catalog_preload(message=msg, state=state, intent=intent)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS

    def test_weak_order_prep_fields_still_lock_during_active_ordering_stage(self):
        """Counterpart to the regression above: the SAME weak-only fields
        (name, no product_id/order_status) must still lock while the
        conversation stage genuinely reflects an active ordering flow —
        the fix narrows the stale-forever case, it does not remove the
        lock for a real, current checkout."""
        msg = "تمام"
        prep = OrderPreparationState(customer_first_name="هيثم")
        state = MerchantConversationState(
            stage=STAGE_ORDERING,
            greeted=True,
            order_prep=prep,
            current_product_focus=dict(_PRODUCT),
        )
        intent = Intent(name="general", confidence=0.5, raw_message=msg)
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966542980511",
            message=msg,
            intent=intent,
            state=state,
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        assert is_fulfillment_session_locked(ctx)
        assert should_block_product_discovery(ctx)

    def test_stale_product_id_and_order_status_ttl_expires_and_unlocks(self):
        """Review point 2 — 'strong' signals (product_id, order_status,
        missing_fields) must not lock discovery forever either. A
        product_id + order_status left over from an order abandoned/failed
        long ago (old ``updated_at``, stage settled back to discovery, no
        payment/receipt evidence) infers ``STAGE_PRODUCT_SELECTED``
        (12h TTL) via the existing, already-shipped
        ``conversation_context_reset`` TTL — and must expire, unlocking a
        fresh product question. This reuses the pre-existing TTL system;
        no new staleness logic was invented for this fix."""
        msg = "عندكم أحذية رياضية بيضاء؟"
        stale_updated_at = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
        prep = OrderPreparationState(product_id="ext-old-abandoned-1")
        state = MerchantConversationState(
            stage="discovery",
            greeted=True,
            order_prep=prep,
            updated_at=stale_updated_at,
        )
        intent = Intent(name="ask_product", confidence=0.82, raw_message=msg)
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966542980511",
            message=msg,
            intent=intent,
            state=state,
            facts=CommerceFacts(has_products=True, orderable=True, store_name="Test"),
        )
        assert not is_fulfillment_session_locked(ctx)
        assert not should_block_product_discovery(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS

    def test_recent_product_id_and_order_status_still_locks(self):
        """Counterpart — the SAME strong signals, freshly updated (well
        inside the TTL window), must still lock. TTL narrows the eternal
        lock; it must not weaken protection for a genuinely current order
        in progress."""
        msg = "تمام"
        recent_updated_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        prep = OrderPreparationState(
            product_id="ext-honey-1",
            order_status="awaiting_address",
        )
        state = MerchantConversationState(
            stage="discovery",
            greeted=True,
            order_prep=prep,
            updated_at=recent_updated_at,
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966542980511",
            message=msg,
            intent=Intent(name="general", confidence=0.5, raw_message=msg),
            state=state,
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        assert is_fulfillment_session_locked(ctx)
        assert should_block_product_discovery(ctx)

    def test_paid_order_status_never_ttl_expires_even_when_old(self):
        """A genuinely paid/in-review order (payment_receipt_received or a
        paid-adjacent order_status) must stay locked regardless of age —
        ``STAGE_PAID_ORDER`` is a deliberate, already-tested exception to
        the TTL system (see conversation_context_reset). The TTL relief
        added for stale strong signals must not weaken this."""
        msg = "عندكم أحذية رياضية بيضاء؟"
        very_old_updated_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        prep = OrderPreparationState(
            product_id="ext-honey-1",
            payment_receipt_received=True,
        )
        state = MerchantConversationState(
            stage="discovery",
            greeted=True,
            order_prep=prep,
            updated_at=very_old_updated_at,
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966542980511",
            message=msg,
            intent=Intent(name="ask_product", confidence=0.82, raw_message=msg),
            state=state,
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        assert is_fulfillment_session_locked(ctx)
        assert should_block_product_discovery(ctx)

    def test_missing_updated_at_conservatively_still_locks(self):
        """No ``updated_at`` at all means age cannot be judged — stay
        conservative and keep locking rather than assume staleness (matches
        ``conversation_context_reset.is_context_expired``'s own default)."""
        prep = OrderPreparationState(product_id="ext-honey-1")
        state = MerchantConversationState(
            stage="discovery",
            greeted=True,
            order_prep=prep,
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966542980511",
            message="تمام",
            intent=Intent(name="general", confidence=0.5, raw_message="تمام"),
            state=state,
            facts=CommerceFacts(has_products=True, orderable=True),
        )
        assert is_fulfillment_session_locked(ctx)

    def test_webhook_suppress_helper_uses_persisted_state(self):
        prep = {
            "product_id": "ext-honey-1",
            "customer_first_name": "محمد",
            "order_status": "awaiting_address",
        }
        assert should_suppress_product_escalation(
            message="مرحبا",
            brain_state={"order_prep": prep, "stage": "discovery"},
        )
        assert not should_suppress_product_escalation(
            message="ورني العروض",
            brain_state={"order_prep": prep, "stage": "discovery"},
        )
