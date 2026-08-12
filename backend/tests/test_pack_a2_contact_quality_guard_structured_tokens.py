"""Pack A2 — structured contact tokens must survive Arabic English-strip scoring."""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in (_backend, os.path.join(_backend, "..")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.compose.templates import faq_owner_contact
from modules.ai.brain.postprocess.commerce_reply_quality_guard import (
    _LATIN_WORD_RE,
    _ARABIC_CHAR_RE,
    _mask_structured_fact_tokens_for_language_scoring,
    _segment_is_primarily_english,
    _strip_english_from_arabic_reply,
    apply_commerce_reply_quality_guard,
)


# Production-equivalent owner_contact block (latin token inflation from URLs/keys).
CONTACT_FAQ = (
    "تقدر تتواصل معنا عبر:\n"
    "البريد: hello@demo.example\n"
    "twitter: https://twitter.com/demo_store\n"
    "youtube: https://www.youtube.com/c/DemoStore\n"
    "telegram: DemoStoreTG\n"
    "instagram: demo_store\n"
    "appstore_link: https://apps.apple.com/sa/app/demo-store/id1148458340\n"
    "googleplay_link: https://play.google.com/store/apps/details?id=com.demo.store&hl=en&gl=US\n"
    "رابط المتجر: https://demo.example/store-a"
)

EMPTY_REPLY = "تعذّرت صياغة الرد الآن — أعد رسالتك لو يناسبك."


def _contact_guard(reply: str, *, inbound: str = "كيف أتواصل معكم؟") -> str:
    return apply_commerce_reply_quality_guard(
        reply,
        inbound_text=inbound,
        intent_name="ask_owner_contact",
        primary_customer_goal="",
        decision_topic="owner_contact",
        chosen_path="rule",
        tenant_id=7,
        llm_candidate_present=False,
    ).reply


class TestStructuredTokenScoring:
    def test_rca_shape_before_mask_looks_english(self) -> None:
        latin = len(_LATIN_WORD_RE.findall(CONTACT_FAQ))
        arabic = len(_ARABIC_CHAR_RE.findall(CONTACT_FAQ))
        assert latin > arabic  # live defect shape: structured Latin inflates score
        # Raw latin count would trip primarily-English; masking must deflate it.
        unmasked_would_strip = latin >= 3 and latin >= arabic
        assert unmasked_would_strip is True
        masked = _mask_structured_fact_tokens_for_language_scoring(CONTACT_FAQ)
        masked_latin = len(_LATIN_WORD_RE.findall(masked))
        masked_arabic = len(_ARABIC_CHAR_RE.findall(masked))
        assert masked_latin < latin
        assert not (masked_latin >= 3 and masked_latin >= masked_arabic)
        assert _segment_is_primarily_english(CONTACT_FAQ) is False

    def test_strip_keeps_contact_block(self) -> None:
        cleaned, stripped = _strip_english_from_arabic_reply(CONTACT_FAQ)
        assert cleaned
        assert "hello@demo.example" in cleaned
        assert "https://demo.example/store-a" in cleaned
        assert "instagram" in cleaned.lower() or "demo_store" in cleaned
        # May report stripped=False when no English prose remains after scoring.
        assert stripped is False or "Let me" not in cleaned


class TestContactQualityGuardSurvival:
    def test_arabic_plus_email_survives(self) -> None:
        raw = "إيميلنا هو:\nhello@demo.example"
        out = _contact_guard(raw, inbound="وش إيميلكم؟")
        assert "hello@demo.example" in out
        assert out.strip() != EMPTY_REPLY

    def test_arabic_plus_url_survives(self) -> None:
        raw = "رابط المتجر:\nhttps://demo.example/store-a"
        out = _contact_guard(raw, inbound="وش رابط المتجر؟")
        assert "https://demo.example/store-a" in out

    def test_arabic_plus_social_handle_survives(self) -> None:
        raw = "حسابنا:\n@demo_store"
        out = _contact_guard(raw, inbound="عندكم حسابات تواصل؟")
        assert "@demo_store" in out

    def test_arabic_plus_multiple_social_urls_survives(self) -> None:
        out = _contact_guard(CONTACT_FAQ)
        assert "hello@demo.example" in out
        assert "https://twitter.com/demo_store" in out
        assert "https://www.youtube.com/c/DemoStore" in out
        assert "demo_store" in out
        assert out.strip() != EMPTY_REPLY
        assert "وش المنتج" not in out

    def test_faq_owner_contact_template_survives_guard(self) -> None:
        text = faq_owner_contact(
            contact_phone="",
            contact_email="a@example.com",
            store_url="https://social.example/a",
            social_links={
                "instagram": "https://instagram.com/store_a",
                "twitter": "https://x.com/store_a",
            },
        )
        out = _contact_guard(text)
        assert "a@example.com" in out
        assert "https://instagram.com/store_a" in out
        assert "https://x.com/store_a" in out
        assert "الجوال:" not in out

    def test_dual_tenant_values_not_confused(self) -> None:
        a = faq_owner_contact(
            contact_email="a@example.com",
            social_links={"instagram": "https://social.example/a"},
        )
        b = faq_owner_contact(
            contact_email="b@example.com",
            social_links={"instagram": "https://social.example/b"},
        )
        out_a = _contact_guard(a)
        out_b = _contact_guard(b)
        assert "a@example.com" in out_a and "b@example.com" not in out_a
        assert "b@example.com" in out_b and "a@example.com" not in out_b
        assert "social.example/a" in out_a and "social.example/b" not in out_a
        assert "social.example/b" in out_b and "social.example/a" not in out_b

    def test_absent_phone_not_invented(self) -> None:
        text = faq_owner_contact(
            contact_phone="",
            contact_email="hello@demo.example",
            social_links={"instagram": "demo_store"},
        )
        out = _contact_guard(text)
        assert "الجوال:" not in out
        assert "05" not in out


class TestEnglishProseStillStripped:
    def test_long_english_prose_still_removed(self) -> None:
        raw = (
            "حياك الله.\n\n"
            "Please note that we currently offer same day delivery only for "
            "selected VIP customers and this paragraph is entirely English "
            "prose that must remain suppressed by the quality guard policy."
        )
        out = apply_commerce_reply_quality_guard(
            raw,
            inbound_text="هل عندكم توصيل؟",
            intent_name="ask_shipping",
            tenant_id=7,
        ).reply
        assert "Please note" not in out
        assert "same day delivery" not in out.lower()
        assert "VIP customers" not in out

    def test_english_prose_with_url_still_stripped(self) -> None:
        raw = (
            "Please visit our partner portal for more details about the "
            "international shipping policy and customer service hours at "
            "https://arbitrary.example/policy and then email us later."
        )
        out = apply_commerce_reply_quality_guard(
            raw,
            inbound_text="هل عندكم توصيل؟",
            intent_name="ask_shipping",
            tenant_id=7,
            llm_candidate_present=False,
        ).reply
        assert "Please visit" not in out
        assert "international shipping policy" not in out.lower()
        # Entire primarily-English segment is dropped (URL alone does not save prose).
        assert "Please" not in out

    def test_arabic_plus_url_plus_english_prose_keeps_arabic_and_url(self) -> None:
        raw = (
            "رابط المتجر:\n"
            "https://demo.example/store-a\n\n"
            "Please note that we currently offer same day delivery only for "
            "selected VIP customers and this paragraph is English prose."
        )
        out = apply_commerce_reply_quality_guard(
            raw,
            inbound_text="وش رابط المتجر؟",
            intent_name="ask_store_info",
            decision_topic="store_info",
            chosen_path="rule",
            tenant_id=7,
        ).reply
        assert "https://demo.example/store-a" in out
        assert "رابط المتجر" in out
        assert "Please note" not in out
        assert "VIP customers" not in out

    def test_powered_by_residue_still_stripped(self) -> None:
        out = apply_commerce_reply_quality_guard(
            "متوفر حالياً.\nPowered by Nahla",
            inbound_text="هل عندكم عسل؟",
            intent_name="ask_product",
            tenant_id=7,
        ).reply
        assert "Powered by Nahla" not in out
        assert "متوفر" in out
