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


# ────────────────────────────────────────────────────────────────────
# Owner-contact escalation (May 2026 #42)
# ────────────────────────────────────────────────────────────────────
#
# Production regression on Tenant 33: a customer typed
#   "أبي أتواصل مع المالك"
# and got the generic store-intro hallucination instead of the staff-
# escalation flow, because the message did not match any of:
#   * is_handoff_request — no "موظف / احد / حولني" wording,
#   * INTENT_TALK_HUMAN target nouns — "المالك" was not enumerated,
#   * INTENT_ASK_OWNER_CONTACT — required noun-form "التواصل" not the
#     verb form "أتواصل".
# These tests pin the new behaviour so a future refactor can't silently
# drop owner-contact phrasings back into the LLM-hallucination path.


def test_handoff_fires_on_owner_contact_phrasings() -> None:
    """Every "أبي أتواصل/أكلم مع المالك / صاحب المحل / الإدارة /
    المسؤول" phrasing must trigger the PRE-BRAIN handoff guard."""
    from core.handoff_detector import is_handoff_request

    samples = (
        # The exact phrase from the production WhatsApp screenshot
        "ابي اتواصل مع المالك",
        "أبي أتواصل مع المالك",
        "ابغى اتواصل مع المالك",
        # Verb variants
        "ابي اكلم المالك",
        "ابغى اكلم المالك",
        "ودي اكلم المالك",
        "ودي اتواصل مع المالك",
        "ابي احكي مع المالك",
        # Shop-owner / store-owner framing
        "ابي اكلم صاحب المحل",
        "اتواصل مع صاحب المحل",
        "ابي صاحب المتجر",
        "ابي اكلم صاحب المتجر",
        # Management / supervisor framing
        "ابي اتواصل مع الادارة",
        "ابي اكلم الادارة",
        "اكلم الادارة",
        "ابي اكلم المسؤول",
        "اتواصل مع المسؤول",
        # English
        "talk to the owner",
        "speak to management",
        "shop owner please",
        "store owner",
    )
    for s in samples:
        assert is_handoff_request(s), (
            f"Owner-contact phrase should fire is_handoff_request: {s!r}"
        )


def test_owner_contact_request_fires_on_explicit_phrases() -> None:
    """The narrower owner-specific detector must fire on every
    production-observed phrasing — these are the cases that get the
    clarifier-style ack instead of the generic team copy."""
    from core.handoff_detector import is_owner_contact_request

    samples = (
        "أبي أتواصل مع المالك",
        "ابي اتواصل مع المالك",
        "ابغى اكلم المالك",
        "ودي اكلم المالك",
        "ابي اكلم صاحب المحل",
        "اتواصل مع صاحب المتجر",
        "ابي اتواصل مع الادارة",
        "اكلم المسؤول",
        "talk to the owner",
        "speak to management",
        "contact the owner",
    )
    for s in samples:
        assert is_owner_contact_request(s), (
            f"Owner-contact detector should fire for {s!r}"
        )


def test_owner_contact_request_does_not_fire_on_unrelated_text() -> None:
    """Conservative detector — must NOT classify product / shipping
    questions that happen to mention "المالك" or "المسؤول" without an
    explicit contact verb."""
    from core.handoff_detector import is_owner_contact_request

    samples = (
        # Product asks
        "السلام عليكم",
        "ابي عسل سدر",
        "كم سعر العسل",
        "وين الفرع",
        # Negative: "صاحب" appears in many polite phrasings unrelated
        # to escalation. We require an OWNER-NOUN token (المالك /
        # صاحب المحل / صاحب المتجر / الادارة / المسؤول) AND a verb.
        "صاحبي يبي عسل",
        "اشتريت من صاحب الموقع امس",
        # Negative: "ادارة" inside an unrelated context
        "كيف ادارة الطلبات عندكم",
        # Negative: bare "المسؤول" without a verb
        "المسؤول هنا غالي شوي",
        # Negative: explicit handoff for a regular employee, NOT owner
        "ابي اتكلم مع موظف",
        "ابي مختص",
    )
    for s in samples:
        assert not is_owner_contact_request(s), (
            f"Owner-contact detector should NOT fire for {s!r}"
        )


def test_owner_contact_ack_text_is_clarifier_style() -> None:
    """Pin the production-facing copy so a future refactor can't
    silently regress to the generic team line. The merchant
    explicitly asked for a clarifier ('ممكن توضح سبب التواصل؟') so
    they receive WHY the customer wants the owner alongside the
    handoff."""
    from core.handoff_detector import (
        HANDOFF_ACK_TEXT_AR,
        HANDOFF_OWNER_ACK_TEXT_AR,
    )

    assert HANDOFF_OWNER_ACK_TEXT_AR != HANDOFF_ACK_TEXT_AR, (
        "Owner ack must be distinct from the generic team ack"
    )
    assert "سبب التواصل" in HANDOFF_OWNER_ACK_TEXT_AR
    assert "المالك" in HANDOFF_OWNER_ACK_TEXT_AR
    # Must promise escalation to management/supervisor — not invent
    # a human team that doesn't exist.
    assert (
        "الإدارة" in HANDOFF_OWNER_ACK_TEXT_AR
        or "الادارة" in HANDOFF_OWNER_ACK_TEXT_AR
        or "المسؤول" in HANDOFF_OWNER_ACK_TEXT_AR
    )


def test_handoff_still_fires_for_non_owner_phrases() -> None:
    """Regression sanity: extending the substring library for owner
    phrasings must not break the existing 'موظف / مختص / احد' rules."""
    from core.handoff_detector import is_handoff_request

    samples = (
        "ابي اتكلم مع احد",
        "حولني لموظف",
        "كلموني",
        "في احد يرد",
    )
    for s in samples:
        assert is_handoff_request(s), (
            f"Pre-existing handoff phrase regressed: {s!r}"
        )
