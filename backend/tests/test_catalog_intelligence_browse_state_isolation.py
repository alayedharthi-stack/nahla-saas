"""Catalog Intelligence — browse turn isolation from stale checkout/fulfillment."""
from __future__ import annotations

import importlib
import logging
import os
import sys
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from modules.ai.brain.catalog.catalog_browse_scope_resolver import (  # noqa: E402
    match_catalog_group,
    resolve_browse_scope,
)
from modules.ai.brain.catalog.catalog_browse_turn_policy import (  # noqa: E402
    is_catalog_browse_message,
    is_catalog_browse_turn,
)
from modules.ai.brain.catalog.catalog_product_card_filter import (  # noqa: E402
    filter_product_card_attachments,
)
from modules.ai.brain.catalog.catalog_ranking_runtime import (  # noqa: E402
    load_best_seller_catalog_products,
)
from modules.ai.brain.commerce.complaint_refund_topic_guard import (  # noqa: E402
    apply_complaint_refund_session_flags,
    should_block_order_draft_injection,
)
from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.order_context_gate import (  # noqa: E402
    is_fulfillment_discovery_unlock,
    should_block_product_discovery,
)
from modules.ai.brain.product_discovery_gate import product_discovery_block_reason  # noqa: E402
from modules.ai.brain.turn.understanding import synthesize_turn_understanding  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from services.final_dispatch_guard import (  # noqa: E402
    should_allow_product_attachment_dispatch,
)


_GROUP_A: Dict[str, Any] = {
    "id": 1,
    "slug": "honey",
    "label": "Honey",
    "catalog_match": "honey,oils category",
    "priority": 1,
    "is_active": True,
}
_GROUP_B: Dict[str, Any] = {
    "id": 2,
    "slug": "oils",
    "label": "Oils",
    "catalog_match": "oil",
    "priority": 2,
    "is_active": True,
}


def _ctx(
    message: str,
    *,
    state: MerchantConversationState | None = None,
    intent_name: str = "general",
) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000000",
        message=message,
        intent=Intent(name=intent_name, confidence=0.85, raw_message=message),
        state=state or MerchantConversationState(),
        facts=CommerceFacts(has_products=True),
    )


def _checkout_state(*, missing: List[str] | None = None) -> MerchantConversationState:
    prep = OrderPreparationState(
        product_id="sku-1",
        missing_fields=list(missing or ["address_location"]),
    )
    return MerchantConversationState(
        stage="checkout",
        order_prep=prep,
        current_product_focus={"id": 10, "title": "Product A"},
        last_question_asked="address_location",
        last_question_answered=False,
    )


class TestStaleCheckoutIsolation:
    def test_browse_message_not_blocked_by_fulfillment(self) -> None:
        ctx = _ctx("وش انواع العسل؟", state=_checkout_state())
        assert should_block_product_discovery(ctx) is False
        assert product_discovery_block_reason(ctx) is None

    def test_turn_understanding_suspends_checkout_for_browse(self) -> None:
        ctx = _ctx("وش انواع العسل؟", state=_checkout_state())
        understanding = synthesize_turn_understanding(ctx)
        assert understanding.should_suspend_stale_state is True
        assert "order_prep" in understanding.suspend_scope
        assert understanding.current_intent == "product_inquiry"
        assert any(
            c.conflict_reason == "catalog_browse_turn_isolates_stale_checkout"
            for c in understanding.conflicts_with_state
        )

    def test_decision_engine_routes_browse_to_search_not_checkout(self) -> None:
        ctx = _ctx("وش المنتجات", state=_checkout_state(), intent_name="ask_product")
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.action != "collect_checkout_slots"


class TestStaleFulfillmentLock:
    def test_browse_unlocks_product_cards(self) -> None:
        decision = should_allow_product_attachment_dispatch(
            brain_action=ACTION_SEARCH_PRODUCTS,
            intent_name="ask_product",
            inbound_message="show me products",
            reply_text="Here are our products",
            fulfillment_discovery_blocked=True,
            brain_state=_checkout_state().to_dict(),
        )
        assert decision.allow is True
        assert decision.reason != "fulfillment_lock"

    def test_category_types_browse_unlocks(self) -> None:
        msg = "وش انواع العسل؟"
        assert is_fulfillment_discovery_unlock(msg) is True
        assert is_catalog_browse_message(msg) is True


class TestBrowseScope:
    def test_catalog_match_resolves_group_a(self) -> None:
        hit = match_catalog_group([_GROUP_A, _GROUP_B], message="show honey", query="")
        assert hit is not None
        assert hit.group_slug == "honey"
        assert hit.match_source == "text"

    def test_resolve_browse_scope_product_ids_from_group_membership(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            lambda _db, _tid: [_GROUP_A, _GROUP_B],
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver._group_product_ids",
            lambda _db, _tid, gid: (10, 11) if gid == 1 else (20,),
        )
        resolution = resolve_browse_scope(MagicMock(), 1, "honey options", "")
        assert resolution.matched is True
        assert resolution.group_slug == "honey"
        assert resolution.product_ids == (10, 11)


