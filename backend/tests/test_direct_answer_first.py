"""
test_direct_answer_first.py
────────────────────────────
Regression suite for the **Direct Answer First (DAF) bypass** in
``modules/ai/brain/decision/engine.py``.

Background
==========
A customer can open a conversation with a *substantive* question that
arrives as plain text but did NOT start with a greeting that the rules
layer recognised — most often:

    * a voice note transcribed by Whisper
       ("السلام عليكم وسهل الخير. اقول لك فيه فاتورة بتاريخ ... يعني
        انا كده سددت اسدد فاتورة اثنين؟")
    * a captioned image / video where the caption carries the question
    * a reply-to-status preamble + caption
    * an OCR / vision-summary block injected by the media normalizer

Before this fix, the engine's `not state.greeted and intent.name ==
INTENT_GENERAL` branch always emitted ACTION_GREET on first turn, so
the bot replied "أنا نحلة مستشارة المبيعات…" and silently dropped the
question. The merchant called it out as "the bot didn't read my
message."

The DAF bypass strips leading greeting / courtesy tokens (reusing the
rules-layer stripper for parity) and lets any input with ≥ 3 word
characters of residue fall through to the rule chain → LLM fallback,
where the brain composes a real answer with full KB / catalog / history
context.

Run
===
    cd backend
    python -m pytest tests/test_direct_answer_first.py -v
"""
from __future__ import annotations

import logging

import pytest

from modules.ai.brain.decision.actions import (
    ACTION_GREET,
    ACTION_LLM_REPLY,
)
from modules.ai.brain.persona_expression import (  # noqa: E402
    PERSONA_KIND_GREETING,
    PERSONA_TOPIC_SOCIAL,
)
from modules.ai.brain.decision.engine import (
    DefaultDecisionEngine,
    _first_turn_has_actionable_substance,
)
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    INTENT_GENERAL,
    INTENT_GREETING,
    MerchantConversationState,
)


# ── Shared builder ───────────────────────────────────────────────────────────


def _first_turn_ctx(
    *,
    message: str,
    intent_name: str = INTENT_GENERAL,
    embedded_greeting: bool = False,
    has_products: bool = True,
) -> BrainContext:
    """Build a BrainContext that simulates the customer's *first* turn.

    ``state.greeted`` defaults to ``False`` (no welcome card sent yet)
    which is the only configuration in which the first-turn greet
    branches in engine.py can fire. Tests that want to assert non-greet
    behaviour after a welcome card already went out should build their
    own state.
    """
    state = MerchantConversationState()
    state.greeted = False
    facts = CommerceFacts(
        has_products=has_products,
        product_count=3 if has_products else 0,
        orderable=has_products,
        has_active_integration=has_products,
        store_name="متجر الاختبار",
    )
    slots = {"embedded_greeting": True} if embedded_greeting else {}
    intent = Intent(
        name=intent_name,
        confidence=0.5,
        slots=slots,
        raw_message=message,
    )
    return BrainContext(
        tenant_id=33,
        customer_phone="+966500000000",
        message=message,
        intent=intent,
        state=state,
        facts=facts,
    )


# ── Substance helper unit tests ──────────────────────────────────────────────


