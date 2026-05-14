"""Unit tests for the WhatsApp link → CTA button normaliser."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from core.wa_link_buttons import (  # noqa: E402
    classify_url,
    extract_first_cta_url,
    split_text_for_cta_buttons,
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


# ─────────────────── split_text_for_cta_buttons (multi-URL) ──────────────────
# Production bug 2026-05-14: customer asked for two products
# ("أبي سمر وطلح") and the bot returned a single message containing
# both product URLs. WhatsApp only renders the first URL as a CTA
# button — the second was left as raw flat text. The splitter is the
# wire-layer defence that turns multi-URL replies into one message
# per product so every link becomes a proper CTA button.


def test_split_no_urls_returns_single_plain_message():
    out = split_text_for_cta_buttons("ممتاز، أرسل لي رقم جوالك للتأكيد.")
    assert len(out) == 1
    assert out[0].cta is None
    assert out[0].body == "ممتاز، أرسل لي رقم جوالك للتأكيد."


def test_split_single_url_matches_legacy_extract_shape():
    """Single-URL replies must keep the byte-identical shape the
    legacy ``extract_first_cta_url`` path produced — we do NOT want
    to perturb existing single-product flows."""
    text = "هذا منتج العسل: https://store.example.com/products/talh-honey"
    out = split_text_for_cta_buttons(text)
    legacy = extract_first_cta_url(text)

    assert len(out) == 1
    assert legacy is not None
    assert out[0].cta is not None
    assert out[0].cta.url == legacy.classification.url
    assert out[0].cta.button_title == legacy.classification.button_title
    assert out[0].body == legacy.cleaned_text


def test_split_two_products_paragraph_separated_produces_two_ctas():
    """The exact production-screenshot scenario: customer asked for
    "سمر وطلح" and the bot replied with two labelled URLs separated
    by blank lines. Expected: TWO CTA messages (one per product) plus
    optional intro / follow-up plain-text messages."""
    text = (
        "ممتاز يا محيس 👍\n"
        "\n"
        "سمر الحجاز:\n"
        "https://store.example.com/products/sammar-hijaz\n"
        "\n"
        "الطلح البلدي:\n"
        "https://store.example.com/products/talh-baladi\n"
        "\n"
        "وش الحجم اللي يناسبك من كل واحد"
    )
    out = split_text_for_cta_buttons(text)

    ctas = [m for m in out if m.cta is not None]
    plains = [m for m in out if m.cta is None]

    # TWO CTAs — one per product, never bundled.
    assert len(ctas) == 2
    # Each CTA carries the OWN product URL — neither swallows the other.
    cta_urls = {m.cta.url for m in ctas}
    assert any("sammar" in u for u in cta_urls)
    assert any("talh" in u for u in cta_urls)
    # Both classifications are products → "عرض المنتج" button title.
    for m in ctas:
        assert m.cta.kind == "product"
        assert m.cta.button_title == "عرض المنتج"
    # The intro + follow-up survive as separate plain-text messages so
    # the natural-language framing isn't lost.
    plain_bodies = [m.body for m in plains]
    assert any("ممتاز يا محيس" in b for b in plain_bodies)
    assert any("وش الحجم" in b for b in plain_bodies)


def test_split_two_products_one_url_per_label_line():
    """LLM variant where each label sits on the same line as the URL
    (no blank line between products). Splitter must still produce one
    CTA per URL."""
    text = (
        "سمر الحجاز: https://store.example.com/products/sammar-hijaz\n"
        "الطلح البلدي: https://store.example.com/products/talh-baladi"
    )
    out = split_text_for_cta_buttons(text)
    ctas = [m for m in out if m.cta is not None]
    assert len(ctas) == 2
    assert any("sammar" in m.cta.url for m in ctas)
    assert any("talh" in m.cta.url for m in ctas)
    # Each CTA's body keeps the label so the customer knows which
    # product the button belongs to.
    bodies = [m.body for m in ctas]
    assert any("سمر" in b for b in bodies)
    assert any("طلح" in b for b in bodies)
    # No CTA body should still contain the URL — that's the whole
    # point of lifting it into the button.
    for m in ctas:
        assert "https://" not in m.body


def test_split_each_cta_body_never_contains_other_url():
    """Hard invariant: a CTA message body must NEVER contain another
    product URL. Otherwise WhatsApp renders the extra URL inline next
    to the button, which is the exact symptom we're fixing."""
    text = (
        "سمر الحجاز:\n"
        "https://store.example.com/products/sammar\n"
        "\n"
        "الطلح البلدي:\n"
        "https://store.example.com/products/talh"
    )
    out = split_text_for_cta_buttons(text)
    for m in out:
        if m.cta is None:
            continue
        own_url = m.cta.url
        # The body is allowed to be empty/default OR to mention the
        # product name, but NOT to contain another https:// URL.
        for char_seq in ("https://", "http://"):
            if char_seq in m.body:
                # The only acceptable case is if the body's URL is the
                # SAME as the CTA's URL — but the splitter strips it.
                # Any other URL is a regression.
                assert m.body.count("https://") == 1
                assert own_url in m.body, (
                    f"CTA body contained a URL that doesn't match its "
                    f"own CTA: body={m.body!r}, cta_url={own_url!r}"
                )