class TestProductCardFilter:
    def test_keeps_group_a_drops_group_b(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.load_merchant_catalog_groups",
            lambda _db, _tid: [_GROUP_A, _GROUP_B],
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_browse_scope_resolver.resolve_browse_scope",
            lambda *_a, **_k: MagicMock(
                matched=True,
                group_slug="honey",
                product_ids=(10, 11),
                evidence={},
            ),
        )
        monkeypatch.setattr(
            "modules.ai.brain.commerce.commerce_browse_category_guard.resolve_browse_category_scope",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_product_card_filter._load_products_for_attachments",
            lambda *_a, **_k: {},
        )
        attachments = [
            {"kind": "product_card", "id": 10, "title": "Honey A"},
            {"kind": "product_card", "id": 20, "title": "Oil B"},
        ]
        result = filter_product_card_attachments(
            attachments,
            db=MagicMock(),
            tenant_id=1,
            message="show honey",
        )
        assert [a["id"] for a in result.attachments] == [10]
        assert result.dropped == 1


class TestBestSellersScoped:
    def test_scoped_best_sellers_use_group_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO)

        def _read_best_sellers(_db, _tid, *, group_id=None, limit=12):
            if group_id == 1:
                return [{"product_id": 10}, {"product_id": 11}]
            return [{"product_id": 99}]

        monkeypatch.setattr(
            "services.catalog_intelligence_service.get_catalog_settings",
            lambda _db, _tid: {"best_seller_mode": "manual"},
        )
        monkeypatch.setattr(
            "services.catalog_intelligence_service.read_best_sellers",
            _read_best_sellers,
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_ranking_runtime.hydrate_catalog_products_by_ids",
            lambda _db, _tid, ids, **_: [{"id": pid, "title": f"P{pid}"} for pid in ids],
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_ranking_runtime._resolve_group_id_for_browse",
            lambda *_a, **_k: 1,
        )

        products = load_best_seller_catalog_products(
            MagicMock(),
            1,
            message="top sellers in honey",
            query="honey",
            state=MerchantConversationState(),
        )
        assert [p["id"] for p in products] == [10, 11]
        assert any(
            "[CATALOG_INTELLIGENCE]" in rec.message and "event=best_sellers" in rec.message
            for rec in caplog.records
        )


class TestGroundingImport:
    def test_catalog_product_grounding_imports_without_syntax_error(self) -> None:
        mod = importlib.import_module("modules.ai.brain.commerce.catalog_product_grounding")
        assert callable(mod.build_catalog_grounded_list_reply)

    def test_grounded_list_reply_formats_bullets(self) -> None:
        from modules.ai.brain.commerce.catalog_product_grounding import (  # noqa: PLC0415
            build_catalog_grounded_list_reply,
        )

        reply = build_catalog_grounded_list_reply(["Alpha", "Beta"], category_hint="items")
        assert "catalog" in reply.lower() or "الكتالوج" in reply
        assert "Alpha" not in reply
        assert "Beta" not in reply


class TestValidationClient:
    def test_dashboard_api_uses_shared_authenticated_client(self) -> None:
        dashboard_api = os.path.join(
            os.path.dirname(_HERE),
            "..",
            "dashboard",
            "src",
            "api",
            "catalogIntelligence.ts",
        )
        text = open(dashboard_api, encoding="utf-8").read()
        assert "apiCall('/catalog-intelligence/validation')" in text
        assert "apiCall('/settings/catalog-intelligence'" in text


class TestCatalogBrowseTurnPolicy:
    def test_is_catalog_browse_turn_with_discovery_entry(self) -> None:
        ctx = _ctx("الاكثر مبيعا", state=MerchantConversationState())
        assert is_catalog_browse_turn("الاكثر مبيعا", ctx=ctx) is True


class TestComplaintStateDoesNotOwnBrowseOrCheckout:
    def test_stale_complaint_flag_does_not_block_address_slot(self) -> None:
        state = _checkout_state(missing=["address", "short_address_code"])
        state.commerce_session = {"complaint_refund_active": True}
        message = (
            "علي الشمري\n"
            "0505360205\n"
            "ينبع الصناعية الجابرية6 شارع المفرق منزل 34\n"
            "العنوان الوطني :YAMA2745\n"
        )

        assert should_block_order_draft_injection(
            brain_state=state.to_dict(),
            customer_message=message,
        ) is False

        apply_complaint_refund_session_flags(state, message)
        assert state.commerce_session.get("complaint_refund_active") is None

    def test_current_complaint_still_blocks_order_flow(self) -> None:
        state = _checkout_state(missing=["address"])
        state.commerce_session = {"complaint_refund_active": True}
        assert should_block_order_draft_injection(
            brain_state=state.to_dict(),
            customer_message="المنتج مغشوش وأبغى استرجاع",
        ) is True
