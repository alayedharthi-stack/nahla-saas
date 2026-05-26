"""
tests/test_relational_dedup_suppression.py
──────────────────────────────────────────
Wave 3 (May 2026) — Relational/seasonal-aware dedup suppression
gate tests.

Covers the six scenarios pinned by the merchant directive:

  1. ``test_three_consecutive_duaa_messages_pass_unmodified``
     — three back-to-back religious supplications get the gate to
     fire on every turn; no dedup substitution.
  2. ``test_eid_greeting_does_not_trigger_dedup_fallback``
     — single Eid greeting is enough to fire the seasonal branch.
  3. ``test_transactional_repeated_question_still_triggers_dedup``
     — neutral commerce question (no markers, no relational moment)
     still falls through to the legacy substitution path.
  4. ``test_payment_order_flow_unaffected``
     — TRANSACTIONAL_ACTIVE moment hard-blocks suppression even when
     the inbound text carries a religious marker.
  5. ``test_handoff_path_unaffected``
     — ESCALATION_REQUEST moment hard-blocks suppression. (The webhook
     bypasses the dedup branch entirely on `_brain_handoff`, so this
     test asserts the gate's BLOCK list as a defence-in-depth.)
  6. ``test_kill_switch_off_preserves_legacy_behaviour``
     — flag default OFF → gate is inert and decision is always
     ``suppress=False reason=flag_off``.

Plus architectural invariants:
  * closed enum / closed marker sets,
  * pure function: never raises on garbage input,
  * ``compute_relational_state`` fires the new moments on the right
    inputs and does NOT fire them mid-funnel.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.relational import (  # noqa: E402
    ConversationMoment,
    DedupSuppressionDecision,
    RELIGIOUS_RITUAL_MARKERS,
    SEASONAL_GREETING_MARKERS,
    compute_relational_state,
    is_relational_dedup_suppression_enabled,
    log_dedup_suppression,
    should_suppress_dedup_substitution,
)
from modules.ai.brain.relational.dedup_suppression import (  # noqa: E402
    REASON_FLAG_OFF,
    REASON_MOMENT_BLOCKS,
    REASON_MOMENT_ELIGIBLE,
    REASON_NO_SIGNAL,
    REASON_RELIGIOUS_TEXT,
    REASON_SEASONAL_TEXT,
    text_indicates_religious_ritual,
    text_indicates_seasonal_greeting,
)


_FLAG = "RELATIONAL_DEDUP_SUPPRESSION_ENABLED"


# ── 0. Kill switch defaults OFF ────────────────────────────────────


def test_kill_switch_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    assert is_relational_dedup_suppression_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_kill_switch_truthy_values(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    monkeypatch.setenv(_FLAG, val)
    assert is_relational_dedup_suppression_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_kill_switch_falsy_values(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    monkeypatch.setenv(_FLAG, val)
    assert is_relational_dedup_suppression_enabled() is False


# ── 1. Architectural invariants ────────────────────────────────────


def test_marker_sets_are_closed_and_non_empty() -> None:
    """Marker sets must stay deliberate. If someone widens them
    accidentally (e.g. dumps a regex match), the count regression
    exposes it."""
    assert isinstance(RELIGIOUS_RITUAL_MARKERS, frozenset)
    assert isinstance(SEASONAL_GREETING_MARKERS, frozenset)
    assert 5 < len(RELIGIOUS_RITUAL_MARKERS) <= 60
    assert 5 < len(SEASONAL_GREETING_MARKERS) <= 60
    # All markers were normalised at import time → no diacritics,
    # no taa marbouta, lower case.
    for m in RELIGIOUS_RITUAL_MARKERS | SEASONAL_GREETING_MARKERS:
        assert m == m.lower()
        assert "ة" not in m
        assert "أ" not in m
        assert "إ" not in m


def test_new_moments_present_in_enum() -> None:
    assert ConversationMoment.RELIGIOUS_RITUAL_EXCHANGE.value == "religious_ritual_exchange"
    assert ConversationMoment.SEASONAL_GREETING.value == "seasonal_greeting"


def test_gate_never_raises_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_FLAG, "1")
    # None / empty / weird shapes must all yield a populated decision.
    for inp in (None, "", "  ", "🌷🌷🌷", 12345, object()):
        d = should_suppress_dedup_substitution(
            inbound_text=inp if isinstance(inp, (str, type(None))) else None,
            relational_moment=inp,
            overlap=0.95,
        )
        assert isinstance(d, DedupSuppressionDecision)
        assert isinstance(d.suppress, bool)


def test_gate_with_flag_off_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even the most obvious religious greeting passes through to
    legacy when the flag is off."""
    monkeypatch.delenv(_FLAG, raising=False)
    d = should_suppress_dedup_substitution(
        inbound_text="بارك الله فيك",
        relational_moment=ConversationMoment.RELIGIOUS_RITUAL_EXCHANGE,
        overlap=0.95,
    )
    assert d.suppress is False
    assert d.reason == REASON_FLAG_OFF
    assert d.flag_enabled is False


