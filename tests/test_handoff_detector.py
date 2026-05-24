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
    silently regress to the generic team line.

    May 2026 #43 polish — merchant feedback on Tenant 33 was that
    the original wording felt "support-gateway" formal. The new
    copy uses Saudi spoken Arabic ("وش الطلب أو المشكلة") and ends
    with action ("مباشرة"). These assertions pin both:
      * The clarifier-question shape (asks for the reason),
      * The honest forwarding promise (no invented team).
    """
    from core.handoff_detector import (
        HANDOFF_ACK_TEXT_AR,
        HANDOFF_OWNER_ACK_TEXT_AR,
    )

    assert HANDOFF_OWNER_ACK_TEXT_AR != HANDOFF_ACK_TEXT_AR, (
        "Owner ack must be distinct from the generic team ack"
    )
    # Must echo the customer's framing — they chose "المالك" not
    # "موظف". The Arabic preposition prefix ("للمالك" / "بالمالك")
    # still counts as echoing the framing, so we accept any
    # standard prefix variant. What we're guarding against is the
    # ack ever using "موظف / فريقنا / فريق المتجر" instead.
    assert any(
        token in HANDOFF_OWNER_ACK_TEXT_AR
        for token in ("المالك", "للمالك", "بالمالك", "صاحب المحل", "صاحب المتجر")
    ), "owner ack must echo the المالك / صاحب المحل framing"
    assert "موظف" not in HANDOFF_OWNER_ACK_TEXT_AR, (
        "owner ack must not redirect to the generic 'موظف' framing"
    )
    # Must ASK for context (clarifier shape — "وش / ما / ممكن"
    # interrogative). Without the clarifier the message is a bare
    # ack and the merchant has to ping the customer twice.
    assert any(
        marker in HANDOFF_OWNER_ACK_TEXT_AR
        for marker in ("وش", "ما هو", "ممكن توضح", "ممكن تعطيني")
    ), "owner ack must ask the customer for the reason"
    # Must promise escalation to management/supervisor — not invent
    # a human team that doesn't exist. Allow the standard Arabic
    # prefix variants (للإدارة / للمسؤول / بالإدارة).
    assert any(
        token in HANDOFF_OWNER_ACK_TEXT_AR
        for token in (
            "الإدارة", "الادارة", "للإدارة", "للادارة", "بالإدارة", "بالادارة",
            "المسؤول", "للمسؤول", "بالمسؤول",
        )
    ), "owner ack must promise forwarding to management/supervisor"
    # New polish (#43): copy must NOT start with a long preamble.
    # The ack reads like a warm one-line answer + a follow-up
    # question, not a corporate ticket form.
    first_line = HANDOFF_OWNER_ACK_TEXT_AR.split("\n", 1)[0]
    assert len(first_line) <= 30, (
        f"first line should be a short warm ack, got: {first_line!r}"
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


# ────────────────────────────────────────────────────────────────────
# Owner-contact tier classifier (May 2026 #44)
# ────────────────────────────────────────────────────────────────────
#
# The tier classifier governs whether the PRE-BRAIN handoff guard
# pauses the AI, flips the full handoff plumbing, or just sends the
# clarifier ack. Three tiers, one decision per turn:
#
#   * VAGUE     — bare owner ask, no reason given.
#   * CLEAR     — owner ask + stated reason ≥ 5 word chars.
#   * COMPLAINT — owner ask + complaint signal (refund / fraud /
#                 formal complaint).
#
# The classifier is pure-string. Tests assert each tier on
# production-observed phrasings + adversarial cases that the merchant
# explicitly worried about ("جربي عسل" looking like a refund verb).


def test_owner_tier_constants_are_pinned_strings() -> None:
    from core.handoff_detector import (
        OWNER_TIER_CLEAR,
        OWNER_TIER_COMPLAINT,
        OWNER_TIER_VAGUE,
    )

    assert OWNER_TIER_VAGUE     == "owner_vague"
    assert OWNER_TIER_CLEAR     == "owner_clear"
    assert OWNER_TIER_COMPLAINT == "owner_complaint"


def test_owner_tier_vague_for_bare_owner_phrases() -> None:
    """Bare owner-contact phrasings without a stated reason map to
    VAGUE. The webhook responds with the clarifier and keeps AI alive."""
    from core.handoff_detector import (
        OWNER_TIER_VAGUE,
        classify_owner_escalation_tier,
    )

    samples = (
        "ابي اتواصل مع المالك",
        "أبي أتواصل مع المالك",
        "ابغى اكلم المالك",
        "ودي اكلم المالك",
        # Pleasantries don't add substance
        "السلام عليكم ابي اكلم المالك",
        "اهلا ابي اكلم المالك",
        # Single-word "صاحب المحل" / "الادارة" with verb but no reason
        "ابي صاحب المحل",
        "ابي اتواصل مع الادارة",
    )
    for s in samples:
        assert classify_owner_escalation_tier(s) == OWNER_TIER_VAGUE, (
            f"Bare owner-contact must classify as VAGUE: {s!r}"
        )


def test_owner_tier_clear_when_reason_is_stated() -> None:
    """Owner-contact + a stated reason → CLEAR tier. Webhook flips
    full handoff but keeps AI alive."""
    from core.handoff_detector import (
        OWNER_TIER_CLEAR,
        classify_owner_escalation_tier,
    )

    samples = (
        "ابي اتواصل مع المالك بخصوص الدفع",
        "ابي اكلم المالك عن مشكله طلبي",
        "ابي اكلم المالك بخصوص ايصال التحويل",
        "ابي اتواصل مع الادارة بخصوص العرض الخاص",
        "ابي اكلم صاحب المحل عن طلب التوصيل المتاخر",
    )
    for s in samples:
        assert classify_owner_escalation_tier(s) == OWNER_TIER_CLEAR, (
            f"Owner-contact + reason must classify as CLEAR: {s!r}"
        )


def test_owner_tier_complaint_for_grievance_signals() -> None:
    """Complaint signal in an owner-contact context → COMPLAINT
    tier. Webhook pauses AI + sends apologetic ack."""
    from core.handoff_detector import (
        OWNER_TIER_COMPLAINT,
        classify_owner_escalation_tier,
    )

    samples = (
        "ابي اكلم المالك هذا غش",
        "ابي اتواصل مع المالك ابي ارد فلوسي",
        "ابي اكلم المالك بشتكي عليكم",
        "ابي اكلم المالك حرام عليكم",
        "ابي اكلم المالك بلغ عنكم لهيئة المستهلك",
        # Standalone bare grievance — even without a "reason" the
        # complaint signal alone is enough.
        "ابي اكلم المالك احتيال",
        # Sensitive case — refund word inside owner-contact context
        "ابغى اتواصل مع الادارة استرداد المبلغ",
    )
    for s in samples:
        assert classify_owner_escalation_tier(s) == OWNER_TIER_COMPLAINT, (
            f"Owner-contact + complaint must classify as COMPLAINT: {s!r}"
        )


def test_complaint_signal_detector_positive_cases() -> None:
    from core.handoff_detector import is_complaint_signal

    samples = (
        "هذا غش",
        "احتيال صريح",
        "ابي ارد فلوسي",
        "ابي استرجاع المنتج",
        "بشتكي عليكم لهيئة المستهلك",
        "بقدم شكوى",
        "حرام عليكم",
        "ظلم والله",
        "scam",
        "i want my money back",
    )
    for s in samples:
        assert is_complaint_signal(s), (
            f"Complaint detector should fire for {s!r}"
        )


def test_complaint_signal_detector_negative_cases() -> None:
    """Don't auto-escalate ambiguous phrases. The brain still gets
    these turns and can craft a context-aware response."""
    from core.handoff_detector import is_complaint_signal

    samples = (
        # Polite questions — no complaint
        "السلام عليكم",
        "كم سعر العسل",
        "وين الفرع",
        # Refund WORDS that aren't complaints in context — these are
        # edge cases the merchant explicitly flagged. We're conservative
        # here: a bare "ارجاع" without "ابي ارجاع المنتج" framing
        # doesn't fire. False positives in this detector trigger an
        # unnecessary AI pause.
        "كم تستغرق سياسة الارجاع",
        # Common Saudi phrases that look like complaints but aren't
        "والله ما اخذت بضاعتي بعد",   # tracking question, not grievance
        "خدعتني ولا لا",               # rhetorical
    )
    for s in samples:
        if is_complaint_signal(s):
            # Some of these may flip when the phrase library expands
            # — but until then, confirm we stay conservative.
            raise AssertionError(
                f"Complaint detector false-positive on neutral phrase: {s!r}"
            )


def test_owner_residue_strips_boilerplate() -> None:
    """The residue helper must remove every owner verb / noun /
    polite filler so the substance threshold is meaningful."""
    from core.handoff_detector import _owner_request_residue

    # Bare owner request leaves nothing of substance after stripping.
    assert _owner_request_residue("أبي أتواصل مع المالك").strip() == ""
    assert _owner_request_residue("ابغى اكلم المالك").strip() == ""
    # Pleasantries get stripped too — VAGUE, not CLEAR.
    assert _owner_request_residue(
        "السلام عليكم ابي اكلم المالك"
    ).strip() == ""
    # Reason words survive the strip — they drive CLEAR tier.
    assert "بخصوص" in _owner_request_residue(
        "ابي اكلم المالك بخصوص الدفع"
    )


# ────────────────────────────────────────────────────────────────────
# Tier-specific ack copy pinning
# ────────────────────────────────────────────────────────────────────


def test_owner_handoff_text_acknowledges_without_pausing_promise() -> None:
    """CLEAR tier ack ('تمام، رفعت طلبك...') must:
      * Confirm forwarding (so the customer knows it landed),
      * Leave the AI door open (because we DON'T pause),
      * NOT promise a specific timing the merchant hasn't approved.
    """
    from core.handoff_detector import HANDOFF_OWNER_HANDOFF_TEXT_AR

    txt = HANDOFF_OWNER_HANDOFF_TEXT_AR
    # Must confirm forwarding
    assert any(token in txt for token in ("رفعت", "وصلني", "وصلنا", "نقلت"))
    # Must keep the conversation open with the AI — invite further
    # parallel questions explicitly.
    assert any(
        marker in txt
        for marker in ("هنا", "أكمل", "تسألين", "تسأل", "احنا")
    ), "CLEAR tier ack must signal the AI is still available"
    # Must NOT use the apologetic complaint wording
    assert "نعتذر" not in txt
    # Must NOT use the bare clarifier "وش الطلب أو المشكلة"
    assert "وش الطلب أو المشكلة" not in txt


def test_owner_complaint_text_is_apologetic_and_action_oriented() -> None:
    """COMPLAINT tier ack must:
      * Open with an apology (no defensiveness),
      * Promise human review (we paused AI — merchant must take over),
      * NOT promise a specific outcome (refund / replacement).
    """
    from core.handoff_detector import HANDOFF_OWNER_COMPLAINT_TEXT_AR

    txt = HANDOFF_OWNER_COMPLAINT_TEXT_AR
    assert any(token in txt for token in ("نعتذر", "اعتذر", "آسف", "متأسف"))
    assert any(
        token in txt
        for token in ("المسؤول", "للمسؤول", "الادارة", "للإدارة", "للادارة")
    )
    # Must NOT auto-promise refund / replacement — that's a merchant
    # decision the AI must not pre-commit.
    assert "نرجع" not in txt
    assert "نسترد" not in txt
    assert "نعيد المبلغ" not in txt
