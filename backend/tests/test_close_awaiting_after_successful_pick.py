"""
Close awaiting_variant_choice after successful variant pick + same-parent ordering inquiry.

Regression matrix from Patch Authorization (close awaiting after pick).
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import ExitStack, contextmanager
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.state_continuity_identity import (  # noqa: E402
    close_awaiting_variant_after_successful_pick,
    maybe_apply_variant_discovery_ownership_before_lock,
    resolve_product_for_state_continuity,
    resolve_variant_pick_from_product,
    try_ordering_same_parent_inquiry_decision,
)
from modules.ai.brain.commerce.variant_pricing import (  # noqa: E402
    try_variant_pricing_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_VARIANT_PRICING,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.orders import DraftOrderHandler  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_GENERAL,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _jacket_product() -> Dict[str, Any]:
    return {
        "id": "501",
        "external_id": "ext-jacket-501",
        "title": "جاكيت رياضي",
        "price": 169,
        "in_stock": True,
        "variants": [
            {"id": "v1", "option_summary": "S", "price": 40, "in_stock": True},
            {"id": "v2", "option_summary": "XL", "price": 44, "in_stock": True},
            {"id": "v3", "option_summary": "L", "price": 38, "in_stock": True},
        ],
    }


def _awaiting_state(*, picked: bool = False) -> MerchantConversationState:
    product = _jacket_product()
    prep = OrderPreparationState(
        awaiting_variant_choice=not picked,
        pending_variant_product_id="" if picked else "501",
        product_id="ext-jacket-501",
        missing_fields=["city", "address_location"],
        city="",
        product_options={"المقاس": {"value_name": "XL"}} if picked else {},
        product_options_meta=[
            {
                "id": "grp-size",
                "name": "المقاس",
                "values": [
                    {"id": "val-s", "name": "S"},
                    {"id": "val-xl", "name": "XL"},
                    {"id": "val-l", "name": "L"},
                ],
            }
        ],
        product_has_required_options=True,
        product_options_loaded=True,
    )
    st = MerchantConversationState(turn=64, stage="ordering" if picked else "exploring")
    st.current_product_focus = dict(product)
    st.order_prep = prep
    st.selected_variant = {"variant_id": "v2", "variant_label": "XL", "price": 44} if picked else None
    return st


def _intent(name: str, *, product_query: str = "") -> Intent:
    slots: Dict[str, Any] = {}
    if product_query:
        slots["product_query"] = product_query
    return Intent(name=name, confidence=0.9, slots=slots)


def _ctx(
    msg: str,
    *,
    state: MerchantConversationState,
    intent: Intent,
) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="+966555000101",
        message=msg,
        raw_message=msg,
        intent=intent,
        state=state,
        facts=CommerceFacts(has_products=True, product_count=28, orderable=True),
        history=[],
    )


def _draft_handler_patch_stack(*, catalog_product: Optional[Dict[str, Any]] = None):
    product = catalog_product if catalog_product is not None else _jacket_product()

    @contextmanager
    def _stack():
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "modules.ai.brain.commerce.state_continuity_identity.resolve_product_for_state_continuity",
                    return_value=product,
                )
            )
            stack.enter_context(
                patch(
                    "modules.ai.brain.execution.orders._ensure_product_options_loaded",
                    new_callable=AsyncMock,
                )
            )
            stack.enter_context(
                patch(
                    "modules.ai.brain.execution.orders._missing_checkout_fields",
                    return_value=["city", "address_location"],
                )
            )
            stack.enter_context(
                patch(
                    "modules.ai.brain.execution.orders._filter_missing_phone_if_known",
                    side_effect=lambda missing, phone: missing,
                )
            )
            stack.enter_context(
                patch(
                    "modules.ai.brain.execution.orders._resolve_checkout_address",
                    new_callable=AsyncMock,
                )
            )
            stack.enter_context(patch("modules.ai.brain.execution.orders._seed_checkout_state"))
            stack.enter_context(
                patch("modules.ai.brain.commerce.cart_state.maybe_apply_cart_message")
            )
            yield

    return _stack()


class TestCloseAwaitingAfterSuccessfulPick:
    def test_numeric_pick_closes_awaiting_and_retains_ordering(self) -> None:
        state = _awaiting_state()
        ctx = _ctx("1", state=state, intent=_intent(INTENT_GENERAL))
        ctx._db = MagicMock()  # type: ignore[attr-defined]
        decision = Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={
                "variant_pick": {"index_one_based": 1},
                "pending_variant_product_id": "501",
            },
            reason="awaiting_variant_choice — mapped customer pick",
        )
        with _draft_handler_patch_stack():
            result = asyncio.run(DraftOrderHandler().handle(decision, ctx))

        prep = OrderPreparationState.from_dict(result.data.get("order_prep") or {})
        assert state.order_prep.awaiting_variant_choice is False
        assert state.order_prep.pending_variant_product_id == ""
        assert prep.product_options
        assert prep.product_id == "ext-jacket-501"
        assert prep.missing_fields == ["city", "address_location"]
        assert state.stage == "ordering" or state.order_prep.missing_fields

    def test_label_pick_xl_closes_awaiting_and_retains_selection(self) -> None:
        state = _awaiting_state()
        ctx = _ctx("XL", state=state, intent=_intent(INTENT_GENERAL))
        ctx._db = MagicMock()  # type: ignore[attr-defined]
        decision = Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={
                "variant_pick": {"label": "XL"},
                "pending_variant_product_id": "501",
            },
            reason="awaiting_variant_choice — mapped customer pick",
        )
        with _draft_handler_patch_stack():
            result = asyncio.run(DraftOrderHandler().handle(decision, ctx))

        prep = OrderPreparationState.from_dict(result.data.get("order_prep") or {})
        assert state.order_prep.awaiting_variant_choice is False
        assert "المقاس" in (prep.product_options or {})
        assert prep.product_options["المقاس"]["value_name"] == "XL"
        assert prep.missing_fields == ["city", "address_location"]

    def test_invalid_pick_keeps_awaiting_true(self) -> None:
        state = _awaiting_state()
        ctx = _ctx("9", state=state, intent=_intent(INTENT_GENERAL))
        ctx._db = MagicMock()  # type: ignore[attr-defined]
        decision = Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={
                "variant_pick": {"index_one_based": 9},
                "pending_variant_product_id": "501",
            },
            reason="awaiting_variant_choice — mapped customer pick",
        )
        with _draft_handler_patch_stack(
            catalog_product={**_jacket_product(), "variants": []},
        ):
            result = asyncio.run(DraftOrderHandler().handle(decision, ctx))

        prep = OrderPreparationState.from_dict(result.data.get("order_prep") or {})
        assert state.order_prep.awaiting_variant_choice is True
        assert state.order_prep.pending_variant_product_id == "501"
        assert not prep.product_options

    def test_catalog_variant_disappears_fails_safe(self) -> None:
        state = _awaiting_state()
        ctx = _ctx("1", state=state, intent=_intent(INTENT_GENERAL))
        ctx._db = MagicMock()  # type: ignore[attr-defined]
        decision = Decision(
            action=ACTION_PROPOSE_DRAFT_ORDER,
            args={
                "variant_pick": {"index_one_based": 1},
                "pending_variant_product_id": "501",
            },
            reason="awaiting_variant_choice — mapped customer pick",
        )
        with _draft_handler_patch_stack(
            catalog_product={**_jacket_product(), "variants": []},
        ):
            result = asyncio.run(DraftOrderHandler().handle(decision, ctx))

        prep = OrderPreparationState.from_dict(result.data.get("order_prep") or {})
        assert state.order_prep.awaiting_variant_choice is True
        assert not prep.product_options


class TestSameParentInquiryAfterPick:
    def test_sizes_inquiry_no_path_c_and_reresolves_parent(self) -> None:
        state = _awaiting_state(picked=True)
        state.stage = "ordering"
        own = maybe_apply_variant_discovery_ownership_before_lock(
            state,
            message="اريد مقاسات الجاكيت",
            intent=_intent(INTENT_ASK_PRODUCT, product_query="الجاكيت"),
        )
        assert own["applied"] is False
        assert own["mode"] == "none"

        dec = try_ordering_same_parent_inquiry_decision(
            _ctx("اريد مقاسات الجاكيت", state=state, intent=_intent(INTENT_ASK_PRODUCT)),
        )
        assert dec is not None
        assert dec.action == ACTION_SEARCH_PRODUCTS
        assert dec.args.get("source") == "state_continuity_reresolve"
        assert dec.args.get("product_id") == "501"
        assert dec.args.get("source") == "state_continuity_reresolve"
        assert state.order_prep.product_options
        assert state.order_prep.missing_fields == ["city", "address_location"]

    def test_price_inquiry_uses_grounded_variant_pricing(self) -> None:
        state = _awaiting_state(picked=True)
        state.stage = "ordering"
        state.current_product_focus = {
            "id": 501,
            "title": "جاكيت رياضي",
            "variants": _jacket_product()["variants"],
        }
        dec = try_ordering_same_parent_inquiry_decision(
            _ctx("وش سعره؟", state=state, intent=_intent(INTENT_ASK_PRICE)),
        )
        assert dec is not None
        assert dec.action == ACTION_VARIANT_PRICING
        facts = (dec.args or {}).get("catalog_fact_products") or []
        assert facts
        assert state.order_prep.missing_fields == ["city", "address_location"]
        assert state.order_prep.product_options

    def test_product_detail_inquiry_retains_variant_selection(self) -> None:
        state = _awaiting_state(picked=True)
        state.stage = "ordering"
        own = maybe_apply_variant_discovery_ownership_before_lock(
            state,
            message="حدثني عن الجاكيت",
            intent=_intent(INTENT_ASK_PRODUCT, product_query="الجاكيت"),
        )
        assert own["applied"] is False

        dec = try_ordering_same_parent_inquiry_decision(
            _ctx("حدثني عن الجاكيت", state=state, intent=_intent(INTENT_ASK_PRODUCT)),
        )
        assert dec is not None
        assert dec.action == ACTION_SEARCH_PRODUCTS
        assert dec.args.get("product_id") == "501"
        assert state.order_prep.product_options.get("المقاس", {}).get("value_name") == "XL"
        assert state.selected_variant is not None

    def test_explicit_different_product_invalidates_old_variant(self) -> None:
        state = _awaiting_state()
        intent = _intent(INTENT_ASK_PRODUCT, product_query="فستان")
        own = maybe_apply_variant_discovery_ownership_before_lock(
            state,
            message="عندكم فستان؟",
            intent=intent,
        )
        assert own["mode"] == "invalidate"
        assert state.current_product_focus is None
        assert state.order_prep.product_options == {}

        dec = DefaultDecisionEngine().decide(_ctx("عندكم فستان؟", state=state, intent=intent))
        assert dec.action == ACTION_SEARCH_PRODUCTS
        assert dec.args.get("product_id") != "501"


class TestTenantIsolation:
    def test_resolve_product_for_state_continuity_is_tenant_scoped(self) -> None:
        db = MagicMock()
        builder = MagicMock()
        builder.get_by_external_id.return_value = None

        from core import store_knowledge  # noqa: PLC0415

        original = store_knowledge.CatalogContextBuilder
        store_knowledge.CatalogContextBuilder = MagicMock(return_value=builder)
        try:
            out = resolve_product_for_state_continuity(
                db,
                tenant_id=1,
                external_id="ext-jacket-501",
            )
        finally:
            store_knowledge.CatalogContextBuilder = original

        assert out is None
        builder.get_by_external_id.assert_called_once_with("ext-jacket-501")


class TestPrePickPathCOwnership:
    def test_free_text_before_pick_still_suspends_for_discovery(self) -> None:
        state = _awaiting_state()
        own = maybe_apply_variant_discovery_ownership_before_lock(
            state,
            message="حدثني عن الجاكيت",
            intent=_intent(INTENT_ASK_PRODUCT, product_query="الجاكيت"),
        )
        assert own["applied"] is True
        assert own["mode"] == "suspend_retain_identity"
        assert state.order_prep.awaiting_variant_choice is False

    def test_real_pick_before_lock_still_variant_owned(self) -> None:
        state = _awaiting_state()
        own = maybe_apply_variant_discovery_ownership_before_lock(
            state,
            message="1",
            intent=_intent(INTENT_GENERAL),
        )
        assert own["mode"] == "retain_pick"
        assert state.order_prep.awaiting_variant_choice is True

        dec = DefaultDecisionEngine().decide(_ctx("1", state=state, intent=_intent(INTENT_GENERAL)))
        assert dec.action == ACTION_PROPOSE_DRAFT_ORDER
        assert "variant_pick" in (dec.args or {})


class TestCloseAwaitingHelper:
    def test_close_helper_only_clears_wait_pin(self) -> None:
        state = _awaiting_state()
        state.order_prep.missing_fields = ["city"]
        state.order_prep.product_options = {"المقاس": {"value_name": "XL"}}
        closed = close_awaiting_variant_after_successful_pick(state, reason="unit")
        assert closed is True
        assert state.order_prep.awaiting_variant_choice is False
        assert state.order_prep.pending_variant_product_id == ""
        assert state.order_prep.missing_fields == ["city"]
        assert state.order_prep.product_options


class TestResolveVariantPickFromProduct:
    def test_index_and_label_resolution(self) -> None:
        product = _jacket_product()
        by_idx = resolve_variant_pick_from_product(product, {"index_one_based": 2})
        assert by_idx is not None
        assert by_idx.get("option_summary") == "XL"
        by_label = resolve_variant_pick_from_product(product, {"label": "XL"})
        assert by_label is not None
        assert by_label.get("id") == "v2"
