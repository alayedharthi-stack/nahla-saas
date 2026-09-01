"""Compose trusted singleton handoff for empty executor products list.

Live: selection_context_unique_presented_identity sets data.product=28
and data.products=[]. Compose must not treat that as T.no_products.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.compose.responder import (  # noqa: E402
    DefaultComposer,
    _trusted_search_compose_candidates,
)
from modules.ai.brain.commerce.product_presentation_selection import (  # noqa: E402
    PRESENTATION_NONE,
    PRESENTATION_SINGLE_RICH,
)
from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    ActionResult,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)
from routers.whatsapp_webhook import build_cta_url_payload  # noqa: E402

LIVE_JACKET = {
    "id": 28,
    "external_id": "1921568272",
    "title": "جاكيت",
    "price": 169.0,
    "in_stock": True,
    "can_checkout": True,
    "customer_selected": False,
    "provenance": "assistant_recommended",
}

SHOE = {
    "id": 55,
    "external_id": "shoe-1",
    "title": "حذاء رياضي أبيض",
    "price": 249,
    "in_stock": True,
    "can_checkout": True,
    "orderable": True,
}


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=30,
        in_stock_count=30,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="متجر تجريبي عام",
    )


def _ctx(message: str = "الجاكيت") -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=message,
        intent=Intent(name="ask_product", confidence=0.9, raw_message=message),
        state=MerchantConversationState(
            greeted=True,
            stage="exploring",
            turn=714,
            selection_context_turn=713,
            last_presented_products=[
                {"id": 22, "external_id": "1638893598", "title": "فستان"},
                {"id": 23, "external_id": "398551325", "title": "فستان"},
                dict(LIVE_JACKET),
            ],
        ),
        facts=_facts(),
        history=[],
    )


def _decision(**args: Any) -> Decision:
    payload = {
        "query": "جاكيت",
        "source": "selection_context_unique_presented_identity",
        "presentation_identity_grounded": True,
        "products": [dict(LIVE_JACKET)],
    }
    payload.update(args)
    return Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args=payload,
        reason="selection context unique_presented_identity",
        confidence=0.92,
    )


def _live_result(**overrides: Any) -> ActionResult:
    data: Dict[str, Any] = {
        "products": [],
        "product": dict(LIVE_JACKET),
        "query": "جاكيت",
        "count": 1,
        "suggest_narrow": False,
        "selection_presentation_text": "",
        "discovery_output_kind": "products",
        "presentation_identity_grounded": True,
    }
    data.update(overrides)
    return ActionResult(success=True, data=data)


def _compose(decision: Decision, result: ActionResult, ctx: BrainContext) -> str:
    async def _run() -> str:
        with patch(
            "modules.ai.brain.persona.catalog_product_answer.try_compose_catalog_product_answer",
            return_value=("", None, None),
        ):
            return await DefaultComposer().compose(decision, result, ctx)

    return asyncio.run(_run())


class TestTrustedSearchComposeCandidates:
    def test_empty_products_grounded_identity_uses_singleton(self) -> None:
        data = {
            "products": [],
            "product": dict(LIVE_JACKET),
            "presentation_identity_grounded": True,
        }
        rows = _trusted_search_compose_candidates(data, _decision())
        assert len(rows) == 1
        assert int(rows[0]["id"]) == 28
        assert data["products"] == []

    def test_empty_products_ungrounded_identity_stays_empty(self) -> None:
        data = {
            "products": [],
            "product": dict(LIVE_JACKET),
        }
        decision = _decision(presentation_identity_grounded=False)
        rows = _trusted_search_compose_candidates(data, decision)
        assert rows == []

    def test_grounded_title_only_fails_closed(self) -> None:
        data = {
            "products": [],
            "product": {"title": "جاكيت"},
            "presentation_identity_grounded": True,
        }
        rows = _trusted_search_compose_candidates(
            data, _decision(presentation_identity_grounded=True)
        )
        assert rows == []

    def test_normal_products_list_is_preferred(self) -> None:
        data = {
            "products": [dict(SHOE)],
            "product": dict(LIVE_JACKET),
            "presentation_identity_grounded": True,
        }
        rows = _trusted_search_compose_candidates(data, _decision())
        assert len(rows) == 1
        assert rows[0]["id"] == 55


class TestLiveShapedComposeHandoff:
    def test_live_empty_products_grounded_singleton_reaches_rich_card(self) -> None:
        decision = _decision()
        result = _live_result()
        reply = _compose(decision, result, _ctx("الجاكيت"))
        absence = T.no_products(variant=0)
        assert "لم أتمكن من العثور على منتجات" not in (reply or "")
        assert reply.strip() != absence.split("\n")[0].strip()
        assert result.data.get("products") == []
        assert result.data.get("product_presentation_kind") == PRESENTATION_SINGLE_RICH
        cards = result.data.get("pending_product_cards") or []
        assert len(cards) == 1
        assert int((result.data.get("pending_candidates") or [{}])[0].get("id") or 0) == 28
        assert LIVE_JACKET["customer_selected"] is False
        assert result.data.get("pending_product_cards")[0].get("needs_variant_choice") in (
            False,
            True,
            None,
        )
        card = cards[0]
        assert not str(card.get("file_url") or "").strip()
        assert not str(card.get("product_url") or "").strip()
        assert (
            build_cta_url_payload(
                to="966500000001",
                body_text=str(card.get("caption") or card.get("title") or "جاكيت"),
                btn_label="عرض المنتج",
                btn_url=str(card.get("product_url") or ""),
            )
            is None
        )

    def test_ungrounded_singleton_keeps_absence_template(self) -> None:
        decision = _decision(presentation_identity_grounded=False)
        result = _live_result(presentation_identity_grounded=False)
        result.data.pop("presentation_identity_grounded", None)
        reply = _compose(decision, result, _ctx("الجاكيت"))
        assert "لم أتمكن من العثور على منتجات" in (reply or "")
        assert result.data.get("product_presentation_kind") in (
            None,
            PRESENTATION_NONE,
            "",
        )
        assert not (result.data.get("pending_product_cards") or [])

    def test_title_only_grounded_flag_fails_closed(self) -> None:
        result = _live_result(
            product={"title": "جاكيت"},
            presentation_identity_grounded=True,
        )
        reply = _compose(_decision(), result, _ctx("الجاكيت"))
        assert "لم أتمكن من العثور على منتجات" in (reply or "")
        assert not (result.data.get("pending_product_cards") or [])

    def test_non_orderable_grounded_singleton_is_not_sellable(self) -> None:
        product = dict(LIVE_JACKET)
        product["can_checkout"] = False
        product["orderable"] = False
        result = _live_result(product=product)
        reply = _compose(_decision(), result, _ctx("الجاكيت"))
        assert "لم أتمكن من العثور على منتجات" in (reply or "")
        assert not (result.data.get("pending_product_cards") or [])

    def test_normal_search_list_not_replaced_by_product(self) -> None:
        data = {
            "products": [dict(SHOE)],
            "product": dict(LIVE_JACKET),
            "query": "حذاء",
            "presentation_identity_grounded": True,
            "discovery_output_kind": "products",
        }
        result = ActionResult(success=True, data=data)
        decision = _decision(
            query="حذاء",
            source="catalog_search",
            products=[dict(SHOE)],
        )
        reply = _compose(decision, result, _ctx("حذاء رياضي أبيض"))
        assert "لم أتمكن من العثور على منتجات" not in (reply or "")
        assert result.data.get("products") == [dict(SHOE)]
        kind = result.data.get("product_presentation_kind")
        assert kind != PRESENTATION_SINGLE_RICH or int(
            ((result.data.get("pending_candidates") or [{}])[0] or {}).get("id") or 0
        ) != 28

    def test_genuine_empty_catalog_still_uses_absence(self) -> None:
        decision = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "جاكيت", "source": "catalog_search"},
            reason="empty catalog",
        )
        result = ActionResult(
            success=True,
            data={"products": [], "query": "جاكيت", "discovery_output_kind": "products"},
        )
        reply = _compose(decision, result, _ctx("الجاكيت"))
        assert "لم أتمكن من العثور على منتجات" in (reply or "")
        assert not (result.data.get("pending_product_cards") or [])
