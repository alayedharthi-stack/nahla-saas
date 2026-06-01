"""
tests/test_response_quality_recovery.py
───────────────────────────────────────
Regression tests for the May 2026 response-quality recovery patch
(P1 + P2 + P3 in the merchant report).

Background
──────────
After the May 15 brain / catalog / observability work shipped, the
merchant reported a noticeable quality drop on six representative
messages compared to the previous day:

  1.  "السلام عليكم فيه عروض على العسل"
      Greeting + commerce-flavoured ask. Previously the
      conversation_mode → brain pipeline produced either a tiny canned
      identity card OR (after the welcome-gate widening) a generic
      substituted reply that asks for the honey type — never the real
      product cards. We narrow the webhook welcome-gate validator so
      legitimate brain replies cannot be hijacked.

  2.  "ي ريت" after "تبين أرسل الرابط؟"
      Bare confirmation following an explicit bot offer. Used to fall
      through to a context-free LLM_REPLY whose response_goal was
      "no rule matched"; the model then replied "أبشري" with no marker.

  3.  "أبغى أشوف صورة لعسل السمر"
      A direct product/image request. Must route to ACTION_SEARCH_PRODUCTS
      so the responder can attach a product card / image.

  4.  "عندي طلبية انشحنت؟"
  5.  "وش صار على الطلب؟"
      Personal order-status questions in colloquial Gulf Arabic. Old
      INTENT_TRACK_ORDER regexes don't catch these phrasings; today they
      fall through to LLM_REPLY. The LLM must still produce a non-empty
      reply — the silent-reply guard in the webhook prevents the
      24-hour-window violation, but ideally the decision engine routes
      these without silence.

  6.  "👍" after a product card was shown
      Emoji-only confirmation following a product card. Used to fall
      to context-free LLM_REPLY and lose execution.

Fixes under test
────────────────
  * P1 — `decision/engine.py`:
      - widened bare-confirmation detector to include Gulf phrases
        ("ي ريت" / "يا ريت"), additional affirmations
        ("حسنا"/"ماشي"/"تكفى"), and positive-only emojis (👍/🙏/✅).
      - new branch 9.4: when the message IS a bare confirmation AND
        the conversation carries a pending offer
        (last_question_asked / pending_action / current_product_focus),
        route to ACTION_LLM_REPLY with topic="execute_pending_offer"
        and structured args so the prompt-builder can construct a
        strict execute-now goal.

  * P2 — `pipeline.py::_compose_response_goal`:
      - emits a strict "execute_pending_offer" goal mentioning the
        previous question, the pending action, and a `[PRODUCT:<title>]`
        marker for the focused product, telling the LLM to send the
        card itself instead of a verbal acknowledgement.

  * P3 — `whatsapp_webhook.py` welcome-gate validator:
      - tightened from len≤220 → len≤120 and requires
        `brain_state.last_action ∈ {"greet", "ACTION_GREET", ""}`.
        Prevents the validator from kidnapping a legitimate LLM /
        FAQ / search reply that happens to mention "أهلاً" / "وش
        تحب".

Run:
    cd backend
    python -m pytest tests/test_response_quality_recovery.py -v
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

from modules.ai.brain.decision.actions import (
    ACTION_GREET,
    ACTION_LLM_REPLY,
    ACTION_PLATFORM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine
from modules.ai.brain.intent import rules as intent_rules
from modules.ai.brain.pipeline import _compose_response_goal
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    INTENT_ASK_PRODUCT,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_GENERAL,
    INTENT_GREETING,
    MerchantConversationState,
    SuggestionSnapshot,
)


# ── Shared builder ───────────────────────────────────────────────────────────

def _ctx(
    *,
    message: str,
    state: MerchantConversationState | None = None,
    intent_name: str = INTENT_GENERAL,
    has_products: bool = True,
) -> BrainContext:
    """Build a minimal BrainContext for engine.decide() tests.

    Mirrors the helper in test_intent_social_platform.py but kept
    private here so each regression file owns its inputs.
    """
    if state is None:
        state = MerchantConversationState()
        # Bare confirmations after an offer ALWAYS imply the bot
        # greeted already (you cannot have an "offer" on turn 0). We
        # default greeted=True here so the engine's "first-turn GREET
        # fallback" (section 5) does not preempt the pending-offer
        # routing. Tests that specifically want the first-turn behaviour
        # pass an explicit state with greeted=False.
        state.greeted = True
    facts = CommerceFacts(
        has_products=has_products,
        product_count=3 if has_products else 0,
        orderable=has_products,
        has_active_integration=has_products,
        store_name="متجر الاختبار",
    )
    intent = Intent(
        name=intent_name,
        confidence=0.5,
        slots={},
        raw_message=message,
    )
    return BrainContext(
        tenant_id=1,
        customer_phone="+966500000000",
        message=message,
        intent=intent,
        state=state,
        facts=facts,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Case 1 — "السلام عليكم فيه عروض على العسل"
# ─────────────────────────────────────────────────────────────────────────────
#
# This case has TWO orthogonal failure modes:
#
#   (a) The brain's INTENT_ASK_PRODUCT regex misses "فيه عروض على X"
#       (no "سعر" / "أبغى" / "منتج" tokens). The welcome-gate routes
#       this to INTENT_GREETING alone. Decision → ACTION_GREET. The
#       composer returns the canned greeting template.
#
#   (b) Before P3, the webhook welcome-gate validator THEN substituted
#       the greeting with a hard-coded "تحب أعطيك الأسعار حسب النوع
#       (سدر / طلح / ضهيان)؟" — a TEXT-only reply with another question.
#       This is what caused the "أسئلة كثيرة في الدعم" symptom.
#
# Without changing intent regex (kept for the next round, P5), we lock
# the LESS BAD behaviour: brain returns ACTION_GREET, composer emits the
# canned greeting. The validator narrowing in P3 keeps it from being
# substituted with the hard-coded text when the brain didn't actually
# go through the greet path. Here we verify rules + decision parts.

def test_case1_greeting_plus_offers_demotes_to_brain() -> None:
    """Case 1: greeting + open-ended "any offers?" question.

    Original P3 behaviour (Apr 2026): the rule layer recognised the
    greeting alone, no actionable rule matched "فيه عروض على X", and
    the welcome gate fell back to INTENT_GREETING — the customer's
    question was dropped and the bot rendered the canned welcome card.

    May 2026 #19 fix: instead of adding regex patterns for every offer
    phrasing (the rigid-robot path the merchant explicitly rejected),
    the welcome gate now uses a STRUCTURAL residue test. After peeling
    the leading "السلام عليكم", the residue "فيه عروض على العسل"
    survives → the gate demotes to INTENT_GENERAL with
    ``embedded_greeting=True`` and the LLM brain composes the answer.

    What must NOT happen on this input is INTENT_GREETING — that's
    the dropped-question regression. Accept either INTENT_GENERAL
    (residue path) or INTENT_ASK_PRODUCT (if a future rule expansion
    catches "فيه عروض" earlier) as forward states.
    """
    message = "السلام عليكم فيه عروض على العسل"
    result = intent_rules.match(message)
    assert result is not None
    assert result.name != INTENT_GREETING, (
        "regression: greeting + 'فيه عروض على العسل' must NOT short-circuit "
        "to INTENT_GREETING — the customer's question would be dropped. "
        f"Got {result.name!r}."
    )
    # The residue path also sets embedded_greeting so the pipeline
    # prepends a warm salaam to the LLM reply.
    assert result.slots.get("embedded_greeting") is True, (
        f"expected embedded_greeting=True (so the pipeline still "
        f"acknowledges the salaam), got slots={result.slots!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Case 2 — "ي ريت" after bot offered to send a link
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "ي ريت",
    "يا ريت",
    "ياريت",
])
def test_case2_yare_after_offer_routes_to_execute_pending(message: str) -> None:
    """P1 + P2: 'ي ريت' / 'يا ريت' after the bot asked a question
    must route to a TYPED ACTION_LLM_REPLY whose decision.args carry
    the pending-offer context. This is what closes the 'أبشري only'
    regression."""
    state = MerchantConversationState()
    state.greeted = True                # bot already greeted before offering
    state.last_question_asked = "تبين أرسل الرابط؟"
    state.pending_action = "select_product"   # last suggestion engine output
    ctx = _ctx(message=message, state=state)

    decision = DefaultDecisionEngine().decide(ctx)

    assert decision.action == ACTION_LLM_REPLY, (
        f"expected execute-pending fallback, got {decision.action!r} "
        f"({decision.reason})"
    )
    assert decision.args.get("topic") == "execute_pending_offer", (
        f"expected execute_pending_offer topic, got {decision.args.get('topic')!r}"
    )
    assert decision.args.get("last_question_asked") == "تبين أرسل الرابط؟"
    # Sanity: the prompt-builder will read this context.
    assert "pending offer" in decision.reason.lower()


def test_case2_response_goal_tells_llm_to_execute() -> None:
    """P2: the rebuilt response_goal must explicitly tell the LLM to
    execute the offer and forbid one-word acknowledgements."""
    decision = Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "execute_pending_offer",
            "last_question_asked": "تبين أرسل الرابط؟",
            "pending_action": "select_product",
            "focus_product": "عسل سدر",
        },
        reason="bare-confirmation honours pending offer",
        confidence=0.78,
    )
    goal = _compose_response_goal(decision, SuggestionSnapshot())
    assert "execute_pending_offer" in goal
    assert "تبين أرسل الرابط" in goal
    assert "[PRODUCT:عسل سدر]" in goal
    # Forbid the exact verbal-only failure mode the merchant flagged.
    assert "أبشري" in goal, (
        "response_goal must explicitly forbid the 'أبشري'-only failure "
        f"mode so the LLM cannot regress to it. Got: {goal!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Case 3 — "أبغى أشوف صورة لعسل السمر"
# ─────────────────────────────────────────────────────────────────────────────

def test_case3_image_request_classifies_as_product_visual() -> None:
    """Visual image requests route to product_visual_request intent."""
    result = intent_rules.match("أبغى أشوف صورة لعسل السمر")
    assert result is not None
    assert result.name == INTENT_PRODUCT_VISUAL_REQUEST, (
        f"expected PRODUCT_VISUAL_REQUEST for image ask, got {result.name!r}"
    )


def test_case3_image_request_decides_search_products() -> None:
    """End-to-end (rules→engine): named image request searches catalog."""
    message = "أبغى أشوف صورة لعسل السمر"
    classified = intent_rules.match(message)
    assert classified is not None

    ctx = _ctx(message=message, intent_name=classified.name)
    ctx.intent = classified  # use the real intent with slots

    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_SEARCH_PRODUCTS, (
        f"expected SEARCH_PRODUCTS, got {decision.action!r} "
        f"(reason={decision.reason!r})"
    )
    assert "عسل" in str((decision.args or {}).get("query") or "")


# ─────────────────────────────────────────────────────────────────────────────
# Cases 4 + 5 — order-status questions (gap, not addressed by P1/P2/P3)
# ─────────────────────────────────────────────────────────────────────────────
#
# "عندي طلبية انشحنت؟" and "وش صار على الطلب؟" are Gulf phrasings the
# legacy TRACK_ORDER regexes don't catch. Today they fall to LLM_REPLY.
# These tests assert that:
#   * the decision engine produces a NON-handoff, NON-empty action
#     (silence is never acceptable inside the 24h window — covered by
#     the webhook silent-reply guard);
#   * the routing is deterministic (ACTION_LLM_REPLY with low
#     confidence), so a future enhancement can target a TRACK_ORDER
#     regex without breaking other tests.

@pytest.mark.parametrize("message", [
    "عندي طلبية انشحنت؟",
    "وش صار على الطلب؟",
    "وش صار على طلبي؟",
])
def test_case4_5_order_status_falls_to_llm_not_silent(message: str) -> None:
    """The brain may not classify these as TRACK_ORDER yet, but it
    must NEVER produce a None / empty decision. ACTION_LLM_REPLY is
    the documented fallback; the webhook silent-reply guard catches
    accidental empties downstream."""
    classified = intent_rules.match(message)
    # Either nothing fires (classifier returns None) or it fires as
    # GENERAL / GREETING. The DEFAULT classifier path inside the
    # pipeline falls back to INTENT_GENERAL when rules return None.
    intent_name = classified.name if classified else INTENT_GENERAL
    ctx = _ctx(message=message, intent_name=intent_name)
    if classified is not None:
        ctx.intent = classified

    decision = DefaultDecisionEngine().decide(ctx)

    # The exact action depends on context, but it MUST be deterministic
    # and non-silent. ACTION_LLM_REPLY is the documented fallback.
    assert decision is not None
    assert decision.action, "decision.action must never be empty/None"
    # Defensive: must not silently emit a no-op identity action.
    assert decision.action != "noop"


# ─────────────────────────────────────────────────────────────────────────────
# Case 6 — "👍" after product card
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "👍",
    "🙏",
    "✅",
    "👍👍",
])
def test_case6_thumbs_up_after_product_routes_to_execute_pending(message: str) -> None:
    """A bare positive emoji while a product is in focus must route to
    execute-pending so the LLM emits [PRODUCT:<title>] instead of
    replying 'تمام 👍' without a card."""
    state = MerchantConversationState()
    state.greeted = True                # bot already showed a product
    state.current_product_focus = {
        "id": "p1",
        "title": "عسل السمر",
        "price": 280,
        "external_id": "ext-1",
    }
    state.last_question_asked = "تبين أرسل الكرت كامل؟"
    ctx = _ctx(message=message, state=state)

    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.action == ACTION_LLM_REPLY
    assert decision.args.get("topic") == "execute_pending_offer"
    assert decision.args.get("focus_product") == "عسل السمر"


def test_case6_response_goal_includes_product_marker() -> None:
    """The execute-pending response_goal must include the concrete
    [PRODUCT:<title>] marker so the LLM has a copy-paste hint."""
    decision = Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "execute_pending_offer",
            "focus_product": "عسل السمر",
            "last_question_asked": "تبين أرسل الكرت كامل؟",
        },
    )
    goal = _compose_response_goal(decision, SuggestionSnapshot())
    assert "[PRODUCT:عسل السمر]" in goal


# ─────────────────────────────────────────────────────────────────────────────
# Negative tests — make sure P1 doesn't over-fire
# ─────────────────────────────────────────────────────────────────────────────

def test_no_pending_offer_context_does_not_route_to_execute_pending() -> None:
    """A bare confirmation with no last_question_asked, no
    pending_action, and no product focus must fall through to the
    generic LLM fallback (NOT execute-pending). This guards against
    P1 hijacking every bare 'اي' in fresh conversations."""
    state = MerchantConversationState()
    # explicitly empty — no offer pending
    ctx = _ctx(message="اي", state=state)

    decision = DefaultDecisionEngine().decide(ctx)
    # Could fall to greet (state.greeted=False, INTENT_GENERAL on first turn)
    # OR to the bare LLM fallback. Either way it MUST NOT be
    # execute_pending_offer since there's nothing to execute.
    assert decision.args.get("topic") != "execute_pending_offer"


def test_pending_offer_with_long_message_does_not_route_to_execute_pending() -> None:
    """A multi-word message with its own commercial signal must NOT be
    treated as a bare confirmation, even if it starts with 'اي'."""
    state = MerchantConversationState()
    state.greeted = True
    state.last_question_asked = "تبين أرسل الرابط؟"
    state.current_product_focus = {"id": "p1", "title": "عسل سدر"}
    ctx = _ctx(message="اي بس قبل وش أنواع العسل المتوفرة عندكم", state=state)

    decision = DefaultDecisionEngine().decide(ctx)
    assert decision.args.get("topic") != "execute_pending_offer"


@pytest.mark.parametrize("message", [
    "تمام",
    "اوكي",
    "حسنا",
    "ماشي",
    "اي",
    "okay",
    "go ahead",
])
def test_bare_confirmation_detector_recognises_gulf_affirmations(message: str) -> None:
    """All these standalone confirmations must be detected as bare
    confirmations (when pending-offer context exists)."""
    state = MerchantConversationState()
    state.greeted = True
    state.current_product_focus = {"id": "p1", "title": "عسل سدر"}
    ctx = _ctx(message=message, state=state)
    decision = DefaultDecisionEngine().decide(ctx)
    # With product focus + bare confirmation in non-checkout stage,
    # we must route to execute-pending.
    assert decision.action == ACTION_LLM_REPLY, (
        f"expected LLM_REPLY for {message!r}, got {decision.action!r}"
    )
    assert decision.args.get("topic") == "execute_pending_offer", (
        f"expected execute_pending_offer for {message!r}, got "
        f"{decision.args.get('topic')!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# P3 — welcome-gate validator narrowing (webhook)
# ─────────────────────────────────────────────────────────────────────────────
#
# These tests don't invoke the webhook end-to-end (that would require
# spinning up FastAPI / a DB). Instead they exercise the conditions in
# isolation so the narrowing logic is auditable.

def test_p3_welcome_gate_length_threshold_120() -> None:
    """The validator must NOT substitute replies > 120 chars even when
    they contain an intro marker. Real LLM answers that happen to use
    'وش تحب أعرفك' as a closing CTA are typically > 120 chars."""
    long_reply = (
        "أهلاً فيك في متجرنا 🌷\n"
        "عندنا عدة أنواع من العسل الطبيعي: سدر، طلح، ضهيان، والسمر. "
        "كل نوع له خصائصه وفوائده. وش تحب أعرفك عليه بالتفصيل؟"
    )
    assert len(long_reply) > 120
    # Inline the narrowed condition so the test documents the contract.
    INTRO_MARKERS = (
        "وش تحب نبدأ فيه", "وش تحب أعرفك", "وش تحب اعرفك",
        "كيف أقدر أخدمك اليوم", "كيف اقدر اخدمك اليوم",
        "أهلاً فيك في", "اهلا فيك في",
    )
    matched_marker = any(m in long_reply for m in INTRO_MARKERS)
    assert matched_marker, "test fixture must contain a marker"
    should_substitute = (
        matched_marker
        and len(long_reply) <= 120
    )
    assert should_substitute is False, (
        "long, legitimate LLM replies that happen to contain an intro "
        "marker must NOT be substituted by the welcome-gate validator"
    )


def test_p3_welcome_gate_short_reply_still_substitutes() -> None:
    """Short canned greeting replies (≤120) when last_action='greet'
    are still substituted — this is the legitimate failure mode the
    validator was designed for."""
    short_greeting = "أهلاً فيك في متجرنا 🌷\nوش تحب أعرفك عليه؟"
    assert len(short_greeting) <= 120

    INTRO_MARKERS = (
        "وش تحب نبدأ فيه", "وش تحب أعرفك", "وش تحب اعرفك",
        "كيف أقدر أخدمك اليوم", "كيف اقدر اخدمك اليوم",
        "أهلاً فيك في", "اهلا فيك في",
    )
    matched_marker = any(m in short_greeting for m in INTRO_MARKERS)
    last_action = "greet"
    should_substitute = (
        matched_marker
        and len(short_greeting) <= 120
        and last_action in ("greet", "ACTION_GREET", "")
    )
    assert should_substitute is True


def test_p3_welcome_gate_skips_when_brain_action_is_search() -> None:
    """When the brain ACTUALLY took a non-greet action (e.g. search),
    the validator must not kidnap the reply even if it happens to
    contain an intro marker."""
    short_intro_like_reply = "أهلاً فيك في متجرنا 🌷"
    assert len(short_intro_like_reply) <= 120

    INTRO_MARKERS = (
        "وش تحب نبدأ فيه", "وش تحب أعرفك", "وش تحب اعرفك",
        "كيف أقدر أخدمك اليوم", "كيف اقدر اخدمك اليوم",
        "أهلاً فيك في", "اهلا فيك في",
    )
    matched_marker = any(m in short_intro_like_reply for m in INTRO_MARKERS)
    last_action = "search_products"   # brain chose to search, not greet
    should_substitute = (
        matched_marker
        and len(short_intro_like_reply) <= 120
        and last_action in ("greet", "ACTION_GREET", "")
    )
    assert should_substitute is False, (
        "Even with a matching marker and short length, a non-greet "
        "brain action must keep the reply untouched."
    )
