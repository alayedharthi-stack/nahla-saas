"""P1 — Arabic gender agreement guard (grammar-only, post-compose)."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.postprocess.gender_agreement_guard import (  # noqa: E402
    apply_gender_agreement_guard,
    validate_gender_agreement,
)
from modules.ai.brain.types import MerchantConversationState  # noqa: E402
from modules.ai.gender.address_guard import apply_address_gender_guard  # noqa: E402
from modules.ai.gender.context import (  # noqa: E402
    REPLY_STYLE_FEMININE,
    REPLY_STYLE_MASCULINE,
    REPLY_STYLE_NEUTRAL,
    SOURCE_EXPLICIT,
    SOURCE_INFERRED_NAME,
    CustomerGenderContext,
    persist_gender_hint,
    resolve_customer_gender_context,
    should_persist_gender_hint,
)
from modules.ai.gender.detector import (  # noqa: E402
    APPLY_CONFIDENCE_THRESHOLD,
    GENDER_FEMALE,
    GENDER_MALE,
    GenderHint,
    detect_gender,
)
from modules.ai.gender.neutral import contains_feminine_markers  # noqa: E402


def _male_profile() -> dict:
    return {
        "gender": "male",
        "gender_source": "profile",
        "gender_confidence": 0.95,
        "name": "محمد",
    }


def _female_profile() -> dict:
    return {
        "gender": "female",
        "gender_source": "profile",
        "gender_confidence": 0.95,
        "name": "نوره",
    }


class TestKnownMaleMismatchCorrected:
    def test_feminine_imperatives_rewritten_to_masculine(self) -> None:
        ctx = resolve_customer_gender_context(profile=_male_profile())
        original = "تفضلي وأرسلي العنوان 🌷"
        out = validate_gender_agreement(original, ctx).reply
        assert out != original
        assert "تفضلي" not in out
        assert "أرسلي" not in out
        assert "تفضل" in out
        assert "أرسل" in out
        assert "🌷" in out


class TestKnownFemaleMismatchCorrected:
    def test_masculine_imperatives_rewritten_to_feminine(self) -> None:
        ctx = resolve_customer_gender_context(profile=_female_profile())
        original = "تفضل 🌷 أرسل العنوان"
        out = validate_gender_agreement(original, ctx).reply
        assert "تفضلي" in out
        assert "أرسلي" in out
        assert original.replace("تفضل", "X") != out.replace("تفضلي", "X")


class TestUnknownGenderNeutralized:
    def test_only_address_words_changed_not_whole_reply(self) -> None:
        ctx = resolve_customer_gender_context(message="مرحبا")
        original = (
            "تمام 🌷 تفضلي وأرسلي العنوان واختاري الحجم "
            "ونكمل الطلب معك."
        )
        out = apply_gender_agreement_guard(original, gender_context=ctx).reply
        assert "ونكمل الطلب معك" in out
        assert "🌷" in out
        assert "تفضلي" not in out
        assert "أرسلي" not in out
        assert "اختاري" not in out
        assert not contains_feminine_markers(out)

    def test_unknown_does_not_inject_canned_sentence(self) -> None:
        ctx = resolve_customer_gender_context(message="123")
        original = "تفضل 🌷"
        out = validate_gender_agreement(original, ctx).reply
        assert "تقدر تختار" not in out
        assert "أرسل العنوان المختصر" not in out
        assert out.count("🌷") == 1


class TestCorrectReplyUnchanged:
    def test_male_correct_reply_passes_through(self) -> None:
        ctx = resolve_customer_gender_context(profile=_male_profile())
        original = "أبشر 🌷 تفضل وأرسل العنوان كامل مع المدينة."
        result = validate_gender_agreement(original, ctx)
        assert result.replaced is False
        assert result.reply == original

    def test_female_correct_reply_passes_through(self) -> None:
        ctx = resolve_customer_gender_context(profile=_female_profile())
        original = "أبشري 🌷 تفضلي وأرسلي العنوان كامل."
        result = validate_gender_agreement(original, ctx)
        assert result.replaced is False
        assert result.reply == original

    def test_neutral_reply_without_gender_markers_unchanged(self) -> None:
        ctx = resolve_customer_gender_context(message="مرحبا")
        original = "تمام 🌷 وصلت رسالتك ونكمل الطلب."
        result = validate_gender_agreement(original, ctx)
        assert result.replaced is False
        assert result.reply == original


class TestNoFullTemplateReplacement:
    def test_long_llm_reply_structure_preserved(self) -> None:
        ctx = resolve_customer_gender_context(profile=_female_profile())
        original = (
            "هذه بعض الأنواع المتوفرة:\n"
            "1. *عسل طلح* — 350 ريال\n"
            "تفضل واختر رقم المنتج 🌷"
        )
        out = validate_gender_agreement(original, ctx).reply
        assert "1. *عسل طلح* — 350 ريال" in out
        assert "تفضلي" in out
        assert out.startswith("هذه بعض الأنواع")

    def test_swap_count_is_small(self) -> None:
        ctx = resolve_customer_gender_context(profile=_male_profile())
        result = apply_gender_agreement_guard(
            "تفضلي 🌷",
            gender_context=ctx,
        )
        assert result.swap_count <= 2


class TestExplicitGenderPersistence:
    def test_explicit_beats_inferred_name(self) -> None:
        assert should_persist_gender_hint(
            stored_source=SOURCE_INFERRED_NAME,
            stored_confidence=0.75,
            new_hint=GenderHint(value=GENDER_FEMALE, confidence=0.90, source="verb"),
        )
        assert not should_persist_gender_hint(
            stored_source=SOURCE_EXPLICIT,
            stored_confidence=0.90,
            new_hint=GenderHint(value=GENDER_MALE, confidence=0.75, source="name"),
        )

    def test_explicit_female_survives_future_turns(self) -> None:
        state = MerchantConversationState()
        persist_gender_hint(
            state,
            hint=GenderHint(value=GENDER_FEMALE, confidence=0.90, source="verb"),
        )
        for msg in ("شكراً", "تمام", "123"):
            detected = detect_gender(
                msg,
                customer_name="محمد",
                prior_hint=GenderHint(
                    value=state.customer_gender_hint,
                    confidence=state.customer_gender_confidence,
                    source=state.customer_gender_source,
                ),
            )
            persist_gender_hint(state, hint=detected)
            ctx = resolve_customer_gender_context(
                message=msg,
                customer_name="محمد",
                state=state,
            )
            assert ctx.gender == GENDER_FEMALE, msg
            assert ctx.confidence_score >= APPLY_CONFIDENCE_THRESHOLD, msg


    def test_structured_masculine_state_rewrites_kamli(self) -> None:
        ctx = CustomerGenderContext(
            gender=GENDER_MALE,
            confidence="high",
            confidence_score=0.95,
            source="profile",
            reply_style=REPLY_STYLE_MASCULINE,
        )
        result = apply_gender_agreement_guard(
            "كملي لي اسم العائلة",
            gender_context=ctx,
            message="انا رجل ولست امرأة",
        )
        assert "كملي" not in result.reply
        assert result.replaced is True
        assert result.gender == GENDER_MALE

    def test_self_id_phrase_does_not_infer_gender(self) -> None:
        hint = detect_gender("انا رجل ولست امرأة")
        assert hint.value != GENDER_MALE or hint.source != "verb"



class TestAddressGuardModes:
    @pytest.mark.parametrize(
        ("style", "src", "dst"),
        [
            (REPLY_STYLE_MASCULINE, "تفضلي", "تفضل"),
            (REPLY_STYLE_FEMININE, "تفضل", "تفضلي"),
            (REPLY_STYLE_NEUTRAL, "تفضلي", "لو سمحت"),
        ],
    )
    def test_single_token_swap(self, style: str, src: str, dst: str) -> None:
        out = apply_address_gender_guard(f"{src} 🌷", style).text
        assert dst in out
        assert out.endswith("🌷")
        if style == REPLY_STYLE_FEMININE:
            assert out.startswith(dst)
        else:
            assert not out.startswith(src)
