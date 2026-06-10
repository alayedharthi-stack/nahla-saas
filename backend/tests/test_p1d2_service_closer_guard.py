"""P1-D-2 regression: service-closer guard hardening + social template cleanup."""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.fallback_policy import strip_closer_segments  # noqa: E402
from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.compose.mirror_replies import mirror_reply  # noqa: E402
from modules.ai.brain.postprocess.service_closer_guard import (  # noqa: E402
    apply_service_closer_guard,
)

_LIVE_CS_REPLY = (
    "الله يسعدك 🌷 إذا تحتاج أي مساعدة أو عندك استفسار، أنا هنا!"
)

_FORBIDDEN_SOCIAL = (
    "كيف حالك اليوم",
    "كيف حالك",
    "وش الخدمة",
    "وش اللي تحتاجه",
    "تحت أمرك",
    "أنا هنا",
    "إذا تحتاج",
    "مساعدة",
    "استفسار",
)

_OPERATIONAL_SAMPLES = (
    "طلبك *عسل سدر* تحت المراجعة — ببلّغك فور التأكيد 🌷",
    "رقم الآيبان: SA1234567890123456789012",
    "السعر 120 ريال للكيلو شامل الضريبة.",
    "تم تسجيل الدفع — طلبك قيد المراجعة.",
    "رقم التتبع: 1234567890 — الشحنة في الطريق.",
)


class TestLiveStringGuard:
    def test_strips_exact_live_cs_reply(self) -> None:
        result = apply_service_closer_guard(_LIVE_CS_REPLY, tenant_id=1)
        assert result.stripped is True
        reply = result.reply
        assert "إذا تحتاج" not in reply
        assert "مساعدة" not in reply
        assert "استفسار" not in reply
        assert "أنا هنا" not in reply
        assert "الله يسعدك" in reply

    def test_greeting_social_tail_stripped(self) -> None:
        raw = "يا هلا 🌷 كيف حالك اليوم؟"
        cleaned, stripped = strip_closer_segments(raw)
        assert stripped is True
        assert "كيف حالك" not in cleaned
        assert "يا هلا" in cleaned


class TestSocialTemplatePools:
    _POOLS = (
        T._SOCIAL_THANKS_VARIANTS,
        T._SOCIAL_BLESSING_VARIANTS,
        T._SOCIAL_BASMALA_VARIANTS,
        T._SOCIAL_GENERAL_COURTESY_VARIANTS,
        T._SOCIAL_WARM_ACK_VARIANTS,
        T._SOCIAL_COMPLIMENT_VARIANTS,
    )

    @pytest.mark.parametrize("pool", _POOLS)
    def test_pools_have_no_cs_closers(self, pool: list[str]) -> None:
        for text in pool:
            for phrase in _FORBIDDEN_SOCIAL:
                assert phrase not in text, f"{phrase!r} in pool entry {text!r}"


class TestMirrorReplies:
    @pytest.mark.parametrize("msg", [
        "تسلم",
        "تسلم يا غالي",
        "تسلموا",
        "تسلمون",
    ])
    def test_mirror_has_no_assistance_tail(self, msg: str) -> None:
        reply = mirror_reply(msg)
        assert reply is not None
        assert "تحت أمرك" not in reply
        assert "تحت أمركم" not in reply
        assert "أي وقت" not in reply


class TestOperationalPreserved:
    @pytest.mark.parametrize("sample", _OPERATIONAL_SAMPLES)
    def test_operational_text_unchanged(self, sample: str) -> None:
        cleaned, stripped = strip_closer_segments(sample)
        assert stripped is False
        assert cleaned == sample

    @pytest.mark.parametrize("sample", _OPERATIONAL_SAMPLES)
    def test_guard_leaves_operational_intact(self, sample: str) -> None:
        result = apply_service_closer_guard(sample, tenant_id=1)
        assert result.stripped is False
        assert result.reply == sample


class TestSecondPassSimulation:
    def test_post_safety_net_closer_removed_on_second_apply(self) -> None:
        after_nets = (
            "تمام 🌷\n\n"
            "إذا تحتاج أي مساعدة أو عندك استفسار، أنا هنا!"
        )
        first = apply_service_closer_guard(after_nets, tenant_id=1)
        second = apply_service_closer_guard(first.reply, tenant_id=1)
        assert second.stripped or first.stripped
        combined = second.reply
        assert "إذا تحتاج" not in combined
        assert "استفسار" not in combined
        assert "أنا هنا" not in combined

    def test_non_commerce_metadata_enables_sales_strip(self) -> None:
        raw = (
            "يا هلا 🌷\n\n"
            "إذا تحتاج أي تفاصيل عن المنتجات أو الأسعار، أنا هنا للمساعدة!"
        )
        result = apply_service_closer_guard(
            raw,
            inbound_metadata={"non_commerce_category": "eid_greeting"},
            block_commerce_escalation=True,
            tenant_id=1,
        )
        assert result.stripped is True
        assert "المنتجات" not in result.reply
        assert "أنا هنا للمساعدة" not in result.reply