@pytest.mark.parametrize("message,expected,reason", [
    # ── Pure tiny first-turn inputs MUST keep the welcome card ───
    ("",                    False, "empty message → no substance"),
    ("   ",                 False, "whitespace only → no substance"),
    ("اي",                  False, "2-char ack → below threshold"),
    ("ok",                  False, "2-char latin ack → below threshold"),
    ("هلا",                 False, "courtesy-only after strip → no residue"),
    ("السلام عليكم",        False, "pure salaam → fully stripped"),
    ("صباح الخير",          False, "pure greeting → fully stripped"),
    # ── Substantive inputs MUST bypass the welcome card ───────────
    ("كم سعره؟",            True,  "real ask → bypass"),
    ("ابغى استفسر عن الفاتورة", True, "real ask without greeting → bypass"),
    (
        "السلام عليكم وسهل الخير. اقول لك فيه فاتورة بتاريخ تمانية "
        "وعشرين اثنين، وفيه تاريخ تمانية وعشرين ثلاثة. طيب انا "
        "مسدد يوم ستة وعشرين ثلاثة. يعني انا كده سددت اسدد فاتورة اثنين؟",
        True,
        "voice transcript with greeting + invoice question → bypass",
    ),
    (
        "هلا نحلة، ممكن تعطيني رقم البائع أمين؟",
        True,
        "salaam + actionable staff-contact ask → bypass",
    ),
    (
        "صباح الخير. ابغى ثلاث كرتونات سدر اوصلها الرياض اليوم لو سمحت.",
        True,
        "greeting + concrete order ask → bypass",
    ),
    (
        "I want to ask about my invoice payment from last month",
        True,
        "english substantive ask → bypass via fallback path",
    ),
])
def test_first_turn_substance_helper_classification(
    message: str, expected: bool, reason: str,
) -> None:
    """Direct unit coverage for ``_first_turn_has_actionable_substance``.

    Pins the contract the engine relies on: small acknowledgements and
    pure greetings stay greet-eligible; anything carrying a real ask
    is flagged actionable.
    """
    assert _first_turn_has_actionable_substance(message) is expected, (
        f"{reason}: helper returned the wrong verdict for {message!r}"
    )


# ── Engine: voice-transcript regression ──────────────────────────────────────


def test_voice_transcript_with_invoice_question_does_not_greet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The exact regression the merchant reported.

    Inbound: a voice note whose Whisper transcript opens with a
    salaam followed by a long, clear question about an invoice and a
    paid amount.
    Required: the engine MUST NOT short-circuit to ACTION_GREET. The
    decision should fall through to the LLM fallback so the brain
    composes a real answer with KB / catalog / history context.

    A `[DAF.BYPASS]` telemetry line must be emitted so on-call can
    grep production for "where did the welcome card go?" without
    re-running anything.
    """
    transcript = (
        "السلام عليكم وسهل الخير. اقول لك فيه فاتورة بتاريخ تمانية "
        "وعشرين اثنين، وفيه تاريخ تمانية وعشرين ثلاثة. طيب انا مسدد "
        "يوم ستة وعشرين ثلاثة. حققت شهر اثنين وشهر ثلاثة. يعني انا "
        "كده سددت اسدد فاتورة اثنين؟"
    )
    ctx = _first_turn_ctx(message=transcript, intent_name=INTENT_GENERAL)

    with caplog.at_level(logging.INFO, logger="nahla.brain.decision"):
        decision = DefaultDecisionEngine().decide(ctx)

    assert decision.action != ACTION_GREET, (
        "regression: first-turn voice transcript with a clear question "
        f"must NOT trigger ACTION_GREET. Got reason={decision.reason!r}."
    )
    assert decision.action == ACTION_LLM_REPLY, (
        f"expected fallthrough to ACTION_LLM_REPLY, got {decision.action!r} "
        f"(reason={decision.reason!r})"
    )
    # Substance may bypass via DAF or an earlier commerce branch; either way
    # the welcome card must not win.
    assert not any(
        rec.getMessage().startswith("[INTENT_COST] kind=greeting route=template")
        for rec in caplog.records
    )


def test_first_turn_general_substantive_ask_does_not_greet() -> None:
    """Pure first-turn substantive ask without ANY greeting.

    Mirrors the OCR / caption / reply-to-status path: by the time the
    text reaches the brain there's no greeting, just a real ask. The
    engine must let it flow through to ANY non-greet action — the
    catalog search router, FAQ router, or the bare LLM fallback all
    qualify. What MUST NOT happen is the welcome card.

    We deliberately test with a no-products tenant so the order-flow
    text-pattern extractor (section 3.8c) cannot preempt the section-5
    GREET branch the bypass is meant to guard.
    """
    ctx = _first_turn_ctx(
        message="كم تستغرق التوصيل لمدينة الرياض ومتى تفتحون يومياً؟",
        intent_name=INTENT_GENERAL,
        has_products=False,
    )

    decision = DefaultDecisionEngine().decide(ctx)

    assert decision.action != ACTION_GREET, (
        "first-turn substantive INTENT_GENERAL must NOT greet; "
        f"got reason={decision.reason!r}"
    )
    assert decision.action == ACTION_LLM_REPLY, (
        f"expected fallthrough to ACTION_LLM_REPLY (no products → order-flow "
        f"router cannot fire), got {decision.action!r} "
        f"(reason={decision.reason!r})"
    )


def test_first_turn_thin_general_still_greets() -> None:
    """Pure tiny INTENT_GENERAL inputs ("اي" / "ok") still need the
    welcome card — there's nothing to answer yet, and skipping the
    greeting would feel cold on a first turn."""
    ctx = _first_turn_ctx(message="اي", intent_name=INTENT_GENERAL)

    decision = DefaultDecisionEngine().decide(ctx)

    assert decision.action == ACTION_GREET, (
        f"thin first-turn INTENT_GENERAL must keep the welcome card. "
        f"Got {decision.action!r} (reason={decision.reason!r})"
    )
    assert "first-turn general" in decision.reason


def test_first_turn_pure_greeting_routes_persona_llm_by_default() -> None:
    """Pure salaam on first turn → persona LLM compose (Phase 3 default)."""
    ctx = _first_turn_ctx(
        message="السلام عليكم", intent_name=INTENT_GREETING,
    )

    decision = DefaultDecisionEngine().decide(ctx)

    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == PERSONA_TOPIC_SOCIAL
    assert decision.args.get("persona_kind") == PERSONA_KIND_GREETING
    assert decision.action != ACTION_GREET


def test_first_turn_pure_greeting_routes_template_when_avoid_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy PR2B template path when routine LLM avoid is on."""
    monkeypatch.setenv("NAHLA_ROUTINE_LLM_AVOID_ENABLED", "true")
    ctx = _first_turn_ctx(
        message="السلام عليكم", intent_name=INTENT_GREETING,
    )

    decision = DefaultDecisionEngine().decide(ctx)

    assert decision.action == ACTION_GREET
    assert decision.action != ACTION_LLM_REPLY


