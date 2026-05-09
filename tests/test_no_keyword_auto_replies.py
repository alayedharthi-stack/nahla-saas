"""Regression tests for the no-keyword-auto-reply contract.

The user reported that customers were receiving canned templates
("وصل طلبك! سأعيد توجيهك لفريق الدعم...", "بالنسبة للشحن:") even when:

* no order, draft, checkout or payment link existed,
* the customer was just chatting / joking / asking unusual product
  questions ("متى انتاجه؟"),
* the customer's message merely contained a substring like "شحن"
  (e.g. "هل وصلت الشحنة؟" — a personal-shipment question).

The fixes audited here:

1. ``RealPolicyGate._auto_escalate`` is OFF by default. Without an
   explicit merchant opt-in flag it MUST NOT promote any decision to
   ``ACTION_HANDOFF``, no matter how long the GENERAL streak is.
2. Even when opted-in, escalation requires an explicit signal
   (frustration / "moadhf" / "إنسان" / "ما فهمت" / etc.) in the
   customer's last message. Casual GENERAL banter never escalates.
3. The handoff variants no longer contain phrases that read like
   "your order has arrived" — variant 1 used to be the single biggest
   source of customer complaints and is now neutral.
4. Intent rules:
   * ``INTENT_ASK_SHIPPING`` no longer fires on a bare ``شحن`` token.
   * ``INTENT_TRACK_ORDER`` now picks up "هل وصلت الشحنة؟" /
     "وصلت طلبيتي" so personal-shipment questions go through the
     order-tracking flow instead of the generic shipping FAQ.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ─────────────────────────────────────────────────────────────────────────
# Auto-escalate guard
# ─────────────────────────────────────────────────────────────────────────

def _make_ctx(*, message: str, general_streak: int, brain_profile: dict | None = None,
              stage: str = "discovery"):
    return SimpleNamespace(
        message=message,
        intent=SimpleNamespace(name="general"),
        state=SimpleNamespace(stage=stage, general_streak=general_streak),
        merchant_context={"brain_profile": brain_profile or {}},
        facts=SimpleNamespace(within_working_hours=True),
        tenant_id=1,
    )


def test_auto_escalate_off_by_default_even_at_high_streak():
    from modules.ai.brain.decision.engine import Decision
    from modules.ai.brain.decision.policy import RealPolicyGate
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_HANDOFF

    gate = RealPolicyGate()
    base = Decision(action=ACTION_LLM_REPLY, reason="rules: general")

    # Customer has been chatting / joking — high streak, no escalation signal,
    # default ai_settings (auto_escalate_enabled missing). Must NOT escalate.
    for streak in (3, 5, 10, 50):
        out = gate._auto_escalate(
            base,
            _make_ctx(message="هههه 😂", general_streak=streak),
        )
        assert out.action == ACTION_LLM_REPLY, (
            f"streak={streak} unexpectedly escalated to {out.action}"
        )
    assert ACTION_HANDOFF != ACTION_LLM_REPLY  # sanity


def test_auto_escalate_off_when_flag_explicitly_false():
    from modules.ai.brain.decision.engine import Decision
    from modules.ai.brain.decision.policy import RealPolicyGate
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY

    gate = RealPolicyGate()
    base = Decision(action=ACTION_LLM_REPLY, reason="x")
    out = gate._auto_escalate(
        base,
        _make_ctx(
            message="موظف",  # signal present
            general_streak=10,
            brain_profile={"auto_escalate_enabled": False, "auto_escalate_after_n": 3},
        ),
    )
    assert out.action == ACTION_LLM_REPLY


def test_auto_escalate_requires_signal_even_when_enabled():
    """Opt-in flag set, streak above threshold, but no signal → still no handoff."""
    from modules.ai.brain.decision.engine import Decision
    from modules.ai.brain.decision.policy import RealPolicyGate
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY

    gate = RealPolicyGate()
    base = Decision(action=ACTION_LLM_REPLY, reason="x")
    out = gate._auto_escalate(
        base,
        _make_ctx(
            message="متى انتاجه؟",  # innocent product question
            general_streak=4,
            brain_profile={"auto_escalate_enabled": True, "auto_escalate_after_n": 3},
        ),
    )
    assert out.action == ACTION_LLM_REPLY, (
        f"escalated without explicit signal: {out.action}"
    )


def test_auto_escalate_fires_with_opt_in_and_explicit_signal():
    from modules.ai.brain.decision.engine import Decision
    from modules.ai.brain.decision.policy import RealPolicyGate
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_HANDOFF

    gate = RealPolicyGate()
    base = Decision(action=ACTION_LLM_REPLY, reason="x")

    for signal in (
        "ابغى موظف يكلمني",
        "كلموني من فضلكم",
        "ما فهمت ايش الموضوع",
        "أبغى أكلم إنسان حقيقي",
        "i don't understand please call me",
        "speak to someone please",
    ):
        out = gate._auto_escalate(
            base,
            _make_ctx(
                message=signal,
                general_streak=3,
                brain_profile={"auto_escalate_enabled": True, "auto_escalate_after_n": 3},
            ),
        )
        assert out.action == ACTION_HANDOFF, f"signal {signal!r} did not escalate"


def test_auto_escalate_skips_when_intent_not_general():
    from modules.ai.brain.decision.engine import Decision
    from modules.ai.brain.decision.policy import RealPolicyGate
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY

    gate = RealPolicyGate()
    base = Decision(action=ACTION_LLM_REPLY, reason="x")
    ctx = _make_ctx(
        message="موظف",
        general_streak=10,
        brain_profile={"auto_escalate_enabled": True, "auto_escalate_after_n": 3},
    )
    ctx.intent = SimpleNamespace(name="ask_product")  # non-general
    out = gate._auto_escalate(base, ctx)
    assert out.action == ACTION_LLM_REPLY


def test_auto_escalate_skips_outside_discovery_or_exploring():
    from modules.ai.brain.decision.engine import Decision
    from modules.ai.brain.decision.policy import RealPolicyGate
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY

    gate = RealPolicyGate()
    base = Decision(action=ACTION_LLM_REPLY, reason="x")
    out = gate._auto_escalate(
        base,
        _make_ctx(
            message="موظف",
            general_streak=10,
            brain_profile={"auto_escalate_enabled": True, "auto_escalate_after_n": 3},
            stage="ordering",
        ),
    )
    assert out.action == ACTION_LLM_REPLY


# ─────────────────────────────────────────────────────────────────────────
# Handoff template variants — wording contract
# ─────────────────────────────────────────────────────────────────────────

def test_handoff_variants_never_say_order_arrived():
    """No variant may start with phrasing that reads like an order
    confirmation. This is the exact bug that caused customers to think
    their purchase had arrived when they had never placed an order."""
    from modules.ai.brain.compose.templates import handoff, _HANDOFF_VARIANTS

    forbidden = (
        "وصل طلبك",          # "your order has arrived"
        "تم استلام طلبك",   # "we've received your order"
        "تم تأكيد طلبك",    # "your order has been confirmed"
    )
    for v_idx in range(3):
        text = handoff(variant=v_idx)
        for needle in forbidden:
            assert needle not in text, (
                f"handoff variant {v_idx} contains forbidden phrase {needle!r}: {text!r}"
            )

    # Also lock down the exact variant count so a future PR can't sneak a
    # bad variant in via ``variant=4``.
    assert len(_HANDOFF_VARIANTS) == 3


def test_handoff_variants_describe_human_handoff_intent():
    """Each variant must actually communicate the handoff (without
    abusing order-confirmation phrasing)."""
    from modules.ai.brain.compose.templates import handoff

    for v_idx in range(3):
        text = handoff(variant=v_idx)
        # Some indication that a person / team will reach out.
        assert any(
            kw in text
            for kw in ("الفريق", "موظف", "أعضاء", "التواصل", "يتواصل", "سيرد")
        ), f"variant {v_idx} doesn't describe a human handoff: {text!r}"


# ─────────────────────────────────────────────────────────────────────────
# Intent rules — shipping vs. tracking
# ─────────────────────────────────────────────────────────────────────────

def test_personal_shipment_question_is_track_order_not_shipping_faq():
    """The exact regression the user reported: a customer asking about
    THEIR shipment (not generic shipping policy) used to be classified
    as INTENT_ASK_SHIPPING because of the bare 'شحن' keyword."""
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import (
        INTENT_TRACK_ORDER, INTENT_ASK_SHIPPING,
    )

    for q in (
        "هل وصلت الشحنة؟",
        "هل وصلت الطلبية",
        "وصلت طلبيتي؟",
        "وين شحنتي؟",
        "متى توصل طلبيتي",
        "did my shipment arrive?",
    ):
        intent = match(q)
        # Either we explicitly identify track_order, or no rule fires
        # (the LLM/general path will then handle it). What we must NOT
        # do is land on ASK_SHIPPING (which routes to the FAQ template).
        if intent is None:
            continue
        assert intent.name != INTENT_ASK_SHIPPING, (
            f"{q!r} mis-classified as ASK_SHIPPING (expected TRACK_ORDER or none)"
        )
        # Most of these should be TRACK_ORDER.
        if "شحنة" in q or "طلبية" in q or "طلبيتي" in q or "شحنتي" in q:
            assert intent.name == INTENT_TRACK_ORDER, (
                f"{q!r} expected TRACK_ORDER, got {intent.name}"
            )


def test_shipping_policy_questions_still_match_ask_shipping():
    """We must not break the legitimate shipping-FAQ flow — questions
    about shipping-policy details still land on ASK_SHIPPING (and
    cost-flavoured questions may legitimately resolve to ASK_PRICE,
    both of which the brain handles correctly with shipping data)."""
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import INTENT_ASK_SHIPPING, INTENT_ASK_PRICE

    # Pure-policy questions: must be ASK_SHIPPING.
    for q in (
        "هل لديكم شحن مجاني؟",
        "ما هي طرق الشحن المتاحة",
        "سياسة الشحن عندكم؟",
        "كم يوم يأخذ التوصيل؟",
        "what is your shipping policy",
        "how many days for delivery?",
    ):
        intent = match(q)
        assert intent is not None, f"{q!r} produced no intent"
        assert intent.name == INTENT_ASK_SHIPPING, (
            f"{q!r} expected ASK_SHIPPING, got {intent.name}"
        )

    # Cost-flavoured questions: either ASK_SHIPPING or ASK_PRICE is OK
    # (the brain handles both with shipping facts injected).
    for q in (
        "كم رسوم الشحن؟",
        "كم سعر التوصيل للرياض",
    ):
        intent = match(q)
        assert intent is not None, f"{q!r} produced no intent"
        assert intent.name in (INTENT_ASK_SHIPPING, INTENT_ASK_PRICE), (
            f"{q!r} expected ASK_SHIPPING/ASK_PRICE, got {intent.name}"
        )


def test_unrelated_chat_is_not_classified_as_shipping_or_order():
    """Banter / jokes / unusual product questions must not trigger any
    keyword interceptor. They should fall through to the LLM path."""
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import (
        INTENT_ASK_SHIPPING, INTENT_TRACK_ORDER, INTENT_TALK_HUMAN,
    )

    for q in (
        "ههههه 😂",
        "تمام شكراً",
        "أحبكم 🐝",
        "متى انتاجه؟",          # asking about production date, not order
        "هل العسل صافي؟",
        "ايش الفرق بين السمر والطلح؟",
    ):
        intent = match(q)
        if intent is None:
            continue
        assert intent.name not in (
            INTENT_ASK_SHIPPING, INTENT_TRACK_ORDER, INTENT_TALK_HUMAN,
        ), f"{q!r} hijacked into {intent.name} — should fall to LLM/general"


def test_explicit_human_request_still_routes_to_talk_human():
    """We must NOT have weakened the legitimate handoff flow.
    Customers who explicitly ask for a human still need to be heard."""
    from modules.ai.brain.intent.rules import match
    from modules.ai.brain.types import INTENT_TALK_HUMAN

    for q in (
        "أبغى أكلم موظف",
        "تواصل مع شخص حقيقي",
        "خدمة العملاء من فضلك",
        "human agent please",
        "speak to someone",
    ):
        intent = match(q)
        assert intent is not None, f"{q!r} produced no intent"
        assert intent.name == INTENT_TALK_HUMAN, (
            f"{q!r} expected TALK_HUMAN, got {intent.name}"
        )


# ─────────────────────────────────────────────────────────────────────────
# _has_escalation_signal helper
# ─────────────────────────────────────────────────────────────────────────

def test_has_escalation_signal_positive_cases():
    from modules.ai.brain.decision.policy import RealPolicyGate

    for msg in (
        "أبغى موظف",
        "اتصلوا بي لو سمحتم",
        "ما فهمت شي",
        "تواصلوا معي عاجل",
        "talk to a human agent please",
        "I don't understand any of this",
    ):
        assert RealPolicyGate._has_escalation_signal(msg) is True, msg


def test_has_escalation_signal_negative_cases():
    """Casual / product / banter messages must not be flagged."""
    from modules.ai.brain.decision.policy import RealPolicyGate

    for msg in (
        "ههههه 😂",
        "كم سعر العسل؟",
        "متى انتاجه؟",
        "أبغى عسل سمر",
        "بكم الكيلو؟",
        "thanks",
    ):
        assert RealPolicyGate._has_escalation_signal(msg) is False, msg


def test_has_escalation_signal_handles_empty_input():
    from modules.ai.brain.decision.policy import RealPolicyGate

    assert RealPolicyGate._has_escalation_signal("") is False
    assert RealPolicyGate._has_escalation_signal("   ") is False
    assert RealPolicyGate._has_escalation_signal(None) is False  # type: ignore[arg-type]
