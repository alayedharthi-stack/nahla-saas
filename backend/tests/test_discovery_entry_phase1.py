"""Phase 1 — unified discovery entry point."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.discovery.entry import (  # noqa: E402
    GLOBAL_BROWSE,
    PRODUCT_SPECIFIC,
    START_ORDER_BARE,
    TOP_PRODUCTS,
    extract_order_product_query,
    resolve_discovery_entry,
    route_discovery_entry,
)
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.state.stages import STAGE_ORDERING  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _facts(*, has_products: bool = True) -> CommerceFacts:
    return CommerceFacts(
        has_products=has_products,
        product_count=10 if has_products else 0,
        in_stock_count=10 if has_products else 0,
        has_active_integration=True,
        orderable=True,
        snapshot_fresh=True,
        store_name="متجر تجريبي",
        top_products=[
            {"title": "عسل طلح", "external_id": "1", "price": 120},
            {"title": "عسل سدر", "external_id": "2", "price": 150},
        ],
    )


def _ctx(
    msg: str,
    *,
    intent_name: str | None = None,
    state: MerchantConversationState | None = None,
) -> BrainContext:
    if intent_name is None:
        intent = rules.match(msg)
        if intent is None:
            intent = Intent(name="general", confidence=0.5, raw_message=msg)
    else:
        intent = Intent(name=intent_name, confidence=0.9, raw_message=msg)
    return BrainContext(
        tenant_id=7,
        customer_phone="966542980511",
        message=msg,
        intent=intent,
        state=state or MerchantConversationState(greeted=True, stage="discovery"),
        facts=_facts(),
    )


def _route(entry, ctx: BrainContext):
    return route_discovery_entry(
        ctx,
        entry,
        facts=ctx.facts,
        product_discovery_blocked=lambda _src: False,
        fulfillment_locked_fallback=lambda: None,
        block_stale_resume=lambda _wf: False,
        is_commerce_blocked=lambda _ctx: False,
    )


class TestResolveDiscoveryEntry:
    def test_bare_start_order(self) -> None:
        entry = resolve_discovery_entry(_ctx("ابي اطلب"))
        assert entry.matched is True
        assert entry.entry_type == START_ORDER_BARE
        assert not (entry.query or "").strip()

    def test_product_bearing_order(self) -> None:
        entry = resolve_discovery_entry(_ctx("ابي اطلب عسل طلح"))
        assert entry.matched is True
        assert entry.entry_type == PRODUCT_SPECIFIC
        assert entry.query == "عسل طلح"

    def test_global_browse_types_overview(self) -> None:
        entry = resolve_discovery_entry(_ctx("وش الأنواع اللي عندكم"))
        assert entry.matched is True
        assert entry.entry_type == GLOBAL_BROWSE

    def test_top_products(self) -> None:
        entry = resolve_discovery_entry(_ctx("الأكثر مبيعاً"))
        assert entry.matched is True
        assert entry.entry_type == TOP_PRODUCTS

    def test_identity_not_discovery(self) -> None:
        entry = resolve_discovery_entry(_ctx("من أنت"))
        assert entry.matched is False

    def test_city_slot_not_discovery(self) -> None:
        prep = OrderPreparationState(
            product_id="ext-honey-1",
            missing_fields=["city"],
            order_status="awaiting_address",
        )
        state = MerchantConversationState(
            stage=STAGE_ORDERING,
            greeted=True,
            order_prep=prep,
            current_product_focus={"title": "عسل", "external_id": "1"},
        )
        entry = resolve_discovery_entry(_ctx("الطايف", state=state))
        assert entry.matched is False

    def test_staff_rejection_resumes_commerce_query(self) -> None:
        msg = "ما أبغى أمين أنا أبغى أشتري عسل"
        entry = resolve_discovery_entry(_ctx(msg))
        assert entry.matched is True
        assert entry.entry_type == PRODUCT_SPECIFIC
        assert "عسل" in (entry.query or "")


class TestRouteDiscoveryEntry:
    def test_bare_start_order_routes_to_search(self) -> None:
        ctx = _ctx("ابي اطلب")
        entry = resolve_discovery_entry(ctx)
        dec = _route(entry, ctx)
        assert dec is not None
        assert dec.action == ACTION_SEARCH_PRODUCTS
        assert dec.args.get("source") == "top_products_start_order"
        assert dec.args.get("query") in ("", None)

    def test_product_order_routes_to_search(self) -> None:
        ctx = _ctx("ابي اطلب عسل طلح")
        entry = resolve_discovery_entry(ctx)
        dec = _route(entry, ctx)
        assert dec is not None
        assert dec.action == ACTION_SEARCH_PRODUCTS
        assert dec.args.get("query") == "عسل طلح"

    def test_identity_engine_stays_persona(self) -> None:
        engine = DefaultDecisionEngine()
        dec = engine.decide(_ctx("من أنت"))
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "persona_identity"


class TestExtractOrderProductQuery:
    def test_bare_start_order_empty_query(self) -> None:
        ctx = _ctx("ابي اطلب")
        assert extract_order_product_query(ctx) == ""

    def test_does_not_extract_identity(self) -> None:
        ctx = _ctx("من أنت")
        assert extract_order_product_query(ctx) == ""
