"""P0 — catalog browse must present real products (names, prices, images)."""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.catalog_browse_reply import (  # noqa: E402
    build_catalog_browse_attachments,
    build_catalog_browse_reply,
    is_vague_browse_reply,
    should_rewrite_vague_browse_reply,
)
from modules.ai.brain.commerce.commerce_browse_category_guard import (  # noqa: E402
    filter_products_to_browse_category,
)
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)

_HONEY_CATALOG: List[Dict[str, Any]] = [
    {
        "id": 1,
        "title": "عسل طلح نجد البري",
        "price": "350",
        "variants_summary": "1 كيلو",
        "description": "عسل طلح طبيعي من مراعي نجد.",
        "image_url": "https://cdn.example/honey1.jpg",
        "product_url": "https://shop.example/honey1",
        "can_checkout": True,
        "in_stock": True,
        "catalog_status": "active",
    },
    {
        "id": 2,
        "title": "عسل سمر الحجاز",
        "price": "180",
        "variants": [
            {"title": "500 جم", "price": "180"},
            {"title": "1 كيلو", "price": "350"},
        ],
        "description": "عسل سمر أصلي من الحجاز.",
        "image_url": "https://cdn.example/honey2.jpg",
        "product_url": "https://shop.example/honey2",
        "can_checkout": True,
        "in_stock": True,
        "catalog_status": "active",
    },
    {
        "id": 3,
        "title": "العسل الصيفي",
        "price": "220",
        "variants_summary": "1 كيلو",
        "description": "عسل صيفي موسمي.",
        "image_url": "https://cdn.example/honey3.jpg",
        "can_checkout": True,
        "in_stock": True,
        "catalog_status": "active",
    },
]

_CREAM_PRODUCT = {
    "id": 99,
    "title": "كريم سم النحل",
    "category": "كريم",
    "price": "90",
    "can_checkout": True,
    "in_stock": True,
    "catalog_status": "active",
}

_VAGUE_REPLY = "عندنا أنواع مميزة 🍯\nأي نوع يناسبك أكثر؟"


def _compose_search(
    products: List[Dict[str, Any]],
    *,
    message: str = "وش الأنواع اللي عندكم",
    source: str = "global_browse",
) -> tuple[str, ActionResult]:
    composer = DefaultComposer()
    decision = Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={"query": "", "source": source},
    )
    result = ActionResult(
        success=True,
        data={"products": products},
    )
    ctx = BrainContext(
        tenant_id=33,
        customer_phone="966500000001",
        message=message,
        intent=Intent(name="ask_product", confidence=0.9, raw_message=message),
        state=MerchantConversationState(greeted=True, stage="discovery"),
        facts=CommerceFacts(has_products=True, orderable=True),
    )

    async def _run() -> str:
        return await composer.compose(decision, result, ctx)

    reply = asyncio.run(_run())
    return reply, result


class TestGlobalBrowseReturnsActualProducts:
    def test_compose_lists_catalog_names_and_prices(self) -> None:
        reply, result = _compose_search(_HONEY_CATALOG)
        assert "عسل طلح نجد البري" in reply
        assert "عسل سمر الحجاز" in reply
        assert "350" in reply or "180" in reply
        assert not is_vague_browse_reply(reply)
        assert result.data.get("chosen_path") == "product_search_results"
        assert len(result.data.get("pending_candidates") or []) >= 1

    def test_vague_reply_rewritten_when_products_exist(self) -> None:
        assert is_vague_browse_reply(_VAGUE_REPLY)
        assert should_rewrite_vague_browse_reply(_VAGUE_REPLY, _HONEY_CATALOG)
        rewritten = build_catalog_browse_reply(_HONEY_CATALOG)
        assert "عسل طلح نجد البري" in rewritten
        assert "عندنا أنواع مميزة" not in rewritten


class TestImagesPreserved:
    def test_attachments_include_image_url(self) -> None:
        attachments = build_catalog_browse_attachments(_HONEY_CATALOG, limit=3)
        assert len(attachments) >= 1
        first = attachments[0]
        assert first.get("kind") == "product_card"
        assert first.get("file_url") == "https://cdn.example/honey1.jpg"
        assert first.get("dispatch_source") == "catalog_browse"

    def test_compose_stores_catalog_browse_attachments(self) -> None:
        _reply, result = _compose_search(_HONEY_CATALOG)
        attachments = list(result.data.get("catalog_browse_attachments") or [])
        assert attachments
        assert any(a.get("file_url") for a in attachments)


class TestVariantsAndPricesDisplayed:
    def test_variant_lines_in_reply(self) -> None:
        reply = build_catalog_browse_reply([_HONEY_CATALOG[1]])
        assert "500 جم" in reply
        assert "1 كيلو" in reply
        assert "180" in reply
        assert "350" in reply

    def test_compose_shows_variant_prices(self) -> None:
        reply, _ = _compose_search([_HONEY_CATALOG[1]])
        assert "500 جم" in reply
        assert "180" in reply


class TestNoHallucinatedCatalog:
    def test_empty_catalog_honest_message(self) -> None:
        reply = build_catalog_browse_reply([])
        assert "ما ظهرت لي منتجات" in reply
        assert "عسل طلح" not in reply

    def test_empty_compose_no_invented_names(self) -> None:
        reply, result = _compose_search([])
        assert "عسل طلح" not in reply
        assert "عسل سمر" not in reply
        assert not result.data.get("catalog_browse_attachments")


class TestHoneyCategoryBrowse:
    def test_honey_browse_excludes_cream_and_oil(self) -> None:
        catalog = _HONEY_CATALOG + [_CREAM_PRODUCT]
        filtered = filter_products_to_browse_category(
            catalog,
            message="وش عندكم عسل",
            query="عسل",
        )
        titles = [p["title"] for p in filtered]
        assert "كريم سم النحل" not in titles
        assert "عسل طلح نجد البري" in titles
        assert "عسل سمر الحجاز" in titles

    def test_honey_types_ask_excludes_non_honey(self) -> None:
        catalog = _HONEY_CATALOG[:2] + [_CREAM_PRODUCT]
        filtered = filter_products_to_browse_category(
            catalog,
            message="اعرض الأعسال",
            query="عسل",
        )
        reply = build_catalog_browse_reply(filtered)
        assert "كريم سم النحل" not in reply
        assert "عسل طلح نجد البري" in reply

    def test_honey_availability_phrase_scope(self) -> None:
        msg = "وش أنواع العسل المتوفرة؟"
        from modules.ai.brain.commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            is_category_scoped_browse,
            resolve_browse_category_scope,
            should_exclude_cross_category_product,
        )

        scope = resolve_browse_category_scope(msg, "عسل")
        assert scope == "عسل", scope
        assert is_category_scoped_browse(msg, "عسل") is True
        assert (
            should_exclude_cross_category_product(
                _HONEY_CATALOG[0],
                scope=scope or "",
                message=msg,
            )
            is False
        )

    def test_honey_availability_phrase_keeps_honey_skus(self) -> None:
        catalog = _HONEY_CATALOG[:2] + [_CREAM_PRODUCT]
        filtered = filter_products_to_browse_category(
            catalog,
            message="وش أنواع العسل المتوفرة؟",
            query="عسل",
        )
        titles = [p["title"] for p in filtered]
        assert "كريم سم النحل" not in titles
        assert "عسل طلح نجد البري" in titles
