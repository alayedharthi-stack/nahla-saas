"""Catalog inquiry subject resolution + OOS fact focus export regressions."""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.catalog_search_evidence import (  # noqa: E402
    apply_catalog_search_evidence_gate,
    has_catalog_search_evidence,
)
from modules.ai.brain.commerce.commerce_focus_owner import (  # noqa: E402
    product_focus_identity,
    set_product_focus,
)
from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: E402
    extract_inquiry_subject,
)
from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.execution.search import (  # noqa: E402
    resolve_search_result_product_for_focus,
)
from modules.ai.brain.product_discovery_gate import (  # noqa: E402
    _deterministic_commerce_subject,
    _normalize_ar,
    _resolved_product_query,
    product_discovery_block_reason,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


def _ctx(
    message: str,
    *,
    intent_name: str = "ask_product",
    slots: dict[str, Any] | None = None,
    tenant_id: int = 1,
    customer_phone: str = "966500000001",
) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        message=message,
        intent=Intent(
            name=intent_name,
            confidence=0.9,
            raw_message=message,
            slots=dict(slots or {}),
            extraction_method="llm",
        ),
        state=MerchantConversationState(greeted=True, stage="exploring"),
        history=[],
        facts=CommerceFacts(has_products=True, orderable=True, product_count=5),
    )


def _perfume_rose_fact() -> dict[str, Any]:
    return {
        "id": "perf-rose-oos-1",
        "external_id": "SKU-PERF-ROSE-OOS",
        "title": "عطر ورد 100ml",
        "price": 149,
        "in_stock": False,
        "can_checkout": False,
    }


def _shirt_blue_fact() -> dict[str, Any]:
    return {
        "product_id": "shirt-blue-oos-9",
        "title": "قميص قطني أزرق",
        "price": 89,
        "in_stock": False,
        "can_checkout": False,
    }


def _watch_silver_orderable() -> dict[str, Any]:
    return {
        "id": "watch-silver-1",
        "external_id": "SKU-WATCH-SILVER",
        "title": "ساعة فضية",
        "price": 299,
        "in_stock": True,
        "can_checkout": True,
    }


class TestInquirySubjectReachesResolvedProductQuery:
    @pytest.mark.parametrize(
        "message,expected",
        [
            ("عندكم عطر ورد؟", "عطر ورد"),
            ("عندكم قميص قطني أزرق؟", "قميص قطني ازرق"),
            ("عندكم عطر ورد 100ml؟", "عطر ورد 100ml"),
        ],
    )
    def test_extract_inquiry_subject_full_names(self, message: str, expected: str) -> None:
        assert _normalize_ar(extract_inquiry_subject(message) or "") == _normalize_ar(expected)

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("عندكم عطر ورد؟", "عطر ورد"),
            ("عندكم قميص قطني أزرق؟", "قميص قطني ازرق"),
            ("عندكم عطر ورد 100ml؟", "عطر ورد 100ml"),
        ],
    )
    def test_resolved_product_query_uses_full_inquiry_subject(
        self,
        message: str,
        expected: str,
    ) -> None:
        ctx = _ctx(message, slots={"product_query": expected.split()[0]})
        assert _normalize_ar(_resolved_product_query(ctx)) == _normalize_ar(expected)

    def test_deterministic_subject_wins_over_truncated_intent_slot(self) -> None:
        ctx = _ctx("عندكم عطر ورد؟", slots={"product_query": "عطر"})
        assert _normalize_ar(_deterministic_commerce_subject(ctx)) == _normalize_ar("عطر ورد")
        assert _normalize_ar(_resolved_product_query(ctx)) == _normalize_ar("عطر ورد")
        assert _normalize_ar(_resolved_product_query(ctx)) != _normalize_ar("عطر")


class TestNonProductTopicsDoNotBecomeCatalogSearch:
    def test_shipping_geo_blocked_by_discovery_gate(self) -> None:
        ctx = _ctx("كم رسوم الشحن للرياض؟", intent_name="general")
        assert product_discovery_block_reason(ctx) in {"non_commerce", "logistics_context"}

    def test_logistics_context_blocked_without_product_browse(self) -> None:
        ctx = _ctx("معك مندوب سمسا", intent_name="general")
        assert product_discovery_block_reason(ctx) == "logistics_context"

    def test_geo_query_rejected_by_catalog_search_evidence_gate(self) -> None:
        ctx = _ctx("عندكم توصيل الرياض؟", intent_name="ask_product")
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "رياض", "source": "order_product_query"},
            reason="test",
        )
        assert has_catalog_search_evidence(ctx, "رياض", decision) is False
        out = apply_catalog_search_evidence_gate(ctx, decision)
        assert out.action != ACTION_SEARCH_PRODUCTS


