"""Class 4 — order status lookup routing and safe compose."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
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
    INTENT_GREETING,
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
GENERIC_CUSTOMER = "أحمد سالم"
_GENERIC_ITEM = {
    "product_id": "sku-shirt-blue",
    "product_name": "قميص قطني أزرق",
    "quantity": 1,
    "unit_price": 149.0,
}

_CREATE_ORDER_PRESSURE = ("إنشاء طلب جديد", "هل تريد إنشاء")
_PICKER_PRESSURE = ("اختر رقم", "اختر من")
_PHONE_ASK = ("رقم الجوال", "رقم جوال", "الجوال المستخدم")
_CHECKOUT_FIELDS = ("اسمك", "عنوانك", "المدينة", "طريقة الدفع")


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


@pytest.fixture()
def tenant_ctx(db):
    tenant = seed_tenant(db, name=GENERIC_MERCHANT)
    customer = seed_customer(db, tenant.id, name=GENERIC_CUSTOMER)
    conv = seed_conversation(db, tenant.id, customer_id=customer.id)
    return SimpleNamespace(
        tenant_id=tenant.id,
        customer_id=customer.id,
        conversation_id=conv.id,
        phone=DEFAULT_PHONE_E164,
    )


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=5,
        in_stock_count=5,
        orderable=True,
        store_name=GENERIC_MERCHANT,
    )


def _ctx(
    tenant_ctx,
    message: str,
    *,
    db=None,
    intent: Intent | None = None,
    slots: dict | None = None,
) -> BrainContext:
    matched = intent or rules.match(message)
    assert matched is not None
    if slots:
        matched = Intent(
            name=matched.name,
            confidence=float(matched.confidence or 0.95),
            slots=dict(slots),
            raw_message=message,
            extraction_method=matched.extraction_method or "rules",
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
    if db is not None:
        brain._db = db  # noqa: SLF001
    return brain


async def _run_track_turn(
    db,
    tenant_ctx,
    message: str,
    *,
    slots: dict | None = None,
    llm_reply: str | None = None,
) -> tuple:
    ctx = _ctx(tenant_ctx, message, db=db, slots=slots)
    decision = DefaultDecisionEngine().decide(ctx)
    handler = TrackOrderHandler()
    result = await handler.handle(decision, ctx)
    composer = DefaultComposer()
    with patch(
        "modules.ai.brain.intent.link_disambiguation.should_use_generative_tracking_follow_up",
        return_value=False,
    ):
        if llm_reply is not None:
            with patch.object(
                composer,
                "_llm_compose",
                new=AsyncMock(return_value=llm_reply),
            ):
                reply = await composer.compose(decision, result, ctx)
        else:
            reply = await composer.compose(decision, result, ctx)
    return decision, result, reply, ctx


def _assert_no_order_status_pressure(reply: str, result) -> None:
    lowered = reply.lower()
    for phrase in _CREATE_ORDER_PRESSURE:
        assert phrase not in reply
    for phrase in _PICKER_PRESSURE:
        assert phrase not in reply
    for phrase in _CHECKOUT_FIELDS:
        assert phrase not in reply
    assert "pending_candidates" not in (result.data or {})
    assert not (result.data or {}).get("pending_candidates")


class TestExplicitOrderNumberLookup:
    def test_known_order_number_returns_status_without_catalog_pressure(
        self, db, tenant_ctx,
    ) -> None:
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            external_id="manual-ord-12345",
            external_order_number="12345",
            status="processing",
            customer_info={"phone": tenant_ctx.phone},
            line_items=[_GENERIC_ITEM],
        )
        decision, result, reply, _ctx_obj = asyncio.run(
            _run_track_turn(
                db,
                tenant_ctx,
                "طلبي رقم 12345",
                slots={"order_id": "12345"},
            ),
        )
        assert decision.action == ACTION_TRACK_ORDER
        assert result.success is True
        assert result.data.get("chosen_path") == "track_order_status"
        assert "12345" in reply
        assert "جاري المعالجة" in reply or "حالة" in reply
        _assert_no_order_status_pressure(reply, result)


class TestMissingOrderNumber:
    def test_track_without_number_asks_order_number_only(
        self, db, tenant_ctx,
    ) -> None:
        llm_reply = "أرسل رقم الطلب لو سمحت حتى أتحقق لك من حالته."
        decision, result, reply, _ctx_obj = asyncio.run(
            _run_track_turn(
                db,
                tenant_ctx,
                "وين طلبي؟",
                llm_reply=llm_reply,
            ),
        )
        assert decision.action == ACTION_TRACK_ORDER
        assert result.success is False
        assert result.data.get("message") == "need_order_number"
        assert result.data.get("chosen_path") == "track_order_need_order_number"
        assert result.data.get("compose_source") == "llm"
        assert result.data.get("response_mode") == "llm"
        assert result.data.get("final_customer_text_source") == "llm"
        assert reply == llm_reply
        assert "رقم الطلب" in reply or "order number" in reply.lower()
        for phrase in _PHONE_ASK:
            assert phrase not in reply
        for phrase in _CHECKOUT_FIELDS:
            assert phrase not in reply
        _assert_no_order_status_pressure(reply, result)


class TestOrderNotFound:
    def test_unknown_order_number_honest_miss_no_create_pressure(
        self, db, tenant_ctx,
    ) -> None:
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            external_id="manual-ord-other",
            external_order_number="11111",
            status="processing",
            customer_info={"phone": tenant_ctx.phone},
            line_items=[_GENERIC_ITEM],
        )
        llm_reply = "ما لقيت طلب بهذا الرقم، تأكد من رقم الطلب لو سمحت."
        decision, result, reply, _ctx_obj = asyncio.run(
            _run_track_turn(
                db,
                tenant_ctx,
                "طلبي رقم 999999",
                slots={"order_id": "999999"},
                llm_reply=llm_reply,
            ),
        )
        assert decision.action == ACTION_TRACK_ORDER
        assert result.success is False
        assert result.data.get("message") == "order_not_found"
        assert result.data.get("chosen_path") == "track_order_not_found"
        assert result.data.get("compose_source") == "llm"
        assert result.data.get("response_mode") == "llm"
        assert reply == llm_reply
        assert reply != T.order_status_not_found()
        assert "11111" not in reply
        _assert_no_order_status_pressure(reply, result)


class TestClass2PriceRegression:
    def test_price_question_not_routed_to_track_order(self, tenant_ctx) -> None:
        message = "كم سعر الطلح؟"
        ctx = _ctx(tenant_ctx, message)
        decision = DefaultDecisionEngine().decide(ctx)
        assert ctx.intent.name != INTENT_TRACK_ORDER
        assert decision.action != ACTION_TRACK_ORDER


class TestGreetingRegression:
    def test_salaam_not_routed_to_track_order(self, tenant_ctx) -> None:
        message = "السلام عليكم"
        ctx = _ctx(tenant_ctx, message)
        decision = DefaultDecisionEngine().decide(ctx)
        assert ctx.intent.name == INTENT_GREETING
        assert decision.action != ACTION_TRACK_ORDER
        assert decision.action in {ACTION_LLM_REPLY}
