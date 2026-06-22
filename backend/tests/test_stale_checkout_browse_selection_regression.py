"""Regression — stale checkout + browse + selection + sanitizer (GPT 5.5 report)."""
from __future__ import annotations

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

from core.outbound_sanitizer import (  # noqa: E402
    ASSET_PHONE,
    contains_promised_asset,
    maybe_scrub_unkept_asset_promise,
)
from modules.ai.brain.catalog.catalog_browse_turn_policy import (  # noqa: E402
    is_fresh_start_order_turn,
    maybe_suspend_stale_checkout_for_turn,
)
from modules.ai.brain.catalog.catalog_ranking_runtime import (  # noqa: E402
    load_best_seller_catalog_products,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.discovery.entry import GLOBAL_BROWSE, resolve_discovery_entry  # noqa: E402
from modules.ai.brain.order_context_gate import (  # noqa: E402
    is_fulfillment_discovery_unlock,
    should_block_product_discovery,
    try_fulfillment_lock_continuation,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    INTENT_PICK_LIST_ITEM,
    MerchantConversationState,
    OrderPreparationState,
)
from modules.observability.delivery_mode import (  # noqa: E402
    DELIVERY_MODE_TEXT_ONLY,
    is_acceptable_mode_for_product_intent,
    new_delivery_audit,
)
from services.final_dispatch_guard import should_allow_product_attachment_dispatch  # noqa: E402

MSG_START = "\u0627\u0628\u064a \u0627\u0637\u0644\u0628"
MSG_BROWSE = "\u0648\u0634 \u0639\u0646\u062f\u0643\u0645"
MSG_PICK = "\u0627\u0644\u0637\u0644\u062d \u0627\u0644\u0628\u0644\u062f\u064a 1 \u0643\u062c\u0645"
ADDRESS_MARKERS = (
    "google maps",
    "الرمز الوطني",
    "موقعك",
    "address_location",
)


def _facts(*, orderable: bool = True) -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=10,
        in_stock_count=10,
        orderable=orderable,
        snapshot_fresh=True,
    )


def _stale_checkout_state() -> MerchantConversationState:
    prep = OrderPreparationState(
        product_id="old-sku",
        missing_fields=["address_location"],
    )
    return MerchantConversationState(
        stage="checkout",
        order_prep=prep,
        current_product_focus={"external_id": "old-sku", "title": "Old Product"},
        last_question_asked="address_location",
        last_question_answered=False,
    )


def _ctx(
    message: str,
    *,
    state: MerchantConversationState | None = None,
    intent_name: str = "start_order",
    facts: CommerceFacts | None = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000000",
        message=message,
        intent=Intent(name=intent_name, confidence=0.9, raw_message=message),
        state=state or MerchantConversationState(),
        facts=facts or _facts(),
    )