def test_first_turn_greeting_with_substance_bypasses_belt_and_suspenders(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defence-in-depth: even if a long voice transcript somehow
    reaches the engine still labelled INTENT_GREETING (rules-layer
    demotion did not fire — e.g. STT artefacts the residue stripper
    didn't recognise as greeting tokens), the DAF bypass MUST still
    catch it and fall through.

    Without this guard a single missed greeting token in rules.py
    would re-introduce the welcome-card regression.
    """
    msg = (
        "السلام عليكم. متى ترجعون من الإجازة وكم تستغرق التوصيل "
        "لمدينة الرياض هذه الأيام؟"
    )
    # No-products tenant so earlier order-flow shortcuts can't preempt
    # the section-5 greet branch the bypass is meant to guard.
    ctx = _first_turn_ctx(
        message=msg, intent_name=INTENT_GREETING, has_products=False,
    )

    with caplog.at_level(logging.INFO, logger="nahla.brain.decision"):
        decision = DefaultDecisionEngine().decide(ctx)

    assert decision.action != ACTION_GREET, (
        "INTENT_GREETING with substantive content on first turn must "
        f"bypass the welcome card. Got reason={decision.reason!r}"
    )
    assert decision.action == ACTION_LLM_REPLY
    assert not any(
        rec.getMessage().startswith("[INTENT_COST] kind=greeting route=template")
        for rec in caplog.records
    )


def test_embedded_greeting_path_still_works() -> None:
    """Pre-existing escape hatch (rules-layer demotion → engine sees
    INTENT_GENERAL with embedded_greeting=True) must continue to
    bypass the welcome card. The DAF bypass is additive, not a
    replacement."""
    ctx = _first_turn_ctx(
        message="السلام عليكم وش عندكم اليوم",
        intent_name=INTENT_GENERAL,
        embedded_greeting=True,
    )

    decision = DefaultDecisionEngine().decide(ctx)

    assert decision.action != ACTION_GREET
    assert decision.action in {
        ACTION_LLM_REPLY,
        "search_products",
        "ask_product",
        "catalog_navigate",
    }
