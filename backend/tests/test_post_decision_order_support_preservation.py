"""Post-decision layers must preserve existing-order support ownership."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: E402
    maybe_enforce_catalog_order_continue_checkout,
)
from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: E402
    build_commerce_turn_contract,
    decision_owned_by_existing_order_support,
    maybe_enforce_commerce_turn_contract_decision,
    order_support_reply_protected,
)
from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    build_order_support_follow_up_args,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)
from modules.ai.brain.state.stages import STAGE_ORDERING  # noqa: E402

GENERIC_ORDER_REF = "284719365"
GENERIC_PRODUCT = "حذاء رياضي أبيض"
SHIPPING_DELAY = "الطلب متأخر والشحن ما وصل"
NOT_FOUND_BODY = (
    "حاولت ابحث عن رقم الطلب لكن ما لقيته في النظام للأسف"
)


def _not_found_history(ref: str = GENERIC_ORDER_REF) -> List[Dict[str, str]]:
    return [
        {"direction": "in", "body": ref},
        {"direction": "out", "body": "لم نجد الطلب"},
    ]


def _catalog_prep(*, stale: bool = True) -> OrderPreparationState:
    prep = OrderPreparationState()
    prep.catalog_line_items_authoritative = True
    prep.catalog_checkout_total = 249.0
    prep.line_items = [
        {
            "name": GENERIC_PRODUCT,
            "qty": 1,
            "from_native_catalog_order": True,
        },
    ]
    if stale:
        prep.draft_order_id = "draft-generic-1"
        prep.order_status = "pending_customer_info"
    return prep


def _catalog_ctx(
    message: str,
    *,
    history: List[Dict[str, str]] | None = None,
    catalog_only: bool = False,
) -> BrainContext:
    state = MerchantConversationState(stage=STAGE_ORDERING, turn=3)
    state.current_product_focus = {
        "title": GENERIC_PRODUCT,
        "external_id": "prod-generic-1",
        "from_native_catalog_order": True,
    }
    state.order_prep = _catalog_prep(stale=not catalog_only)
    return BrainContext(
        tenant_id=1,
        customer_phone="966500000099",
        message=message,
        intent=Intent(
            name="ask_product",
            confidence=0.85,
            slots={},
            raw_message=message,
            extraction_method="rules",
        ),
        state=state,
        facts=CommerceFacts(orderable=True, has_products=True),
        history=history if history is not None else _not_found_history(),
        profile={"inbound_metadata": {}},
    )


def _support_decision(message: str = SHIPPING_DELAY) -> Decision:
    args = build_order_support_follow_up_args(
        message=message,
        history=_not_found_history(),
        order_verified=False,
    )
    return Decision(
        action=ACTION_LLM_REPLY,
        args=args,
        reason="existing_order_support_ownership:test",
        confidence=0.94,
    )


def _pipeline_after_decide(ctx: BrainContext, raw: Decision) -> Decision:
    contract = build_commerce_turn_contract(ctx, db=None)
    after_contract = maybe_enforce_commerce_turn_contract_decision(ctx, contract, raw)
    return maybe_enforce_catalog_order_continue_checkout(ctx, after_contract)


class TestSharedOwnershipPredicate:
    def test_llm_existing_order_support_protected(self) -> None:
        d = _support_decision()
        assert decision_owned_by_existing_order_support(d) is True
        assert order_support_reply_protected(
            decision_action=d.action,
            decision_args=d.args,
        )

    def test_track_order_protected(self) -> None:
        assert order_support_reply_protected(
            decision_action=ACTION_TRACK_ORDER,
            decision_args={},
        )

    def test_browse_not_protected(self) -> None:
        assert order_support_reply_protected(
            decision_action=ACTION_SEARCH_PRODUCTS,
            decision_args={"query": "أحذية"},
        ) is False


class TestCatalogCheckoutPostContractPreservation:
    def test_full_pipeline_preserves_support_after_catalog_enforce(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _catalog_ctx(SHIPPING_DELAY)
        raw = _support_decision()
        final = _pipeline_after_decide(ctx, raw)
        assert final.action == ACTION_LLM_REPLY
        assert final.args.get("topic") == "existing_order_support"
        assert "existing_order_support" in str(final.args.get("response_goal") or "")
        assert final.args.get("override_skipped_reason") == "existing_order_support_owned"
        assert final.action != ACTION_PROPOSE_DRAFT_ORDER

    def test_production_shaped_voice_via_decide_engine(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _catalog_ctx(SHIPPING_DELAY)
        raw = DefaultDecisionEngine().decide(ctx)
        assert raw.action == ACTION_LLM_REPLY
        assert raw.args.get("topic") == "existing_order_support"
        final = _pipeline_after_decide(ctx, raw)
        assert final.action == ACTION_LLM_REPLY
        assert final.args.get("topic") == "existing_order_support"

    def test_genuine_checkout_continues_without_support_ownership(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED", "true")
        ctx = _catalog_ctx(
            "ابي اطلب منتج جديد",
            history=[],
            catalog_only=True,
        )
        raw = Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": ""},
            reason="checkout_ack",
        )
        final = _pipeline_after_decide(ctx, raw)
        assert final.action == ACTION_PROPOSE_DRAFT_ORDER


class TestLoopGuardOrderSupportProtection:
    def test_track_order_skips_checkout_slot_substitution_path(self) -> None:
        from modules.ai.brain.commerce.checkout_slot_fallback import (  # noqa: PLC0415
            build_checkout_slot_fallback_reply,
        )

        assert order_support_reply_protected(decision_action=ACTION_TRACK_ORDER) is True
        stale_state = {
            "order_prep": {
                "catalog_line_items_authoritative": True,
                "line_items": [{"name": GENERIC_PRODUCT, "qty": 1}],
                "missing_fields": ["delivery_address"],
            },
        }
        slot = build_checkout_slot_fallback_reply(
            state=stale_state,
            inbound_text=GENERIC_ORDER_REF,
        )
        assert slot
        # Protection means webhook must not pass `slot` as checkout_recovery_reply.

    def test_support_llm_skips_slot_substitution(self) -> None:
        d = _support_decision()
        assert order_support_reply_protected(
            decision_action=d.action,
            decision_args=d.args,
        )

    def test_genuine_address_collection_still_uses_slot_fallback(self) -> None:
        from modules.ai.brain.commerce.checkout_slot_fallback import (  # noqa: PLC0415
            build_checkout_slot_fallback_reply,
        )

        assert order_support_reply_protected(
            decision_action=ACTION_PROPOSE_DRAFT_ORDER,
            decision_args={"topic": "checkout"},
        ) is False
        stale_state = {
            "order_prep": {
                "catalog_line_items_authoritative": True,
                "line_items": [{"name": GENERIC_PRODUCT, "qty": 1}],
                "missing_fields": ["delivery_address"],
            },
        }
        slot = build_checkout_slot_fallback_reply(
            state=stale_state,
            inbound_text="حي النرجس الرياض",
        )
        assert slot
        assert "العنوان" in slot or "عنوان" in slot
