"""Regression: CHAT_DEDUP hard-tier must not drop commerce replies on operational delta."""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.dedup_operational_delta import (  # noqa: E402
    extract_operational_slots,
    has_operational_delta_since_last_reply,
)


def _hist(*turns):
    return list(turns)


def test_shipping_then_product_unlocks():
    """City/shipping ask then product + buy intent → new operational delta."""
    history = _hist(
        {"direction": "in", "body": "كيف التوصيل احنا في الرياض"},
        {
            "direction": "out",
            "body": (
                "توجد معلومات متعارضة حول التوفر الحالي للمنتجات. "
                "يرجى التواصل معنا للتأكد من التوفر قبل الطلب."
            ),
        },
    )
    candidate = (
        "المنتج متوفر حالياً في المخزون. سعر الكيلو 80 ريال "
        "شامل التوصيل عبر شركة الشحن."
    )
    assert has_operational_delta_since_last_reply(
        "نبغى نشتري سدر",
        candidate,
        history[-1]["body"],
        history=history,
    )


def test_weight_after_product_unlocks():
    """Weight/size added after product mention → new delta."""
    history = _hist(
        {"direction": "in", "body": "كيف التوصيل احنا في الرياض"},
        {"direction": "out", "body": "التوصيل متاح للرياض عبر شركات الشحن."},
        {"direction": "in", "body": "أبي طلح"},
    )
    candidate = "طلح متوفر. سعر الكيلو 120 ريال."
    assert has_operational_delta_since_last_reply(
        "كيلو طلح",
        candidate,
        history[-1]["body"],
        history=history,
    )
    assert "weight:unit" in extract_operational_slots("كيلو طلح")


def test_true_duplicate_no_unlock():
    """Exact repeat with no new slot → no operational delta."""
    history = _hist(
        {"direction": "in", "body": "أبي سدر"},
        {"direction": "out", "body": "سدر متوفر. سعر الكيلو 200 ريال."},
        {"direction": "in", "body": "أبي سدر"},
    )
    candidate = "سدر متوفر. سعر الكيلو 200 ريال."
    assert not has_operational_delta_since_last_reply(
        "أبي سدر",
        candidate,
        history[1]["body"],
        history=history,
    )


def test_social_greeting_no_commerce_slots():
    """Repeated social greeting carries no operational slots."""
    slots = extract_operational_slots("السلام عليكم")
    assert not slots
    assert not has_operational_delta_since_last_reply(
        "السلام عليكم",
        "وعليكم السلام",
        "وعليكم السلام ورحمة الله",
        history=[
            {"direction": "in", "body": "السلام عليكم"},
            {"direction": "out", "body": "وعليكم السلام ورحمة الله"},
        ],
    )


def test_general_words_not_product_tokens():
    """Ack/commerce filler words must never become product_token slots."""
    generic = ("تمام", "طيب", "اوكي", "موجود", "سعر", "شحن", "توصيل", "كم", "بكم")
    for word in generic:
        slots = extract_operational_slots(f"أبي {word}")
        assert f"product_token:{word}" not in slots, word
        assert not {s for s in slots if s.startswith("product_token:")}

    assert "product_token:سدر" in extract_operational_slots("أبي سدر")
    assert "product_token:طلح" in extract_operational_slots("كيلو طلح")
    loc_slots = extract_operational_slots("الشحن للرياض")
    assert "intent:delivery_intent" in loc_slots
    assert "location:رياض" in loc_slots


def test_empty_candidate_never_unlocks():
    history = _hist(
        {"direction": "in", "body": "كيف التوصيل"},
        {"direction": "out", "body": "التوصيل متاح."},
    )
    assert not has_operational_delta_since_last_reply(
        "نبغى نشتري سدر",
        "",
        history[-1]["body"],
        history=history,
    )


def test_hard_tier_unlock_with_overlap_candidate():
    """Unlock even when overlap math would classify the reply as hard-tier."""
    from modules.ai.brain.commerce.dedup_operational_delta import last_outbound_body
    from routers.whatsapp_webhook import _max_outbound_overlap

    history = [
        {"direction": "in", "body": "كيف التوصيل احنا في الرياض"},
        {
            "direction": "out",
            "body": (
                "توجد معلومات متعارضة حول التوفر الحالي. "
                "تواصل معنا للتأكد من التوفر."
            ),
        },
    ]
    candidate = "المنتج متوفر حالياً. سعر الكيلو 80 ريال شامل التوصيل."
    _max_outbound_overlap(candidate, history)

    assert has_operational_delta_since_last_reply(
        "نبغى نشتري سدر",
        candidate,
        last_outbound_body(history),
        history=history,
    )
