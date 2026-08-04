"""BQ-1 — tracking evidence must survive track_order → compose → context."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.active_order_context import (  # noqa: E402
    load_commerce_bundle,
    merge_track_evidence_into_bundle,
    prepare_tracking_follow_up_decision,
    tracking_available_from_bundle,
)
from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.decision.actions import ACTION_TRACK_ORDER  # noqa: E402
from modules.ai.brain.execution.orders import TrackOrderHandler  # noqa: E402
from modules.ai.brain.postprocess.shipment_truth_guard import (  # noqa: E402
    apply_shipment_truth_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE,
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_order,
    seed_shipment,
    seed_tenant,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
OTHER_PHONE = "+966500000099"
_GENERIC_ITEM = {
    "product_id": "sku-shoe-white",
    "product_name": "حذاء رياضي أبيض",
    "quantity": 1,
    "unit_price": 299.0,
}


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


@pytest.fixture()
def tenant_ctx(db):
    tenant = seed_tenant(db, name=GENERIC_MERCHANT)
    customer = seed_customer(db, tenant.id, phone=DEFAULT_PHONE_E164, name="أحمد سالم")
    conv = seed_conversation(db, tenant.id, customer_id=customer.id)
    return SimpleNamespace(
        tenant_id=tenant.id,
        customer_id=customer.id,
        conversation_id=conv.id,
        phone=DEFAULT_PHONE_E164,
    )


def _ctx(tenant_ctx, message: str, *, db=None, slots: dict | None = None) -> BrainContext:
    intent = Intent(
        name="track_order",
        confidence=0.95,
        slots=dict(slots or {}),
        raw_message=message,
        extraction_method="rules",
    )
    brain = BrainContext(
        tenant_id=tenant_ctx.tenant_id,
        customer_phone=tenant_ctx.phone,
        customer_id=tenant_ctx.customer_id,
        conversation_id=tenant_ctx.conversation_id,
        message=message,
        intent=intent,
        state=MerchantConversationState(greeted=True, stage="support"),
        facts=CommerceFacts(has_products=True, store_name=GENERIC_MERCHANT),
        history=[],
    )
    if db is not None:
        brain._db = db  # noqa: SLF001
    return brain


async def _run_track(db, tenant_ctx, message: str, *, slots: dict | None = None) -> tuple:
    ctx = _ctx(tenant_ctx, message, db=db, slots=slots)
    decision = SimpleNamespace(action=ACTION_TRACK_ORDER, args=dict(slots or {}))
    result = await TrackOrderHandler().handle(decision, ctx)
    composer = DefaultComposer()
    with patch(
        "modules.ai.brain.intent.link_disambiguation.should_use_generative_tracking_follow_up",
        return_value=False,
    ):
        reply = await composer.compose(decision, result, ctx)
    return result, reply, ctx


class TestTrackOrderHandlerPassesEvidence:
    @pytest.mark.parametrize(
        "tracking_number",
        ["1Z999AA10123456784", "SF123456789CN"],
    )
    def test_shipped_order_tracking_number_in_handler_and_reply(
        self, db, tenant_ctx, tracking_number: str,
    ) -> None:
        order = seed_order(
            db,
            tenant_ctx.tenant_id,
            external_order_number="ORD-9001",
            status="shipped",
            customer_info={"phone": tenant_ctx.phone},
            line_items=[_GENERIC_ITEM],
        )
        seed_shipment(
            db,
            tenant_ctx.tenant_id,
            order.id,
            tracking_number=tracking_number,
            provider="smsa",
            status="shipped",
        )
        result, reply, _ctx_obj = asyncio.run(
            _run_track(db, tenant_ctx, "وين طلبي؟"),
        )
        assert result.success is True
        assert result.data.get("tracking_number") == tracking_number
        assert tracking_number in reply

    def test_tracking_url_present_in_reply(self, db, tenant_ctx) -> None:
        order = seed_order(
            db,
            tenant_ctx.tenant_id,
            external_order_number="ORD-9002",
            status="shipped",
            customer_info={"phone": tenant_ctx.phone},
            line_items=[_GENERIC_ITEM],
        )
        shipment = seed_shipment(
            db,
            tenant_ctx.tenant_id,
            order.id,
            tracking_number="SF123456789CN",
            provider="aramex",
            status="shipped",
        )
        shipment.label_url = "https://track.example/SF123456789CN"
        db.add(shipment)
        db.commit()

        result, reply, _ctx_obj = asyncio.run(
            _run_track(db, tenant_ctx, "وين طلبي؟"),
        )
        assert result.success is True
        assert "https://track.example/SF123456789CN" in reply
        assert result.data.get("tracking_url") == "https://track.example/SF123456789CN"

    def test_processing_order_does_not_invent_tracking(self, db, tenant_ctx) -> None:
        seed_order(
            db,
            tenant_ctx.tenant_id,
            external_order_number="ORD-9003",
            status="processing",
            customer_info={"phone": tenant_ctx.phone},
            line_items=[_GENERIC_ITEM],
        )
        result, reply, _ctx_obj = asyncio.run(
            _run_track(db, tenant_ctx, "وين طلبي؟"),
        )
        assert result.success is True
        assert not result.data.get("tracking_number")
        assert not result.data.get("tracking_url")
        assert "رقم التتبع" not in reply
        assert "رابط التتبع" not in reply


class TestTrackOrderPrivacyAndSelection:
    def test_other_customer_order_not_exposed(self, db, tenant_ctx) -> None:
        other_tenant = seed_tenant(db, name="متجر آخر")
        other_customer = seed_customer(db, other_tenant.id, phone=OTHER_PHONE, name="نورة عبدالله")
        other_order = seed_order(
            db,
            other_tenant.id,
            external_order_number="ORD-PRIVATE",
            status="shipped",
            customer_info={"phone": OTHER_PHONE},
            line_items=[_GENERIC_ITEM],
        )
        seed_shipment(
            db,
            other_tenant.id,
            other_order.id,
            tracking_number="SF-PRIVATE-ONLY",
        )

        own_order = seed_order(
            db,
            tenant_ctx.tenant_id,
            external_order_number="ORD-MINE",
            status="shipped",
            customer_info={"phone": tenant_ctx.phone},
            line_items=[_GENERIC_ITEM],
        )
        seed_shipment(
            db,
            tenant_ctx.tenant_id,
            own_order.id,
            tracking_number="1Z999AA10123456784",
        )

        result, reply, _ctx_obj = asyncio.run(
            _run_track(
                db,
                tenant_ctx,
                "طلبي رقم ORD-PRIVATE",
                slots={"order_id": "ORD-PRIVATE"},
            ),
        )
        assert result.success is False
        assert "SF-PRIVATE-ONLY" not in (reply or "")
        assert "1Z999AA10123456784" not in (reply or "")

    def test_multi_order_returns_selected_order_tracking(self, db, tenant_ctx) -> None:
        first = seed_order(
            db,
            tenant_ctx.tenant_id,
            external_order_number="ORD-A-100",
            status="shipped",
            customer_info={"phone": tenant_ctx.phone},
            line_items=[_GENERIC_ITEM],
        )
        second = seed_order(
            db,
            tenant_ctx.tenant_id,
            external_order_number="ORD-B-200",
            status="shipped",
            customer_info={"phone": tenant_ctx.phone},
            line_items=[_GENERIC_ITEM],
        )
        seed_shipment(
            db,
            tenant_ctx.tenant_id,
            first.id,
            tracking_number="1Z999AA10123456784",
        )
        seed_shipment(
            db,
            tenant_ctx.tenant_id,
            second.id,
            tracking_number="SF123456789CN",
        )

        result, reply, _ctx_obj = asyncio.run(
            _run_track(
                db,
                tenant_ctx,
                "طلبي رقم ORD-B-200",
                slots={"order_id": "ORD-B-200"},
            ),
        )
        assert result.success is True
        assert result.data.get("reference") == "ORD-B-200"
        assert "SF123456789CN" in reply
        assert "1Z999AA10123456784" not in reply


class TestTrackingContextFollowUp:
    def test_follow_up_sees_structured_tracking_after_first_track(
        self, db, tenant_ctx,
    ) -> None:
        order = seed_order(
            db,
            tenant_ctx.tenant_id,
            external_order_number="ORD-FOLLOW",
            status="shipped",
            customer_info={"phone": tenant_ctx.phone},
            line_items=[_GENERIC_ITEM],
        )
        seed_shipment(
            db,
            tenant_ctx.tenant_id,
            order.id,
            tracking_number="1Z999AA10123456784",
        )

        _result, _reply, ctx = asyncio.run(
            _run_track(db, tenant_ctx, "وين طلبي؟"),
        )
        bundle = getattr(ctx, "commerce_bundle", None) or {}
        assert tracking_available_from_bundle(bundle) is True
        assert bundle["active_order_context"]["tracking_number"] == "1Z999AA10123456784"

        follow_up = prepare_tracking_follow_up_decision(ctx)
        assert follow_up["tracking_available"] is True
        assert follow_up.get("order_reference")


class TestActiveOrderContextHelpers:
    def test_tracking_available_from_number_without_url(self) -> None:
        bundle = merge_track_evidence_into_bundle(
            {},
            order_id="42",
            status="shipped",
            tracking_number="SF123456789CN",
        )
        assert tracking_available_from_bundle(bundle) is True

    def test_order_status_template_renders_tracking_fields_only_when_present(self) -> None:
        with_tracking = T.order_status(
            reference="ORD-1",
            status="shipped",
            status_label_ar="تم الشحن",
            tracking_number="1Z999AA10123456784",
            tracking_url="https://track.example/pkg",
            carrier="smsa",
        )
        assert "1Z999AA10123456784" in with_tracking
        assert "https://track.example/pkg" in with_tracking
        assert "smsa" in with_tracking

        without_tracking = T.order_status(
            reference="ORD-2",
            status="processing",
            status_label_ar="جاري المعالجة",
        )
        assert "رقم التتبع" not in without_tracking
        assert "رابط التتبع" not in without_tracking


class TestShipmentTruthGuardPreservesGroundedTracking:
    def test_guard_allows_grounded_tracking_number_claim(self) -> None:
        bundle = merge_track_evidence_into_bundle(
            {},
            order_id="55",
            status="shipped",
            tracking_number="SF123456789CN",
            shipping_status="shipped",
        )
        llm_reply = "تم الشحن، رقم التتبع SF123456789CN"
        result = apply_shipment_truth_guard(
            reply=llm_reply,
            commerce_bundle=bundle,
        )
        assert result.action == "allowed"
        assert "SF123456789CN" in result.reply