def test_split_three_products_produces_three_ctas():
    """Customer asks for three products at once — every one gets a
    dedicated CTA, no bundling, no silent drops."""
    text = (
        "خياراتنا:\n\n"
        "سمر: https://store.example.com/products/sammar\n\n"
        "طلح: https://store.example.com/products/talh\n\n"
        "سدر: https://store.example.com/products/sidr"
    )
    out = split_text_for_cta_buttons(text)
    ctas = [m for m in out if m.cta is not None]
    assert len(ctas) == 3
    urls = {m.cta.url for m in ctas}
    assert any("sammar" in u for u in urls)
    assert any("talh" in u for u in urls)
    assert any("sidr" in u for u in urls)


def test_split_mixed_product_and_payment_urls():
    """A reply that includes both a product link AND a payment link
    must still produce one CTA per URL with the correct classification
    + button title."""
    text = (
        "تفاصيل المنتج:\n"
        "https://store.example.com/products/honey-1kg\n"
        "\n"
        "للدفع:\n"
        "https://checkout.tap.company/pay/abc123"
    )
    out = split_text_for_cta_buttons(text)
    ctas = [m for m in out if m.cta is not None]
    assert len(ctas) == 2
    kinds = {m.cta.kind for m in ctas}
    assert kinds == {"product", "payment"}


def test_split_empty_string_returns_single_empty_message():
    out = split_text_for_cta_buttons("")
    assert len(out) == 1
    assert out[0].cta is None
    assert out[0].body == ""


def test_split_url_only_text_provides_default_body():
    """If the body would be empty after stripping the URL, the
    splitter substitutes a per-kind default body so the WhatsApp
    interactive payload doesn't reject an empty body field."""
    out = split_text_for_cta_buttons(
        "https://store.example.com/products/honey-1kg"
    )
    assert len(out) == 1
    assert out[0].cta is not None
    assert out[0].body  # non-empty


def test_split_preserves_order_of_urls():
    """When the customer sees the messages in order, the FIRST URL in
    the source text must arrive as the FIRST CTA — otherwise the
    label-to-CTA mapping is wrong from the customer's perspective."""
    text = (
        "سمر: https://store.example.com/products/A\n"
        "طلح: https://store.example.com/products/B\n"
        "سدر: https://store.example.com/products/C"
    )
    out = split_text_for_cta_buttons(text)
    ctas = [m for m in out if m.cta is not None]
    assert ctas[0].cta.url.endswith("/A")
    assert ctas[1].cta.url.endswith("/B")
    assert ctas[2].cta.url.endswith("/C")
