"""P0 — active-order bare quantity / variant consumption (Abu Naif turn)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.wa_draft_confirmation import compose_wa_order_flow_reply  # noqa: E402
from modules.ai.brain.commerce.cart_state import maybe_apply_cart_message  # noqa: E402
from modules.ai.brain.intent.active_order_quantity_extract import (  # noqa: E402
    extract_active_order_quantity_fallback,
    message_has_bare_quantity_or_variant_signal,
    resolve_active_order_quantity_reply,
)
from modules.ai.brain.intent.cart_intent_extractor import (  # noqa: E402
    extract_cart_intents,
    extract_cart_intents_with_context,
)
from modules.ai.brain.postprocess.staff_escalation_truth_guard import (  # noqa: E402
    SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR,
    apply_staff_escalation_truth_guard,
)
from modules.ai.brain.types import OrderPreparationState  # noqa: E402


def _active_state(*, product: str = "عسل طلح", variant: str = "") -> SimpleNamespace:
    item = {"product_name": product, "quantity": 1}
    if variant:
        item["variant"] = variant
    prep = OrderPreparationState(
        line_items=[item],
        order_status="awaiting_variant_choice",
        missing_fields=["customer_first_name"],
    )
    return SimpleNamespace(
        cart_items=[dict(item)],
        current_product_focus={"title": product, "variant": variant or None},
        order_prep=prep,
        awaiting_option_confirmation=False,
        last_question_asked="",
    )


class TestBareQuantitySignal:
    def test_detects_half_kilo_split(self) -> None:
        assert message_has_bare_quantity_or_variant_signal("نص كيلo ونص كيلo")

    def test_detects_variant_detail(self) -> None:
        assert message_has_bare_quantity_or_variant_signal("فيه نص كيلo بالشمع والعكبر")


class TestActiveOrderSplitQuantity:
    def test_split_half_kilo_asks_clarification_not_empty_intents(self) -> None:
        state = _active_state()
        intents = extract_cart_intents_with_context(
            "نص كيلo ونص كيلo",
            cart_items=state.cart_items,
            product_focus=state.current_product_focus,
            order_prep=state.order_prep,
            active_commerce=True,
        )
        assert intents
        assert intents[0]["action"] == "active_order_clarify"
        assert "كل نصف" in intents[0]["reply"]

    def test_split_applied_via_maybe_apply_sets_clarification(self) -> None:
        state = _active_state()
        _, _, changed = maybe_apply_cart_message(
            state=state,
            prep=state.order_prep,
            message="نص كيلo ونص كيلo",
        )
        assert not changed
        assert state.order_prep.active_order_quantity_clarification
        assert "كل نصف" in state.order_prep.active_order_quantity_clarification

    def test_no_generic_ack_for_split_during_active_order(self) -> None:
        state = _active_state()
        result = apply_staff_escalation_truth_guard(
            reply="تم تحويلك لفريق الدعم",
            inbound_text="نص كيلo ونص كيلo",
            state=state,
            tenant_id=1,
        )
        assert SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR not in result.reply
        assert "وصلت رسالتك" not in result.reply
        assert "كل نصف" in result.reply


class TestDuaPrefixWithQuantity:
    def test_dua_prefix_still_extracts_commerce(self) -> None:
        msg = "الله يسلمك من كل شر\nنص كيلo ونص كيلo"
        state = _active_state()
        intents = extract_cart_intents_with_context(
            msg,
            cart_items=state.cart_items,
            product_focus=state.current_product_focus,
            order_prep=state.order_prep,
            active_commerce=True,
        )
        assert intents[0]["action"] == "active_order_clarify"

    def test_guard_not_pure_social_ack(self) -> None:
        state = _active_state()
        msg = "الله يسلمك من كل شر\nنص كيلo ونص كيلo"
        result = apply_staff_escalation_truth_guard(
            reply="تم تحويلك لفريق الدعم",
            inbound_text=msg,
            state=state,
            tenant_id=1,
        )
        assert "وصلت رسالتك" not in result.reply
        assert result.reply.strip()


class TestVariantDetailConsumption:
    def test_variant_detail_attached_to_active_line(self) -> None:
        state = _active_state()
        _, _, changed = maybe_apply_cart_message(
            state=state,
            prep=state.order_prep,
            message="فيه نص كيلo بالشمع والعكبر",
        )
        assert changed
        item = state.cart_items[0]
        assert item.get("variant") == "500g"
        assert "الشمع" in str(item.get("edition") or item.get("notes") or "")

    def test_variant_detail_no_generic_ack(self) -> None:
        state = _active_state()
        result = apply_staff_escalation_truth_guard(
            reply="تم تحويلك لفريق الدعم",
            inbound_text="فيه نص كيلo بالشمع والعكبر",
            state=state,
            tenant_id=1,
        )
        assert "وصلت رسالتك" not in result.reply

    def test_single_half_kilo_updates_variant(self) -> None:
        state = _active_state()
        _, _, changed = maybe_apply_cart_message(
            state=state,
            prep=state.order_prep,
            message="نص كيلo",
        )
        assert changed
        assert state.cart_items[0]["variant"] == "500g"


class TestOutsideActiveOrder:
    def test_bare_split_does_not_create_cart(self) -> None:
        state = SimpleNamespace(cart_items=[], current_product_focus={}, order_prep=OrderPreparationState())
        _, _, changed = maybe_apply_cart_message(
            state=state,
            prep=state.order_prep,
            message="نص كيلo ونص كيلo",
        )
        assert not changed
        assert not state.cart_items

    def test_outside_order_asks_for_product(self) -> None:
        reply = resolve_active_order_quantity_reply(
            "نص كيلo ونص كيلo",
            active_commerce=False,
        )
        assert reply
        assert "المنتج" in reply

    def test_extract_cart_intents_unchanged_outside_active(self) -> None:
        assert extract_cart_intents("نص كيلo ونص كيلo") == []


class TestRegression:
    def test_existing_product_search_intents(self) -> None:
        intents = extract_cart_intents("أبغى كيلo طلح ونصف كيلo سمر")
        assert len(intents) >= 2
        assert any("طلح" in i.get("product_name", "") for i in intents)

    def test_context_kilo_follow_up(self) -> None:
        cart = [{"product_name": "عسل سمر", "quantity": 1}]
        follow = extract_cart_intents_with_context("كيلo", cart_items=cart)
        assert follow[0]["action"] == "update_variant"
        assert follow[0]["new_variant"] == "1kg"

    def test_compose_injects_clarification(self) -> None:
        prep = OrderPreparationState(
            active_order_quantity_clarification="فهمت إنك تبغى نصف كيلo ونصف كيلo.",
        )
        injected = compose_wa_order_flow_reply(
            order_prep=prep,
            brain_state={},
            cart_changed=False,
            existing_reply="",
        )
        assert injected and "نصف" in injected
