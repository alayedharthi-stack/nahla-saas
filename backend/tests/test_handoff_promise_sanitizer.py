"""
backend/tests/test_handoff_promise_sanitizer.py
────────────────────────────────────────────────
May 2026 P1 regression suite for the "verbal handoff without state"
bug. The smoking gun was a hard-coded string in the loop-guard pause
branch of the webhook (``whatsapp_webhook.py:5623-5626``) that
promised a human handoff to the customer but only flipped
``ai_paused`` under REASON_BOT_LOOP — none of the canonical handoff
flags (``status='human'``, ``is_human_handoff``, ``needs_human``,
``handoff_active``) were raised, so the conversation never appeared
in the dashboard's "طلب موظف" inbox and the AI silently resumed on
the next inbound. From the customer's POV: the AI made a promise it
didn't keep.

Three layers of fix:

  1. Loop-guard branch now flips every canonical flag + creates a
     HandoffSession row + pauses AI under REASON_HUMAN_HANDOFF.
  2. Persona prompt (``nahla_persona.py:91``) no longer encourages
     the LLM to emit "راح أحوّل المحادثة" as a sample reply.
  3. Wire-layer sanitizer (``core.outbound_sanitizer.
     maybe_scrub_handoff_promise``) rewrites any outbound text that
     promises a handoff when the conversation flags don't back it up.

These tests cover layer 3 + the conservative-rewrite behaviour.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — wire-layer scrub
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        # The exact production string from Tenant 33 incident.
        "أشوف إنه فيه شيء أحتاج فهمه أكثر — سأحوّل المحادثة "
        "لفريق المتجر الآن وسيرد عليك أحد الموظفين قريباً 🌷",
        # The persona-prompted variant.
        "أكيد 🙏 راح أحوّل المحادثة لفريق المتجر الآن.",
        # The owner-fallback-detector variants.
        "سأحوّلك للفريق وراح يردون عليك قريبًا.",
        "أحوّلك للفريق الآن.",
        "احولك للفريق",
        "الفريق راح يتواصل معك قريبًا.",
        "راح يتواصل معك أحد الموظفين.",
        "سيتواصل معك الفريق خلال دقائق.",
        "تم تحويل المحادثة لفريق المتجر.",
        "سيرد عليك أحد الموظفين قريبًا.",
    ],
)
def test_contains_handoff_promise_detects_canonical_phrases(phrase: str) -> None:
    """The detector must catch the canonical Arabic phrase family that
    the persona prompt + loop-guard branch + LLM paraphrases produce.
    A miss here is a silent leak — the wire-layer scrub never fires
    and the AI's false promise lands in the customer's WhatsApp."""
    from core.outbound_sanitizer import contains_handoff_promise

    match = contains_handoff_promise(phrase)
    assert match is not None, f"missed handoff promise in: {phrase!r}"


@pytest.mark.parametrize(
    "phrase",
    [
        "أكيد 🌷 هذا المنتج متوفر، تحب نكمل الطلب؟",
        "الشحن متوفر 🚚 وغالباً يوصل خلال 2–4 أيام عمل.",
        "تفضل باركود الراجحي 🌷",
        # An ack that does NOT promise a transfer.
        "وصلت رسالتك 🌷 خبرني وش تحتاج بالتفصيل.",
        # Empty / falsy inputs.
        "",
    ],
)
def test_contains_handoff_promise_no_false_positives(phrase: str) -> None:
    from core.outbound_sanitizer import contains_handoff_promise

    assert contains_handoff_promise(phrase) is None


def test_scrub_honest_promise_when_state_active() -> None:
    """When handoff state IS already active, the text is honest — let
    it through unchanged. The sanitizer's job is to prevent FALSE
    promises, not to mute legitimate ones."""
    from core.outbound_sanitizer import maybe_scrub_handoff_promise

    text = (
        "أكيد 🌷 سأحوّل المحادثة لفريق المتجر الآن، "
        "وراح يتواصل معك أحد الموظفين."
    )
    out, scrubbed = maybe_scrub_handoff_promise(
        text, handoff_state_active=True, tenant_id=33,
    )
    assert out == text
    assert scrubbed is False


def test_scrub_false_promise_when_state_inactive() -> None:
    """The smoking gun: handoff text without backing state must be
    rewritten to a neutral acknowledgement (no transfer claim)."""
    from core.outbound_sanitizer import maybe_scrub_handoff_promise

    text = (
        "أشوف إنه فيه شيء أحتاج فهمه أكثر — سأحوّل المحادثة "
        "لفريق المتجر الآن وسيرد عليك أحد الموظفين قريباً 🌷"
    )
    out, scrubbed = maybe_scrub_handoff_promise(
        text, handoff_state_active=False, tenant_id=33,
    )
    assert scrubbed is True
    assert "سأحوّل" not in out
    assert "سيرد عليك أحد الموظفين" not in out
    assert "أحوّلك" not in out
    # The replacement should still ACK the message — we don't want the
    # AI to look silent. Just no false transfer claim.
    assert len(out) > 10
    assert "وصلت رسالتك" in out or "أخبر فريق المتجر" in out


def test_scrub_passes_through_neutral_replies() -> None:
    """When the text is fine, the scrub is a no-op — the boolean must
    stay False so observers (tests, logs) can distinguish "didn't
    fire" from "fired but kept the text"."""
    from core.outbound_sanitizer import maybe_scrub_handoff_promise

    text = "تفضل باركود الراجحي 🌷 تقدر تسدد مباشرة من تطبيق الراجحي."
    out, scrubbed = maybe_scrub_handoff_promise(
        text, handoff_state_active=False, tenant_id=33,
    )
    assert out == text
    assert scrubbed is False


