# -*- coding: utf-8 -*-
"""Tests for compact WhatsApp product reply-button titles (merchant-agnostic)."""
from core.product_button_label import (
    WA_REPLY_BUTTON_TITLE_MAX,
    compact_whatsapp_product_button_title,
)


def _assert_wa_title(label: str) -> None:
    assert isinstance(label, str)
    assert 0 < len(label) <= WA_REPLY_BUTTON_TITLE_MAX


class TestCompactWhatsappProductButtonTitle:
    def test_single_token_clothing_keeps_catalog_token(self):
        label = compact_whatsapp_product_button_title("جاكيت")
        _assert_wa_title(label)
        assert "جاكيت" in label
        # Must not invent a honey-domain prefix.
        assert not label.startswith("عسل")

    def test_two_token_clothing_keeps_both(self):
        label = compact_whatsapp_product_button_title("قميص قطني")
        _assert_wa_title(label)
        assert "قميص" in label
        assert "قطني" in label or "قطن" in label

    def test_long_title_respects_wa_cap(self):
        title = (
            "سماعات لاسلكية مقاومة للماء مع علبة شحن سريعة "
            "ونظام إلغاء ضوضاء نشط إصدار برو"
        )
        label = compact_whatsapp_product_button_title(title)
        _assert_wa_title(label)
        assert label.split()[0]

    def test_title_with_weight_keeps_identity_and_weight(self):
        title = "عسل طلح نجد البري إنتاج منحلنا 1 كيلو"
        label = compact_whatsapp_product_button_title(title)
        _assert_wa_title(label)
        assert "طلح" in label
        assert "1" in label
        assert "كجم" in label
        assert "إنتاج" not in label

    def test_title_with_year_and_weight_prefers_weight(self):
        title = "عسل سمر الحجاز إنتاج 1446 وزن 5 كيلو"
        label = compact_whatsapp_product_button_title(title)
        _assert_wa_title(label)
        assert "سمر" in label
        assert "5" in label
        assert "كجم" in label

    def test_year_only_suffix_when_no_weight(self):
        title = "كتاب التاريخ الحديث طبعة 2024"
        label = compact_whatsapp_product_button_title(title)
        _assert_wa_title(label)
        assert "2024" in label

    def test_honey_title_keeps_honey_token_from_catalog(self):
        title = "عسل الطلح"
        label = compact_whatsapp_product_button_title(title)
        _assert_wa_title(label)
        assert "طلح" in label
        # Honey word retained from catalog data when present (not stripped).
        assert "عسل" in label

    def test_electronics_single_token(self):
        label = compact_whatsapp_product_button_title("لابتوب")
        _assert_wa_title(label)
        assert "لابتوب" in label
        assert "عسل" not in label

    def test_no_price_in_label(self):
        title = "عسل سمر 1 كيلو"
        label = compact_whatsapp_product_button_title(title)
        assert "150" not in label
        assert "ريال" not in label

    def test_english_edition_series(self):
        title = "Edition Series legacy harvest 500 g"
        label = compact_whatsapp_product_button_title(title)
        _assert_wa_title(label)
        assert "edition" in label.lower() or "500" in label

    def test_empty_title(self):
        assert compact_whatsapp_product_button_title("") == ""
        assert compact_whatsapp_product_button_title("   ") == ""

    def test_multi_tenant_same_helper_different_catalog_titles(self):
        """Same helper, different merchants' titles — no cross-domain leakage."""
        clothing = compact_whatsapp_product_button_title("جاكيت")
        honey = compact_whatsapp_product_button_title("عسل سدر")
        electronics = compact_whatsapp_product_button_title("راوتر")
        for label in (clothing, honey, electronics):
            _assert_wa_title(label)
        assert "عسل" not in clothing
        assert "عسل" in honey
        assert "عسل" not in electronics
        assert clothing != honey
        assert clothing != electronics
