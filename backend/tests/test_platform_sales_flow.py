"""
tests/test_platform_sales_flow.py
─────────────────────────────────
Regression tests for the May 2026 #21 platform-sales-flow fixes.

The Nahla platform's own WhatsApp number runs a separate state machine
from the merchant brain (``core/conversation_engine.py`` + the
``_is_platform_tenant=True`` branch in ``routers/whatsapp_webhook.py``).
Three customer-reported bugs drove this work:

1. "مساء الخير نحلة كيف حالك باسألك عن العايد وش نشاطهم"
   → bot replied with a generic identity intro ("حياك الله 🌷 أنا
     نحلة مستشارة المبيعات في متجر آل عايد 🐝 كيف أقدر أخدمك اليوم؟")
   → ignored the actual question (what's آل عايد's business?).

2. "اعطني أسعار الباقات"
   → bot listed plan tiers without showing the actual prices, and
     ended with "تبي تجرب أو تحتاج تفاصيل أكثر عن باقة معينة؟"
   → customer was forced into a second turn to get prices.

3. "تفاصيل أكثر" (after the package teaser above)
   → bot replied "إذا في شي ثاني تحتاجه أنا معك." — a closing line
     that read as the bot dismissing the customer mid-question.

The fixes:

* ``IntentEngine.classify`` now strips greeting tokens and demotes
  mixed greeting+actionable turns from ``greeting`` to ``general``
  via ``_has_substantive_residue``.
* A new intent ``ask_elaborate`` catches "تفاصيل أكثر / اشرح / مزيد"
  and routes to a dedicated ``SHOW_PLAN_DETAILS`` action when the
  previous deterministic action was ``SHOW_PLANS``.
* ``FactGuard.STATIC_FACTS["plans"]`` carries the launch promo prices
  (Starter 449 / Growth 849 / Scale 1,499) instead of the original
  May 2025 prices (899 / 1,499 / 2,499).
* The Claude system prompt (``core/nahla_knowledge.py``) gained
  explicit anti-closing rules and an آل عايد ↔ نحلة relationship
  blurb so the LLM can answer "وش نشاطهم؟" without re-introducing
  itself.

These tests pin the behaviour so a future regression — accidentally
re-adding 899 to FactGuard, dropping the elaborate intent, or
re-enabling the closing fallback — surfaces immediately.
"""
from __future__ import annotations

from core.conversation_engine import (
    DecisionEngine,
    FactGuard,
    GENERATE_AI_REPLY,
    IntentEngine,
    SEND_FOUNDER_LINK,
    SHOW_PLAN_DETAILS,
    SHOW_PLANS,
    SHOW_WELCOME_MENU,
    ConversationSlots,
    ConversationState,
    recommend_plan,
)


