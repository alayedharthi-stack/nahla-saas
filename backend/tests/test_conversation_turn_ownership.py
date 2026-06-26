"""Central conversation turn ownership — routing fallback guards."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    attach_commerce_turn_contract,
    build_commerce_turn_contract,
)
from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.discovery.entry import resolve_discovery_entry  # noqa: E402
from modules.ai.brain.product_discovery_gate import product_discovery_block_reason  # noqa: E402
from modules.ai.brain.state.stages import STAGE_ORDERING  # noqa: E402
from modules.ai.brain.turn.contract import (  # noqa: E402
    OWNER_CHECKOUT,
    OWNER_DISCOVERY,
    OWNER_HEALTH_ADVISORY,
    OWNER_PAYMENT,
    OWNER_STAFF_ESCALATION,
)
from modules.ai.brain.turn.ownership import (  # noqa: E402
    FALLBACK_PRODUCT_DISCOVERY,
    FALLBACK_STALE_CHECKOUT_SUSPEND,
    attach_conversation_turn_ownership,
    ownership_forbids_fallback,
    resolve_conversation_turn_ownership,
)
from modules.ai.brain.turn.shadow import prepare_turn_arbitration  # noqa: E402
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
        ],
    )


def _checkout_state(*, missing: list[str] | None = None) -> MerchantConversationState:
    op = OrderPreparationState(
        product_id="sku-1",
        missing_fields=missing or ["city"],
        line_items=[{"product_name": "عسل طلح", "quantity": 1, "price": 120}],
    )
    return MerchantConversationState(
        greeted=True,
        stage=STAGE_ORDERING,
        order_prep=op,
        current_product_focus={"product_id": "sku-1"},
    )


def _ctx(
    msg: str,
    *,
    intent_name: str = "general",
    state: MerchantConversationState | None = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=7,
        customer_phone="966542980511",
        message=msg,
        raw_message=msg,
        intent=Intent(name=intent_name, confidence=0.9, raw_message=msg),
        state=state or MerchantConversationState(greeted=True, stage="discovery"),
        facts=_facts(),
    )


def _prep_ownership(ctx: BrainContext) -> None:
    prepare_turn_arbitration(ctx)
    contract = build_commerce_turn_contract(ctx, db=None)
    attach_commerce_turn_contract(ctx, contract)
    ownership = resolve_conversation_turn_ownership(ctx)
    attach_conversation_turn_ownership(ctx, ownership)


class TestResolveConversationTurnOwnership:
    @pytest.mark.parametrize(
        "msg",
        [
            "كم ادفع",
            "كم أدفع",
            "ما المبلغ",
        ],
    )
    def test_active_checkout_payment_amount_variations(self, msg: str) -> None:
        ctx = _ctx(msg, state=_checkout_state(missing=["payment_method"]))
        _prep_ownership(ctx)
        ownership = resolve_conversation_turn_ownership(ctx)
        assert ownership.turn_owner in {OWNER_PAYMENT, OWNER_CHECKOUT}
        assert ownership.forbids(FALLBACK_PRODUCT_DISCOVERY)

    @pytest.mark.parametrize(
        "msg",
        [
            "مكة المكرمة",
            "مكة",
        ],
    )
    def test_address_slot_city_variations(self, msg: str) -> None:
        ctx = _ctx(msg, state=_checkout_state(missing=["city"]))
        _prep_ownership(ctx)
        ownership = resolve_conversation_turn_ownership(ctx)
        assert ownership.turn_owner in {OWNER_CHECKOUT, "ordering"}
        assert ownership.forbids(FALLBACK_PRODUCT_DISCOVERY)
        assert ownership.forbids(FALLBACK_STALE_CHECKOUT_SUSPEND)

    def test_health_advisory_variations(self) -> None:
        ctx = _ctx("عسل للمعدة والهضم", intent_name="general")
        _prep_ownership(ctx)
        ownership = resolve_conversation_turn_ownership(ctx)
        assert ownership.turn_owner in {OWNER_HEALTH_ADVISORY, OWNER_STAFF_ESCALATION}
        assert ownership.forbids(FALLBACK_PRODUCT_DISCOVERY)

    def test_health_advisory_need_based_intent(self) -> None:
        ctx = _ctx("نبغى عسل للعلاج", intent_name="solution_seeking_commerce")
        _prep_ownership(ctx)
        ownership = resolve_conversation_turn_ownership(ctx)
        assert ownership.turn_owner == OWNER_HEALTH_ADVISORY
        assert ownership.forbids(FALLBACK_PRODUCT_DISCOVERY)

    @pytest.mark.parametrize(
        "msg",
        [
            "ارسل الأرقام",
            "أحد يكلمني",
            "حولني لموظف",
        ],
    )
    def test_contact_variations(self, msg: str) -> None:
        ctx = _ctx(msg, intent_name="talk_to_human")
        _prep_ownership(ctx)
        ownership = resolve_conversation_turn_ownership(ctx)
        assert ownership.turn_owner in {OWNER_STAFF_ESCALATION, "support"}
        assert ownership.forbids(FALLBACK_PRODUCT_DISCOVERY)

    def test_explicit_browse_still_discovery(self) -> None:
        ctx = _ctx("ابي اشوف المنتجات")
        _prep_ownership(ctx)
        ownership = resolve_conversation_turn_ownership(ctx)
        assert ownership.turn_owner == OWNER_DISCOVERY
        assert ownership.explicit_browse_intent
        assert not ownership.forbids(FALLBACK_PRODUCT_DISCOVERY)
        entry = resolve_discovery_entry(ctx)
        assert entry.matched is True


class TestDiscoveryConsumers:
    def test_discovery_suppressed_during_checkout_payment(self) -> None:
        ctx = _ctx("كم ادفع", state=_checkout_state())
        _prep_ownership(ctx)
        entry = resolve_discovery_entry(ctx)
        assert entry.matched is False
        assert "conversation_ownership" in str(entry.reason or "")

    def test_product_discovery_gate_blocks_checkout_address(self) -> None:
        ctx = _ctx("مكة المكرمة", state=_checkout_state(missing=["city"]))
        _prep_ownership(ctx)
        reason = product_discovery_block_reason(ctx, source="top_products")
        assert reason is not None

    def test_decision_engine_no_search_products_on_payment_turn(self) -> None:
        ctx = _ctx("كم ادفع", state=_checkout_state())
        _prep_ownership(ctx)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_SEARCH_PRODUCTS


class TestStaleCheckoutSuspend:
    def test_city_answer_does_not_suspend_checkout(self) -> None:
        from modules.ai.brain.catalog.catalog_browse_turn_policy import (  # noqa: PLC0415
            should_suspend_stale_checkout_for_turn,
        )

        ctx = _ctx("مكة المكرمة", state=_checkout_state(missing=["city"]))
        _prep_ownership(ctx)
        assert should_suspend_stale_checkout_for_turn(
            ctx.message or "",
            intent_name="general",
            ctx=ctx,
        ) is False


class TestCatalogOrderPreservation:
    def test_catalog_order_contract_still_forbids_browse(self) -> None:
        from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: E402
            is_current_catalog_order_submitted,
        )

        ctx = _ctx("مرحبا", state=MerchantConversationState(greeted=True))
        ctx.profile = {
            "inbound_metadata": {
                "source_type": "catalog_order",
                "product_items": [{"product_name": "عسل", "quantity": 1}],
            },
        }
        assert is_current_catalog_order_submitted(ctx)
        _prep_ownership(ctx)
        assert ownership_forbids_fallback(ctx, FALLBACK_PRODUCT_DISCOVERY) is not None