class TestResolveSearchResultProductForFocus:
    def test_singular_orderable_result_still_wins(self) -> None:
        product = _watch_silver_orderable()
        resolved = resolve_search_result_product_for_focus(
            products=[product],
            catalog_fact_products=[_perfume_rose_fact()],
            query="ساعة فضية",
        )
        assert resolved is product
        assert product_focus_identity(resolved) == "SKU-WATCH-SILVER"

    def test_singular_oos_fact_with_specific_query_exports_product(self) -> None:
        fact = _shirt_blue_fact()
        resolved = resolve_search_result_product_for_focus(
            products=[],
            catalog_fact_products=[fact],
            query="قميص قطني أزرق",
        )
        assert resolved is fact
        assert product_focus_identity(resolved) == "shirt-blue-oos-9"

    def test_singular_oos_fact_perfume_specific_query(self) -> None:
        fact = _perfume_rose_fact()
        resolved = resolve_search_result_product_for_focus(
            products=[],
            catalog_fact_products=[fact],
            query="عطر ورد 100ml",
        )
        assert resolved is fact
        assert product_focus_identity(resolved) == "SKU-PERF-ROSE-OOS"

    def test_generic_one_token_category_with_singular_fact_returns_none(self) -> None:
        fact = {
            "id": "cat-perfume-1",
            "title": "عطر",
            "price": 99,
        }
        assert (
            resolve_search_result_product_for_focus(
                products=[],
                catalog_fact_products=[fact],
                query="عطر",
            )
            is None
        )

    def test_generic_accessory_category_with_singular_fact_returns_none(self) -> None:
        fact = {
            "external_id": "SKU-BAG-1",
            "title": "حقيبة",
            "price": 120,
        }
        assert (
            resolve_search_result_product_for_focus(
                products=[],
                catalog_fact_products=[fact],
                query="حقيبة",
            )
            is None
        )

    def test_multiple_facts_returns_none(self) -> None:
        assert (
            resolve_search_result_product_for_focus(
                products=[],
                catalog_fact_products=[_perfume_rose_fact(), _shirt_blue_fact()],
                query="عطر ورد 100ml",
            )
            is None
        )

    def test_title_only_fact_without_canonical_id_returns_none(self) -> None:
        assert (
            resolve_search_result_product_for_focus(
                products=[],
                catalog_fact_products=[{"title": "عطر ورد 100ml", "price": 149}],
                query="عطر ورد 100ml",
            )
            is None
        )

    def test_multiple_orderable_products_returns_none(self) -> None:
        assert (
            resolve_search_result_product_for_focus(
                products=[_watch_silver_orderable(), _watch_silver_orderable()],
                catalog_fact_products=[],
                query="ساعة",
            )
            is None
        )


class TestPipelineFocusOwnerIsolation:
    """Mirror pipeline focus-pin guard — tenant state stays isolated."""

    def test_search_result_product_pins_focus_per_tenant_state(self) -> None:
        tenant_a = MerchantConversationState(greeted=True, stage="discovery", turn=2)
        tenant_b = MerchantConversationState(greeted=True, stage="discovery", turn=2)
        fact_a = _perfume_rose_fact()
        fact_b = {
            "id": "tenant-b-perf",
            "external_id": "SKU-B-PERF",
            "title": "عطر ورد 100ml",
            "price": 159,
        }
        result_a = resolve_search_result_product_for_focus(
            products=[],
            catalog_fact_products=[fact_a],
            query="عطر ورد 100ml",
        )
        result_b = resolve_search_result_product_for_focus(
            products=[],
            catalog_fact_products=[fact_b],
            query="عطر ورد 100ml",
        )
        assert result_a is not None and result_b is not None
        set_product_focus(tenant_a, result_a, reason="executor_product_search", turn=2)
        set_product_focus(tenant_b, result_b, reason="executor_product_search", turn=2)
        assert product_focus_identity(tenant_a.current_product_focus) == "SKU-PERF-ROSE-OOS"
        assert product_focus_identity(tenant_b.current_product_focus) == "SKU-B-PERF"
        assert tenant_a.current_product_focus is not tenant_b.current_product_focus
