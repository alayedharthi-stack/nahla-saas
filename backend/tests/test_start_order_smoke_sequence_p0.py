"""P0 smoke — greeting → bare start-order → honey types browse sequence."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.commerce_browse_category_guard import (  # noqa: E402
    filter_products_for_browse_turn,
    resolve_browse_category_scope,
)
from modules.ai.brain.commerce.commerce_conversation_guard import (  # noqa: E402
    catalog_has_honey_skus,
    maybe_lock_order_category_context,
)
from modules.ai.brain.commerce.start_order_verb_guard import (  # noqa: E402
    is_bare_start_order_phrase,
)
from modules.ai.brain.decision.actions import ACTION_CATALOG_NAVIGATE, ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.postprocess.staff_escalation_truth_guard import (  # noqa: E402
    SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR,
    apply_staff_escalation_truth_guard,
)
from modules.ai.brain.product_discovery_gate import (  # noqa: E402
    product_discovery_block_reason,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_START_ORDER,
    MerchantConversationState,
)


def _honey_store_catalog() -> list[dict]:
    return [
        {"id": 1, "title": "عسل طلح نجد 250 جرام", "category": "عسل", "quantity": 5},
        {"id": 2, "title": "عسل سمر الحجاز 500 جرام", "category": "عسل", "quantity": 3},
        {"id": 3, "title": "كريم سم النحل 25 جم", "category": "عناية", "quantity": 5},
        {"id": 4, "title": "زيت سم النحل بخاخ 30", "category": "عناية", "quantity": 2},
    ]


def _facts(catalog: list[dict] | None = None) -> CommerceFacts:
    cat = catalog or _honey_store_catalog()
    return CommerceFacts(
        has_products=True,
        product_count=len(cat),
        in_stock_count=len(cat),
        orderable=True,
        snapshot_fresh=True,
        store_name="متجر عسل",
        top_products=cat,
    )


def _ctx(
    msg: str,
    *,
    state: MerchantConversationState | None = None,
    greeted: bool = True,
) -> BrainContext:
    intent = rules.match(msg)
    if intent is None:
        from modules.ai.brain.types import Intent

        intent = Intent(name="general", confidence=0.5, raw_message=msg)
    return BrainContext(
        tenant_id=7,
        customer_phone="966542980511",
        message=msg,
        intent=intent,
        state=state or MerchantConversationState(greeted=greeted, stage="discovery"),
        facts=_facts(),
    )


class TestBareStartOrderNotBlockedByDiscoveryGate:
    def test_abi_otlob_allows_top_products_start_order(self) -> None:
        ctx = _ctx("ابي اطلب", greeted=True)
        assert ctx.intent.name == INTENT_START_ORDER
        reason = product_discovery_block_reason(
            ctx, source="top_products_start_order",
        )
        assert reason is None

    def test_decision_routes_top_products_start_order(self) -> None:
        ctx = _ctx("ابي اطلب", greeted=True)
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_SEARCH_PRODUCTS
        assert dec.args.get("source") == "top_products_start_order"
        assert dec.args.get("query") in {"", None}


class TestBareStartOrderBlocksGenericAck:
    def test_staff_escalation_guard_uses_order_prompt_not_receipt_stub(self) -> None:
        result = apply_staff_escalation_truth_guard(
            reply="تم تحويلك للدعم وسيتم التواصل معك",
            inbound_text="ابي اطلب",
        )
        assert result.replaced is True
        assert result.reply != SAFE_NO_ESCALATION_EVIDENCE_REPLY_AR
        assert "وصلت رسالتك" not in result.reply
        assert "أبشر" in result.reply
        assert "منتج" in result.reply or "تطلب" in result.reply


class TestHoneySessionLockAfterStartOrder:
    def test_bare_start_order_locks_honey_category(self) -> None:
        state = MerchantConversationState(greeted=True)
        catalog = _honey_store_catalog()
        assert catalog_has_honey_skus(catalog)
        assert maybe_lock_order_category_context(
            state,
            "ابي اطلب",
            catalog=catalog,
        ) is True
        assert state.commerce_session.get("active_category") == "عسل"
        assert state.commerce_session.get("order_intent") is True

    def test_wesh_alanwa3_scoped_to_honey_after_lock(self) -> None:
        state = MerchantConversationState(greeted=True)
        maybe_lock_order_category_context(
            state,
            "ابي اطلب",
            catalog=_honey_store_catalog(),
        )
        msg = "وش الأنواع"
        assert resolve_browse_category_scope(
            msg,
            active_category=state.commerce_session.get("active_category", ""),
            source="top_products",
        ) == "عسل"
        filtered = filter_products_for_browse_turn(
            _honey_store_catalog(),
            message=msg,
            source="top_products",
            state=state,
        )
        titles = " ".join(p["title"] for p in filtered)
        assert "عسل" in titles
        assert "كريم" not in titles
        assert "زيت" not in titles


class TestSmokeSequenceRegression:
    """السلام عليكم → ابي اطلب → وش الأنواع"""

    def test_full_sequence_routing(self) -> None:
        catalog = _honey_store_catalog()
        state = MerchantConversationState(greeted=False, stage="discovery")
        engine = DefaultDecisionEngine()

        greet = rules.match("السلام عليكم")
        assert greet is not None
        assert greet.name == "greeting"
        state.greeted = True

        assert is_bare_start_order_phrase("ابي اطلب")
        maybe_lock_order_category_context(state, "ابي اطلب", catalog=catalog)
        start_ctx = BrainContext(
            tenant_id=7,
            customer_phone="966542980511",
            message="ابي اطلب",
            intent=rules.match("ابي اطلب"),
            state=state,
            facts=_facts(catalog),
        )
        start_dec = engine.decide(start_ctx)
        assert start_dec.action == ACTION_SEARCH_PRODUCTS
        assert start_dec.args.get("source") == "top_products_start_order"

        types_ctx = BrainContext(
            tenant_id=7,
            customer_phone="966542980511",
            message="وش الأنواع",
            intent=rules.match("وش الأنواع") or start_ctx.intent,
            state=state,
            facts=_facts(catalog),
        )
        types_dec = engine.decide(types_ctx)
        assert types_dec.action in (ACTION_SEARCH_PRODUCTS, ACTION_CATALOG_NAVIGATE)
        filtered = filter_products_for_browse_turn(
            catalog,
            message="وش الأنواع",
            source=str(types_dec.args.get("source") or "top_products"),
            state=state,
        )
        assert filtered
        joined = " ".join(p["title"] for p in filtered)
        assert "كريم" not in joined
        assert "زيت" not in joined
