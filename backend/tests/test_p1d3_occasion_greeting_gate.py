"""P1-D-3 regression: occasion / holiday greeting gate."""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.fallback_policy import strip_closer_segments  # noqa: E402
from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.intent.non_commerce_classifier import (  # noqa: E402
    NC_EID_GREETING,
    classify_non_commerce,
    inbound_has_occasion_signal,
)
from modules.ai.brain.postprocess.occasion_reply_guard import (  # noqa: E402
    apply_occasion_reply_guard,
)

_LIVE_EID_TEMPLATE = (
    "كل عام وأنت بخير 🌹\nالله يجعل أيامكم مباركة."
)

_FORBIDDEN_OCCASION = (
    "كل عام",
    "عيدكم",
    "أيامكم مباركة",
    "تقبل الله",
)

_HONEY_VIDEO_TEXT = (
    "[تصنيف الوسائط: محتوى اجتماعي/ديني — بدون نية شراء]\n"
    "[وصف الفيديو] عسل سدر طبيعي للبيع. بارك الله فيكم."
)

_OPERATIONAL_SAMPLES = (
    "طلبك *عسل سدر* تحت المراجعة — ببلّغك فور التأكيد 🌷",
    "السعر 120 ريال للكيلو شامل الضريبة.",
    "رقم التتبع: 1234567890 — الشحنة في الطريق.",
)


class TestOccasionSignal:
    def test_honey_sale_not_occasion_signal(self) -> None:
        assert inbound_has_occasion_signal("عسل سدر طبيعي للبيع. بارك الله فيكم.") is False

    def test_explicit_eid_is_occasion_signal(self) -> None:
        assert inbound_has_occasion_signal("عيدكم مبارك") is True
        assert inbound_has_occasion_signal("كل عام وأنتم بخير") is True


class TestClassifierCommerceVeto:
    def test_honey_product_with_barakah_not_eid_greeting(self) -> None:
        m = classify_non_commerce(
            "عسل سدر طبيعي للبيع. بارك الله فيكم.",
            media_type="video",
            topic_hints=["نحل_أو_عسل", "دعاء_أو_تهنئة"],
        )
        assert m is None

    def test_honey_topic_hint_vetoes_without_eid_signal(self) -> None:
        m = classify_non_commerce(
            _HONEY_VIDEO_TEXT,
            media_type="video",
            topic_hints=["نحل_أو_عسل"],
        )
        assert m is None

    def test_explicit_eid_customer_text_classifies(self) -> None:
        m = classify_non_commerce("عيدكم مبارك")
        assert m is not None
        assert m.category == NC_EID_GREETING

    def test_kull_am_reciprocal_allowed(self) -> None:
        m = classify_non_commerce("كل عام وأنتم بخير")
        assert m is not None
        assert m.category == NC_EID_GREETING


class TestSocialReplyGate:
    def test_eid_pool_blocked_without_occasion_signal(self) -> None:
        for variant in range(len(T._SOCIAL_EID_GREETING_VARIANTS)):
            reply = T.social_reply(
                category="eid_greeting",
                variant=variant,
                sub_variant=0,
                inbound_text="عسل سدر طبيعي للبيع",
            )
            assert reply == ""

    def test_eid_pool_allowed_with_occasion_signal(self) -> None:
        reply = T.social_reply(
            category="eid_greeting",
            variant=3,
            sub_variant=0,
            inbound_text="كل عام وأنت بخير",
        )
        assert reply == _LIVE_EID_TEMPLATE

    def test_dua_pool_blocked_without_signal(self) -> None:
        reply = T.social_reply(
            category="dua",
            variant=0,
            inbound_text="بارك الله فيكم وعسل للبيع",
        )
        assert reply == ""


class TestOccasionReplyGuard:
    def test_live_template_stripped_without_inbound_occasion(self) -> None:
        result = apply_occasion_reply_guard(
            _LIVE_EID_TEMPLATE,
            inbound_text="عسل للبيع",
            tenant_id=1,
        )
        assert result.stripped is True
        for phrase in _FORBIDDEN_OCCASION:
            assert phrase not in result.reply

    def test_live_template_kept_when_customer_greeted(self) -> None:
        result = apply_occasion_reply_guard(
            _LIVE_EID_TEMPLATE,
            inbound_text="كل عام وأنت بخير",
            tenant_id=1,
        )
        assert result.stripped is False
        assert "كل عام" in result.reply


class TestOperationalPreserved:
    @pytest.mark.parametrize("sample", _OPERATIONAL_SAMPLES)
    def test_operational_untouched_by_occasion_guard(self, sample: str) -> None:
        result = apply_occasion_reply_guard(sample, inbound_text="تمام", tenant_id=1)
        assert result.stripped is False
        assert result.reply == sample

    @pytest.mark.parametrize("sample", _OPERATIONAL_SAMPLES)
    def test_operational_untouched_by_closer_strip(self, sample: str) -> None:
        cleaned, stripped = strip_closer_segments(sample)
        assert stripped is False
        assert cleaned == sample


class TestCampaignUntouched:
    def test_seasonal_campaign_template_still_present(self) -> None:
        from services.whatsapp_templates import nahla_templates  # noqa: PLC0415

        keys = [t.get("key") for t in nahla_templates.NAHLA_TEMPLATES]
        assert "seasonal_offer_template" in keys
