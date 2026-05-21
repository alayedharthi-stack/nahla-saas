"""
tests/test_welcome_gate_residue.py
──────────────────────────────────
Regression tests for the May 2026 #19 fix:

    Customer sends "مساء الخير نحلة كيف حالك بسألك عن العايد وش نشاطهم".
    Pre-fix: classifier picks INTENT_GREETING (because the leading
    salaam matches), the question "وش نشاطهم" is dropped, and the bot
    replies with the generic welcome card.
    Post-fix: the welcome gate strips the leading greeting / vocative
    / "كيف حالك" tokens and notices there's substantive content left
    ("بسألك عن العايد وش نشاطهم") → demotes to INTENT_GENERAL with
    ``embedded_greeting=True`` so the LLM brain sees and answers the
    embedded question.

Why these tests matter
──────────────────────
The merchant explicitly asked: NO keyword→reply rules, NO rigid lists
for "وش نشاطهم" / "ايش تبيعون". The fix is STRUCTURAL — it strips a
small fixed vocabulary of greeting phrases and checks the residue.
These tests pin both behaviours:

  * pure greetings still classify as INTENT_GREETING (the bot can
    still play its warm welcome card on "السلام عليكم" alone),
  * mixed turns where the salaam carries a real question fall through
    to the LLM with ``embedded_greeting`` set.

Pinning both shapes prevents a future "strip every greeting → always
demote" over-correction that would kill the existing welcome card.
"""
from __future__ import annotations

import os
import sys

# Make ``backend/`` importable when pytest is run from the repo root.
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from modules.ai.brain.intent import rules as intent_rules  # noqa: E402
from modules.ai.brain.types import (                       # noqa: E402
    INTENT_GENERAL,
    INTENT_GREETING,
)


# ── Pure greetings — must still classify as INTENT_GREETING ──────────────────

def test_pure_salaam_stays_greeting():
    """"السلام عليكم" alone is the canonical first-contact case — keep
    the warm welcome card, do NOT route to the LLM."""
    intent = intent_rules.match("السلام عليكم")
    assert intent is not None
    assert intent.name == INTENT_GREETING


def test_time_of_day_greeting_stays_greeting():
    intent = intent_rules.match("مساء الخير")
    assert intent is not None
    assert intent.name == INTENT_GREETING


def test_greeting_with_bot_vocative_stays_greeting():
    """Bot-name tag is part of the salaam, not substantive content."""
    intent = intent_rules.match("مساء الخير يا نحلة")
    assert intent is not None
    assert intent.name == INTENT_GREETING


def test_greeting_with_how_are_you_stays_greeting():
    """`كيف حالك` is courtesy filler — does NOT count as a real ask."""
    intent = intent_rules.match("مساء الخير نحلة كيف حالك")
    assert intent is not None
    assert intent.name == INTENT_GREETING


def test_english_greeting_alone_stays_greeting():
    intent = intent_rules.match("hello")
    assert intent is not None
    assert intent.name == INTENT_GREETING


def test_english_greeting_with_how_are_you_stays_greeting():
    intent = intent_rules.match("hi how are you")
    assert intent is not None
    assert intent.name == INTENT_GREETING


# ── Mixed turns — salaam + substantive content → INTENT_GENERAL ──────────────

def test_greeting_with_embedded_open_question_demotes_to_general():
    """The bug the merchant reported on screen: salaam + open-ended
    store-nature question that no rigid rule matches. Must reach the
    LLM with embedded_greeting set."""
    intent = intent_rules.match(
        "مساء الخير نحلة كيف حالك بسألك عن العايد وش نشاطهم"
    )
    assert intent is not None
    assert intent.name == INTENT_GENERAL, (
        f"expected INTENT_GENERAL, got {intent.name}"
    )
    assert intent.slots.get("embedded_greeting") is True


def test_greeting_with_short_followup_question_demotes_to_general():
    """Short residue still counts — `وش عندكم اليوم` reads as a real ask."""
    intent = intent_rules.match("هلا والله وش عندكم اليوم")
    # Note: this might match INTENT_ASK_PRODUCT via the existing
    # actionable-embedded path (since "عندكم" appears in ASK_PRODUCT
    # patterns). Either INTENT_ASK_PRODUCT (best — actionable wins)
    # or INTENT_GENERAL (residue fallback) is acceptable — what MUST
    # not happen is a pure INTENT_GREETING that drops the question.
    assert intent is not None
    assert intent.name != INTENT_GREETING


def test_greeting_with_long_residue_demotes_to_general():
    """Even when the trailing content has no rule match, it must reach
    the LLM. This case asks something speculative the brain should
    handle conversationally."""
    intent = intent_rules.match(
        "صباح الخير نحلة عندي سؤال غريب شوي عن نوع العسل اللي ينفع للأطفال"
    )
    assert intent is not None
    # Either INTENT_ASK_PRODUCT (if rules catch "ينفع ل") or
    # INTENT_GENERAL (residue fallback). Crucially NOT INTENT_GREETING.
    assert intent.name != INTENT_GREETING


def test_english_greeting_with_residue_demotes_to_general():
    intent = intent_rules.match("hi i want to know something")
    assert intent is not None
    assert intent.name != INTENT_GREETING


# ── Direct unit tests for the residue helpers ────────────────────────────────

def test_strip_greeting_residue_removes_pure_salaam():
    assert intent_rules._strip_greeting_residue("السلام عليكم") == ""


def test_strip_greeting_residue_removes_time_of_day_plus_vocative():
    out = intent_rules._strip_greeting_residue("مساء الخير يا نحلة")
    assert out == "", f"expected empty residue, got {out!r}"


def test_strip_greeting_residue_removes_how_are_you_chain():
    out = intent_rules._strip_greeting_residue("هلا والله كيف حالك")
    assert out == "", f"expected empty residue, got {out!r}"


def test_strip_greeting_residue_preserves_substantive_tail():
    out = intent_rules._strip_greeting_residue(
        "مساء الخير نحلة كيف حالك بسألك عن العايد وش نشاطهم"
    )
    assert "نشاطهم" in out, f"expected the question to survive, got {out!r}"


def test_has_substantive_residue_false_on_pure_greeting():
    assert intent_rules._has_substantive_residue("السلام عليكم") is False
    assert intent_rules._has_substantive_residue("مساء الخير") is False
    assert intent_rules._has_substantive_residue("hi how are you") is False


def test_has_substantive_residue_true_on_mixed_turn():
    assert intent_rules._has_substantive_residue(
        "مساء الخير نحلة كيف حالك بسألك عن العايد وش نشاطهم"
    ) is True
    assert intent_rules._has_substantive_residue(
        "hi i need help with something"
    ) is True


def test_strip_handles_empty_input():
    assert intent_rules._strip_greeting_residue("") == ""
    assert intent_rules._strip_greeting_residue(None) == ""  # type: ignore[arg-type]
    assert intent_rules._has_substantive_residue("") is False
    assert intent_rules._has_substantive_residue(None) is False  # type: ignore[arg-type]