# ── 2. Headline scenario tests ─────────────────────────────────────


def test_three_consecutive_duaa_messages_pass_unmodified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directive scenario #1. Three religious supplications in a row
    must each be allowed through (gate fires on every turn)."""
    monkeypatch.setenv(_FLAG, "1")
    duaas = [
        "الله يحفظكم ويرعاكم ويبارك في رزقكم",
        "بارك الله فيك يا أخوي وفي عيالك",
        "ربي يجزاك خير ويعطيك الصحة والعافية",
    ]
    for inbound in duaas:
        # No relational moment provided → the gate falls back to the
        # text-marker backstop. This mirrors the "relational layer
        # OFF" deployment topology.
        d_no_moment = should_suppress_dedup_substitution(
            inbound_text=inbound,
            relational_moment=None,
            overlap=0.92,
        )
        assert d_no_moment.suppress is True, inbound
        assert d_no_moment.reason == REASON_RELIGIOUS_TEXT
        assert d_no_moment.matched_marker  # populated

        # And with a relational moment provided (the canonical
        # post-W3.1 deployment) the moment branch fires.
        d_with_moment = should_suppress_dedup_substitution(
            inbound_text=inbound,
            relational_moment=ConversationMoment.RELIGIOUS_RITUAL_EXCHANGE,
            overlap=0.92,
        )
        assert d_with_moment.suppress is True
        assert d_with_moment.reason == REASON_MOMENT_ELIGIBLE


def test_eid_greeting_does_not_trigger_dedup_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directive scenario #2. A seasonal greeting alone is enough."""
    monkeypatch.setenv(_FLAG, "1")
    eid_messages = [
        "كل عام وأنتم بخير 🌷",
        "عيدكم مبارك وكل سنة وأنتم سالمين",
        "تقبل الله طاعتكم وعساكم من عواده",
        "Eid Mubarak from Riyadh",
    ]
    for inbound in eid_messages:
        d = should_suppress_dedup_substitution(
            inbound_text=inbound,
            relational_moment=None,
            overlap=0.91,
        )
        assert d.suppress is True, inbound
        assert d.reason in (REASON_SEASONAL_TEXT, REASON_RELIGIOUS_TEXT), inbound


def test_transactional_repeated_question_still_triggers_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directive scenario #3. A neutral commerce question with no
    markers and no relational moment must fall through to legacy.
    This is the contract that protects the dedup guard for real
    transactional loops."""
    monkeypatch.setenv(_FLAG, "1")
    questions = [
        "كم سعر العسل؟",
        "متى يوصل الطلب",
        "أبي معلومات عن المنتج",
        "وش طريقة التوصيل",
    ]
    for inbound in questions:
        d = should_suppress_dedup_substitution(
            inbound_text=inbound,
            relational_moment=None,
            overlap=0.91,
        )
        assert d.suppress is False, inbound
        assert d.reason == REASON_NO_SIGNAL


def test_payment_order_flow_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Directive scenario #4. Even when the customer adds a religious
    blessing to a mid-funnel turn, TRANSACTIONAL_ACTIVE wins and
    suppression is BLOCKED. Protects the dedup guard for payment /
    order loops."""
    monkeypatch.setenv(_FLAG, "1")
    inbound = "بارك الله فيك، أرسلت الإيصال قبل قليل"
    d = should_suppress_dedup_substitution(
        inbound_text=inbound,
        relational_moment=ConversationMoment.TRANSACTIONAL_ACTIVE,
        overlap=0.96,
    )
    assert d.suppress is False
    assert d.reason == REASON_MOMENT_BLOCKS

    # Also for the dedicated complaint moments — even though they
    # were never routed to the dedup branch in production today,
    # a future router change must not silently start suppressing
    # complaint loops.
    for blocked in (
        ConversationMoment.COMPLAINT_PRODUCT_QUALITY,
        ConversationMoment.COMPLAINT_SHIPPING_DELAY,
        ConversationMoment.COMPLAINT_GENERIC,
        ConversationMoment.RECOVERY_AFTER_FAILURE,
    ):
        d2 = should_suppress_dedup_substitution(
            inbound_text=inbound,
            relational_moment=blocked,
            overlap=0.96,
        )
        assert d2.suppress is False, blocked
        assert d2.reason == REASON_MOMENT_BLOCKS, blocked


