"""Phase 2A — commerce discovery strategy foundation (shadow, no behavior change)."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_discovery_shadow import (  # noqa: E402
    trace_commerce_discovery_shadow,
)
from modules.ai.brain.commerce.commerce_objective import (  # noqa: E402
    ALL_COMMERCE_OBJECTIVES,
    COMMERCE_OBJECTIVE_DISCOVERY,
    COMMERCE_OBJECTIVE_ORDERING,
    COMMERCE_OBJECTIVE_POST_PURCHASE,
    COMMERCE_OBJECTIVE_SUPPORT,
    CommerceObjective,
    is_valid_commerce_objective,
)
from modules.ai.brain.commerce.discovery_strategy import (  # noqa: E402
    CatalogContextSnapshot,
    DiscoveryMode,
    DiscoveryPlan,
    DiscoveryStrategyResult,
    resolve_discovery_strategy,
    resolve_discovery_strategy_for_ctx,
)
from modules.ai.brain.commerce.post_purchase_feedback_guard import (  # noqa: E402
    classify_product_quality_feedback,
    try_post_purchase_feedback_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.discovery.entry import (  # noqa: E402
    GLOBAL_BROWSE,
    START_ORDER_BARE,
    TOP_PRODUCTS,
    resolve_discovery_entry,
)
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.state.stages import STAGE_ORDERING  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_START_ORDER,
    INTENT_WHO_ARE_YOU,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _ctx(
    msg: str,
    *,
    state: MerchantConversationState | None = None,
    history: list | None = None,
) -> BrainContext:
    intent = rules.match(msg)
    if intent is None:
        intent = Intent(name="general", confidence=0.5, raw_message=msg)
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000001",
        message=msg,
        intent=intent,
        state=state or MerchantConversationState(greeted=True, stage="discovery"),
        facts=CommerceFacts(
            has_products=True,
            product_count=10,
            in_stock_count=10,
            orderable=True,
            store_name="test",
        ),
        history=history or [],
    )


class TestCommerceObjectiveEnum:
    def test_all_objectives_present(self) -> None:
        assert {o.value for o in CommerceObjective} == set(ALL_COMMERCE_OBJECTIVES)

    def test_from_value_roundtrip(self) -> None:
        assert CommerceObjective.from_value("discovery") == CommerceObjective.DISCOVERY
        assert CommerceObjective.from_value("invalid") is None

    def test_is_valid_commerce_objective(self) -> None:
        assert is_valid_commerce_objective("ordering")
        assert not is_valid_commerce_objective("checkout")


class TestDiscoveryPlanResolverDefaults:
    def test_discovery_plan_alias(self) -> None:
        assert DiscoveryPlan is DiscoveryStrategyResult

    def test_ctx_api_matches_legacy_kwargs(self) -> None:
        ctx = _ctx("وش الأنواع")
        legacy = resolve_discovery_strategy(
            commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
            entry_type=GLOBAL_BROWSE,
            catalog_context=CatalogContextSnapshot(product_count=10, collection_count=0),
        )
        via_ctx = resolve_discovery_strategy(ctx)
        assert via_ctx.mode == legacy.mode

    def test_global_browse_default_direct_catalog(self) -> None:
        plan = resolve_discovery_strategy(
            commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
            entry_type=GLOBAL_BROWSE,
            catalog_context=CatalogContextSnapshot(product_count=20, collection_count=0),
        )
        assert plan.mode == DiscoveryMode.DIRECT_CATALOG

    def test_top_products_default_featured_first(self) -> None:
        plan = resolve_discovery_strategy(
            commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
            entry_type=TOP_PRODUCTS,
            catalog_context=CatalogContextSnapshot(product_count=20),
        )
        assert plan.mode == DiscoveryMode.FEATURED_FIRST

    def test_start_order_bare_small_catalog_direct(self) -> None:
        plan = resolve_discovery_strategy(
            commerce_objective=COMMERCE_OBJECTIVE_DISCOVERY,
            entry_type=START_ORDER_BARE,
            catalog_context=CatalogContextSnapshot(product_count=5, collection_count=0),
        )
        assert plan.mode == DiscoveryMode.DIRECT_CATALOG

    def test_resolve_for_ctx_entry_aware(self) -> None:
        ctx = _ctx("الأكثر مبيعاً")
        plan = resolve_discovery_strategy_for_ctx(ctx)
        assert plan.mode == DiscoveryMode.FEATURED_FIRST
        assert plan.evidence.get("entry_type") == TOP_PRODUCTS


class TestCommerceDiscoveryShadow:
    def test_shadow_traces_browse_entry(self) -> None:
        payload = trace_commerce_discovery_shadow(_ctx("وش الأنواع"))
        assert payload is not None
        assert payload["entry_type"] == GLOBAL_BROWSE
        assert payload["discovery_mode"] == DiscoveryMode.DIRECT_CATALOG.value
        assert payload["shadow"] is True

    def test_shadow_skips_identity_probe(self) -> None:
        assert trace_commerce_discovery_shadow(_ctx("من أنت")) is None


class TestRoutingRegressionPhase2A:
    """Existing scenarios must route unchanged after Phase 2A wiring."""

    def test_abi_otlob_routes_start_order(self) -> None:
        ctx = _ctx("ابي اطلب")
        assert ctx.intent.name == INTENT_START_ORDER
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_SEARCH_PRODUCTS
        assert dec.args.get("source") == "top_products_start_order"

    def test_wesh_alanwa3_global_browse(self) -> None:
        entry = resolve_discovery_entry(_ctx("وش الأنواع"))
        assert entry.matched is True
        assert entry.entry_type == GLOBAL_BROWSE
        dec = DefaultDecisionEngine().decide(_ctx("وش الأنواع"))
        assert dec.action == ACTION_SEARCH_PRODUCTS

    def test_top_sellers_featured_entry(self) -> None:
        entry = resolve_discovery_entry(_ctx("الأكثر مبيعاً"))
        assert entry.matched is True
        assert entry.entry_type == TOP_PRODUCTS
        dec = DefaultDecisionEngine().decide(_ctx("الأكثر مبيعاً"))
        assert dec.action == ACTION_SEARCH_PRODUCTS

    def test_identity_not_discovery(self) -> None:
        ctx = _ctx("من أنت")
        assert resolve_discovery_entry(ctx).matched is False
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "persona_identity"
        assert ctx.intent.name == INTENT_WHO_ARE_YOU

    def test_complaint_refund_routes_support(self) -> None:
        state = MerchantConversationState(
            greeted=True,
            stage=STAGE_ORDERING,
            commerce_objective=COMMERCE_OBJECTIVE_ORDERING,
        )
        state.order_prep = OrderPreparationState(order_status="awaiting_payment")
        ctx = _ctx("ارجعوا لي فلوسي", state=state)
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "support_complaint_refund"
        assert dec.action != ACTION_SEARCH_PRODUCTS

    def test_post_purchase_feedback_routes_support(self) -> None:
        feedback = (
            "يا هلا العسل خفيف والله مو زي دايم وزايد حلاه مو زي السمره اللي دايم"
        )
        assert classify_product_quality_feedback(feedback) is True
        history = [
            {
                "direction": "out",
                "body": "تم توصيل طلبك رقم 266982457 ونود أن نعرف رأيك في العسل",
                "source": "external",
            },
        ]
        state = MerchantConversationState(
            greeted=True,
            stage="complete",
            commerce_objective=COMMERCE_OBJECTIVE_ORDERING,
        )
        ctx = _ctx(feedback, state=state, history=history)
        guard_dec = try_post_purchase_feedback_decision(ctx)
        assert guard_dec is not None
        assert guard_dec.args.get("topic") == "support_product_feedback"
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "support_product_feedback"
        assert dec.action != ACTION_SEARCH_PRODUCTS


@pytest.mark.parametrize(
    "mode",
    list(DiscoveryMode),
)
def test_discovery_mode_values_stable(mode: DiscoveryMode) -> None:
    assert mode.value in {
        "featured_first",
        "collections_first",
        "direct_catalog",
        "guided_discovery",
    }


@pytest.mark.parametrize(
    "objective",
    list(CommerceObjective),
)
def test_commerce_objective_separate_from_stage(objective: CommerceObjective) -> None:
    assert objective.value != "discovery_stage"
    assert objective.value in ALL_COMMERCE_OBJECTIVES
