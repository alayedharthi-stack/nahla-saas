"""Class 9 PR1 — deterministic quality flag detectors."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.ai_quality_detectors import detect_quality_flags  # noqa: E402


def _meta(**kwargs):
    return dict(kwargs)


class TestPriceQuestionWithPicker:
    def test_flags_picker_reply(self) -> None:
        result = detect_quality_flags(
            inbound_text="كم سعر جاكيت؟",
            reply_text="اختر رقم الخيار:\n1- جاكيت\n2- بنطلون",
            metadata=_meta(
                intent="ask_price",
                question_kind="price",
                decision_action="search_products",
                chosen_path="catalog_picker",
            ),
        )
        assert "price_question_with_picker" in result.flag_ids


class TestGreetingWithCatalogAction:
    def test_flags_catalog_action_on_greeting(self) -> None:
        result = detect_quality_flags(
            inbound_text="السلام عليكم",
            reply_text="عندنا خيارات كثيرة في الكتالوج",
            metadata=_meta(
                intent="greeting",
                decision_action="search_products",
                chosen_path="brain.compose.templates.search_products",
            ),
        )
        assert "greeting_with_catalog_action" in result.flag_ids


class TestOrderLookupWithCatalogAction:
    def test_flags_catalog_on_order_status(self) -> None:
        result = detect_quality_flags(
            inbound_text="طلبي رقم 12345",
            reply_text="اختر رقم المنتج المناسب",
            metadata=_meta(
                intent="track_order",
                decision_action="track_order",
                chosen_path="track_order_status",
            ),
        )
        assert "order_lookup_with_catalog_action" in result.flag_ids


class TestPaymentLinkMissingOrFake:
    def test_flags_missing_url(self) -> None:
        result = detect_quality_flags(
            inbound_text="أرسل رابط الدفع",
            reply_text="حاضر، أكمل الدفع من المتجر",
            metadata=_meta(intent="pay_now", decision_action="send_payment_link"),
        )
        assert "payment_link_missing_or_fake" in result.flag_ids

    def test_flags_fake_url(self) -> None:
        result = detect_quality_flags(
            inbound_text="أرسل رابط الدفع",
            reply_text="تفضل https://pay.example.com/link",
            metadata=_meta(intent="pay_now", decision_action="send_payment_link"),
        )
        assert "payment_link_missing_or_fake" in result.flag_ids


class TestTrackingNumberFakeOrMissing:
    def test_flags_awb_without_evidence(self) -> None:
        result = detect_quality_flags(
            inbound_text="رقم التتبع",
            reply_text="رقم التتبع: AWB123456789",
            metadata=_meta(intent="track_order", decision_action="track_order"),
        )
        assert "tracking_number_fake_or_missing" in result.flag_ids


class TestCheckoutPressureWithoutOrderIntent:
    def test_flags_gratitude_with_address_ask(self) -> None:
        result = detect_quality_flags(
            inbound_text="شكراً",
            reply_text="العفو، ممكن ترسل اسمك والمدينة؟",
            metadata=_meta(intent="social_reply", decision_action="llm_reply"),
        )
        assert "checkout_pressure_without_order_intent" in result.flag_ids


class TestGroundingRewriteAfterGroundedPrice:
    def test_flags_grounding_phrase_with_catalog_price(self) -> None:
        result = detect_quality_flags(
            inbound_text="كم سعر الطلح؟",
            reply_text="ما ظهر عندي سعر مؤكد من الكتالوج الآن.",
            metadata=_meta(
                intent="ask_price",
                question_kind="price",
                price_source="catalog",
                catalog_product_ids=[501],
                chosen_path="catalog_product_answer",
            ),
        )
        assert "grounding_rewrite_after_grounded_price" in result.flag_ids


class TestAvailabilityRewriteAfterCatalogPrice:
    def test_flags_availability_guard_on_price(self) -> None:
        result = detect_quality_flags(
            inbound_text="كم سعر الطلح؟",
            reply_text="عسل الطلح سعره 387 ريال",
            metadata=_meta(
                question_kind="price",
                price_source="catalog",
                catalog_product_ids=[501],
                guards_triggered=["product_availability_truth_guard"],
            ),
        )
        assert "availability_rewrite_after_catalog_price" in result.flag_ids


class TestRepeatedAckTemplate:
    def test_flags_repeated_ack(self) -> None:
        result = detect_quality_flags(
            inbound_text="وين طلبي؟",
            reply_text="تمام وصلت رسالتك",
            metadata=_meta(intent="track_order", decision_action="track_order"),
            recent_outbound_bodies=["تمام وصلت رسالتك 🙏"],
        )
        assert "repeated_ack_template" in result.flag_ids


class TestMissingMetadataForQuality:
    def test_flags_missing_route_metadata(self) -> None:
        result = detect_quality_flags(
            inbound_text="السلام عليكم",
            reply_text="وعليكم السلام",
            metadata=_meta(),
        )
        assert "missing_metadata_for_quality" in result.flag_ids

    def test_no_flag_when_metadata_present(self) -> None:
        result = detect_quality_flags(
            inbound_text="السلام عليكم",
            reply_text="وعليكم السلام",
            metadata=_meta(
                chosen_path="persona_greeting",
                decision_action="llm_reply",
            ),
        )
        assert "missing_metadata_for_quality" not in result.flag_ids
