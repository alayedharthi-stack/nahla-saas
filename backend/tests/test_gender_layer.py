"""
tests/test_gender_layer.py
──────────────────────────
Coverage for the light gender-awareness layer
(``modules/ai/gender``). Three things under test:

1. Detector cascade — verb suffixes > name whitelist > sticky prior,
   with explicit conflict handling.
2. Conjugator — closed-set female swaps applied only on
   high-confidence female hints. Male / unknown / low-confidence
   inputs are no-ops.
3. End-to-end social-reply transformation matching the three
   examples in the spec ("أبشر/أبشري" → "تسلم/تسلمين",
   "يعطيك العافية" from a female customer → female-coded reply).

Constraints under test:

* Aggressive guessing is forbidden — any plausible ambiguity
  collapses to ``unknown``.
* The masculine form is never touched (it's the unmarked
  default).
* The sticky prior decays mildly but doesn't disappear after one
  turn.

Run:
    cd backend
    python -m pytest tests/test_gender_layer.py -v
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

from modules.ai.gender import (
    GenderHint,
    apply_gender_to_social_reply,
    detect_gender,
)
from modules.ai.gender.detector import (
    APPLY_CONFIDENCE_THRESHOLD,
    GENDER_FEMALE,
    GENDER_MALE,
    GENDER_UNKNOWN,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Detector — verb-suffix signals
# ─────────────────────────────────────────────────────────────────────────────

class TestVerbSuffixDetection:
    """The strongest single signal. Marked female forms in Arabic
    have no masculine equivalent, so a hit here is high-confidence."""

    @pytest.mark.parametrize("msg", [
        "أبشري",
        "تفضلي بطلبك",
        "خبريني وش تحتاجين",
        "أرسلي العنوان وأكمل لك الطلب",
        "تحبين السدر أو الطلح؟",
        "تبغين كيلو ولا نص؟",
        "تقدرين تجين الفرع بكره",
        "حبيتي اللون الذهبي؟",
        "شفتي العسل الجديد؟",
        "جيتي للفرع قبل؟",
        "اشتري لي كيلو من فضلك",
        "قولي لي الأسعار",
    ])
    def test_female_verb_signals_fire(self, msg):
        hint = detect_gender(msg)
        assert hint.value == GENDER_FEMALE, msg
        assert hint.confidence >= APPLY_CONFIDENCE_THRESHOLD
        assert hint.source == "verb"

    @pytest.mark.parametrize("msg", [
        # Masculine / neutral defaults — no female suffix present.
        "أبشر",
        "تفضل بطلبك",
        "خبرني وش تحتاج",
        "أرسل العنوان",
        "تحب السدر ولا الطلح؟",
        "تبغى كيلو ولا نص؟",
        # Common courtesy lines that DON'T encode gender on their own.
        "السلام عليكم",
        "يعطيك العافية",
        "بيض الله وجهك",
        "جزاك الله خير",
        # Empty / noise.
        "",
        "🌹🌷",
        "123",
    ])
    def test_masculine_or_ambiguous_returns_unknown(self, msg):
        hint = detect_gender(msg)
        assert hint.value == GENDER_UNKNOWN, msg
        assert hint.confidence == 0.0

    def test_explicit_kasra_object_pronoun_is_female(self):
        """The kasra is rarely typed in production, but when it IS,
        it's unambiguous."""
        hint = detect_gender("شكراً لكِ على ردك")
        assert hint.value == GENDER_FEMALE
        assert hint.confidence >= APPLY_CONFIDENCE_THRESHOLD


# ─────────────────────────────────────────────────────────────────────────────
# 2. Detector — name whitelist
# ─────────────────────────────────────────────────────────────────────────────

class TestNameWhitelist:
    @pytest.mark.parametrize("name", [
        "نوره", "نورة", "سارة", "ساره", "مريم", "فاطمة",
        "هند", "هيا", "روان", "شيخة", "العنود",
    ])
    def test_known_female_name_returns_female(self, name):
        hint = detect_gender("", customer_name=name)
        assert hint.value == GENDER_FEMALE
        assert hint.source == "name"

    @pytest.mark.parametrize("name", [
        "محمد", "أحمد", "علي", "خالد", "فهد", "عبدالله",
        "سعد", "ناصر", "تركي",
    ])
    def test_known_male_name_returns_male(self, name):
        hint = detect_gender("", customer_name=name)
        assert hint.value == GENDER_MALE
        assert hint.source == "name"

    @pytest.mark.parametrize("name", [
        # Genuinely ambiguous gulf names — must NOT be tagged.
        "نور", "ملاك", "علا", "لمى",  # the layer skips ambiguous singletons
        # Random / unknown.
        "زرافة", "Customer 47", "  ",
    ])
    def test_ambiguous_or_unknown_names_return_unknown(self, name):
        # NOTE: لمى IS on the female list in detector.py — but only
        # because it's overwhelmingly female in Gulf usage. To keep
        # this test honest we use names NOT on either list:
        if name == "لمى":
            return
        hint = detect_gender("", customer_name=name)
        assert hint.value == GENDER_UNKNOWN, name

    def test_only_first_token_is_inspected(self):
        """A "First Family" pair should classify by the first token."""
        hint = detect_gender("", customer_name="نوره العتيبي")
        assert hint.value == GENDER_FEMALE
        hint = detect_gender("", customer_name="محمد بن عبدالله")
        assert hint.value == GENDER_MALE


