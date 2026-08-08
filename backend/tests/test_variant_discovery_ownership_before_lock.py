"""
Path C — variant vs Discovery ownership before fulfillment lock.

Regression matrix from Patch Authorization (awaiting_variant_choice).
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.commerce.state_continuity_identity import (  # noqa: E402
    maybe_apply_variant_discovery_ownership_before_lock,
    resolve_product_for_state_continuity,
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
from modules.ai.brain.order_context_gate import (  # noqa: E402
    should_block_product_discovery,
    should_skip_catalog_preload,
)
from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: E402
    build_product_claim_grounding_evidence,
)
from modules.ai.brain.postprocess.product_claim_grounding_guard import (  # noqa: E402
    apply_product_claim_grounding_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_GENERAL,
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    OrderPreparationState,
)


def _awaiting_jacket_state() -> MerchantConversationState:
    st = MerchantConversationState(turn=54, stage="exploring")
    st.current_product_focus = {
        "id": "501",
        "external_id": "1921568272",
        "title": "جاكيت",
        "price": 169,
        "in_stock": True,
        "variants": [
            {"id": "v1", "name": "S", "price": 40, "in_stock": True},
            {"id": "v2", "name": "XL", "price": 44, "in_stock": True},
            {"id": "v3", "name": "L", "price": 38, "in_stock": True},
        ],
    }
    st.order_prep = OrderPreparationState(
        awaiting_variant_choice=True,
        pending_variant_product_id="501",
    )
    st.last_search_candidates = [dict(st.current_product_focus)]
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
    facts: Optional[CommerceFacts] = None,
) -> BrainContext:
    return BrainContext(
        tenant_id=1,
        customer_phone="+966555906901",
        message=msg,
        raw_message=msg,
        intent=intent,
        state=state,
        facts=facts or CommerceFacts(has_products=True, product_count=28, orderable=True),
        history=[],
    )


def test_numeric_variant_pick_retains_awaiting_and_routes_to_pick() -> None:
    state = _awaiting_jacket_state()
    own = maybe_apply_variant_discovery_ownership_before_lock(
        state,
        message="1",
        intent=_intent(INTENT_GENERAL),
    )
    assert own["mode"] == "retain_pick"
    assert own["applied"] is False
    assert state.order_prep.awaiting_variant_choice is True

    ctx = _ctx("1", state=state, intent=_intent(INTENT_GENERAL))
    dec = DefaultDecisionEngine().decide(ctx)
    assert dec.action == ACTION_PROPOSE_DRAFT_ORDER
    assert "variant_pick" in (dec.args or {})


def test_label_variant_pick_xl_still_works() -> None:
    state = _awaiting_jacket_state()
    own = maybe_apply_variant_discovery_ownership_before_lock(
        state,
        message="XL",
        intent=_intent(INTENT_GENERAL),
    )
    assert own["mode"] == "retain_pick"
    assert state.order_prep.awaiting_variant_choice is True

    ctx = _ctx("XL", state=state, intent=_intent(INTENT_GENERAL))
    dec = DefaultDecisionEngine().decide(ctx)
    assert dec.action == ACTION_PROPOSE_DRAFT_ORDER
    assert (dec.args or {}).get("variant_pick", {}).get("label") == "XL"


def test_same_product_free_text_suspends_and_allows_catalog_preload() -> None:
    state = _awaiting_jacket_state()
    intent = _intent(INTENT_ASK_PRODUCT, product_query="الجاكيت")
    own = maybe_apply_variant_discovery_ownership_before_lock(
        state,
        message="حدثني عن الجاكيت",
        intent=intent,
    )
    assert own["applied"] is True
    assert own["mode"] == "suspend_retain_identity"
    assert state.order_prep.awaiting_variant_choice is False
    assert state.current_product_focus is not None
    assert state.current_product_focus.get("id") == "501"
    assert "price" not in (state.current_product_focus or {})

    assert should_skip_catalog_preload(
        message="حدثني عن الجاكيت",
        state=state,
        intent=intent,
    ) is False

    ctx = _ctx("حدثني عن الجاكيت", state=state, intent=intent)
    assert should_block_product_discovery(ctx) is False
    dec = DefaultDecisionEngine().decide(ctx)
    assert dec.action != ACTION_PROPOSE_DRAFT_ORDER
    assert "variant_pick" not in (dec.args or {})
    assert dec.action == ACTION_SEARCH_PRODUCTS


def test_implicit_price_projects_variant_facts_and_guard_allows() -> None:
    state = _awaiting_jacket_state()
    intent = _intent(INTENT_ASK_PRICE)
    own = maybe_apply_variant_discovery_ownership_before_lock(
        state,
        message="وش سعره؟",
        intent=intent,
    )
    assert own["mode"] == "suspend_retain_identity"
    assert state.order_prep.awaiting_variant_choice is False

    # Focus retains identity; hydrate uses focus.variants already present after
    # re-attach of live catalog shape for this unit test.
    state.current_product_focus = {
        "id": 501,
        "external_id": "1921568272",
        "title": "جاكيت",
        "price": 169,
        "variants": [
            {"id": "v1", "name": "S", "price": 40, "in_stock": True, "is_default": False},
            {"id": "v2", "name": "XL", "price": 44, "in_stock": True, "is_default": False},
            {"id": "v3", "name": "L", "price": 38, "in_stock": True, "is_default": False},
        ],
    }
    ctx = _ctx("وش سعره؟", state=state, intent=intent)
    dec = try_variant_pricing_decision(ctx)
    assert dec is not None
    assert dec.action == ACTION_VARIANT_PRICING
    assert "ambiguous" in (dec.reason or "")
    facts = (dec.args or {}).get("catalog_fact_products") or []
    assert facts, "live variant rows must project into catalog_fact_products"
    reply = str((dec.args or {}).get("reply_text") or "")
    assert "40" in reply and "44" in reply and "38" in reply
    assert "169" not in reply  # parent must not silently replace variant SoT

    evidence = build_product_claim_grounding_evidence(
        None,
        1,
        catalog_fact_products=facts,
        chosen_path="variant_pricing",
    )
    assert 40 in evidence.grounded_prices
    assert 44 in evidence.grounded_prices
    assert 38 in evidence.grounded_prices

    guard = apply_product_claim_grounding_guard(
        reply=reply,
        db=None,
        tenant_id=1,
        catalog_fact_products=facts,
        chosen_path="variant_pricing",
    )
    assert guard.action == "allowed"
    assert "ما ظهر عندي سعر مؤكد" not in guard.reply


def test_explicit_new_product_invalidates_and_searches() -> None:
    state = _awaiting_jacket_state()
    intent = _intent(INTENT_ASK_PRODUCT, product_query="فستان")
    own = maybe_apply_variant_discovery_ownership_before_lock(
        state,
        message="عندكم فستان؟",
        intent=intent,
    )
    assert own["mode"] == "invalidate"
    assert state.order_prep.awaiting_variant_choice is False
    assert state.order_prep.pending_variant_product_id == ""
    assert state.current_product_focus is None  # no jacket identity leak

    assert should_skip_catalog_preload(
        message="عندكم فستان؟",
        state=state,
        intent=intent,
    ) is False

    ctx = _ctx("عندكم فستان؟", state=state, intent=intent)
    dec = DefaultDecisionEngine().decide(ctx)
    assert dec.action == ACTION_SEARCH_PRODUCTS
    query = str((dec.args or {}).get("query") or "")
    assert "فستان" in query
    assert "جاكيت" not in query
    # Must not re-resolve old jacket id
    assert not (dec.args or {}).get("product_id")
    assert (dec.args or {}).get("source") != "state_continuity_reresolve"


def test_ambiguous_free_text_not_silent_variant_pick() -> None:
    state = _awaiting_jacket_state()
    intent = _intent(INTENT_GENERAL)
    own = maybe_apply_variant_discovery_ownership_before_lock(
        state,
        message="أبغى غيره",
        intent=intent,
    )
    assert own["applied"] is True
    assert own["mode"] == "suspend_retain_identity"
    assert state.order_prep.awaiting_variant_choice is False

    ctx = _ctx("أبغى غيره", state=state, intent=intent)
    dec = DefaultDecisionEngine().decide(ctx)
    assert dec.action != ACTION_PROPOSE_DRAFT_ORDER
    assert "variant_pick" not in (dec.args or {})


def test_catalog_reresolve_is_tenant_scoped() -> None:
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
            external_id="1921568272",
        )
    finally:
        store_knowledge.CatalogContextBuilder = original

    assert out is None
    builder.get_by_external_id.assert_called_once_with("1921568272")


def test_no_awaiting_variant_ordinary_discovery_unchanged() -> None:
    st = MerchantConversationState(turn=2, stage="exploring")
    st.order_prep = OrderPreparationState(awaiting_variant_choice=False)
    intent = _intent(INTENT_ASK_PRODUCT, product_query="حذاء")
    own = maybe_apply_variant_discovery_ownership_before_lock(
        st,
        message="عندكم حذاء؟",
        intent=intent,
    )
    assert own["applied"] is False
    assert own["mode"] == "none"

    ctx = _ctx("عندكم حذاء؟", state=st, intent=intent)
    dec = DefaultDecisionEngine().decide(ctx)
    assert dec.action == ACTION_SEARCH_PRODUCTS