def _fresh_state(**kwargs) -> ConversationState:
    return ConversationState(phone="+966500000000", slots=ConversationSlots(), **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Intent classification — greeting + actionable question.
# ─────────────────────────────────────────────────────────────────────────────


def test_pure_greeting_classifies_as_greeting() -> None:
    state = _fresh_state()
    intent, conf = IntentEngine.classify("السلام عليكم", state)
    assert intent == "greeting"
    assert conf >= 0.9


def test_pure_greeting_with_bot_name_still_greeting() -> None:
    state = _fresh_state()
    intent, _ = IntentEngine.classify("مساء الخير نحلة", state)
    assert intent == "greeting"


def test_greeting_plus_business_question_demoted_to_general() -> None:
    state = _fresh_state()
    intent, conf = IntentEngine.classify(
        "مساء الخير نحلة كيف حالك باسألك عن العايد وش نشاطهم", state,
    )
    assert intent != "greeting", (
        "mixed greeting+question must NOT classify as greeting "
        "(would trigger the welcome card and ignore the question)"
    )
    # The classifier returns "general" with elevated confidence (0.7)
    # so downstream logs distinguish mixed turns from raw fallbacks.
    assert intent == "general"
    assert conf >= 0.7


def test_greeting_plus_how_classifies_as_how() -> None:
    """If the substantive half hits a stronger rule (e.g. ask_how_it_works)
    that rule wins over the greeting — same contract as the merchant
    brain's welcome gate."""
    state = _fresh_state()
    intent, _ = IntentEngine.classify("هلا، كيف تشتغل نحلة؟", state)
    assert intent == "ask_how_it_works"


def test_greeting_plus_price_classifies_as_price() -> None:
    state = _fresh_state()
    intent, _ = IntentEngine.classify("السلام عليكم، كم الأسعار؟", state)
    assert intent == "ask_price"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Intent classification — elaborate / follow-up.
# ─────────────────────────────────────────────────────────────────────────────


def test_elaborate_keyword_classifies_as_ask_elaborate() -> None:
    state = _fresh_state()
    for msg in ("تفاصيل أكثر", "تفاصيل اكثر", "اشرح", "وضح أكثر",
                "أبي التفاصيل", "وش الفرق", "more details", "tell me more"):
        intent, conf = IntentEngine.classify(msg, state)
        assert intent == "ask_elaborate", f"failed for: {msg!r}"
        assert conf >= 0.9


def test_elaborate_does_not_collide_with_greeting() -> None:
    state = _fresh_state()
    # "أكثر" alone is in _ELABORATE; ensure it doesn't accidentally
    # trip a greeting branch.
    intent, _ = IntentEngine.classify("أكثر", state)
    assert intent == "ask_elaborate"


# ─────────────────────────────────────────────────────────────────────────────
# 3. DecisionEngine — elaborate routes to SHOW_PLAN_DETAILS after SHOW_PLANS.
# ─────────────────────────────────────────────────────────────────────────────


def test_decision_ask_price_triggers_show_plans() -> None:
    state = _fresh_state()
    action, reason = DecisionEngine.decide("ask_price", state)
    assert action == SHOW_PLANS
    assert "price" in reason


def test_decision_elaborate_after_show_plans_triggers_details() -> None:
    state = _fresh_state(last_action=SHOW_PLANS)
    action, reason = DecisionEngine.decide("ask_elaborate", state)
    assert action == SHOW_PLAN_DETAILS
    assert "show_plans" in reason


def test_decision_elaborate_after_details_routes_to_founder() -> None:
    """Two repeats of ‘تفاصيل أكثر’ → don't loop on details, escalate
    to the founder so the customer hears a human."""
    state = _fresh_state(last_action=SHOW_PLAN_DETAILS)
    action, _ = DecisionEngine.decide("ask_elaborate", state)
    assert action == SEND_FOUNDER_LINK


def test_decision_elaborate_with_no_prior_action_uses_brain() -> None:
    state = _fresh_state(last_action=None)
    action, reason = DecisionEngine.decide("ask_elaborate", state)
    assert action == GENERATE_AI_REPLY
    assert "elaborate_after:" in reason


# ─────────────────────────────────────────────────────────────────────────────
# 4. DecisionEngine — greeting behaviour with vs. without prior contact.
# ─────────────────────────────────────────────────────────────────────────────


def test_decision_pure_greeting_first_turn_shows_welcome() -> None:
    state = _fresh_state()  # greeted=False, stage='discovery'
    action, _ = DecisionEngine.decide("greeting", state)
    assert action == SHOW_WELCOME_MENU


def test_decision_pure_greeting_after_first_turn_uses_brain() -> None:
    state = _fresh_state(greeted=True)
    action, _ = DecisionEngine.decide("greeting", state)
    assert action == GENERATE_AI_REPLY


def test_decision_general_intent_routes_to_brain() -> None:
    """The mixed greeting+question turn (classifier returns ``general``)
    must land on ``GENERATE_AI_REPLY`` so Claude answers the substantive
    half instead of replaying the welcome card."""
    state = _fresh_state()
    action, _ = DecisionEngine.decide("general", state)
    assert action == GENERATE_AI_REPLY


# ─────────────────────────────────────────────────────────────────────────────
# 5. FactGuard — launch prices, plan names, anti-leak guarantees.
# ─────────────────────────────────────────────────────────────────────────────


def test_factguard_carries_launch_prices() -> None:
    plans = FactGuard.STATIC_FACTS["plans"]
    assert set(plans.keys()) == {"Starter", "Growth", "Scale"}
    assert plans["Starter"]["price_sar"] == 449
    assert plans["Growth"]["price_sar"] == 849
    assert plans["Scale"]["price_sar"] == 1499


def test_factguard_block_contains_launch_prices_and_promo_note() -> None:
    block = FactGuard.build_fact_block()
    assert "449" in block
    assert "849" in block
    assert "1,499" in block
    assert "بخصم 50٪" in block or "50%" in block
    # Old plan names must be gone from the customer-facing fact block.
    assert "Business" not in block
    assert "Pro " not in block  # space-suffix to avoid matching "Pro" inside other words
    # Original (pre-discount) prices must NOT leak.
    for forbidden in ("899", "2,499", "2499"):
        assert forbidden not in block, f"forbidden price '{forbidden}' in fact block"


def test_factguard_verify_reply_accepts_launch_prices() -> None:
    clean_reply = "Starter 449، Growth 849، Scale 1,499 — تجربة 14 يوم."
    ok, issues = FactGuard.verify_reply(clean_reply)
    assert ok, f"verifier flagged a clean reply: {issues}"


def test_factguard_verify_reply_flags_old_price() -> None:
    bad_reply = "Starter 899 ريال شهرياً."
    ok, issues = FactGuard.verify_reply(bad_reply)
    assert not ok
    assert any("suspicious_numbers" in i for i in issues)


# ─────────────────────────────────────────────────────────────────────────────
# 6. recommend_plan uses the new plan ladder.
# ─────────────────────────────────────────────────────────────────────────────


def test_recommend_plan_maps_to_new_names() -> None:
    s = _fresh_state()
    s.slots.store_size = "small"
    assert recommend_plan(s) == "Starter"
    s.slots.store_size = "medium"
    assert recommend_plan(s) == "Growth"
    s.slots.store_size = "large"
    assert recommend_plan(s) == "Scale"


# ─────────────────────────────────────────────────────────────────────────────
# 7. nahla_knowledge — anti-closing rules and آل عايد context.
# ─────────────────────────────────────────────────────────────────────────────


def test_nahla_knowledge_module_blocks_closing_fallback() -> None:
    """The system prompt must explicitly forbid the closing line that
    misfired in the screenshot ('إذا في شي ثاني تحتاجه أنا معك.')."""
    from core import nahla_knowledge

    rules = nahla_knowledge._PLATFORM_INFO + "\n" + (
        # language_rules is built inside build_nahla_system_prompt; we
        # call it without a db to get the static rules half.
        nahla_knowledge.build_nahla_system_prompt(db=None)
    )
    assert "إذا في شي ثاني تحتاجه أنا معك" in rules
    assert "ممنوع" in rules and "إغلاق" in rules


def test_nahla_knowledge_carries_al_ayed_relationship() -> None:
    """The bot must be able to answer 'وش نشاط آل عايد؟' from the
    system prompt alone — without prompting Claude to invent facts."""
    from core import nahla_knowledge

    info = nahla_knowledge._PLATFORM_INFO
    assert "آل عايد" in info
    assert "مؤسس" in info
    # Make sure we explicitly told the LLM to answer the relationship
    # question naturally instead of re-introducing itself.
    assert "بدون إعادة تعريف" in info


def test_nahla_knowledge_examples_use_launch_prices() -> None:
    from core import nahla_knowledge

    rules_text = nahla_knowledge.build_nahla_system_prompt(db=None)
    # The price-question example should now quote launch prices.
    assert "449" in rules_text
    assert "849" in rules_text
    # And the May 2025 example price (899) must not appear in examples.
    # (build_fact_block lives on a different code path — those tests
    # are above.)
    assert "899 ريال" not in rules_text


def test_nahla_knowledge_greeting_plus_question_example() -> None:
    """An explicit example pinning the right behaviour for the screenshot."""
    from core import nahla_knowledge
    rules_text = nahla_knowledge.build_nahla_system_prompt(db=None)
    assert "تحية + سؤال" in rules_text
    assert "مساء النور" in rules_text  # the recommended salaam reply
