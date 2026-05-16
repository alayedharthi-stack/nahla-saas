"""
tests/test_mirror_replies.py
────────────────────────────
Regression tests for the May 2026 #9 culturally-mirrored reply
layer.

Production gap
──────────────
The pool-rotation ``social_reply()`` returned generic variants for
turns where the customer's phrasing carries a culturally canonical
reciprocal. Example failures:

  customer "تسلم"            → pool: "وياك يا غالي 🌹..."
                                expected mirror: "الله يسلمك ..."

  customer "بيض الله وجهك"   → pool: "الله يبيض وجهك مثل ما بيضت
                                       وجهنا ..." (too heavy)
                                expected mirror: "وجهك أبيض ..."

  customer "الله يسعدك ما خلص اللي عندنا" (deferred):
                                LLM closer: "دوم بخير، أي وقت
                                              تحتاجين نحن هنا"
                                expected:   reciprocal tied to the
                                            LEFTOVER product, not a
                                            generic pleasantry.

The mirror layer is a small, ordered table of compiled regexes →
canonical reply strings. ``social_reply()`` consults it FIRST and
falls back to the rotating pool when no rule fires. The fall-back is
critical: this module never tries to cover every social phrasing,
only the ones with a strong cultural contract.
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
from modules.ai.brain.compose.mirror_replies import (  # noqa: E402
    mirror_reply,
)
from modules.ai.brain.intent.stance_detector import (  # noqa: E402
    STANCE_DEFERRED,
    STANCE_DIRECTIVES,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Canonical mirror reciprocals fire.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg, must_contain", [
    # تسلم — singular and plural with optional vocative.
    ("تسلم",                     "الله يسلمك"),
    ("تسلم يا غالي",             "الله يسلمك"),
    ("تسلمي",                    "الله يسلمك"),
    ("تسلموا",                   "الله يسلمكم"),
    ("تسلمون",                   "الله يسلمكم"),
    # بيض الله وجهك — singular and plural.
    ("بيض الله وجهك",            "وجهك أبيض"),
    ("بيّض الله وجهك",           "وجهك أبيض"),
    ("بيض الله وجوهكم",          "وجوهكم أبيض"),
    # جزاك الله خير.
    ("جزاك الله خير",            "وإياك"),
    ("جزاكم الله كل خير",        "وإياكم"),
    # الله يعطيك العافية / الله يعافيك.
    ("الله يعطيك العافية",       "الله يعافيك"),
    ("يعطيك العافية",            "الله يعافيك"),
    ("الله يعافيك",              "وإياك"),
    # الله يسعدك.
    ("الله يسعدك",               "يسعد قلبك"),
    ("الله يسعدكم",              "يسعد قلوبكم"),
    # الله يحفظك.
    ("الله يحفظك",               "ويحفظك"),
    # الله يبارك فيك.
    ("الله يبارك فيك",           "وفيك بارك الله"),
    ("الله يبارك فيكم",          "وفيكم بارك الله"),
    # الله يكثر خيرك.
    ("الله يكثر خيرك",           "وخيرك دائم"),
    # ربي/الله يوفقك.
    ("الله يوفقك",               "الله يوفقك"),
])
def test_mirror_rules_fire(msg: str, must_contain: str) -> None:
    """Every documented mirror rule must return a reply that opens
    with the conventional reciprocal phrase. If a rule regresses the
    customer hears a disconnected pool variant again."""
    reply = mirror_reply(msg)
    assert reply is not None, f"mirror_reply({msg!r}) returned None"
    assert must_contain in reply, (
        f"mirror reply for {msg!r} did not contain {must_contain!r}: "
        f"got {reply!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Defensive — patterns must NOT fire on ambiguous / unrelated text.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    # "تسلم لي على ..." is a different idiom (carry my greetings).
    "تسلم لي على الأولاد",
    # Long mixed turn — let the pool / brain handle.
    "تسلم يا غالي وأبغى أطلب عسل سدر كيلو واحد",
    # Commercial / unrelated.
    "أبغى أشوف الكتالوج",
    "كم سعر السمر؟",
    "السلام عليكم",
    # Empty / noise.
    "",
    "   ",
    "👍",
])
def test_mirror_skips_when_no_rule(msg: str) -> None:
    """No rule should fire for ambiguous or unrelated text. The
    caller (``social_reply``) then falls back to the rotating pool."""
    assert mirror_reply(msg) is None, (
        f"mirror_reply({msg!r}) unexpectedly returned a reply"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Integration — social_reply() honours the mirror over the pool.
# ─────────────────────────────────────────────────────────────────────────────

def test_social_reply_uses_mirror_over_pool() -> None:
    """When ``inbound_text`` matches a mirror rule, the returned
    reply MUST be the mirror string, not a pool variant. We assert
    this stays consistent across variant indices so the rotation
    cannot leak through."""
    for variant in range(6):
        for sub in range(5):
            reply = T.social_reply(
                category="thanks",
                variant=variant,
                sub_variant=sub,
                inbound_text="تسلم",
            )
            assert "الله يسلمك" in reply, (
                f"variant=({variant},{sub}) leaked pool reply: {reply!r}"
            )


def test_social_reply_falls_back_to_pool_without_match() -> None:
    """When no mirror rule fires the pool rotation must run
    unchanged — covers backward compatibility for the long tail."""
    reply = T.social_reply(
        category="thanks", variant=0, sub_variant=0,
        inbound_text="مشكورين على الذوق الراقي",  # no mirror rule
    )
    assert reply
    assert "الله يسلمك" not in reply  # mirror did NOT activate
    assert "وجهك أبيض" not in reply
    # The reply must be a known thanks-pool entry.
    assert reply in T._SOCIAL_THANKS_VARIANTS


def test_social_reply_back_compat_without_inbound_text() -> None:
    """Callers that don't pass ``inbound_text`` still work — pool
    rotation behaves exactly as before. Protects against accidental
    coupling of the mirror layer to every call site."""
    reply = T.social_reply(category="blessing", variant=2, sub_variant=1)
    assert reply
    assert reply in T._SOCIAL_BLESSING_VARIANTS


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Mirror reciprocals must stay LIGHT — no heavy reciprocal allowed
#     in the routine mirror table (that's the strong_praise pool's job).
# ─────────────────────────────────────────────────────────────────────────────

def test_mirror_replies_never_emit_heavy_reciprocal() -> None:
    """The mirror layer is supposed to mirror LIGHTLY. The heavy
    "الله يبيض وجهك مثل ما بيضت وجهنا" reciprocal stays in the
    strong_praise pool only — never returned by mirror_reply, even
    for "بيض الله وجهك" (which gets the lighter "وجهك أبيض")."""
    heavy_fragments = (
        "مثل ما بيضت",
        "بيضت وجوهنا",
    )
    test_phrases = [
        "تسلم", "تسلموا", "تسلمي",
        "بيض الله وجهك", "بيض الله وجوهكم",
        "جزاك الله خير", "الله يعافيك", "الله يسعدك",
        "الله يحفظك", "الله يبارك فيك",
    ]
    for phrase in test_phrases:
        reply = mirror_reply(phrase) or ""
        for frag in heavy_fragments:
            assert frag not in reply, (
                f"mirror reply for {phrase!r} unexpectedly contains "
                f"heavy fragment {frag!r}: {reply!r}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Deferred stance directive — forbid generic pleasantries, tie
#     reply to the leftover product.
# ─────────────────────────────────────────────────────────────────────────────

def test_deferred_directive_forbids_generic_pleasantries() -> None:
    """The May 2026 #9 directive update explicitly forbids generic
    closers like «دوم بخير / تحت أمرك / أي وقت تحتاجين» on deferred
    turns, because they ignore the LEFTOVER PRODUCT context."""
    directive = STANCE_DIRECTIVES[STANCE_DEFERRED]
    assert "دوم بخير" in directive, (
        "deferred directive must explicitly forbid «دوم بخير»"
    )
    # Must list at least ONE concrete reply that ties to the leftover
    # product so the LLM has a positive example to follow.
    assert (
        "يبارك لك فيه" in directive
        or "متّعك الله به" in directive
        or "متعك الله به" in directive
        or "عافية على عمر" in directive
    ), "deferred directive lacks a concrete leftover-product reciprocal"


def test_deferred_directive_still_forbids_sales_pitch() -> None:
    """Sales pitch / منتج بديل / خصومات tلقائية must remain
    forbidden — the May 2026 #7 contract still holds."""
    directive = STANCE_DIRECTIVES[STANCE_DEFERRED]
    assert "pitch" in directive or "بيعي" in directive
    assert "ممنوع" in directive