def test_handoff_path_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Directive scenario #5. ESCALATION_REQUEST moment is in the
    BLOCK list. (In production the webhook also short-circuits the
    dedup branch on ``_brain_handoff``; this test pins the gate as
    defence-in-depth.)"""
    monkeypatch.setenv(_FLAG, "1")
    inbound = "أبي أتواصل مع المالك بارك الله فيكم"
    d = should_suppress_dedup_substitution(
        inbound_text=inbound,
        relational_moment=ConversationMoment.ESCALATION_REQUEST,
        overlap=0.97,
    )
    assert d.suppress is False
    assert d.reason == REASON_MOMENT_BLOCKS


def test_kill_switch_off_preserves_legacy_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directive scenario #6. With the flag off, the gate refuses
    to suppress regardless of inputs. The legacy dedup substitution
    runs unchanged at the call site."""
    monkeypatch.delenv(_FLAG, raising=False)
    cases: List[tuple] = [
        ("بارك الله فيك", ConversationMoment.RELIGIOUS_RITUAL_EXCHANGE),
        ("كل عام وأنت بخير", ConversationMoment.SEASONAL_GREETING),
        ("شكراً جزيلاً", ConversationMoment.GRATITUDE_GENERIC),
        ("الحمد لله رب العالمين", None),
    ]
    for inbound, moment in cases:
        d = should_suppress_dedup_substitution(
            inbound_text=inbound,
            relational_moment=moment,
            overlap=0.99,
        )
        assert d.suppress is False, (inbound, moment)
        assert d.reason == REASON_FLAG_OFF, (inbound, moment)
        assert d.flag_enabled is False


# ── 3. Eligible-moment list ────────────────────────────────────────


@pytest.mark.parametrize("moment", [
    ConversationMoment.SOCIAL_CHECK_IN,
    ConversationMoment.GRATITUDE_GENERIC,
    ConversationMoment.PRAISE_POST_DELIVERY,
    ConversationMoment.RELIGIOUS_RITUAL_EXCHANGE,
    ConversationMoment.SEASONAL_GREETING,
])
def test_all_eligible_moments_suppress(
    monkeypatch: pytest.MonkeyPatch, moment: ConversationMoment,
) -> None:
    monkeypatch.setenv(_FLAG, "1")
    d = should_suppress_dedup_substitution(
        inbound_text="رد عادي",
        relational_moment=moment,
        overlap=0.9,
    )
    assert d.suppress is True
    assert d.reason == REASON_MOMENT_ELIGIBLE
    assert d.moment_token == moment.value


