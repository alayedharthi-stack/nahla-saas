"""
backend/tests/test_asset_promise_sanitizer.py
─────────────────────────────────────────────
Regression suite for ``maybe_scrub_unkept_asset_promise`` — the
wire-layer guard that prevents the AI from claiming "I will send
you the link / number / barcode / location" when no corresponding
asset is actually queued for the outbound dispatch.

Production trigger (May 2026 P1, Tenant 33): after the progressive-
selling rewrite the LLM started saying things like:

  * "أرسل لك الرابط بعد التأكد منه"   (no URL in reply)
  * "تفضل رقم أبو هشام"                 (no phone digits / call card)
  * "امسح الباركود من تطبيق الراجحي"   (no [MEDIA_KEY:...] attached)

Each of those leaves the customer waiting for an asset that never
arrives. The new guard rewrites the offending span to a neutral
copy that asks a clarifying question instead.

Three invariants under test:

  1. Promise + matching asset present → text passes through unchanged.
  2. Promise + matching asset MISSING → text is rewritten.
  3. No promise at all → text passes through, no rewrite.

We also assert false-positive resilience on neutral Arabic prose
that happens to contain the verbs but no promise context.
"""
from __future__ import annotations

import os
import sys

import pytest

# Ensure ``backend/`` is on the path under pytest's repo-root collection.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


# ── contains_promised_asset (pure predicate) ────────────────────────────────


@pytest.mark.parametrize(
    "text,expected_class",
    [
        ("أرسل لك الرابط بعد التأكد منه", "link"),
        ("تفضل الرابط 🌷", "link"),
        ("أعطيك الرابط حالاً", "link"),
        ("راح أرسل لك رابط المتجر", "link"),
        ("تفضل الباركود 🌷", "barcode"),
        ("أرسل لك الباركود", "barcode"),
        ("هذا باركود الراجحي", "barcode"),
        ("تفضل رقم أبو هشام", "phone"),
        ("أرسل لك رقم التواصل", "phone"),
        ("هذا رقم الإدارة", "phone"),
        ("تفضل الموقع على الخريطة", "location"),
        ("أرسل لك موقع الفرع", "location"),
        ("سأرسل لك الرابط", "link"),
        ("بأرسل لك الباركود", "barcode"),
    ],
)
def test_contains_promised_asset_detects_canonical_phrasings(
    text: str, expected_class: str,
) -> None:
    """Each canonical Arabic promise pattern must be classified
    deterministically. ``contains_promised_asset`` is the predicate
    the rest of the guard relies on."""
    from core.outbound_sanitizer import contains_promised_asset

    assert contains_promised_asset(text) == expected_class, (
        f"expected {expected_class!r} for: {text!r}"
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "السلام عليكم 🌷",
        "حياك الله، أهلاً وسهلاً",
        "نشحن إلى جميع مدن المملكة عبر سمسا.",
        "متى يصل الطلب؟",
        # Past-tense + plural-third-person — historical fact, not a promise.
        "أرسلنا لكم الرابط في الرسالة السابقة",
        "العميل أرسل الرقم أمس",
        # Question shape — asking the customer.
        "هل تريد الرابط؟",
    ],
)
def test_contains_promised_asset_no_false_positives(text: str) -> None:
    from core.outbound_sanitizer import contains_promised_asset

    assert contains_promised_asset(text) is None, (
        f"should NOT classify a promise in: {text!r}"
    )


# ── maybe_scrub_unkept_asset_promise — happy path (asset present) ──────────


def test_promise_passes_through_when_url_present() -> None:
    """A link promise is honest when an https URL is in the same text."""
    from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise

    text = "تفضل الرابط: https://example.sa/products/123 🌷"
    out, scrubbed, asset_class = maybe_scrub_unkept_asset_promise(
        text,
        has_url=True, has_media=False, has_phone=False,
        has_product_card=False,
    )
    assert out == text
    assert scrubbed is False
    assert asset_class == "link"


def test_promise_passes_through_when_product_card_queued() -> None:
    """A link promise is honest when a product card carrying a CTA URL
    is queued (even if the text body itself has no raw URL)."""
    from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise

    text = "تفضل رابط المنتج 🌷"
    out, scrubbed, _ = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False, has_media=False, has_phone=False,
        has_product_card=True,
    )
    assert out == text
    assert scrubbed is False


