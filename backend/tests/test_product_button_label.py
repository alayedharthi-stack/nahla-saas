# -*- coding: utf-8 -*-
"""Tests for compact WhatsApp product reply-button titles."""
from core.product_button_label import (
    WA_REPLY_BUTTON_TITLE_MAX,
    compact_whatsapp_product_button_title,
)


class TestCompactWhatsappProductButtonTitle:
    def test_long_honey_title_with_weight(self):
        title = "\u0639\u0633\u0644 \u0637\u0644\u062d \u0646\u062c\u062f \u0627\u0644\u0628\u0631\u064a \u0625\u0646\u062a\u0627\u062c \u0645\u0646\u062d\u0644\u0646\u0627 1 \u0643\u064a\u0644\u0648"
        label = compact_whatsapp_product_button_title(title)
        assert len(label) <= WA_REPLY_BUTTON_TITLE_MAX
        assert "\u0637\u0644\u062d" in label
        assert "1" in label
        assert "\u0643\u062c\u0645" in label
        assert "\u0625\u0646\u062a\u0627\u062c" not in label

    def test_samar_with_year_and_weight_prefers_weight(self):
        title = "\u0639\u0633\u0644 \u0633\u0645\u0631 \u0627\u0644\u062d\u062c\u0627\u0632 \u0625\u0646\u062a\u0627\u062c 1446 \u0648\u0632\u0646 5 \u0643\u064a\u0644\u0648"
        label = compact_whatsapp_product_button_title(title)
        assert len(label) <= WA_REPLY_BUTTON_TITLE_MAX
        assert "\u0633\u0645\u0631" in label
        assert "5" in label
        assert "\u0643\u062c\u0645" in label

    def test_short_title_without_weight_gets_honey_prefix(self):
        title = "\u0639\u0633\u0644 \u0627\u0644\u0637\u0644\u062d"
        label = compact_whatsapp_product_button_title(title)
        assert len(label) <= WA_REPLY_BUTTON_TITLE_MAX
        assert "\u0637\u0644\u062d" in label

    def test_no_price_in_label(self):
        title = "\u0639\u0633\u0644 \u0633\u0645\u0631 1 \u0643\u064a\u0644\u0648"
        label = compact_whatsapp_product_button_title(title)
        assert "ر" not in label or "\u0631\u064a\u0627\u0644" not in label
        assert "150" not in label

    def test_english_edition_series(self):
        title = "Edition Series legacy harvest 500 g"
        label = compact_whatsapp_product_button_title(title)
        assert len(label) <= WA_REPLY_BUTTON_TITLE_MAX
        assert "edition" in label.lower() or "500" in label
