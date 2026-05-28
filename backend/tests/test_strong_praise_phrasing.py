"""
tests/test_strong_praise_phrasing.py
────────────────────────────────────
Regression tests for the May 2026 #8 "heavy reciprocal compliment"
fix.

Production bug
──────────────
The bot was replying with "الله يبيض وجهك مثل ما بيضت وجهنا 🌹" to
turns where the customer hadn't praised us at all — sometimes even
on deferred-purchase messages where the customer was politely
declining. The reciprocal felt over-the-top and broke rapport.

The fix has three layers — each is covered here:

  1.  ``social_classifier`` carves out a new ``SOCIAL_STRONG_PRAISE``
      category and routes the explicit triggers ("بيض الله وجهك" /
      "ما قصرت" / "كفو" / "رفعت رأسنا" / "خدمة كبيرة") into it.

  2.  ``templates.social_reply()`` keeps the heavy reciprocal ONLY in
      the ``strong_praise`` pool. The lighter ``thanks`` and
      ``blessing`` pools must never emit the phrase.

  3.  ``high_priority_layer`` carries a hard-rule string instructing
      the LLM never to use the phrase unless an explicit trigger
      appears in the customer's message.

We also guard against the related over-firing where
"الله يسعدك، ما خلص اللي عندنا" was classifying as a plain blessing
and getting a canned reply that ignored the deferred frame. The
classifier now treats explicit deferred / support signals as
disqualifiers so the brain pipeline + stance detector take over.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.intent.social_classifier import (  # noqa: E402
    SOCIAL_BLESSING,
    SOCIAL_COMPLIMENT,
    SOCIAL_STRONG_PRAISE,
    SOCIAL_THANKS,
    classify_social,
)
from modules.ai.prompts.high_priority_layer import (  # noqa: E402
    BASELINE_POLICY_RULES,
    BASELINE_STYLE_RULES,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Strong-praise triggers route to the dedicated category.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "بيض الله وجهك",
    "بيضتم وجهنا",
    "ما قصرت والله",
    "كفو يا الغالي",
    "رفعتم رأسنا",
    "خدمة كبيرة منكم",
])
def test_strong_praise_triggers_route_to_strong_praise_category(msg: str) -> None:
    """The explicit heavy-praise tokens MUST route to the reserved
    SOCIAL_STRONG_PRAISE category — that's the single bucket allowed
    to emit the "الله يبيض وجهك" reciprocal."""
    result = classify_social(msg)
    assert result is not None, f"{msg!r} unexpectedly returned None"
    assert result.category == SOCIAL_STRONG_PRAISE, (
        f"{msg!r} routed to {result.category!r} instead of strong_praise"
    )
    assert result.confidence >= 0.9


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Plain thanks / blessing DO NOT escalate to strong_praise.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg, expected", [
    ("شكرًا",                      SOCIAL_THANKS),
    ("شكرا جزيلا",                 SOCIAL_THANKS),
    ("الله يعافيك",                SOCIAL_BLESSING),
    ("جزاك الله خير",              SOCIAL_THANKS),
    ("الله يسعدك",                 SOCIAL_BLESSING),
    ("يعطيك العافيه",              SOCIAL_BLESSING),
])
def test_plain_courtesy_does_not_escalate(msg: str, expected: str) -> None:
    """Ordinary thanks / blessing MUST stay in their own category so
    they never reach the strong-praise pool. This is the exact bug
    that produced the over-the-top reciprocal in production."""
    result = classify_social(msg)
    assert result is not None
    assert result.category == expected, (
        f"{msg!r} routed to {result.category!r} (expected {expected!r})"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Template pools — the heavy reciprocal is ONLY in strong_praise.
# ─────────────────────────────────────────────────────────────────────────────

_HEAVY_PHRASE_FRAGMENTS = (
    "يبيض وجهك",
    "بيضت وجهنا",
    "بيّض وجهك",
    "بيضت وجوهنا",
)


def _emits_heavy_phrase(reply: str) -> bool:
    return any(frag in (reply or "") for frag in _HEAVY_PHRASE_FRAGMENTS)


@pytest.mark.parametrize("category", [
    "thanks",
    "blessing",
    "compliment",
    "general_courtesy",
    "basmala",
    "prophet_invocation",
])
def test_no_heavy_phrase_in_non_strong_praise_pools(category: str) -> None:
    """Walk every variant index in every non-strong-praise pool and
    confirm the heavy reciprocal NEVER appears. This is the central
    regression guard — if a template gets accidentally re-added to
    the lighter pools the customer would see the over-the-top reply
    on routine turns again."""
    bucket = T._SOCIAL_REPLIES_BY_CATEGORY.get(category) or []
    assert bucket, f"category {category!r} has empty pool — unexpected"
    for idx in range(len(bucket) * 3):  # rotation covers > pool size
        for sub in range(3):
            reply = T.social_reply(
                category=category, variant=idx, sub_variant=sub,
            )
            assert not _emits_heavy_phrase(reply), (
                f"category {category!r} variant=({idx},{sub}) "
                f"unexpectedly emitted heavy phrase: {reply!r}"
            )


def test_strong_praise_pool_always_emits_heavy_phrase() -> None:
    """Symmetric to the above — the reserved pool MUST keep the heavy
    reciprocal so the routing has a real payoff."""
    bucket = T._SOCIAL_REPLIES_BY_CATEGORY["strong_praise"]
    assert bucket, "strong_praise pool is empty"
    for idx, reply in enumerate(bucket):
        assert _emits_heavy_phrase(reply), (
            f"strong_praise variant {idx} missing heavy reciprocal: {reply!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Deferred / support signals disqualify social classification.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    # The production screenshot — blessing + deferred leftover.
    "الله يسعدك لسي ماخلص الي عندنا من اول",
    "شكرًا، لسه عندي من المرة اللي قبل",
    "الله يعافيك، باقي عندنا كمية",
    "جزاك الله خير، إن شاء الله المرة الجاية أطلب",
    "تسلم، فيه مشكلة في الطلب",
])
def test_mixed_courtesy_plus_relational_signal_yields_to_brain(msg: str) -> None:
    """Mixed turns must drop out of the social classifier so the
    brain pipeline + stance detector can honour the deferred /
    support frame. Treating these as plain social would dispatch a
    canned blessing reply (and historically the heavy reciprocal)
    that ignores the real intent."""
    assert classify_social(msg) is None, (
        f"{msg!r} unexpectedly classified as social — should yield to brain"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5.  High-priority prompt rule is present so the LLM cannot
#     organically over-use the heavy reciprocal either.
# ─────────────────────────────────────────────────────────────────────────────

def test_prompt_rule_forbids_unsolicited_heavy_reciprocal() -> None:
    """The baseline policy rules carry the May 2026 #8 guard. If a
    cleanup ever removes the rule this test fails so we catch the
    regression at CI time. The rule was placed in POLICY (not STYLE)
    because it constrains WHEN a phrase may be used, not how the
    reply is shaped."""
    joined = " | ".join(BASELINE_STYLE_RULES + BASELINE_POLICY_RULES)
    assert "الله يبيض وجهك" in joined, (
        "Heavy-reciprocal guard rule missing from high-priority layer"
    )
    assert "ما قصرت" in joined and "كفو" in joined, (
        "Trigger list incomplete — strong-praise tokens out of sync"
    )
    assert "رأسنا" in joined or "رأسي" in joined
    # The rule must list at least one LIGHTER reciprocal so the LLM
    # has a concrete alternative to fall back on instead of staying
    # silent.
    assert (
        "الله يكرمك" in joined
        or "آمين وإياك" in joined
        or "الله يسعدك" in joined
        or "دوم بخير" in joined
    ), "Rule lacks a lighter alternative — LLM will have no fallback"


# ─────────────────────────────────────────────────────────────────────────────
# 6.  Defensive — overlapping tokens (كفو was in compliment, now in
#     strong_praise) must not accidentally fall into compliment too.
# ─────────────────────────────────────────────────────────────────────────────

def test_kfo_routes_to_strong_praise_not_compliment() -> None:
    """`كفو` was previously in COMPLIMENT but is now reserved for
    SOCIAL_STRONG_PRAISE. The shift is part of the fix — the
    compliment pool MUST not steal it back."""
    r = classify_social("كفو")
    assert r is not None and r.category == SOCIAL_STRONG_PRAISE


def test_generic_compliment_still_works() -> None:
    """The generic compliment bucket stays functional for tokens that
    are NOT strong-praise triggers.

    May 2026 #14 — bare ``ممتاز`` was moved OUT of
    ``_COMPLIMENT_KEYWORDS`` because customers also use it as a
    product descriptor ("هل ممتاز للأطفال؟"). The brain pipeline now
    reads the adjective in context. To keep this test honest about
    the generic-compliment path we switch to ``والنعم`` — an
    unambiguous compliment that has no descriptive double-life and
    stays in the classifier's bucket.
    """
    r = classify_social("والنعم")
    assert r is not None and r.category == SOCIAL_COMPLIMENT
