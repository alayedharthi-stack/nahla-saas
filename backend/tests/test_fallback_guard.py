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
    detect_semantic_dead_end,
    fallback_fingerprint,
    is_recent_topic_active,
    resolve_active_topic,
    should_block_fallback_repeat,
    stamp_recent_topic,
)
from modules.ai.brain.commerce.solution_seeking import (  # noqa: E402
    contextual_non_product_clarification,
    detect_solution_seeking_suppression,
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
