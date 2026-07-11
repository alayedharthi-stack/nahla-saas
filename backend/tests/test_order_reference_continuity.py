"""PR A — bare order-reference recognition and continuity regressions."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.inbound_fragment_guard import (  # noqa: E402
    evaluate_duplicate_fragment_turn,
    reset_fragment_cache_for_tests,
)
from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    boost_track_order_intent,
    extract_bare_order_reference,
    extract_order_reference_from_history,
    has_pending_order_reference_evidence,
    is_order_support_operational_follow_up,
    try_order_reference_continuity_decision,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_LLM_REPLY,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.orders import TrackOrderHandler  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_ASK_PRICE,
    INTENT_SOCIAL,
    INTENT_TRACK_ORDER,
    Intent,
    MerchantConversationState,
)
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_order,
    seed_tenant,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "نورة عبدالله"
GENERIC_ORDER_REF = "284719365"
GENERIC_ORDER_REF_2 = "901234567"
GENERIC_PRODUCT_LINE = "حذاء رياضي أبيض وقميص قطني أزرق"


@pytest.fixture(autouse=True)
def _clear_fragment_cache():
    reset_fragment_cache_for_tests()
    yield
    reset_fragment_cache_for_tests()


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=5,
        in_stock_count=5,
        orderable=True,
        store_name=GENERIC_MERCHANT,
    )


def _ctx(
    message: str,
    *,
    intent: Intent | None = None,
    history: list | None = None,
    commerce_bundle: dict | None = None,
    inbound_metadata: dict | None = None,
) -> BrainContext:
    state = MerchantConversationState()
    if intent is None:
        intent = rules.match(message) or Intent(name="general", confidence=0.5, raw_message=message)
    profile = {"inbound_metadata": inbound_metadata or {}}
    return BrainContext(
        tenant_id=1,
        message=message,
        intent=intent,
        state=state,
        facts=_facts(),
        history=history or [],
        commerce_bundle=commerce_bundle or {},
        profile=profile,
        customer_phone=DEFAULT_PHONE_E164,
    )


class TestBareOrderReferenceExtraction:
    def test_a1_bare_numeric_reference_detected(self) -> None:
        assert extract_bare_order_reference(GENERIC_ORDER_REF) == GENERIC_ORDER_REF

    def test_a1_boosts_track_order_intent(self) -> None:
        boosted = boost_track_order_intent(GENERIC_ORDER_REF)
        assert boosted is not None
        assert boosted.name == INTENT_TRACK_ORDER
        assert boosted.slots.get("order_id") == GENERIC_ORDER_REF

    def test_non_order_number_not_bare_reference(self) -> None:
        assert extract_bare_order_reference("12345") == ""
        assert extract_bare_order_reference("ابي اطلب") == ""


class TestRepeatedOrderReferenceFragment:
    def test_a3_repeated_numeric_reference_not_duplicate_fragment(self) -> None:
        tenant_id = 1
        phone = "+966501112233"
        first = evaluate_duplicate_fragment_turn(
            tenant_id=tenant_id, customer_phone=phone, text=GENERIC_ORDER_REF,
        )
        second = evaluate_duplicate_fragment_turn(
            tenant_id=tenant_id, customer_phone=phone, text=GENERIC_ORDER_REF,
        )
        assert first.process_turn is True
        assert second.process_turn is True
        assert second.send_clarification_once is False


class TestPendingOrderReferenceEvidence:
    def test_history_bare_reference_is_pending_evidence(self) -> None:
        history = [
            {"direction": "in", "body": GENERIC_ORDER_REF},
        ]
        assert extract_order_reference_from_history(history) == GENERIC_ORDER_REF
        assert has_pending_order_reference_evidence(history=history) is True

    def test_voice_shipping_follow_up_after_pending_reference(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF}]
        voice_msg = (
            "السلام عليكم، أبي أغير شركة الشحن للطلب "
            "وهل فيه تأخير بالتوصيل؟"
        )
        assert is_order_support_operational_follow_up(
            voice_msg,
            history=history,
        ) is True

    def test_a5_unresolved_reference_voice_stays_support_oriented(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF_2}]
        msg = "وين طلبي وهل وصل للمستودع؟"
        decision = try_order_reference_continuity_decision(
            _ctx(msg, history=history, intent=Intent(name=INTENT_SOCIAL, confidence=0.9, raw_message=msg)),
        )
        assert decision is not None
        assert decision.action in {ACTION_TRACK_ORDER, ACTION_LLM_REPLY}
        assert decision.action != "social"


class TestEngineOrderReferenceRouting:
    def test_a1_engine_routes_bare_reference_to_track_order(self) -> None:
        ctx = _ctx(GENERIC_ORDER_REF, intent=Intent(name="general", confidence=0.4, raw_message=GENERIC_ORDER_REF))
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_TRACK_ORDER
        assert decision.args.get("order_number") == GENERIC_ORDER_REF

    def test_a4_voice_shipping_after_reference_not_social(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF}]
        msg = "السلام عليكم، متى يوصل الطلب وهل أقدر أغير شركة الشحن؟"
        ctx = _ctx(
            msg,
            history=history,
            intent=Intent(name=INTENT_SOCIAL, confidence=0.92, raw_message=msg),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action in {ACTION_LLM_REPLY, ACTION_TRACK_ORDER}
        assert "social courtesy" not in (decision.reason or "").lower()

    def test_a6_product_mention_after_reference_not_catalog(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF}]
        msg = f"الطلب فيه {GENERIC_PRODUCT_LINE}"
        ctx = _ctx(
            msg,
            history=history,
            intent=Intent(name="ask_product", confidence=0.8, raw_message=msg),
        )
        state = ctx.state
        state.last_search_candidates = [
            {"title": "حذاء رياضي أبيض", "external_id": "sku-shoe", "can_checkout": True},
            {"title": "قميص قطني أزرق", "external_id": "sku-shirt", "can_checkout": True},
        ]
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action in {ACTION_LLM_REPLY, ACTION_TRACK_ORDER}
        assert decision.action not in {"search_products", "propose_draft_order"}


class TestTrackOrderExecution:
    @pytest.fixture()
    def db(self):
        session, _engine = make_scenario_db()
        yield session
        session.close()

    @pytest.fixture()
    def tenant_ctx(self, db):
        tenant = seed_tenant(db, name=GENERIC_MERCHANT)
        customer = seed_customer(db, tenant.id, name=GENERIC_CUSTOMER)
        conv = seed_conversation(db, tenant.id, customer_id=customer.id)
        return SimpleNamespace(
            tenant_id=tenant.id,
            customer_id=customer.id,
            conversation_id=conv.id,
            phone=DEFAULT_PHONE_E164,
        )

    def _track_ctx(self, tenant_ctx, message: str, *, db, intent: Intent | None = None):
        matched = intent or rules.match(message) or Intent(
            name=INTENT_TRACK_ORDER,
            confidence=0.95,
            raw_message=message,
            slots={"order_id": extract_bare_order_reference(message)},
        )
        state = MerchantConversationState(greeted=True, stage="discovery")
        brain = BrainContext(
            tenant_id=tenant_ctx.tenant_id,
            customer_phone=tenant_ctx.phone,
            customer_id=tenant_ctx.customer_id,
            conversation_id=tenant_ctx.conversation_id,
            message=message,
            intent=matched,
            state=state,
            facts=_facts(),
            history=[],
        )
        brain._db = db  # noqa: SLF001
        return brain

    def test_a1_found_order_reference_resolves(self, db, tenant_ctx) -> None:
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            external_id=f"manual-ord-{GENERIC_ORDER_REF}",
            external_order_number=GENERIC_ORDER_REF,
            status="processing",
            customer_info={"phone": tenant_ctx.phone},
            line_items=[{"product_name": "عطر ورد 100ml", "quantity": 1}],
        )
        ctx = self._track_ctx(tenant_ctx, GENERIC_ORDER_REF, db=db)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_TRACK_ORDER

        async def _run():
            return await TrackOrderHandler().handle(decision, ctx)

        result = asyncio.run(_run())
        assert result.success is True
        assert str(result.data.get("reference") or "") == GENERIC_ORDER_REF

    def test_a2_missing_order_reference_fails_honestly(self, db, tenant_ctx) -> None:
        ctx = self._track_ctx(tenant_ctx, GENERIC_ORDER_REF_2, db=db)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_TRACK_ORDER

        async def _run():
            return await TrackOrderHandler().handle(decision, ctx)

        result = asyncio.run(_run())
        assert result.success is False
        assert result.error == "order_not_found"


class TestRegressionIntact:
    def test_a7_class4_track_order_rules_still_match(self) -> None:
        intent = rules.match("وين طلبي")
        assert intent is not None
        assert intent.name == INTENT_TRACK_ORDER

    def test_a8_class2_price_path_still_matches(self) -> None:
        intent = rules.match("بكم القميص")
        assert intent is not None
        assert intent.name == INTENT_ASK_PRICE

    def test_a9_social_greeting_not_order_reference(self) -> None:
        msg = "السلام عليكم"
        assert extract_bare_order_reference(msg) == ""
        assert boost_track_order_intent(msg) is None
