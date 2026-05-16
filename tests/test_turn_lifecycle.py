"""
tests/test_turn_lifecycle.py
────────────────────────────
Tests for the turn-lifecycle observability layer (May 2026 #16).

Covers:

  1. ``fallback_policy.is_informational_question`` — Arabic
     interrogative classifier handles "وشلون / كيف / كم / متى /
     وين / هل / ايش" with and without diacritics.
  2. ``fallback_policy.is_explicit_handoff_request`` — pinned to
     explicit handoff phrasings only; informational asks DON'T
     trigger it.
  3. ``fallback_policy.choose_safe_fallback`` — the production-
     incident scenario ("وشلون طريقة التوصيل") returns
     ``SOFT_RETRY``, NEVER the false-handoff template that caused
     the bug. Explicit handoff requests still route to the
     handoff ack. No-AI path returns the no-AI text regardless of
     question shape.
  4. ``turn_trace.TurnTrace.mark_outbound_sent`` — idempotency,
     lock semantics, valid source enum.
  5. ``turn_trace.TurnTrace.emit`` — never raises, even with
     garbage values; produces the ``[TURN]`` log line shape that
     downstream dashboards filter on.
  6. End-to-end policy contract — the exact text the production
     incident produced is NO LONGER returned for the offending
     inbound.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from services.fallback_policy import (  # noqa: E402
    FALLBACK_KIND_HANDOFF_ACK,
    FALLBACK_KIND_NEUTRAL_RETRY,
    FALLBACK_KIND_NO_AI,
    FALLBACK_KIND_SOFT_RETRY,
    FALLBACK_REASON_BRAIN_EXCEPTION,
    FALLBACK_REASON_BRAIN_SILENT,
    FALLBACK_REASON_NO_API_KEY,
    FALLBACK_REASON_OUTER_EXCEPTION,
    GOAL_ACK,
    GOAL_HANDOFF,
    GOAL_RETRY,
    choose_safe_fallback,
    is_explicit_handoff_request,
    is_informational_question,
)
from services.turn_trace import (  # noqa: E402
    DELIVERY_TEXT,
    SOURCE_BRAIN,
    SOURCE_BRAIN_EXCEPTION,
    SOURCE_UNKNOWN,
    TurnTrace,
    new_trace,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1.  is_informational_question — Arabic interrogative classifier
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "وشلون طريقة توصيل الطلبات عندكم",           # the actual production incident
    "كيف الدفع",
    "كم سعر العسل؟",
    "متى يصل الطلب؟",
    "وين موقعكم",
    "هل عندكم توصيل لجدة؟",
    "ايش الانواع المتوفرة",
    "اش عندكم اليوم",
    "وش الاسعار",
    "shlon al-tawseel",                            # Latin transliteration
])
def test_classifier_recognises_informational_arabic(text):
    assert is_informational_question(text), f"failed to classify {text!r} as informational"


@pytest.mark.parametrize("text", [
    "السلام عليكم",                               # greeting
    "شكرا لك",                                    # closer
    "ممتاز",                                      # ack
    "أبي أتكلم مع موظف",                          # handoff request — NOT informational
    "حولني لشخص",                                 # handoff request
    "تكييف الغرفه",                                # contains "كييف" substring but no boundary → must not fire
    "",                                            # empty
    "    ",                                        # whitespace only
])
def test_classifier_does_not_misfire(text):
    assert not is_informational_question(text), f"misclassified {text!r} as informational"


@pytest.mark.parametrize("text", [
    "كَيْفَ حالك",                                # with tashkeel
    "كــــــيف الطلب",                            # with elongation (tatweel)
    "كيـف الدفع؟",
])
def test_classifier_normalises_diacritics_and_tatweel(text):
    assert is_informational_question(text)


# ─────────────────────────────────────────────────────────────────────────────
# 2.  is_explicit_handoff_request
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "أبي أتكلم مع موظف",
    "ابي اتكلم مع موظف",
    "ابغى محادثه مع موظف",
    "حولني لانسان",
    "أحتاج أكلم شخص",
    "Can I talk to an agent?",
    "I want a human",
])
def test_handoff_classifier_matches(text):
    assert is_explicit_handoff_request(text), f"failed to detect handoff in {text!r}"


@pytest.mark.parametrize("text", [
    "كيف توصلون الطلبات",
    "السلام عليكم",
    "كم سعر العسل",
])
def test_handoff_classifier_does_not_misfire_on_informational(text):
    assert not is_explicit_handoff_request(text)


# ─────────────────────────────────────────────────────────────────────────────
# 3.  choose_safe_fallback — the production incident contract
# ─────────────────────────────────────────────────────────────────────────────


def test_production_incident_now_returns_soft_retry_not_false_handoff():
    """May 2026 incident: customer asked an informational question,
    Brain crashed, system replied with a fake-handoff template
    promising a human reply that didn't exist.

    Post-fix: the exact same inbound MUST route to ``SOFT_RETRY``
    (no false human promise). This test is the regression guard."""
    decision = choose_safe_fallback(
        "وشلون طريقة توصيل الطلبات عندكم",
        reason=FALLBACK_REASON_BRAIN_EXCEPTION,
        store_has_live_agent=False,
    )
    assert decision.kind == FALLBACK_KIND_SOFT_RETRY
    assert decision.response_goal == GOAL_RETRY
    # The forbidden false-handoff template must NEVER come back for
    # this question. Pin the exact substring that caused the bug.
    assert "سيتم الرد عليك في أقرب وقت من فريق المتجر" not in decision.text
    # The new copy should ask the customer to re-send — that's the
    # "honest" part of the contract.
    assert decision.text  # non-empty


def test_soft_retry_no_longer_offers_four_topic_menu():
    """May 2026 #18 production regression: the original SOFT_RETRY
    copy "(عن المنتج / السعر / التوصيل / الدفع)" surfaced too often
    for clear informational asks like "وش عندكم عسل" / "وشلون
    التوصيل" / "كم مدة التوصيل" — Brain was crashing on these and
    the customer saw a four-topic clarification menu that made the
    AI feel dumber than it actually is.

    The fix rolls the SOFT_RETRY wording back to a simple, honest
    retry that does NOT imply the customer was vague and does NOT
    push a topic menu. This test pins both pathologies as
    forbidden so a future "let's add helpful topic hints" patch
    doesn't reintroduce the regression."""
    decision = choose_safe_fallback(
        "وش عندكم عسل",
        reason=FALLBACK_REASON_BRAIN_EXCEPTION,
    )
    # The four-topic parenthetical is the most insulting bit — it
    # offers a fixed menu that doesn't match what the customer
    # asked. Must never reappear.
    assert "(عن المنتج / السعر / التوصيل / الدفع)" not in decision.text
    # "بتفاصيل أكثر" implies the customer was vague. For a clear
    # ask, that's wrong-footed. Must never reappear either.
    assert "بتفاصيل أكثر" not in decision.text
    # We're still in the soft-retry telemetry bucket so the team
    # can see informational-ask retries in [TURN] logs.
    assert decision.kind == FALLBACK_KIND_SOFT_RETRY