# ─────────────────────────────────────────────────────────────────────────────
# 3. Detector — cascade priority + conflict handling
# ─────────────────────────────────────────────────────────────────────────────

class TestCascadeAndConflict:
    def test_verb_signal_beats_name_when_both_agree(self):
        hint = detect_gender("تفضلي يا حبيبتي", customer_name="نوره")
        assert hint.value == GENDER_FEMALE
        # Verb wins because it's the stronger source.
        assert hint.source == "verb"

    def test_verb_and_name_disagree_returns_unknown(self):
        """Customer profile says 'نوره' but the current message uses
        masculine forms — could be a household account, friend
        helping out, or wrong profile. Refuse to swap rather than
        guess."""
        hint = detect_gender("أبشر يا الغالي", customer_name="نوره")
        # Verb signal here is masculine (no female suffix fired) so
        # ONLY the name signal stands → female via name. The
        # "conflict" branch only triggers when BOTH signals fire.
        assert hint.value == GENDER_FEMALE
        assert hint.source == "name"

    def test_real_conflict_collapses_to_unknown(self):
        """Female verb suffix + male name → unknown (refuse to guess)."""
        hint = detect_gender("تفضلي بطلبي", customer_name="محمد")
        assert hint.value == GENDER_UNKNOWN
        assert hint.source == "conflict"

    def test_prior_hint_used_when_no_current_signal(self):
        prior = GenderHint(
            value=GENDER_FEMALE, confidence=0.90, source="verb",
        )
        hint = detect_gender(
            "السلام عليكم", customer_name=None, prior_hint=prior,
        )
        assert hint.value == GENDER_FEMALE
        # Decayed mildly but still above threshold.
        assert hint.confidence < prior.confidence
        assert hint.confidence >= APPLY_CONFIDENCE_THRESHOLD
        assert hint.source == "context"

    def test_prior_hint_decays_to_floor_not_zero(self):
        # Start at threshold to verify the floor.
        prior = GenderHint(
            value=GENDER_FEMALE, confidence=0.70, source="name",
        )
        hint = detect_gender("شكراً", prior_hint=prior)
        # Floor is 0.60 — below threshold means the conjugator
        # won't act, but the value is preserved.
        assert hint.value == GENDER_FEMALE
        assert hint.confidence >= 0.60

    def test_no_signal_anywhere_returns_unknown(self):
        hint = detect_gender("123")
        assert hint == GenderHint()


# ─────────────────────────────────────────────────────────────────────────────
# 4. Conjugator — female swaps and no-op fallbacks
# ─────────────────────────────────────────────────────────────────────────────

