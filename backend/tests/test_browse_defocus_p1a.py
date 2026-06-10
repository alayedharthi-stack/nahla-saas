"""
tests/test_browse_defocus_p1a.py
────────────────────────────────
P1a — global availability browse defocus + short visual captions.
"""
from __future__ import annotations

import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.product_breadth_policy import (  # noqa: E402
    global_availability_browse_requested,
    resolve_kb_active_product_ids,
)
from modules.ai.brain.state.state_relevance import (  # noqa: E402
    detect_topic_shift,
    validate_state_relevance,
)
from modules.ai.brain.types import MerchantConversationState  # noqa: E402
from services.product_resolver import (  # noqa: E402
    ProductResolution,
    format_product_card_caption,
)


def _state_with_talh_focus() -> MerchantConversationState:
    state = MerchantConversationState(turn=26)
    state.current_product_focus = {
        "id": 109,
        "title": "عسل طلح",
        "external_id": "e109",
    }
    return state


class TestGlobalAvailabilityBrowseDetection:
    def test_wesh_motawfer_now(self):
        assert global_availability_browse_requested("وش المتوفر الآن")

    def test_wesh_products_and_types(self):
        for msg in (
            "وش المنتجات",
            "وش الأنواع",
            "وش عندكم",
            "وش المنتجات كلها",
        ):
            assert global_availability_browse_requested(msg), msg

    def test_specific_product_not_global_browse(self):
        assert not global_availability_browse_requested("كم سعر طلح")
        assert not global_availability_browse_requested("ابي صورة الطلح")


class TestKbScopingDefocus:
    def test_global_browse_ignores_stale_focus(self):
        state = _state_with_talh_focus()
        assert resolve_kb_active_product_ids(state, "وش المتوفر الآن") is None

    def test_specific_turn_keeps_focus_scoping(self):
        state = _state_with_talh_focus()
        pids = resolve_kb_active_product_ids(state, "كم سعر طلح")
        assert pids == {109}

    def test_topic_shift_on_global_browse(self):
        assert detect_topic_shift("وش المتوفر الآن")

    def test_stale_focus_not_relevant_on_global_browse(self):
        state = _state_with_talh_focus()
        ctx = type("_Ctx", (), {
            "message": "وش المتوفر الآن",
            "state": state,
            "intent": type("I", (), {"name": "general", "slots": {}})(),
            "semantic_interpretation": None,
        })()
        verdict = validate_state_relevance(ctx)
        assert not verdict.stale_product_focus_relevant
        assert verdict.detected_topic_shift


class TestVisualProductCaption:
    def _resolution(self) -> ProductResolution:
        return ProductResolution(
            id=109,
            external_id="e109",
            title="عسل طلح",
            price="150",
            sale_price=None,
            image_url="https://example.com/talh.jpg",
            product_url="https://store.example/talh",
            description=(
                "عسل طلح طبيعي من جبال عسير. "
                "تاريخ الإنتاج 2025-01-01. "
                "نقية ومفحوصة مخبرياً."
            ),
            in_stock=True,
            can_checkout=True,
        )

    def test_visual_caption_omits_description(self):
        cap = format_product_card_caption(
            self._resolution(), include_description=False,
        )
        assert "عسل طلح" in cap
        assert "150" in cap
        assert "تاريخ الإنتاج" not in cap
        assert "جبال عسير" not in cap

    def test_detail_caption_includes_description(self):
        cap = format_product_card_caption(
            self._resolution(), include_description=True,
        )
        assert "عسل طلح" in cap
        assert "جبال عسير" in cap or "عسل طلح طبيعي" in cap

    def test_visual_caption_title_only_when_no_price(self):
        res = ProductResolution(
            id=1,
            external_id="e1",
            title="منتج تجريبي",
            price=None,
            sale_price=None,
            image_url=None,
            product_url=None,
            description="وصف طويل جداً لا يجب أن يظهر في بطاقة الصورة.",
            in_stock=True,
            can_checkout=True,
        )
        cap = format_product_card_caption(res, include_description=False)
        assert cap.strip() == "منتج تجريبي"
        assert "وصف" not in cap