def test_promise_passes_through_when_media_queued() -> None:
    """A barcode promise is honest when at least one media attachment
    is queued for the same outbound dispatch."""
    from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise

    text = "تفضل الباركود 🌷 امسحه من تطبيق الراجحي."
    out, scrubbed, asset_class = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False, has_media=True, has_phone=False,
    )
    assert out == text
    assert scrubbed is False
    assert asset_class == "barcode"


def test_promise_passes_through_when_phone_in_text() -> None:
    """A phone promise is honest when the Saudi mobile digits are
    actually in the reply text."""
    from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise

    text = "تفضل رقم أبو هشام: 0501234567"
    out, scrubbed, _ = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False, has_media=False, has_phone=True,
    )
    assert out == text
    assert scrubbed is False


def test_promise_passes_through_when_call_target_queued() -> None:
    """A phone promise is honest when a contact card is queued (even
    without explicit digits in the text)."""
    from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise

    text = "تفضل رقم أبو هشام، راح يخدمك"
    out, scrubbed, _ = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False, has_media=False,
        has_phone=True,  # caller passes True when _call_targets is non-empty
    )
    assert out == text
    assert scrubbed is False


# ── maybe_scrub_unkept_asset_promise — scrub path (asset missing) ──────────


def test_link_promise_without_url_gets_scrubbed() -> None:
    """The Tenant 33 production case: the AI said it would send the
    link, but no URL was in the reply and no product card was queued.
    The guard must rewrite the promise span to a neutral copy."""
    from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise

    text = (
        "أهلاً بك 🌷 أرسل لك الرابط بعد التأكد منه."
    )
    out, scrubbed, asset_class = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False, has_media=False, has_phone=False,
        has_product_card=False,
    )
    assert scrubbed is True
    assert asset_class == "link"
    assert "أرسل لك الرابط" not in out
    # The neutral replacement carries the production-safe copy.
    assert "تكفي لحظة" in out or "التفاصيل الكاملة" in out
    # The intro ("أهلاً بك 🌷") should survive — we only rewrite the
    # offending span, not the whole reply.
    assert "أهلاً" in out


def test_barcode_promise_without_media_gets_scrubbed() -> None:
    """The Tenant 33 Rajhi case: AI said "تفضل الباركود" but no media
    was attached — rewrite to a neutral copy that doesn't promise
    delivery."""
    from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise

    text = "تفضل الباركود 🌷 امسحه من تطبيق الراجحي."
    out, scrubbed, asset_class = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False, has_media=False, has_phone=False,
    )
    assert scrubbed is True
    assert asset_class == "barcode"
    assert "تفضل الباركود" not in out


def test_phone_promise_without_phone_gets_scrubbed() -> None:
    """The Tenant 33 staff-contact case: AI said "تفضل رقم أبو هشام"
    but no digits were emitted and no contact card was queued."""
    from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise

    text = "تفضل رقم أبو هشام، يخدمك بالتفصيل."
    out, scrubbed, asset_class = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False, has_media=False, has_phone=False,
    )
    assert scrubbed is True
    assert asset_class == "phone"
    assert "تفضل رقم أبو هشام" not in out


def test_location_promise_without_link_gets_scrubbed() -> None:
    from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise

    text = "تفضل الموقع على الخريطة 🌷"
    out, scrubbed, asset_class = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False, has_media=False, has_phone=False,
        has_product_card=False,
    )
    assert scrubbed is True
    assert asset_class == "location"


# ── Standalone intro shape ("الرابط:" on its own line) ─────────────────────


def test_standalone_intro_without_asset_gets_scrubbed() -> None:
    """The LLM sometimes emits a header line followed by an empty body
    where the URL was supposed to go (the marker didn't resolve). The
    standalone-intro pattern catches that shape."""
    from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise

    text = "أكيد 🌷\nالرابط:\n"
    out, scrubbed, asset_class = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False, has_media=False, has_phone=False,
    )
    assert scrubbed is True
    assert asset_class == "link"


# ── No-promise replies pass through ────────────────────────────────────────


def test_neutral_reply_with_no_promise_passes_through() -> None:
    from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise

    text = "حياك الله 🌷 وش أقدر أخدمك فيه؟"
    out, scrubbed, asset_class = maybe_scrub_unkept_asset_promise(
        text,
        has_url=False, has_media=False, has_phone=False,
    )
    assert out == text
    assert scrubbed is False
    assert asset_class is None


def test_empty_input_is_safe() -> None:
    from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise

    out, scrubbed, asset_class = maybe_scrub_unkept_asset_promise(
        "",
        has_url=False, has_media=False, has_phone=False,
    )
    assert out == ""
    assert scrubbed is False
    assert asset_class is None
