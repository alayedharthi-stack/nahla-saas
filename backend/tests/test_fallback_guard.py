"""Tests for global fallback / dead-end safeguards."""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.fallback_guard import (  # noqa: E402
    detect_hard_topic_shift,
    detect_semantic_dead_end,
    evaluate_hard_topic_shift,
    fallback_fingerprint,
    invalidate_suppression_memory,
    is_recent_topic_active,
    resolve_active_topic,
    should_block_fallback_repeat,
    stamp_recent_topic,
)
from modules.ai.brain.commerce.solution_seeking import (  # noqa: E402
    contextual_non_product_clarification,
    detect_solution_seeking_suppression,
)
from modules.ai.brain.product_discovery_gate import (  # noqa: E402
    extract_inquiry_product_query,
    has_explicit_product_inquiry,
)
from modules.ai.brain.types import MerchantConversationState  # noqa: E402


def test_payment_clarification_is_short():
    q = contextual_non_product_clarification("لك فلوس معاي")
    assert q is not None
    assert len(q) < 80


def test_multi_turn_delivery_topic_persists():
    state = MerchantConversationState(turn=5)
    stamp_recent_topic(state, "delivery_intent", turn=3)
    assert is_recent_topic_active(state, current_turn=5)
    assert resolve_active_topic("ايوه", state, []) == "delivery_intent"


def test_location_after_delivery_ack():
    state = MerchantConversationState(turn=4, recent_topic="delivery_intent", recent_topic_turn=2)
    topic = resolve_active_topic(
        "موقعي البيعة",
        state,
        [{"direction": "in", "body": "فيه توصيل؟"}],
    )
    assert topic in {"delivery_intent", "location_intent"}


def test_semantic_dead_end_two_turn_price_then_all_sizes():
    history = [{"direction": "in", "body": "كم السعر"}]
    goal = detect_semantic_dead_end("كل الحجام", history=history, state=None)
    assert goal == "all_variant_prices"


def test_semantic_dead_end_all_variants():
    history = [
        {"direction": "in", "body": "كم السعر"},
        {"direction": "in", "body": "أي حجم"},
        {"direction": "in", "body": "كل الأحجام"},
    ]
    goal = detect_semantic_dead_end("ابي سعر كل الحجام", history=history, state=None)
    assert goal == "all_variant_prices"


def test_fallback_repeat_blocked():
    state = MerchantConversationState(turn=3, last_fallback_turn=2)
    fp = fallback_fingerprint("same text")
    state.last_fallback_fingerprint = fp
    assert should_block_fallback_repeat(state, "same text", current_turn=3)


def test_post_repair_suppression():
    assert detect_solution_seeking_suppression("فيه توصيل") == "delivery_intent"


def test_hard_topic_shift_availability_product():
    history = [{"direction": "in", "body": "\u0623\u0628\u063a\u0649 \u0627\u0644\u0627\u0633\u062a\u0641\u0633\u0627\u0631 \u0639\u0646 \u0627\u0644\u0639\u0633\u0644"}]
    assert detect_hard_topic_shift(
        "\u0645\u062a\u0649 \u064a\u062a\u0648\u0641\u0631 \u0639\u0633\u0644 \u0627\u0644\u0633\u062f\u0631",
        history=history,
    )


def test_hard_topic_shift_clears_stale_recent_topic():
    state = MerchantConversationState(turn=4, recent_topic="delivery_intent", recent_topic_turn=2)
    history = [{"direction": "in", "body": "\u0623\u0628\u063a\u0649 \u0627\u0644\u0627\u0633\u062a\u0641\u0633\u0627\u0631 \u0639\u0646 \u0627\u0644\u0639\u0633\u0644"}]
    assert resolve_active_topic(
        "\u0645\u062a\u0649 \u064a\u062a\u0648\u0641\u0631 \u0639\u0633\u0644 \u0627\u0644\u0633\u062f\u0631",
        state,
        history,
    ) is None


def test_hard_topic_shift_unblocks_fallback_repeat():
    state = MerchantConversationState(turn=3, last_fallback_turn=2)
    fp = fallback_fingerprint("same text")
    state.last_fallback_fingerprint = fp
    history = [{"direction": "in", "body": "\u0623\u0628\u063a\u0649 \u0627\u0644\u0627\u0633\u062a\u0641\u0633\u0627\u0631 \u0639\u0646 \u0627\u0644\u0639\u0633\u0644"}]
    assert not should_block_fallback_repeat(
        state,
        "same text",
        current_turn=3,
        message="\u0645\u062a\u0649 \u064a\u062a\u0648\u0641\u0631 \u0639\u0633\u0644 \u0627\u0644\u0633\u062f\u0631",
        history=history,
    )


