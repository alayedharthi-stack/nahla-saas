"""
tests/test_greeting_etiquette.py
────────────────────────────────
Universal Arabic salam-return etiquette layer.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.compose.greeting_etiquette import (  # noqa: E402
    SALAM_BARAKA,
    SALAM_BASIC,
    SALAM_RAHMA,
    apply_greeting_etiquette,
    detect_salam_level,
    reply_already_has_salam_return,
    salam_return_text,
)
from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.types import MerchantConversationState  # noqa: E402


def test_detect_simple_salam():
    assert detect_salam_level("السلام عليكم") == SALAM_BASIC


def test_detect_salam_rahma():
    assert detect_salam_level("السلام عليكم ورحمة الله") == SALAM_RAHMA


def test_detect_salam_baraka():
    assert detect_salam_level("السلام عليكم ورحمة الله وبركاته") == SALAM_BARAKA


def test_salam_return_matches_level():
    assert "وبركاته" in salam_return_text(SALAM_BARAKA)
    assert "ورحمة الله" in salam_return_text(SALAM_RAHMA)
    assert salam_return_text(SALAM_BASIC) == "وعليكم السلام 🌷"


def test_prepend_before_intro():
    intro = T.greeting(store_name="متجر", assistant_name="نحلة", variant=0)
    out = apply_greeting_etiquette(intro, "السلام عليكم", MerchantConversationState(turn=1))
    assert out.startswith("وعليكم السلام")
    assert "نحلة" in out
    assert out.count("وعليكم السلام") == 1


def test_no_duplicate_salam():
    reply = "وعليكم السلام 🌷\nحياك الله"
    out = apply_greeting_etiquette(reply, "السلام عليكم", MerchantConversationState(turn=1))
    assert out.count("وعليكم السلام") == 1


def test_salam_plus_commerce_question():
    answer = "سعر العسل يبدأ من 100 ريال."
    msg = "السلام عليكم ورحمة الله كم سعر العسل"
    out = apply_greeting_etiquette(answer, msg, MerchantConversationState(turn=1))
    assert out.startswith("وعليكم السلام ورحمة الله")
    assert "100" in out


def test_repeated_salam_cooldown():
    state = MerchantConversationState(turn=3, last_salam_return_turn=2)
    out = apply_greeting_etiquette("حياك الله", "السلام عليكم", state)
    assert out == "حياك الله"
    assert not reply_already_has_salam_return(out)


def test_greeting_template_with_full_salam():
    intro = T.greeting(store_name="متجر", assistant_name="نحلة", variant=0)
    out = apply_greeting_etiquette(
        intro,
        "السلام عليكم ورحمة الله وبركاته",
        MerchantConversationState(turn=1),
    )
    assert out.startswith("وعليكم السلام ورحمة الله وبركاته")
    assert "نحلة" in out


def test_salam_media_transcript():
    """Voice/OCR transcript carrying salam still triggers return."""
    out = apply_greeting_etiquette(
        "كيف أقدر أساعدك؟",
        "السلام عليكم ورحمة الله",
        MerchantConversationState(turn=1),
    )
    assert out.startswith("وعليكم السلام ورحمة الله")
