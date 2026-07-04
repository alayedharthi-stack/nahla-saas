"""Phase A.1 — social checkout pressure guard regressions."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.postprocess.social_checkout_pressure_guard import (  # noqa: E402
    apply_social_checkout_pressure_guard,
    is_checkout_pressure_line,
    is_pure_phatic_bypass_turn,
    strip_checkout_pressure_segments,
)
from modules.ai.brain.social_human_context import compute_social_human_context  # noqa: E402
from modules.ai.brain.types import Intent, MerchantConversationState  # noqa: E402
from modules.ai.order_flow_v2.state import should_resume_checkout_on_greeting  # noqa: E402


@pytest.mark.parametrize(
    "line",
    [
        "أرسل عنوانك",
        "وش طريقة الدفع المناسبة لك؟",
        "أعتمد التوصيل لعنوانك",
        "نكمل طلبك السابق",
        "محتاج اسمك الكامل عشان نكمل الطلب",
        "اسمك الكامل لو تكرمت؟",
        "نحتاج اسمك الكامل عشان نخلص الطلب",
    ],
)
def test_checkout_pressure_lines_detected(line: str) -> None:
    assert is_checkout_pressure_line(line)


def test_gentle_open_order_hint_not_pressure() -> None:
    assert not is_checkout_pressure_line(
        "وعندك طلب سابق موجود، نكمله متى ما تحب."
    )


@pytest.mark.parametrize(
    "inbound",
    [
        "شكراً",
        "الله يعطيك العافية",
        "كيف الحال",
        "السلام عليكم",
    ],
)
def test_pure_phatic_bypass_turns_detected(inbound: str) -> None:
    assert is_pure_phatic_bypass_turn(inbound)


def test_guard_strips_address_after_dua_inbound() -> None:
    result = apply_social_checkout_pressure_guard(
        "الله يعافيك 🌷\nأرسل عنوانك",
        inbound_text="الله يعطيك العافية",
    )
    assert result.stripped
    assert "أرسل عنوان" not in result.reply
    assert "الله يعافيك" in result.reply


def test_guard_strips_payment_after_thanks_inbound() -> None:
    result = apply_social_checkout_pressure_guard(
        "العفو يا غالي\nوش طريقة الدفع المناسبة لك؟",
        inbound_text="شكراً",
    )
    assert result.stripped
    assert "الدفع" not in result.reply


def test_guard_all_pressure_reply_uses_no_silence_fallback() -> None:
    result = apply_social_checkout_pressure_guard(
        "أرسل عنوانك",
        inbound_text="شكراً",
    )
    assert result.stripped
    assert result.empty_fallback
    assert result.reply.strip()
    assert "أرسل عنوان" not in result.reply


def test_guard_all_pressure_dua_inbound_non_empty() -> None:
    result = apply_social_checkout_pressure_guard(
        "وش طريقة الدفع المناسبة لك؟",
        inbound_text="الله يعطيك العافية",
    )
    assert result.stripped
    assert result.reply.strip()
    assert "الدفع" not in result.reply


@pytest.mark.parametrize(
    ("reply", "inbound"),
    [
        ("أرسل عنوانك", "الله يعطيك العافية"),
        ("وش طريقة الدفع المناسبة لك؟", "شكراً"),
        ("نكمل الدفع؟", "كيف الحال"),
    ],
)
def test_empty_strip_uses_emergency_fallback_not_silence(reply: str, inbound: str) -> None:
    """Full checkout-pressure-only replies must not become reply_len=0 on phatic turns."""
    result = apply_social_checkout_pressure_guard(reply, inbound_text=inbound)
    assert result.stripped
    assert result.empty_fallback
    assert result.reply.strip()
    assert not is_checkout_pressure_line(result.reply)
    assert "أرسل عنوان" not in result.reply
    assert "الدفع" not in result.reply
    assert "نكمل الدفع" not in result.reply


def test_emergency_fallback_is_not_checkout_pressure() -> None:
    result = apply_social_checkout_pressure_guard(
        "أرسل عنوانك",
        inbound_text="شكراً",
    )
    assert result.empty_fallback
    assert result.reason == "phatic_bypass_checkout_pressure_empty_fallback"
    assert "منتج" not in result.reply


def test_guard_ignores_non_phatic_inbound() -> None:
    result = apply_social_checkout_pressure_guard(
        "تمام، وش طريقة الدفع المناسبة لك؟",
        inbound_text="تحويل بنكي",
    )
    assert not result.stripped
    assert "الدفع" in result.reply


def test_strip_preserves_social_segment_only() -> None:
    cleaned, stripped = strip_checkout_pressure_segments(
        "بخير الله يسعدك\nنكمل طلبك السابق. أرسل عنوانك"
    )
    assert stripped
    assert "بخير" in cleaned
    assert "أرسل عنوان" not in cleaned


def test_phatic_thanks_in_ordering_stage_is_pure_social() -> None:
    state = MerchantConversationState(stage="ordering")
    intent = Intent(name="social", confidence=0.9, slots={"social_category": "thanks"})
    shc = compute_social_human_context(
        message="شكراً",
        intent=intent,
        state=state,
    )
    assert shc.is_pure_social_turn
    assert shc.block_commerce_tail
    assert shc.block_commerce_escalation


def test_should_resume_false_for_salaam_with_active_checkout() -> None:
    prep = {
        "order_flow_v2_active": True,
        "line_items": [{"product_name": "حذاء رياضي أبيض", "quantity": 1}],
    }
    assert not should_resume_checkout_on_greeting(
        prep,
        {},
        message="السلام عليكم",
    )


def test_should_resume_true_for_explicit_resume_not_pure_phatic() -> None:
    prep = {
        "order_flow_v2_active": True,
        "line_items": [{"product_name": "حذاء رياضي أبيض", "quantity": 1}],
    }
    assert should_resume_checkout_on_greeting(
        prep,
        {},
        message="كمل الطلب",
    )


# ─── Name-slot pressure (post-#444 smoke regressions) ─────────────────────────


@pytest.mark.parametrize(
    ("inbound", "reply", "kept_fragment"),
    [
        (
            "كيف الحال",
            "الحمد لله تمام 🌷 بس محتاج اسمك الكامل عشان نكمل الطلب بإذن الله 😊",
            "الحمد لله تمام",
        ),
        (
            "شكراً",
            "عفواً يا الغالي 🌷 اسمك الكامل لو تكرمت؟",
            "عفواً يا الغالي",
        ),
        (
            "الله يعطيك العافية",
            "الله يعافيك 😊 اسمك الكامل عشان نخلص الطلب؟",
            "الله يعافيك",
        ),
        (
            "انت وش أخبارك؟",
            "الحمد لله بخير 🌷 بس نحتاج اسمك الكامل عشان نكمل معك، ممكن؟",
            "الحمد لله بخير",
        ),
    ],
)
def test_guard_strips_name_slot_pressure_from_smoke_replies(
    inbound: str, reply: str, kept_fragment: str
) -> None:
    result = apply_social_checkout_pressure_guard(reply, inbound_text=inbound)
    assert result.stripped
    assert result.reply.strip()
    assert kept_fragment in result.reply
    assert "اسمك الكامل" not in result.reply
    assert "نكمل الطلب" not in result.reply
    assert "نخلص الطلب" not in result.reply
    assert not result.reply.rstrip().endswith("بس")
    assert not result.reply.rstrip().endswith("لكن")


def test_guard_tail_strip_leaves_clean_social_prefix_not_dangling_bس() -> None:
    result = apply_social_checkout_pressure_guard(
        "الحمد لله تمام 🌷 بس محتاج اسمك الكامل عشان نكمل الطلب بإذن الله 😊",
        inbound_text="كيف الحال",
    )
    assert result.reply == "الحمد لله تمام 🌷"
    assert "بس" not in result.reply


def test_guard_name_only_pressure_uses_no_silence_fallback() -> None:
    result = apply_social_checkout_pressure_guard(
        "اسمك الكامل لو تكرمت؟",
        inbound_text="شكراً",
    )
    assert result.stripped
    assert result.reply.strip()
    assert "اسمك الكامل" not in result.reply


def test_guard_ignores_explicit_name_slot_answer() -> None:
    result = apply_social_checkout_pressure_guard(
        "تمام يا هشام، وش طريقة الدفع المناسبة لك؟",
        inbound_text="اسمي هشام العتيبي",
    )
    assert not result.stripped
    assert "هشام" in result.reply
    assert "الدفع" in result.reply


def test_guard_ignores_checkout_continuation_yes() -> None:
    result = apply_social_checkout_pressure_guard(
        "تمام، نكمل الدفع؟",
        inbound_text="نعم",
    )
    assert not result.stripped
    assert "نكمل الدفع" in result.reply


def test_guard_output_has_no_generic_product_placeholder() -> None:
    result = apply_social_checkout_pressure_guard(
        "محتاج اسمك الكامل عشان نكمل الطلب",
        inbound_text="كيف الحال",
    )
    assert result.reply.strip()
    assert "منتج" not in result.reply
