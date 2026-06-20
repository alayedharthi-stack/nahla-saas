"""Phase 2 — commerce objective persistence."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_objective import (  # noqa: E402
    COMMERCE_OBJECTIVE_DISCOVERY,
    COMMERCE_OBJECTIVE_ORDERING,
    COMMERCE_OBJECTIVE_SELECTION,
    get_commerce_objective,
    update_commerce_objective,
)
from modules.ai.brain.discovery.entry import (  # noqa: E402
    GLOBAL_BROWSE,
    PRODUCT_SPECIFIC,
    SHOW_MORE,
    TOP_PRODUCTS,
    DiscoveryEntryDecision,
)
from modules.ai.brain.state.stages import STAGE_ORDERING  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _ctx(msg: str = "", *, state: MerchantConversationState | None = None) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=msg,
        intent=Intent(name="general", confidence=0.7, raw_message=msg),
        state=state or MerchantConversationState(greeted=True, stage="discovery"),
        facts=CommerceFacts(has_products=True, product_count=10, orderable=True),
    )


class TestCommerceObjectivePersistence:
    def test_global_browse_sets_discovery(self) -> None:
        ctx = _ctx("وش الأنواع")
        entry = DiscoveryEntryDecision(
            matched=True,
            entry_type=GLOBAL_BROWSE,
            source="top_products",
            query="",
            category_scope=None,
            reason="test",
        )
        obj = update_commerce_objective(ctx, entry)
        assert obj == COMMERCE_OBJECTIVE_DISCOVERY
        assert get_commerce_objective(ctx.state) == COMMERCE_OBJECTIVE_DISCOVERY

    def test_top_products_reinforces_discovery(self) -> None:
        ctx = _ctx("الأكثر مبيعاً")
        ctx.state.commerce_objective = COMMERCE_OBJECTIVE_DISCOVERY
        entry = DiscoveryEntryDecision(
            matched=True,
            entry_type=TOP_PRODUCTS,
            source="top_products",
            query="",
            category_scope=None,
            reason="test",
        )
        obj = update_commerce_objective(ctx, entry)
        assert obj == COMMERCE_OBJECTIVE_DISCOVERY

    def test_show_more_keeps_discovery(self) -> None:
        ctx = _ctx("ورني أكثر")
        ctx.state.commerce_objective = COMMERCE_OBJECTIVE_DISCOVERY
        entry = DiscoveryEntryDecision(
            matched=True,
            entry_type=SHOW_MORE,
            source="show_more",
            query="",
            category_scope=None,
            reason="test",
        )
        obj = update_commerce_objective(ctx, entry)
        assert obj == COMMERCE_OBJECTIVE_DISCOVERY

    def test_product_specific_moves_to_selection(self) -> None:
        ctx = _ctx("ابي اطلب عسل")
        entry = DiscoveryEntryDecision(
            matched=True,
            entry_type=PRODUCT_SPECIFIC,
            source="order_product_query",
            query="عسل",
            category_scope=None,
            reason="test",
        )
        obj = update_commerce_objective(ctx, entry)
        assert obj == COMMERCE_OBJECTIVE_SELECTION

    def test_ordering_stage_overrides_discovery(self) -> None:
        prep = OrderPreparationState(
            product_id="1",
            order_status="awaiting_address",
            missing_fields=["city"],
        )
        state = MerchantConversationState(
            stage=STAGE_ORDERING,
            order_prep=prep,
            commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
        )
        ctx = _ctx("الطايف", state=state)
        obj = update_commerce_objective(ctx, None)
        assert obj == COMMERCE_OBJECTIVE_ORDERING
