"""Customer-facing Arabic order status labels (P1.2)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.local_order_resolver import (  # noqa: E402
    local_order_to_track_payload,
    LocalOrderSnapshot,
)
from core.order_status_label import order_status_label_ar  # noqa: E402
from core.order_status_dedup_reply import build_dedup_local_order_short_reply  # noqa: E402
from modules.ai.brain.compose import templates as T  # noqa: E402
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_order,
    seed_tenant,
)

_GENERIC_ITEM = {
    "product_id": "sku-shirt-blue",
    "product_name": "قميص قطني أزرق",
    "quantity": 1,
    "unit_price": 149.0,
}


class TestOrderStatusLabelAr:
    def test_payment_pending_arabic(self) -> None:
        assert order_status_label_ar("payment_pending") == "قيد إكمال الدفع"

    def test_under_review_arabic(self) -> None:
        assert order_status_label_ar("under_review") == "قيد المراجعة"

    def test_delivered_arabic(self) -> None:
        assert order_status_label_ar("delivered") == "تم التسليم"

    def test_cancelled_arabic(self) -> None:
        assert order_status_label_ar("cancelled") == "ملغي"

    def test_abandoned_incomplete_not_open_order(self) -> None:
        label = order_status_label_ar("abandoned")
        assert "غير مكتمل" in label
        assert label != "قيد التنفيذ"

    def test_unknown_status_not_invented(self) -> None:
        assert order_status_label_ar("weird_platform_slug_xyz") == (
            "حالة الطلب الحالية: weird_platform_slug_xyz"
        )

    def test_empty_status_unclear(self) -> None:
        assert order_status_label_ar("") == "حالة الطلب الحالية غير واضحة"

    @pytest.mark.parametrize("source", ["whatsapp", "salla", "shopify", "zid", "manual"])
    def test_generic_sources_share_slug_labels(self, source: str) -> None:
        assert order_status_label_ar("payment_pending", source=source) == "قيد إكمال الدفع"


class TestTrackOrderTemplateArabic:
    def test_full_template_payment_pending_not_raw_slug(self) -> None:
        body = T.order_status(
            reference="ORD-1001",
            status="payment_pending",
            total=741.0,
            currency="SAR",
        )
        assert "payment_pending" not in body
        assert "قيد إكمال الدفع" in body
        assert "ORD-1001" in body

    def test_full_template_under_review(self) -> None:
        body = T.order_status(reference="ORD-2", status="under_review", total=0)
        assert "under_review" not in body
        assert "قيد المراجعة" in body


class TestLocalOrderTrackPayloadStatusLabel:
    def test_payload_includes_arabic_status_label(self) -> None:
        snap = LocalOrderSnapshot(
            order_id=1,
            external_id="ext-1",
            external_order_number="GEN-9001",
            status="delivered",
            source="manual",
            total="100",
            customer_name="أحمد",
            line_items=[],
        )
        payload = local_order_to_track_payload(snap)
        assert payload["status_label_ar"] == "تم التسليم"
        assert payload["status"] == "delivered"


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


@pytest.fixture()
def tenant_ctx(db):
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(db, tenant.id, name="نورة عبدالله")
    conv = seed_conversation(db, tenant.id, customer_id=customer.id)
    return SimpleNamespace(
        tenant_id=tenant.id,
        customer_id=customer.id,
        conversation_id=conv.id,
        phone=DEFAULT_PHONE_E164,
    )


class TestDedupShortReplyStillWorks:
    def test_p1_1_short_reply_uses_unified_label(self, db, tenant_ctx) -> None:
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            external_id="manual-1",
            external_order_number="ORD-DEDUP-1",
            status="payment_pending",
            customer_info={"phone": tenant_ctx.phone},
            line_items=[_GENERIC_ITEM],
        )
        reply = build_dedup_local_order_short_reply(
            db,
            tenant_id=tenant_ctx.tenant_id,
            phone=tenant_ctx.phone,
            conversation_id=tenant_ctx.conversation_id,
            inbound_text="وين طلبي",
            previous_outbound="طلبك رقم ORD-DEDUP-1 مسجّل عندنا.",
        )
        assert reply
        assert "قيد إكمال الدفع" in reply
        assert "payment_pending" not in reply
