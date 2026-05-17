"""
tests/test_handoff_detector.py
───────────────────────────────
Coverage for the pre-brain handoff detector used by the WhatsApp
webhook. The detector MUST fire deterministically on explicit
"transfer me to a human" phrasings AND on post-payment modification
requests, while NOT misfiring on unrelated commerce text.

The webhook integration test
(``tests/test_webhook_pre_brain_handoff_smoke``) exercises the full
DB-touching guard path; this file isolates the pure-string detector
so refactors there can move fast without spinning up SQLAlchemy.
"""

from __future__ import annotations

import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ────────────────────────────────────────────────────────────────────
# normalize_arabic_text
# ────────────────────────────────────────────────────────────────────


def test_normalize_collapses_alef_variants() -> None:
    from core.handoff_detector import normalize_arabic_text

    assert (
        normalize_arabic_text("أبي إكلم آحد ٱنا")
        == normalize_arabic_text("ابي اكلم احد انا")
    )


def test_normalize_strips_diacritics_and_tatweel() -> None:
    from core.handoff_detector import normalize_arabic_text

    assert (
        normalize_arabic_text("مَرْحَبًاـــ")
        == normalize_arabic_text("مرحبا")
    )


def test_normalize_collapses_yaa_and_ta_marbuta() -> None:
    from core.handoff_detector import normalize_arabic_text

    assert (
        normalize_arabic_text("مكتبة الخدمة الكاملةى")
        == normalize_arabic_text("مكتبه الخدمه الكاملهي")
    )


def test_normalize_lowercases_latin() -> None:
    from core.handoff_detector import normalize_arabic_text

    assert normalize_arabic_text("Customer SUPPORT") == "customer support"


def test_normalize_empty_input() -> None:
    from core.handoff_detector import normalize_arabic_text

    assert normalize_arabic_text(None) == ""
    assert normalize_arabic_text("") == ""
    assert normalize_arabic_text("   ") == ""


# ────────────────────────────────────────────────────────────────────
# is_handoff_request — POSITIVE cases (must fire)
# ────────────────────────────────────────────────────────────────────


def test_handoff_fires_on_canonical_saudi_phrases() -> None:
    """Every phrase in the production-observed list must classify
    as a handoff request after Arabic normalisation."""
    from core.handoff_detector import is_handoff_request

    samples = (
        # The exact phrase from the production WhatsApp screenshot
        "ابي اتكلم مع احد",
        "أبي أتكلم مع أحد",
        "ابغى اتكلم مع احد",
        # Asks for staff explicitly
        "ابي موظف",
        "أبي موظف",
        "ابغى موظف",
        "ابي مختص",
        "ابغى مختص",
        "ابي مسؤول",
        # Transfer me
        "حولني على موظف",
        "حولني لموظف",
        "حولني للموظف",
        "حولني للمشرف",
        # Call me back
        "كلموني",
        "كلميني",
        "كلمني",
        "كلموني لو سمحتم",
        "اتصلوا بي",
        "ردوا علي",
        # Is anyone there
        "احد يرد علي",
        "في احد يرد",
        "فيه احد يرد علي",
        "محد رد علي",
        "ماحد رد علي",
        "ما حد رد",
        # Direct "talk to / want to talk"
        "ابي اكلم احد",
        "ابغى اكلم احد",
        "ابي اكلم موظف",
        "ابغى اكلم موظف",
        "ودي اكلم احد",
        "ابي اتكلم مع موظف",
        "ابي اتكلم مع مختص",
        "ابي اتكلم مع مسؤول",
        "ابي اتكلم مع شخص",
        "ابي اتكلم مع انسان",
        # Need a human
        "احتاج موظف",
        "احتاج مختص",
        "اريد التحدث مع موظف",
        # English fallbacks
        "talk to a human",
        "I need support",
        "customer service please",
    )
    for s in samples:
        assert is_handoff_request(s), f"Handoff should fire for {s!r}"


# ────────────────────────────────────────────────────────────────────
# is_handoff_request — NEGATIVE cases (must NOT fire)
# ────────────────────────────────────────────────────────────────────


def test_handoff_does_not_fire_on_commerce_phrases() -> None:
    """Sanity: ordinary commerce phrases must not classify as
    handoff requests — those keep flowing into the brain."""
    from core.handoff_detector import is_handoff_request

    samples = (
        "السلام عليكم",
        "ابي عسل سدر",
        "كم السعر",
        "وين الفرع",
        "ابي اشتري",
        "ابي اعرف الاسعار",
        "متى الشحن",
        "بكم العسل",
        # Tricky negative: "موظف" inside an unrelated phrase
        "انا موظف في الشركه",
        "انا موظف لدى ارامكو",
    )
    for s in samples:
        assert not is_handoff_request(s), (
            f"Handoff should NOT fire for {s!r}"
        )


# ────────────────────────────────────────────────────────────────────
# is_post_payment_modification_request — POSITIVE cases
# ────────────────────────────────────────────────────────────────────


def test_post_payment_modification_fires_on_add_phrases() -> None:
    """Add-product phrasings must classify as a modification
    request — they trigger the post-payment handoff branch when
    the conversation is in a post-paid state."""
    from core.handoff_detector import is_post_payment_modification_request

    samples = (
        "ابي اضيف منتج",
        "ابغى اضيف منتج",
        "ضيف لي عسل سدر",
        "اضيف شي ثاني",
        "اضافه منتج للطلب",
        "ابي اطلب معه عسل برسيم",
    )
    for s in samples:
        assert is_post_payment_modification_request(s), (
            f"Add-product modification should fire for {s!r}"
        )


def test_post_payment_modification_fires_on_modify_phrases() -> None:
    from core.handoff_detector import is_post_payment_modification_request

    samples = (
        "ابي اعدل الطلب",
        "اعدل طلبي",
        "تعديل الطلب",
        "ابي اغير المنتج",
        "غيروا لي الكميه",
        "زيد الكميه",
        "نقص الكميه",
        "بدل المنتج",
    )
    for s in samples:
        assert is_post_payment_modification_request(s), (
            f"Modify-order request should fire for {s!r}"
        )


def test_post_payment_modification_fires_on_cancel_phrases() -> None:
    from core.handoff_detector import is_post_payment_modification_request

    samples = (
        "ابي احذف المنتج",
        "احذف منتج",
        "الغي الطلب",
        "إلغاء الطلب",
        "ابي الغي",
        "تراجع عن الطلب",
        "ما ابغى الطلب",
    )
    for s in samples:
        assert is_post_payment_modification_request(s), (
            f"Cancel-order request should fire for {s!r}"
        )


# ────────────────────────────────────────────────────────────────────
# is_post_payment_modification_request — NEGATIVE cases
# ────────────────────────────────────────────────────────────────────


def test_post_payment_modification_does_not_fire_on_unrelated_text() -> None:
    """Don't fire on plain commerce / status questions. Those keep
    going to the brain regardless of whether payment has been made."""
    from core.handoff_detector import is_post_payment_modification_request

    samples = (
        "السلام عليكم",
        "كم السعر",
        "متى يوصل الطلب",
        "وين الطلب",
        "تم التحويل",
        "هذا الايصال",
        "شكرا",
    )
    for s in samples:
        assert not is_post_payment_modification_request(s), (
            f"Modification detector should NOT fire for {s!r}"
        )
