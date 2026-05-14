"""Unit tests for the WhatsApp link → CTA button normaliser."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from core.wa_link_buttons import (  # noqa: E402
    classify_url,
    extract_first_cta_url,
)


# ─────────────────────────── classify_url ────────────────────────────
def test_classify_product_path():
    c = classify_url("https://nahlah.salla.sa/products/talh-honey")
    assert c.kind == "product"
    assert c.button_title == "عرض المنتج"
    assert c.url.endswith("/talh-honey")


def test_classify_payment_by_path():
    c = classify_url("https://store.example.com/checkout?token=abc")
    assert c.kind == "payment"
    assert c.button_title == "إتمام الدفع"


def test_classify_payment_by_host():
    c = classify_url("https://checkout.tap.company/pay/abc123")
    assert c.kind == "payment"


def test_classify_tracking():
    c = classify_url("https://www.aramex.com/track/results?ShipmentNumber=XYZ")
    assert c.kind == "tracking"
    assert c.button_title == "تتبع الطلب"


def test_classify_location_google_maps():
    c = classify_url("https://maps.app.goo.gl/abcDEF")
    assert c.kind == "location"
    assert c.button_title == "موقع المتجر"


def test_classify_general_fallback():
    c = classify_url("https://wa.me/966555906901")
    assert c.kind == "general"


def test_classify_store_homepage_salla():
    # Salla storefront root URL — should lift into "افتح المتجر" CTA so the
    # FAQ store_info template never leaks a 200-char URL inline. Customers
    # who ask "رابط المتجر" must see a button, not raw text.
    c = classify_url("https://nahlah.salla.sa/")
    assert c.kind == "store"
    assert c.button_title == "افتح المتجر"


def test_classify_store_homepage_matches_configured_domain():
    c = classify_url("https://shop.example.com", store_domain="shop.example.com")
    assert c.kind == "store"
    assert c.button_title == "افتح المتجر"


def test_classify_product_path_beats_store_classification():
    # Deep links to a product page must keep the product CTA so we don't
    # downgrade "/products/talh-honey" to a generic "open store" button.
    c = classify_url("https://nahlah.salla.sa/products/talh-honey")
    assert c.kind == "product"


def test_extract_store_homepage_provides_friendly_fallback_body():
    out = extract_first_cta_url("https://nahlah.salla.sa/")
    assert out is not None
    assert out.classification.kind == "store"
    assert out.cleaned_text  # WhatsApp requires non-empty body
    assert "متجر" in out.cleaned_text


def test_button_title_clamped_to_20_chars():
    c = classify_url("https://store.example.com/products/x")
    assert len(c.button_title) <= 20


# ─────────────────────────── extract_first_cta_url ────────────────────
def test_extract_strips_url_from_body_product():
    text = "هذا عسل الطلح البلدي 🌿 مناسب لمن يبحث عن طعم قوي.\nالرابط: https://store.example.com/products/talh-honey"
    out = extract_first_cta_url(text)
    assert out is not None
    assert out.classification.kind == "product"
    assert "https://" not in out.cleaned_text
    assert "talh" not in out.cleaned_text  # URL fully removed
    assert "عسل الطلح" in out.cleaned_text


def test_extract_keeps_inline_arabic_intro():
    text = "تفضل رابط الدفع: https://checkout.tap.company/pay/xyz"
    out = extract_first_cta_url(text)
    assert out is not None
    assert out.classification.kind == "payment"
    assert "https://" not in out.cleaned_text
    # The trailing colon after "الدفع" gets cleaned up.
    assert not out.cleaned_text.rstrip().endswith(":")


def test_extract_returns_none_when_no_url():
    out = extract_first_cta_url("شكراً جزيلاً، هل تحب أعرض لك منتج آخر؟")
    assert out is None


def test_extract_provides_fallback_body_when_text_was_only_url():
    out = extract_first_cta_url("https://store.example.com/products/abc")
    assert out is not None
    assert out.cleaned_text  # never empty (WhatsApp requires body.text)
    assert out.classification.kind == "product"


def test_extract_only_takes_first_url_when_multiple():
    text = (
        "رابط المنتج: https://store.example.com/products/talh\n"
        "ورابط الدفع لاحقاً: https://checkout.tap.company/pay/xyz"
    )
    out = extract_first_cta_url(text)
    assert out is not None
    assert out.classification.kind == "product"
    # Second URL must remain in the body so the customer can still see it.
    assert "checkout.tap.company" in out.cleaned_text


def test_extract_strips_trailing_punctuation_in_url():
    text = "هذا الرابط: https://store.example.com/products/talh-honey."
    out = extract_first_cta_url(text)
    assert out is not None
    # The trailing dot must NOT end up appended to the button URL.
    assert not out.classification.url.endswith(".")