def test_explicit_handoff_request_keeps_handoff_ack():
    decision = choose_safe_fallback(
        "أبي أتكلم مع موظف",
        reason=FALLBACK_REASON_BRAIN_EXCEPTION,
        store_has_live_agent=True,
    )
    assert decision.kind == FALLBACK_KIND_HANDOFF_ACK
    assert decision.response_goal == GOAL_HANDOFF


def test_explicit_handoff_softened_when_no_live_agent():
    """When the customer asks for a human but the tenant has no live
    team, the reply still acks but in softer wording — we don't
    promise an immediate human reply we can't deliver."""
    decision = choose_safe_fallback(
        "حولني لموظف",
        reason=FALLBACK_REASON_BRAIN_EXCEPTION,
        store_has_live_agent=False,
    )
    assert decision.kind == FALLBACK_KIND_HANDOFF_ACK
    # The softened text shouldn't say "سيتم الرد عليك في أقرب وقت
    # من فريق المتجر" — it uses gentler "نتواصل معك" framing.
    assert "في أقرب وقت ممكن" in decision.text or "نتواصل معك" in decision.text


def test_no_api_key_path_returns_no_ai_text_regardless_of_question():
    """Even an informational question, when the AI is fully
    disabled, gets the no-AI text (honest about AI being off)."""
    decision = choose_safe_fallback(
        "وشلون التوصيل",
        reason=FALLBACK_REASON_NO_API_KEY,
        store_has_live_agent=False,
    )
    assert decision.kind == FALLBACK_KIND_NO_AI
    assert decision.response_goal == GOAL_ACK


@pytest.mark.parametrize("inbound", ["", "أهلاً", "شكرا", "ok"])
def test_unclassified_falls_to_neutral_retry(inbound):
    decision = choose_safe_fallback(
        inbound, reason=FALLBACK_REASON_BRAIN_EXCEPTION,
    )
    assert decision.kind == FALLBACK_KIND_NEUTRAL_RETRY
    assert decision.response_goal == GOAL_RETRY


def test_all_fallback_paths_have_non_empty_text_and_valid_goal():
    """Pin the contract: every reason × every question shape must
    produce SOMETHING — silence is unacceptable inside the 24h
    WhatsApp window. Validates the policy is total."""
    for reason in (
        FALLBACK_REASON_BRAIN_EXCEPTION,
        FALLBACK_REASON_BRAIN_SILENT,
        FALLBACK_REASON_OUTER_EXCEPTION,
        FALLBACK_REASON_NO_API_KEY,
    ):
        for inbound in ["وشلون", "أبي أتكلم مع موظف", "", "ok شكرا"]:
            d = choose_safe_fallback(inbound, reason=reason)
            assert d.text.strip(), (reason, inbound, "empty fallback")
            assert d.response_goal in (GOAL_RETRY, GOAL_HANDOFF, GOAL_ACK)
            assert d.kind  # non-empty kind label


# ─────────────────────────────────────────────────────────────────────────────
# 4.  TurnTrace — outbound lock semantics
# ─────────────────────────────────────────────────────────────────────────────