def test_invalidate_suppression_memory_clears_fields():
    state = MerchantConversationState(
        turn=5,
        recent_topic="delivery_intent",
        recent_topic_turn=3,
        last_fallback_turn=4,
        customer_goal="all_variant_prices",
    )
    state.last_fallback_fingerprint = "abc"
    invalidate_suppression_memory(state, reason="test")
    assert state.recent_topic == ""
    assert state.last_fallback_fingerprint == ""
    assert state.customer_goal == ""


def test_extract_inquiry_product_query():
    assert extract_inquiry_product_query(
        "\u0623\u0628\u063a\u0649 \u0627\u0644\u0627\u0633\u062a\u0641\u0633\u0627\u0631 \u0639\u0646 \u0627\u0644\u0639\u0633\u0644",
    ) in {"\u0627\u0644\u0639\u0633\u0644", "\u0639\u0633\u0644"}


def test_has_explicit_product_inquiry():
    assert has_explicit_product_inquiry(
        "\u0623\u0628\u063a\u0649 \u0627\u0644\u0627\u0633\u062a\u0641\u0633\u0627\u0631 \u0639\u0646 \u0627\u0644\u0639\u0633\u0644",
    )


def test_no_hard_shift_on_price_size_continuation():
    history = [{"direction": "in", "body": "\u0643\u0645 \u0627\u0644\u0633\u0639\u0631"}]
    assert not detect_hard_topic_shift(
        "\u0643\u0644 \u0627\u0644\u062d\u062c\u0627\u0645",
        history=history,
    )
    assert not detect_hard_topic_shift(
        "\u0637\u064a\u0628 \u0648\u0627\u0644\u0643\u064a\u0644\u0648\u061f",
        history=history,
    )


def test_no_hard_shift_on_lexical_honey_rephrase():
    history = [{"direction": "in", "body": "\u0639\u0633\u0644 \u0637\u0628\u064a\u0639\u064a"}]
    assert not detect_hard_topic_shift(
        "\u0627\u0644\u0639\u0633\u0644 \u0627\u0644\u0637\u0628\u064a\u0639\u064a",
        history=history,
    )


def test_hard_shift_on_semantic_sku_change():
    history = [{"direction": "in", "body": "\u0623\u0628\u063a\u0649 \u0627\u0644\u0627\u0633\u062a\u0641\u0633\u0627\u0631 \u0639\u0646 \u0627\u0644\u0639\u0633\u0644"}]
    verdict = evaluate_hard_topic_shift(
        "\u0645\u062a\u0649 \u064a\u062a\u0648\u0641\u0631 \u0639\u0633\u0644 \u0627\u0644\u0633\u062f\u0631",
        history=history,
    )
    assert verdict.detected
    assert verdict.reason == "availability_ask"
    assert verdict.new_topic == "product_availability"


def test_hard_shift_on_payment_after_product_clarify():
    state = MerchantConversationState(
        turn=3,
        recent_topic="general_attribute",
        recent_topic_turn=2,
    )
    history = [{"direction": "in", "body": "\u0623\u0628\u063a\u0649 \u0627\u0644\u0627\u0633\u062a\u0641\u0633\u0627\u0631 \u0639\u0646 \u0627\u0644\u0639\u0633\u0644"}]
    verdict = evaluate_hard_topic_shift(
        "\u0648\u064a\u0646 \u0631\u0642\u0645 \u0627\u0644\u062d\u0633\u0627\u0628 \u0644\u0644\u062a\u062d\u0648\u064a\u0644",
        history=history,
        state=state,
    )
    assert verdict.detected
    assert verdict.new_topic == "payment_intent"


def test_hard_shift_on_delivery_after_product_inquiry():
    history = [{"direction": "in", "body": "\u0623\u0628\u063a\u0649 \u0627\u0644\u0627\u0633\u062a\u0641\u0633\u0627\u0631 \u0639\u0646 \u0627\u0644\u0639\u0633\u0644"}]
    verdict = evaluate_hard_topic_shift(
        "\u0641\u064a\u0647 \u062a\u0648\u0635\u064a\u0644\u061f",
        history=history,
    )
    assert verdict.detected
    assert verdict.new_topic == "delivery_intent"


def test_delivery_ack_after_delivery_question_not_hard_shift():
    history = [{"direction": "in", "body": "\u0641\u064a\u0647 \u062a\u0648\u0635\u064a\u0644\u061f"}]
    assert not detect_hard_topic_shift("\u0627\u064a\u0648\u0647", history=history)
