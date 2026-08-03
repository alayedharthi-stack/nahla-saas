"""P1-A — generic merchant isolation (platform-wide, not honey-only)."""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from modules.ai.brain.commerce.commerce_conversation_guard import (  # noqa: E402
    maybe_lock_order_category_context,
)
from modules.ai.brain.commerce.honey_browse_strategy import (  # noqa: E402
    apply_category_browse_strategy,
    should_collapse_to_category_types,
)
from modules.ai.brain.commerce.product_ordering_prompt import (  # noqa: E402
    build_product_ordering_prompt,
    build_short_product_order_clarify_reply,
    is_short_product_order_request,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    INTENT_START_ORDER,
    MerchantConversationState,
)
from modules.ai.prompts.high_priority_layer import SALES_BEHAVIOR_EXAMPLES  # noqa: E402


def _ctx(
    message: str,
    *,
    intent_name: str = "general",
    candidates: list | None = None,
) -> BrainContext:
    state = MerchantConversationState(greeted=True, stage="discovery")
    if candidates:
        state.last_search_candidates = candidates
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        intent=Intent(name=intent_name, confidence=0.9, raw_message=message),
        state=state,
        facts=CommerceFacts(
            store_name="متجر تجريبي عام",
            has_products=True,
            product_count=len(candidates or []),
            in_stock_count=len(candidates or []),
            orderable=True,
            snapshot_fresh=True,
            top_products=candidates or [],
        ),
    )


_HONEY_CATALOG = [
    {"id": 1, "title": "عسل طلح نجد 250 جرام", "category": "عسل", "quantity": 5},
    {"id": 2, "title": "عسل سمر الحجاز 500 جرام", "category": "عسل", "quantity": 3},
]

_SHOE_CATALOG = [
    {"id": 10, "title": "حذاء رياضي أبيض مقاس 40", "category": "أحذية", "quantity": 4},
    {"id": 11, "title": "حذاء رياضي أبيض مقاس 42", "category": "أحذية", "quantity": 2},
    {"id": 12, "title": "حذاء كاجوال بني مقاس 41", "category": "أحذية", "quantity": 3},
]

_PERFUME_CATALOG = [
    {"id": 20, "title": "عطر ورد 100ml", "category": "عطور", "quantity": 5},
    {"id": 21, "title": "عطر خشب 100ml", "category": "عطور", "quantity": 3},
]


class TestShortProductOrderClarify:
    def test_generic_shoe_order_does_not_require_honey_keywords(self) -> None:
        msg = "ابغى حذاء رياضي مقاس 42"
        assert is_short_product_order_request(msg)
        reply = build_short_product_order_clarify_reply(msg)
        assert "من أي نوع عسل" not in reply
        assert "كتالوج" in reply or "واتساب" in reply

    def test_honey_order_still_detected_via_quantity_hint(self) -> None:
        msg = "ابغى ربع كيلو عسل"
        assert is_short_product_order_request(msg)

    def test_honey_catalog_prompt_uses_catalog_titles(self) -> None:
        ctx = _ctx(
            "أبي عسل",
            intent_name=INTENT_START_ORDER,
            candidates=_HONEY_CATALOG,
        )
        prompt = build_product_ordering_prompt(ctx)
        assert "طلح" in prompt or "سمر" in prompt
        assert "من العسل:" not in prompt


class TestOrderCategoryLock:
    def test_shoe_catalog_locks_shoes_not_honey(self) -> None:
        state = MerchantConversationState(greeted=True)
        locked = maybe_lock_order_category_context(
            state,
            "ابي اطلب",
            catalog=_SHOE_CATALOG,
        )
        assert locked is True
        assert state.commerce_session.get("active_category") == "أحذية"
        assert state.commerce_session.get("active_category") != "عسل"

    def test_perfume_catalog_locks_perfume_category(self) -> None:
        state = MerchantConversationState(greeted=True)
        maybe_lock_order_category_context(
            state,
            "ابي اطلب",
            catalog=_PERFUME_CATALOG,
        )
        assert state.commerce_session.get("active_category") == "عطور"

    def test_honey_catalog_still_locks_via_tenant_category(self) -> None:
        state = MerchantConversationState(greeted=True)
        maybe_lock_order_category_context(
            state,
            "ابي اطلب",
            catalog=_HONEY_CATALOG,
        )
        assert state.commerce_session.get("active_category") == "عسل"


class TestCategoryBrowseStrategy:
    def test_honey_message_without_honey_catalog_no_collapse(self) -> None:
        assert should_collapse_to_category_types(
            "وش الخيارات؟",
            active_category="عسل",
            source="top_products",
            products=_SHOE_CATALOG,
        ) is False

    def test_shoe_catalog_generic_browse_collapses(self) -> None:
        result = apply_category_browse_strategy(
            _SHOE_CATALOG,
            message="وش الخيارات؟",
            active_category="أحذية",
            source="top_products",
        )
        titles = {p["title"] for p in result}
        assert len(result) <= 3
        assert any("حذاء" in t for t in titles)

    def test_honey_catalog_still_collapses_by_type(self) -> None:
        catalog = _HONEY_CATALOG + [
            {"id": 3, "title": "كريم سم النحل", "category": "عناية", "quantity": 5},
        ]
        result = apply_category_browse_strategy(
            catalog,
            message="وش الخيارات؟",
            active_category="عسل",
            source="top_products",
        )
        titles = " ".join(p["title"] for p in result)
        assert "كريم" not in titles
        assert "طلح" in titles
        assert "سمر" in titles


class TestHighPriorityExamples:
    def test_no_ayed_honey_price_dump_strings(self) -> None:
        blob = " ".join(
            part
            for example in SALES_BEHAVIOR_EXAMPLES
            for part in example
        )
        assert "عسل الطلح البلدي البري" not in blob
        assert "ربع كيلو 126" not in blob
        assert "حذاء رياضي" in blob or "عطر ورد" in blob


class TestTenantIsolation:
    def test_two_catalogs_lock_different_categories(self) -> None:
        shoe_state = MerchantConversationState(greeted=True)
        perfume_state = MerchantConversationState(greeted=True)
        maybe_lock_order_category_context(
            shoe_state,
            "ابي اطلب",
            catalog=_SHOE_CATALOG,
        )
        maybe_lock_order_category_context(
            perfume_state,
            "ابي اطلب",
            catalog=_PERFUME_CATALOG,
        )
        assert shoe_state.commerce_session.get("active_category") == "أحذية"
        assert perfume_state.commerce_session.get("active_category") == "عطور"
        assert (
            shoe_state.commerce_session.get("active_category")
            != perfume_state.commerce_session.get("active_category")
        )
