"""
tests/test_media_framing_wave0.py
──────────────────────────────────
ARCH-MEDIA-001 Phase 2 Wave 0 — framing strip + vision query stoplist.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.product_visual import (  # noqa: E402
    extract_visual_product_query,
    is_product_visual_request,
    strip_bot_media_framing,
)
from modules.ai.brain.intent.rules import match as match_intent  # noqa: E402
from modules.ai.brain.types import INTENT_PRODUCT_VISUAL_REQUEST  # noqa: E402

CAR_KEY_VISION = (
    "[وصف الصورة المرسلة] نوع المحتوى: صورة عامة.\n\n"
    "يظهر في الصورة مفتاح سيارة فضي على خلفية داكنة."
)

RANDOM_IMAGE_VISION = (
    "[وصف الصورة المرسلة] نوع المحتوى: صورة عامة.\n\n"
    "شخص يحمل هاتفاً في ممر مكتب."
)

CAPTIONED_PRODUCT_IMAGE = (
    "هذا اللي أبغاه\n\n"
    "[وصف الصورة] عبوة عسل سدر على رف خشبي."
)


class TestStripBotMediaFraming:
    def test_removes_framing_keeps_vision_body(self):
        stripped = strip_bot_media_framing(CAR_KEY_VISION)
        assert "[وصف الصورة" not in stripped
        assert "مفتاح سيارة" in stripped
        assert "نوع المحتوى" in stripped

    def test_keeps_customer_caption(self):
        stripped = strip_bot_media_framing(CAPTIONED_PRODUCT_IMAGE)
        assert stripped.startswith("هذا اللي أبغاه")
        assert "عسل سدر" in stripped


class TestCarKeyAndRandomImages:
    def test_car_key_not_product_visual_intent(self):
        intent = match_intent(CAR_KEY_VISION)
        assert intent is None or intent.name != INTENT_PRODUCT_VISUAL_REQUEST

    def test_car_key_not_visual_request_helper(self):
        assert not is_product_visual_request(CAR_KEY_VISION)

    def test_car_key_no_catalog_query_extracted(self):
        assert extract_visual_product_query(CAR_KEY_VISION) == ""

    def test_random_image_not_product_visual_intent(self):
        intent = match_intent(RANDOM_IMAGE_VISION)
        assert intent is None or intent.name != INTENT_PRODUCT_VISUAL_REQUEST

    def test_random_image_no_vision_stoplist_query(self):
        assert extract_visual_product_query(RANDOM_IMAGE_VISION) == ""


class TestExplicitProductVisualRequests:
    def test_named_talh_visual_intent(self):
        intent = match_intent("أبي أشوف صورة الطلح")
        assert intent is not None
        assert intent.name == INTENT_PRODUCT_VISUAL_REQUEST

    def test_named_talh_visual_helper(self):
        assert is_product_visual_request("أبي أشوف صورة الطلح")

    def test_named_talh_query_extraction(self):
        assert "طلح" in extract_visual_product_query("أبي أشوف صورة الطلح")

    def test_voice_stt_variants_still_visual(self):
        """Same contract as test_product_visual_dispatch — helper detection."""
        for msg in (
            "ابي اشوف الصوره",
            "ابغا الصوره",
            "وين الصوره",
            "ورني شكله",
        ):
            assert is_product_visual_request(msg), msg


class TestVisionQueryStoplist:
    def test_stoplist_tokens_never_extracted(self):
        for token in (
            "المحتوى",
            "نوع المحتوى",
            "صورة عامة",
            "وصف الصورة",
        ):
            msg = f"[وصف الصورة المرسلة] نوع المحتوى: {token}."
            assert extract_visual_product_query(msg) == ""

    def test_framing_only_does_not_fake_visual_intent(self):
        msg = "[وصف الصورة المرسلة] نوع المحتوى: صورة عامة."
        assert not is_product_visual_request(msg)
        intent = match_intent(msg)
        assert intent is None or intent.name != INTENT_PRODUCT_VISUAL_REQUEST