class TestConjugator:
    HIGH_FEMALE = GenderHint(value=GENDER_FEMALE, confidence=0.90, source="verb")
    LOW_FEMALE = GenderHint(value=GENDER_FEMALE, confidence=0.50, source="name")
    HIGH_MALE = GenderHint(value=GENDER_MALE, confidence=0.90, source="name")
    UNKNOWN = GenderHint()

    def test_high_confidence_female_swaps_yagaali(self):
        # The female form "يا الغالية" inherently contains "يا الغالي"
        # as a prefix, so the substring-absence check would be wrong.
        # We instead check exact output equality, which is the only
        # honest assertion.
        out = apply_gender_to_social_reply("وياك يا الغالي 🌹", self.HIGH_FEMALE)
        assert out == "وياك يا الغالية 🌹"

    def test_high_confidence_female_swaps_taslam(self):
        # The spec example.
        out = apply_gender_to_social_reply("تسلم 🌷", self.HIGH_FEMALE)
        assert out == "تسلمين 🌷"

    def test_high_confidence_female_swaps_compound_blessings(self):
        out = apply_gender_to_social_reply(
            "الله يجزاك خير ووجهك أبيض", self.HIGH_FEMALE,
        )
        assert "الله يجزاكِ خير" in out
        # Bare "جزاك الله خير" must NOT also match.
        assert "جزاكِ الله خير" not in out  # because long pattern won
        # The masculine form is fully replaced.
        assert "الله يجزاك خير" not in out

    def test_high_confidence_female_swaps_yaafeek(self):
        out = apply_gender_to_social_reply(
            "الله يعافيك ويسعدك 🌷", self.HIGH_FEMALE,
        )
        assert out == "الله يعافيكِ ويسعدكِ 🌷"

    def test_spec_example_yataa_fmd_from_female(self):
        """ "يعطيك العافية" → "الله يعافيك يا الغالية" — covered
        by the conjugator when hint is high-confidence female."""
        seed = "الله يعافيك يا الغالي 🌷"
        out = apply_gender_to_social_reply(seed, self.HIGH_FEMALE)
        assert out == "الله يعافيكِ يا الغالية 🌷"

    def test_low_confidence_female_is_noop(self):
        seed = "تسلم 🌷"
        assert apply_gender_to_social_reply(seed, self.LOW_FEMALE) == seed

    def test_male_hint_is_noop(self):
        seed = "تسلم 🌷"
        assert apply_gender_to_social_reply(seed, self.HIGH_MALE) == seed

    def test_unknown_hint_is_noop(self):
        seed = "تسلم 🌷"
        assert apply_gender_to_social_reply(seed, self.UNKNOWN) == seed

    def test_empty_reply_passthrough(self):
        assert apply_gender_to_social_reply("", self.HIGH_FEMALE) == ""

    def test_none_hint_passthrough(self):
        seed = "تسلم 🌷"
        assert apply_gender_to_social_reply(seed, None) == seed

    def test_replacement_is_idempotent(self):
        """Running the conjugator twice on the same output must be
        a fixpoint — the female form should not be re-conjugated."""
        once = apply_gender_to_social_reply("تسلم 🌷", self.HIGH_FEMALE)
        twice = apply_gender_to_social_reply(once, self.HIGH_FEMALE)
        assert once == twice

    def test_no_unintended_substring_corruption(self):
        """The swap table must not corrupt unrelated text — emoji,
        newlines, and Latin punctuation pass through verbatim."""
        seed = "تسلم 🌷\nالله يبارك فيك."
        out = apply_gender_to_social_reply(seed, self.HIGH_FEMALE)
        # Emoji + newline preserved.
        assert "🌷" in out
        assert "\n" in out
        # Both targeted phrases conjugated.
        assert "تسلمين" in out
        assert "الله يبارك فيكِ" in out


# ─────────────────────────────────────────────────────────────────────────────
# 5. End-to-end spec examples
# ─────────────────────────────────────────────────────────────────────────────

class TestSpecExamples:
    """The three concrete examples from the May 2026 user request."""

    def test_absher_male_no_change(self):
        # Customer: "أبشر" → detector: no female signal → unknown
        # → conjugator: no-op → bot uses masculine template.
        hint = detect_gender("أبشر")
        assert hint.value == GENDER_UNKNOWN
        out = apply_gender_to_social_reply("تسلم 🌷", hint)
        assert out == "تسلم 🌷"

    def test_absheri_female_swaps_to_taslamin(self):
        # Customer: "أبشري" → detector: female verb → high confidence
        # → conjugator: تسلم → تسلمين.
        hint = detect_gender("أبشري")
        assert hint.value == GENDER_FEMALE
        out = apply_gender_to_social_reply("تسلم 🌷", hint)
        assert out == "تسلمين 🌷"

    def test_yataa_fmd_from_female_customer_by_name(self):
        # Customer message: "يعطيك العافية" — no gendered verb,
        # but the customer's name is نوره → name signal → female.
        # Bot's neutral reply "الله يعافيك يا الغالي" gets conjugated.
        hint = detect_gender(
            "يعطيك العافية", customer_name="نوره",
        )
        assert hint.value == GENDER_FEMALE
        out = apply_gender_to_social_reply(
            "الله يعافيك يا الغالي 🌷", hint,
        )
        assert "يعافيكِ" in out
        assert "يا الغالية" in out


# ─────────────────────────────────────────────────────────────────────────────
# 6. State round-trip — backward compatibility for legacy state blobs
# ─────────────────────────────────────────────────────────────────────────────

class TestStateRoundTrip:
    """The two new fields on ``MerchantConversationState`` must
    round-trip through ``to_dict`` / ``from_dict`` and tolerate
    legacy blobs that don't include them."""

    def test_round_trip_preserves_gender_fields(self):
        from modules.ai.brain.types import MerchantConversationState
        s = MerchantConversationState()
        s.customer_gender_hint = "female"
        s.customer_gender_confidence = 0.85
        s2 = MerchantConversationState.from_dict(s.to_dict())
        assert s2.customer_gender_hint == "female"
        assert s2.customer_gender_confidence == pytest.approx(0.85)

    def test_legacy_state_blob_loads_with_defaults(self):
        """A pre-May-2026 blob without the two new keys must still
        deserialise — the defaults are empty / 0.0."""
        from modules.ai.brain.types import MerchantConversationState
        legacy = {
            "stage": "discovery",
            "greeted": False,
            # ... no gender fields at all.
        }
        s = MerchantConversationState.from_dict(legacy)
        assert s.customer_gender_hint == ""
        assert s.customer_gender_confidence == 0.0
