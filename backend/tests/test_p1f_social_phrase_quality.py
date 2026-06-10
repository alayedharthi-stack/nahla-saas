"""P1-F — strip-only social phrase guard + fallback pool hygiene."""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.postprocess.social_phrase_quality_guard import (  # noqa: E402
    apply_social_phrase_quality_guard,
)

_FORBIDDEN_POOL_MARKERS = (
    "يطري",
    "يطرّي",
    "يطرى",
    "دوم بخير",
    "تدوم",
    "تحت أمرك",
    "بالخدمة",
    "بالخدمه",
    "ولك بمثل ما دعيت",
    "ولك بالمثل",
)

_SOCIAL_POOLS = (
    T._SOCIAL_THANKS_VARIANTS,
    T._SOCIAL_BLESSING_VARIANTS,
    T._SOCIAL_BASMALA_VARIANTS,
    T._SOCIAL_GENERAL_COURTESY_VARIANTS,
    T._SOCIAL_WARM_ACK_VARIANTS,
    T._SOCIAL_COMPLIMENT_VARIANTS,
    T._SOCIAL_STRONG_PRAISE_VARIANTS,
    T._SOCIAL_EID_GREETING_VARIANTS,
    T._SOCIAL_DUA_VARIANTS,
    T._SOCIAL_CONDOLENCE_VARIANTS,
)

_OPERATIONAL_SAMPLES = (
    "طلبك *عسل سدر* تحت المراجعة — ببلّغك فور التأكيد 🌷",
    "رقم الآيبان: SA1234567890123456789012",
    "السعر 120 ريال للكيلو شامل الضريبة.",
    "تم تسجيل الدفع — طلبك قيد المراجعة.",
    "رقم التتبع: 1234567890 — الشحنة في الطريق.",
)


class TestFallbackPoolHygiene:
    @pytest.mark.parametrize("pool", _SOCIAL_POOLS)
    def test_fallback_pools_have_no_forbidden_markers(self, pool: list[str]) -> None:
        for text in pool:
            for marker in _FORBIDDEN_POOL_MARKERS:
                assert marker not in text, f"{marker!r} in pool entry {text!r}"


class TestSocialPhraseQualityGuard:
    @pytest.mark.parametrize(
        "raw, forbidden",
        [
            ("الله يطري أيامك ويسعدك ❤️", "يطري"),
            ("دوم بخير يا الغالي", "دوم بخير"),
            ("حياك الله 🌷 تحت أمرك", "تحت أمرك"),
            ("تسلم 🤍 بالخدمة", "بالخدمة"),
        ],
    )
    def test_guard_strips_without_replacement(self, raw: str, forbidden: str) -> None:
        result = apply_social_phrase_quality_guard(raw, tenant_id=1)
        assert result.stripped is True
        assert forbidden not in result.reply

    @pytest.mark.parametrize("sample", _OPERATIONAL_SAMPLES)
    def test_guard_preserves_operational_facts(self, sample: str) -> None:
        result = apply_social_phrase_quality_guard(sample, tenant_id=1)
        assert result.stripped is False
        assert result.reply == sample
