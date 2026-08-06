"""CARD-01 — topic-scoped surgical availability enforcement (platform-wide)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    _UNKNOWN_REPLY_AR,
    apply_product_availability_truth_guard,
)


def _sku(pid: int, title: str, *, checkout: bool) -> dict:
    from core.product_entity_resolution import family_key_from_title  # noqa: E402

    return {
        "id": pid,
        "title": title,
        "sku": f"SKU-{pid}",
        "external_id": f"ext-{pid}",
        "can_checkout": checkout,
        "in_stock": checkout,
        "years": [],
        "weights": [],
        "family_key": family_key_from_title(title),
    }


def _ctx(*, skus: list, connected: bool = True) -> dict:
    return {
        "platform_connected": connected,
        "focus_product": None,
        "recommended_product_ids": [],
        "catalog_skus": skus,
        "kb_signals": [],
        "product_links": [],
    }


_ME49677_PAYMENT_REPLY = (
    "وسائل الدفع المتاحة تظهر لك عند إتمام الطلب. "
    "يمكنك اختيار الطريقة الأنسب لك. "
    "إذا تحتاج مساعدة في شيء ثاني، خبرني!"
)


class TestCARD01TopicScopePayment:
    def setup_method(self) -> None:
        self._prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"

    def teardown_method(self) -> None:
        if self._prev is None:
            os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
        else:
            os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = self._prev

    def test_me49677_payment_reply_not_replaced_with_unknown(self) -> None:
        """ME 49677 — pay_now + payment prose must not become _UNKNOWN_REPLY_AR."""
        result = apply_product_availability_truth_guard(
            reply=_ME49677_PAYMENT_REPLY,
            availability_context=_ctx(skus=[_sku(1, "حذاء رياضي أبيض", checkout=True)]),
            inbound_text="أبغى أدفع",
            decision_topic="pay_now",
            tenant_id=1,
        )
        assert result.reply != _UNKNOWN_REPLY_AR
        assert "الدفع" in result.reply
        assert "إتمام الطلب" in result.reply
        assert result.reason == "topic_scope_skip_full_rewrite"

    def test_ask_payment_info_preserves_payment_keywords(self) -> None:
        reply = (
            "عند إتمام الطلب تقدر تختار الدفع عند الاستلام أو التحويل البنكي."
        )
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_ctx(skus=[]),
            inbound_text="كيف الدفع؟",
            decision_topic="ask_payment_info",
            tenant_id=99,
        )
        assert result.reply != _UNKNOWN_REPLY_AR
        assert "الدفع" in result.reply or "التحويل" in result.reply


class TestCARD01CatalogListPreservation:
    def setup_method(self) -> None:
        self._prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"

    def teardown_method(self) -> None:
        if self._prev is None:
            os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
        else:
            os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = self._prev

    def test_catalog_sizes_prices_not_wiped_to_unknown(self) -> None:
        """ME 48714-class — grounded size/price list must survive enforce."""
        reply = (
            "المقاسات المتوفرة:\n"
            "• 36 — 120 ريال\n"
            "• 38 — 130 ريال\n"
            "• 40 — 140 ريال"
        )
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_ctx(
                skus=[_sku(10, "بنطال رياضي", checkout=True)],
            ),
            inbound_text="وش مقاسات البنطال؟",
            decision_topic="ask_product",
            tenant_id=99,
        )
        assert result.reply != _UNKNOWN_REPLY_AR
        assert "120" in result.reply
        assert "36" in result.reply
        assert "38" in result.reply


class TestCARD01HonestNegativePreserved:
    def setup_method(self) -> None:
        self._prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"

    def teardown_method(self) -> None:
        if self._prev is None:
            os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
        else:
            os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = self._prev

    def test_honest_negative_oos_not_replaced(self) -> None:
        """ME 47435-class — honest negative claim with unresolved evidence."""
        reply = "للأسف ما عندنا حذاء رياضي حالياً، لكن عندنا أحذية كاجوال."
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_ctx(skus=[]),
            inbound_text="عندكم حذاء رياضي؟",
            decision_topic="ask_product",
            tenant_id=99,
        )
        assert result.reply != _UNKNOWN_REPLY_AR
        assert "حذاء" in result.reply or "أحذية" in result.reply


class TestCARD01ScopedSurgicalEnforcement:
    def setup_method(self) -> None:
        self._prev = os.environ.get("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE")
        os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = "enforce"

    def teardown_method(self) -> None:
        if self._prev is None:
            os.environ.pop("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", None)
        else:
            os.environ["NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE"] = self._prev

    def test_scoped_ungrounded_positive_stripped_or_unknown(self) -> None:
        result = apply_product_availability_truth_guard(
            reply="متوفر",
            availability_context=_ctx(skus=[], connected=False),
            inbound_text="هل عطر ورد متوفر؟",
            tenant_id=99,
        )
        assert result.replaced is True
        assert "متوفر" not in result.reply
        assert result.reply == _UNKNOWN_REPLY_AR

    def test_scoped_mixed_reply_strips_claim_line_only(self) -> None:
        reply = (
            "تفاصيل المنتج:\n"
            "• عطر ورد 100ml — 199 ريال\n"
            "متوفر الآن للطلب."
        )
        result = apply_product_availability_truth_guard(
            reply=reply,
            availability_context=_ctx(skus=[], connected=False),
            inbound_text="هل عطر ورد متوفر؟",
            tenant_id=99,
        )
        assert result.replaced is True
        assert "199" in result.reply
        assert "متوفر" not in result.reply
        assert result.reply != _UNKNOWN_REPLY_AR