def test_turntrace_outbound_lock_acquires_once():
    """First caller acquires; subsequent calls are no-ops. This is
    the defence against double-send when a primary reply succeeded
    and a fallback path also tries to fire for the same turn."""
    t = new_trace(tenant_id=1, phone="966500000000", message_id="wamid_1")
    assert t.outbound_lock_acquired() is True
    t.mark_outbound_sent(source=SOURCE_BRAIN, length=42)
    assert t.outbound_lock_acquired() is False
    # Idempotency: subsequent marks don't change anything.
    t.mark_outbound_sent(source=SOURCE_BRAIN_EXCEPTION, length=999)
    assert t.reply_source == SOURCE_BRAIN     # original wins
    assert t.reply_len    == 42


def test_turntrace_records_brain_exception_class():
    t = new_trace(tenant_id=1, phone="966500000000")
    try:
        raise ValueError("boom")
    except Exception as exc:
        t.mark_brain_exception(exc)
    assert t.brain_failed is True
    assert t.brain_exc_class == "ValueError"


def test_new_trace_truncates_inbound_text():
    """Long inbound should be truncated by the constructor so the
    [TURN] log line stays bounded."""
    long_text = "x" * 5000
    t = new_trace(tenant_id=1, phone="9", inbound_text=long_text)
    assert len(t.inbound_text) <= 240


# ─────────────────────────────────────────────────────────────────────────────
# 5.  TurnTrace.emit — never raises, single log line
# ─────────────────────────────────────────────────────────────────────────────


def test_emit_produces_single_log_line_with_required_fields(caplog):
    t = new_trace(
        tenant_id=42, phone="966500000099",
        message_id="wamid_xyz", inbound_text="وشلون التوصيل",
    )
    t.mode          = "live_chat"
    t.stance        = "informational"
    t.brain_called  = True
    t.mark_outbound_sent(source=SOURCE_BRAIN, length=120, mode=DELIVERY_TEXT)

    with caplog.at_level(logging.INFO, logger="services.turn_trace"):
        t.emit()

    # Exactly one [TURN] line.
    turn_lines = [r for r in caplog.records if "[TURN]" in r.getMessage()]
    assert len(turn_lines) == 1
    msg = turn_lines[0].getMessage()
    # Required fields appear with the canonical key=value shape.
    for needle in (
        "tenant=42",
        "mode=live_chat",
        "stance=informational",
        "brain_called=True",
        "reply_source=brain",
        "delivery=text",
        "outbound_sent=True",
        "wamid=wamid_xyz",
    ):
        assert needle in msg, f"missing {needle!r} in TURN line: {msg!r}"


def test_emit_never_raises_on_garbage_values(caplog):
    """A typo upstream that puts a non-canonical value into
    ``reply_source`` must NOT break the emit. The trace downgrades
    to ``unknown`` and appends a hint so the offending site is
    findable, but the log line still emits."""
    t = new_trace(tenant_id=1, phone="9", message_id="m")
    t.reply_source = "made_up_source"           # not in _ALL_SOURCES

    with caplog.at_level(logging.INFO, logger="services.turn_trace"):
        t.emit()   # MUST NOT raise

    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "reply_source=unknown" in msg
    assert "invalid_reply_source=" in msg


def test_emit_resilient_to_internal_failure(caplog, monkeypatch):
    """If something inside the emit path raises (e.g. a future
    refactor adds a non-stringable field), the trace must NOT
    propagate the exception to the webhook handler."""
    t = new_trace(tenant_id=1, phone="9")
    # Force the inner logger to raise — emit must swallow it.
    import services.turn_trace as ts

    def boom(*_a, **_k):
        raise RuntimeError("simulated logger failure")

    monkeypatch.setattr(ts.logger, "info", boom)
    # Must NOT raise.
    t.emit()


# ─────────────────────────────────────────────────────────────────────────────
# 6.  End-to-end contract for the bug class
# ─────────────────────────────────────────────────────────────────────────────


def test_bug_class_fingerprint_is_findable_in_logs(caplog):
    """The defining filter for the bug class is
    ``reply_source=brain_exception`` on the [TURN] line. This test
    pins that fingerprint: a brain crash should always emit a TURN
    record with this exact label, so production alerting can grep."""
    t = new_trace(tenant_id=1, phone="9", inbound_text="وشلون التوصيل")
    t.brain_called = True
    try:
        raise RuntimeError("simulated brain crash")
    except Exception as exc:
        t.mark_brain_exception(exc)
    # Caller acquires the lock and sends the policy text.
    decision = choose_safe_fallback(
        t.inbound_text, reason=FALLBACK_REASON_BRAIN_EXCEPTION,
    )
    t.fallback_source = decision.kind
    t.response_goal   = decision.response_goal
    t.mark_outbound_sent(source=SOURCE_BRAIN_EXCEPTION, length=len(decision.text))

    with caplog.at_level(logging.INFO, logger="services.turn_trace"):
        t.emit()

    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "reply_source=brain_exception" in msg
    assert "fallback_source=soft_retry" in msg
    assert "brain_failed=True" in msg
    assert "brain_exc=RuntimeError" in msg