def test_scrub_handles_empty_input() -> None:
    """Defensive: never raise on falsy input. Caller treats the empty
    string as "nothing to scrub"."""
    from core.outbound_sanitizer import maybe_scrub_handoff_promise

    out, scrubbed = maybe_scrub_handoff_promise(
        "", handoff_state_active=False, tenant_id=33,
    )
    assert out == ""
    assert scrubbed is False


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — payment-query regex now accepts bare باركود
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        "ابي الباركود",        # The exact Tenant 33 phrasing.
        "ابي الباركود",
        "أبي الباركود",
        "أحتاج الباركود",
        "ودي بالباركود",
        "ممكن باركود",
        "رمز الدفع",
        "رمز التحويل",
        "رمز السداد",
        "QR code",
        "كيوار",
        # Still match the bank-qualified form (no regression).
        "ابي باركود الراجحي",
        "احتاج باركود التحويل",
        "باركود الدفع",
    ],
)
def test_payment_query_regex_accepts_bare_barcode(phrase: str) -> None:
    """Pre-fix the regex required ``باركود`` to be followed by
    ``التحويل|الدفع|البنك|الراجحي``, so a bare 'ابي الباركود' missed
    the legacy hard-override path. Fix: accept the bare noun too as a
    safety net (the modern media_key registry handles the same
    phrasing via ``is_generic_payment_barcode_query``, but having the
    legacy override as backup matters when the merchant's media row
    still has ``media_key=NULL``)."""
    from core.ai_libraries import is_payment_query

    assert is_payment_query(phrase), (
        f"is_payment_query should match: {phrase!r}"
    )


@pytest.mark.parametrize(
    "phrase",
    [
        "السلام عليكم",
        "كيف الأسعار؟",
        "ابي عسل السمر",
        # Bare "ابي" without barcode shouldn't trip the regex.
        "ابي أعرف الشحن",
    ],
)
def test_payment_query_regex_no_false_positive(phrase: str) -> None:
    from core.ai_libraries import is_payment_query

    assert not is_payment_query(phrase), (
        f"is_payment_query should NOT match: {phrase!r}"
    )
