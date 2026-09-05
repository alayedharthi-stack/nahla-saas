"""P1-D-1 regression: template residual / canned fallback cleanup."""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.fallback_policy import (  # noqa: E402
    empty_reply_fallback,
    is_personality_fallback_text,
    operational_compose_error_fallback,
    strip_closer_segments,
)
from core.order_flow import context_aware_dedup_fallback  # noqa: E402
from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.postprocess.service_closer_guard import (  # noqa: E402
    apply_service_closer_guard,
)


class TestDedupNoPersonalityPool:
    def test_dedup_operational_substitute_empty_without_state(self):
        class _FakeDB:
            pass

        import core.order_flow as of

        def _fake_load(db, tenant_id, phone):
            return None, {}

        def _fake_focus(bs):
            return {}

        orig_load = of._load_brain_state
        orig_focus = of._focus_summary
        try:
            of._load_brain_state = _fake_load
            of._focus_summary = _fake_focus
            reply = context_aware_dedup_fallback(
                _FakeDB(),
                tenant_id=1,
                phone="966500000001",
                history=[],
                default_fallback="",
                inbound_text="مرحبا",
            )
        finally:
            of._load_brain_state = orig_load
            of._focus_summary = orig_focus

        assert reply == ""
        assert "أي نقطة تحب أوضحها" not in reply

    def test_personality_pool_markers_detected(self):
        assert is_personality_fallback_text(
            "هذي قريبة من سؤال قبل قليل 🌷 أي نقطة تحب أوضحها لك أكثر؟"
        )


class TestEmptyReplyFallback:
    def test_empty_reply_not_cs_opener(self):
        fb = empty_reply_fallback()
        assert fb
        assert "كيف أقدر أساعدك" not in fb
        assert "أنا هنا للمساعدة" not in fb


class TestGenericFallbackRetired:
    def test_generic_fallback_not_sales_cs(self):
        text = T.generic_fallback(variant=0)
        assert text == operational_compose_error_fallback()
        assert "يمكنني مساعدتك في البحث" not in text
        assert "أنا هنا لمساعدتك" not in text


class TestServiceCloserGuard:
    def test_non_commerce_strips_sales_closer(self):
        raw = (
            "يا هلا 🌷\n\n"
            "إذا تحتاج أي تفاصيل عن المنتجات أو الأسعار، أنا هنا للمساعدة!"
        )
        result = apply_service_closer_guard(
            raw,
            inbound_metadata={"non_commerce_category": "eid_greeting"},
            tenant_id=1,
        )
        assert result.stripped is True
        assert "المنتجات أو الأسعار" not in result.reply
        assert "أنا هنا للمساعدة" not in result.reply
        assert "كيف أقدر أساعدك" not in result.reply

    def test_strip_does_not_add_canned_replacement(self):
        raw = "رد مفيد.\n\nكيف أقدر أساعدك؟"
        cleaned, stripped = strip_closer_segments(raw, non_commerce=False)
        assert stripped is True
        assert cleaned == "رد مفيد."
        assert "كيف أقدر أساعدك" not in cleaned


class TestOperationalDeterministicPreserved:
    def test_dedup_payment_under_review_still_operational(self):
        class _FakeDB:
            pass

        summary_state = {
            "payment_receipt_received": True,
            "selected_product": "عسل طلح",
        }

        import core.order_flow as of

        def _fake_load(db, tenant_id, phone):
            return None, {"order_prep": summary_state}

        def _fake_focus(bs):
            return dict(summary_state)

        orig_load = of._load_brain_state
        orig_focus = of._focus_summary
        try:
            of._load_brain_state = _fake_load
            of._focus_summary = _fake_focus
            reply = context_aware_dedup_fallback(
                _FakeDB(),
                tenant_id=1,
                phone="966500000001",
                history=[],
                default_fallback="",
                inbound_text="تمام",
                decision_action="propose_draft_order",
            )
        finally:
            of._load_brain_state = orig_load
            of._focus_summary = orig_focus

        assert "تحت المراجعة" in reply

    def test_dedup_active_order_nudge_still_operational(self):
        class _FakeDB:
            pass

        summary_state = {
            "selected_product": "عسل سدر",
            "price": 120,
            "currency": "SAR",
        }

        import core.order_flow as of

        def _fake_load(db, tenant_id, phone):
            return None, {"order_prep": summary_state}

        def _fake_focus(bs):
            return dict(summary_state)

        orig_load = of._load_brain_state
        orig_focus = of._focus_summary
        try:
            of._load_brain_state = _fake_load
            of._focus_summary = _fake_focus
            reply = context_aware_dedup_fallback(
                _FakeDB(),
                tenant_id=1,
                phone="966500000001",
                history=[],
                default_fallback="",
                inbound_text="ok",
                decision_action="propose_draft_order",
            )
        finally:
            of._load_brain_state = orig_load
            of._focus_summary = orig_focus

        assert "عسل سدر" in reply
        assert "120" in reply