class TestFreshStartOrderStaleCheckout:
    def test_abi_otlob_clears_stale_checkout_before_decide(self) -> None:
        state = _stale_checkout_state()
        ctx = _ctx(MSG_START, state=state, intent_name="start_order")
        assert maybe_suspend_stale_checkout_for_turn(ctx) is True
        assert state.current_product_focus is None
        assert state.order_prep.missing_fields == []

    def test_abi_otlob_does_not_resurrect_address_checkout(self) -> None:
        state = _stale_checkout_state()
        ctx = _ctx(MSG_START, state=state, intent_name="start_order")
        maybe_suspend_stale_checkout_for_turn(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER
        combined = f"{decision.reason} {decision.args}".lower()
        for marker in ADDRESS_MARKERS:
            assert marker not in combined

    def test_fresh_start_order_is_bare_opener(self) -> None:
        assert is_fresh_start_order_turn(MSG_START) is True


class TestBrowseAfterStaleCheckout:
    def test_wesh_aindakom_routes_browse_not_fulfillment_lock(self) -> None:
        state = _stale_checkout_state()
        ctx = _ctx(MSG_BROWSE, state=state, intent_name="ask_product")
        maybe_suspend_stale_checkout_for_turn(ctx)
        assert is_fulfillment_discovery_unlock(MSG_BROWSE) is True
        assert should_block_product_discovery(ctx) is False
        assert try_fulfillment_lock_continuation(ctx) is None

    def test_wesh_aindakom_decision_is_search_not_checkout(self) -> None:
        state = _stale_checkout_state()
        ctx = _ctx(MSG_BROWSE, state=state, intent_name="ask_product")
        maybe_suspend_stale_checkout_for_turn(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_browse_unlocks_product_card_dispatch(self) -> None:
        state = _stale_checkout_state()
        decision = should_allow_product_attachment_dispatch(
            brain_action=ACTION_SEARCH_PRODUCTS,
            intent_name="ask_product",
            inbound_message=MSG_BROWSE,
            reply_text="browse list",
            fulfillment_discovery_blocked=True,
            brain_state=state.to_dict(),
        )
        assert decision.allow is True
        assert decision.reason != "fulfillment_lock"


class TestGlobalBrowseTelemetry:
    def test_global_browse_entry_without_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modules.ai.brain.discovery.entry._apply_catalog_group_scope",
            lambda _ctx, entry: entry,
        )
        ctx = _ctx(MSG_BROWSE, intent_name="ask_product")
        entry = resolve_discovery_entry(ctx)
        assert entry.matched is True
        assert entry.entry_type == GLOBAL_BROWSE

    def test_best_sellers_group_none_logs_global_browse_reason(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO)

        monkeypatch.setattr(
            "services.catalog_intelligence_service.get_catalog_settings",
            lambda _db, _tid: {"best_seller_mode": "manual"},
        )
        monkeypatch.setattr(
            "services.catalog_intelligence_service.read_best_sellers",
            lambda _db, _tid, *, group_id=None, limit=12: [{"product_id": 1}],
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_ranking_runtime.hydrate_catalog_products_by_ids",
            lambda _db, _tid, ids, **_: [{"id": pid, "title": f"P{pid}"} for pid in ids],
        )
        monkeypatch.setattr(
            "modules.ai.brain.catalog.catalog_ranking_runtime._resolve_group_id_for_browse",
            lambda *_a, **_k: None,
        )

        products = load_best_seller_catalog_products(
            MagicMock(),
            1,
            message=MSG_BROWSE,
            query="",
            state=MerchantConversationState(),
        )
        assert len(products) == 1
        assert any(
            "[CATALOG_INTELLIGENCE]" in rec.message
            and "event=best_sellers" in rec.message
            and "reason=global_browse" in rec.message
            for rec in caplog.records
        )


class TestSelectionToOrderFlow:
    def test_numeric_pick_with_can_checkout_proposes_draft_even_when_facts_not_orderable(
        self,
    ) -> None:
        candidate = {
            "title": MSG_PICK,
            "external_id": "146",
            "can_checkout": True,
            "orderable": True,
        }
        state = MerchantConversationState(
            last_search_candidates=[candidate],
            last_action="search_products",
        )
        ctx = _ctx(
            "1",
            state=state,
            intent_name=INTENT_PICK_LIST_ITEM,
            facts=_facts(orderable=False),
        )
        ctx.intent = Intent(
            name=INTENT_PICK_LIST_ITEM,
            confidence=0.95,
            raw_message="1",
            slots={"list_index": 1},
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert "not orderable, confirm product" not in decision.reason

    def test_button_title_pick_proposes_draft_when_candidate_orderable(self) -> None:
        candidate = {
            "title": MSG_PICK,
            "external_id": "146",
            "can_checkout": True,
            "orderable": True,
        }
        state = MerchantConversationState(
            last_search_candidates=[candidate],
            last_action="search_products",
        )
        ctx = _ctx(
            MSG_PICK,
            state=state,
            intent_name="general",
            facts=_facts(orderable=False),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert "confirm product" not in decision.reason


class TestOutboundSanitizerProductOptionPhrases:
    @pytest.mark.parametrize(
        "text",
        [
            "اختر رقم الخيار أو اسم المنتج وأكمل معك",
            "اكتب رقم المنتج أو اسمه",
            "اختر رقم المنتج",
            "رقم الخيار",
            "هذا أقرب خيار لطلبك\n\nاختر رقم الخيار أو اسم المنتج وأكمل معك.",
        ],
    )
    def test_product_option_phrases_not_asset_phone(self, text: str) -> None:
        assert contains_promised_asset(text) != ASSET_PHONE

    @pytest.mark.parametrize("text", [
        "اختر رقم الخيار أو اسم المنتج وأكمل معك",
        "اكتب رقم المنتج أو اسمه",
    ])
    def test_product_option_phrases_not_scrubbed(self, text: str) -> None:
        out, scrubbed, asset = maybe_scrub_unkept_asset_promise(
            text,
            has_url=False,
            has_media=False,
            has_phone=False,
            has_product_card=False,
        )
        assert scrubbed is False
        assert asset is None
        assert "لا يوجد رقم تواصل" not in out


class TestDeliveryGuardBrowseButtons:
    def test_interactive_buttons_acceptable_for_search_products(self) -> None:
        audit = new_delivery_audit()
        audit["interactive_buttons_sent"] = True
        assert is_acceptable_mode_for_product_intent(
            DELIVERY_MODE_TEXT_ONLY,
            audit=audit,
            brain_action="search_products",
        ) is True

    def test_interactive_buttons_without_product_action_still_alarm(self) -> None:
        audit = new_delivery_audit()
        audit["interactive_buttons_sent"] = True
        assert is_acceptable_mode_for_product_intent(
            DELIVERY_MODE_TEXT_ONLY,
            audit=audit,
            brain_action="greet",
        ) is False
