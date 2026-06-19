"""P0 — bare start-order phrases must not become catalog product queries."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.clarification.resolved_product_guard import (  # noqa: E402
    compose_resolved_product_search_miss,
    extract_resolved_product_subject,
    has_resolved_product_subject,
)
from modules.ai.brain.commerce.product_breadth_policy import (  # noqa: E402
    global_availability_browse_requested,
)
from modules.ai.brain.commerce.start_order_verb_guard import (  # noqa: E402
    extract_start_order_product_query,
    is_bare_start_order_phrase,
    is_order_verb_only_query,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_START_ORDER,
    INTENT_WHO_ARE_YOU,
    Intent,
    MerchantConversationState,
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


def _ctx(msg: str, *, intent_name: str | None = None) -> BrainContext:
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
        state=MerchantConversationState(greeted=True, stage="discovery"),
        facts=_facts(),
    )


class TestStartOrderVerbGuardUnit:
    @pytest.mark.parametrize(
        "msg",
        [
            "ابي اطلب",
            "ابغى أطلب",
            "أبي أطلب",
            "ابغى اشتري",
            "أبي أشتري",
            "حاب أطلب",
            "أطلب منكم",
        ],
    )
    def test_bare_phrases_detected(self, msg: str) -> None:
        assert is_bare_start_order_phrase(msg) is True
        assert extract_start_order_product_query(msg) == ""
        assert is_order_verb_only_query("اطلب") is True

    def test_product_tail_extracted(self) -> None:
        assert extract_start_order_product_query("ابي اطلب عسل طلح") == "عسل طلح"

    def test_order_verb_not_product_substance(self) -> None:
        ctx = _ctx("ابي اطلب")
        assert has_resolved_product_subject(ctx) is False
        miss = compose_resolved_product_search_miss("اطلب")
        assert "بخصوص" not in miss
        assert "اطلب" not in miss


class TestStartOrderVerbGuardRouting:
    def test_bare_abi_otlob_routes_top_products_not_verb_search(self) -> None:
        ctx = _ctx("ابي اطلب")
        assert ctx.intent.name == INTENT_START_ORDER
        assert extract_start_order_product_query(ctx.message) == ""

        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_SEARCH_PRODUCTS
        assert dec.args.get("source") == "top_products_start_order"
        assert dec.args.get("query") in {"", None}
        assert "اطلب" not in str(dec.args.get("query") or "")
        assert "start_order with no product query" in dec.reason

    def test_bare_abgha_ashtari_routes_start_order(self) -> None:
        ctx = _ctx("ابغى أشتري")
        assert ctx.intent.name == INTENT_START_ORDER
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_SEARCH_PRODUCTS
        assert dec.args.get("source") == "top_products_start_order"
        assert "ما لقيت تطابق" not in dec.reason

    def test_order_with_product_searches_catalog(self) -> None:
        ctx = _ctx("ابي اطلب عسل طلح")
        assert extract_start_order_product_query(ctx.message) == "عسل طلح"
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_SEARCH_PRODUCTS
        assert dec.args.get("query") == "عسل طلح"
        assert dec.args.get("after_search") == "propose_order"

    def test_top_sellers_browse_pattern(self) -> None:
        msg = "أكثر مبيعاً"
        assert global_availability_browse_requested(msg) is True
        assert is_bare_start_order_phrase(msg) is False
        ctx = _ctx(msg, intent_name="ask_product")
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_SEARCH_PRODUCTS
        assert dec.args.get("source") in {"top_products", "global_browse", "category_browse"}

    def test_global_types_browse_pattern(self) -> None:
        msg = "وش الأنواع اللي عندكم"
        assert global_availability_browse_requested(msg) is True
        assert is_bare_start_order_phrase(msg) is False
        ctx = _ctx(msg, intent_name="ask_product")
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_SEARCH_PRODUCTS
        assert dec.args.get("source") in {"top_products", "global_browse", "category_browse"}

    def test_resolved_subject_empty_for_bare_start_order(self) -> None:
        ctx = _ctx("ابي اطلب")
        subject = extract_resolved_product_subject(
            ctx,
            query=str((DefaultDecisionEngine().decide(ctx).args or {}).get("query") or ""),
        )
        assert subject == ""

    def test_identity_phrase_not_order_product_query(self) -> None:
        """Regression: «من أنت» must not be hijacked as catalog search."""
        msg = "من أنت"
        assert is_bare_start_order_phrase(msg) is False
        assert extract_start_order_product_query(msg) == ""
        ctx = BrainContext(
            tenant_id=7,
            customer_phone="966542980511",
            message=msg,
            intent=Intent(name=INTENT_WHO_ARE_YOU, confidence=0.98, raw_message=msg),
            state=MerchantConversationState(greeted=False, stage="discovery"),
            facts=_facts(),
        )
        dec = DefaultDecisionEngine().decide(ctx)
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "persona_identity"
        assert dec.args.get("block_commerce_escalation") is True