def test_unknown_moment_token_falls_back_to_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo / drift in the moment token must not silently
    suppress. The gate validates against the closed enum and
    falls through to the text-marker backstop."""
    monkeypatch.setenv(_FLAG, "1")
    d = should_suppress_dedup_substitution(
        inbound_text="بارك الله فيك",
        relational_moment="this_is_not_a_real_moment",
        overlap=0.95,
    )
    assert d.suppress is True
    assert d.reason == REASON_RELIGIOUS_TEXT
    assert d.moment_token == ""


# ── 4. Convenience predicates used by the classifier ───────────────


def test_text_predicate_helpers() -> None:
    assert text_indicates_religious_ritual("بارك الله فيك") is True
    assert text_indicates_religious_ritual("الله يحفظكم") is True
    assert text_indicates_religious_ritual("شكراً جزيلاً") is False
    assert text_indicates_religious_ritual(None) is False
    assert text_indicates_religious_ritual("") is False
    assert text_indicates_seasonal_greeting("كل عام وأنت بخير") is True
    assert text_indicates_seasonal_greeting("عيدكم مبارك") is True
    assert text_indicates_seasonal_greeting("متى يوصل الطلب") is False


# ── 5. Classifier integration (W3.1 → moments fire on the right inputs)


def test_classifier_fires_seasonal_on_pure_eid_message() -> None:
    rs = compute_relational_state(
        inbound_text="كل عام وأنتم بخير، عيدكم مبارك",
    )
    assert rs.moment == ConversationMoment.SEASONAL_GREETING


def test_classifier_fires_religious_on_pure_dua() -> None:
    rs = compute_relational_state(
        inbound_text="الله يحفظكم ويرعاكم ويبارك في رزقكم",
    )
    # Pure supplication → religious branch (no gratitude marker
    # present, so GRATITUDE_GENERIC at step 4b doesn't fire).
    assert rs.moment == ConversationMoment.RELIGIOUS_RITUAL_EXCHANGE


def test_classifier_does_not_fire_seasonal_mid_funnel() -> None:
    """Eid greeting from a customer mid-payment-flow stays in
    TRANSACTIONAL_ACTIVE — protects the dedup guard for the funnel."""
    rs = compute_relational_state(
        inbound_text="كل عام وأنتم بخير، أنا قاعد أرسل الإيصال",
        order_state={
            "order_status": "awaiting_receipt",
            "selected_product": "نصف كيلو طلح",
        },
    )
    assert rs.moment == ConversationMoment.TRANSACTIONAL_ACTIVE


def test_classifier_does_not_fire_religious_mid_funnel() -> None:
    rs = compute_relational_state(
        inbound_text="بارك الله فيكم، حولت المبلغ",
        order_state={
            "order_status": "awaiting_receipt",
            "selected_product": "كيلو سدر",
        },
    )
    assert rs.moment == ConversationMoment.TRANSACTIONAL_ACTIVE


def test_classifier_complaint_beats_seasonal() -> None:
    """An Eid greeting wrapped around a real shipping complaint
    must NOT silence the complaint moment."""
    rs = compute_relational_state(
        inbound_text="كل عام وأنتم بخير، بس شحنتي تأخرت كثير ولين الحين ما وصلت",
    )
    # Shipping-delay branch (#3) fires before the new W3 branches,
    # so the complaint moment wins.
    assert rs.moment == ConversationMoment.COMPLAINT_SHIPPING_DELAY


def test_classifier_gratitude_beats_religious() -> None:
    """A turn with both a thanks token and a religious blessing
    keeps the existing GRATITUDE_GENERIC behaviour (additive-only
    insertion contract)."""
    rs = compute_relational_state(
        inbound_text="شكراً جزيلاً، الله يحفظكم",
    )
    # GRATITUDE_GENERIC fires at step 4b before our new branches.
    # Both moments are in the suppression-eligible list, so this
    # is a labelling concern, not a behavioural one.
    assert rs.moment == ConversationMoment.GRATITUDE_GENERIC


# ── 6. Telemetry log line ──────────────────────────────────────────


def test_log_line_format_and_grep_tokens(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators grep ``[CX] dedup_suppression decision=...`` etc.
    Pin the keys so a refactor can't silently drop one."""
    monkeypatch.setenv(_FLAG, "1")
    caplog.set_level(logging.INFO, logger="nahla.relational")
    d = should_suppress_dedup_substitution(
        inbound_text="بارك الله فيك",
        relational_moment=ConversationMoment.RELIGIOUS_RITUAL_EXCHANGE,
        overlap=0.93,
    )
    log_dedup_suppression(
        decision=d,
        tenant_id=33,
        conversation_id=12345,
        overlap=0.93,
        would_have_replaced=True,
    )
    msgs = [r.getMessage() for r in caplog.records
            if "[CX] dedup_suppression" in r.getMessage()]
    assert msgs, "no dedup_suppression log emitted"
    line = msgs[-1]
    for token in (
        "decision=suppress",
        "reason=moment_eligible",
        "moment=religious_ritual_exchange",
        "overlap=0.93",
        "would_have_replaced=true",
        "flag_enabled=true",
        "tenant_id=33",
        "conversation_id=12345",
    ):
        assert token in line, (token, line)


def test_log_line_legacy_decision(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when the gate doesn't fire, the line is emitted with
    decision=legacy so we measure suppress/legacy ratios."""
    monkeypatch.setenv(_FLAG, "1")
    caplog.set_level(logging.INFO, logger="nahla.relational")
    d = should_suppress_dedup_substitution(
        inbound_text="كم سعر العسل",
        relational_moment=None,
        overlap=0.88,
    )
    log_dedup_suppression(
        decision=d,
        tenant_id=33,
        conversation_id=42,
        overlap=0.88,
        would_have_replaced=True,
    )
    line = next(
        r.getMessage() for r in caplog.records
        if "[CX] dedup_suppression" in r.getMessage()
    )
    assert "decision=legacy" in line
    assert "reason=no_marker" in line


# ── 7. Webhook wiring smoke test (function-level, no DB / network) ──
# We don't spin up the FastAPI app here — that's covered by the
# whatsapp-webhook regression suite. Instead we assert the gate is
# discoverable and importable from the same path the webhook uses.


def test_webhook_import_path_is_stable() -> None:
    from modules.ai.brain.relational import (
        log_dedup_suppression as _l,
        should_suppress_dedup_substitution as _s,
    )
    assert callable(_l)
    assert callable(_s)


# ── 8. Architectural rule: payment / order / OCR / handoff untouched
# We assert that no payment / order / OCR / handoff symbols leak
# into the gate's public surface. If a future change adds one, this
# test catches it before merge.


def test_gate_module_does_not_import_payment_or_order_modules() -> None:
    import modules.ai.brain.relational.dedup_suppression as mod
    src = Path(mod.__file__).read_text(encoding="utf-8")
    forbidden_imports = [
        "core.order_flow",
        "core.payment_intent",
        "core.payment_evidence",
        "core.payment_understanding",
        "core.tenant_payment_accounts",
        "core.receipt_verdict",
        "core.receipt_extraction",
        "core.handoff_state",
        "core.ai_pause_guard",
        "modules.ai.brain.brain_engine",
    ]
    for sym in forbidden_imports:
        assert sym not in src, f"forbidden import leaked into gate: {sym}"
