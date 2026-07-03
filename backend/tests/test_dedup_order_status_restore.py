"""Hard dedup must not silence repeated order-status turns with local evidence."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.order_status_dedup_reply import (  # noqa: E402
    build_dedup_local_order_short_reply,
)
from modules.ai.brain.commerce.dedup_operational_delta import (  # noqa: E402
    is_local_order_status_inquiry,
    should_restore_brain_reply_after_dedup_silence,
)
from routers.whatsapp_webhook import _max_outbound_overlap  # noqa: E402
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_order,
    seed_tenant,
)

_GENERIC_ITEM = {
    "product_id": "sku-shoe-white",
    "product_name": "حذاء رياضي أبيض",
    "quantity": 1,
    "unit_price": 249.0,
}

_OPTION_A_REF = "269866315"
_LONG_TRACK_TEMPLATE = (
    f"طلبك رقم {_OPTION_A_REF} مسجّل عندنا.\n"
    "حالته الحالية: قيد إكمال الدفع.\n"
    "بمجرد اكتمال الدفع نكمل معالجة الطلب."
)
_SOCIAL_ONLY = "صباح النور! 👋 🌿"


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


def _seed_local_order(
    db,
    tenant_ctx,
    *,
    external_order_number: str = "ORD-GEN-1001",
    status: str = "processing",
    source: str = "manual",
) -> None:
    seed_order(
        db,
        tenant_ctx.tenant_id,
        source=source,
        external_id=f"{source}-local-1",
        external_order_number=external_order_number,
        status=status,
        customer_info={"phone": tenant_ctx.phone},
        line_items=[_GENERIC_ITEM],
    )


def _short_reply(
    db,
    tenant_ctx,
    inbound: str,
    *,
    previous_outbound: str = _LONG_TRACK_TEMPLATE,
) -> str:
    return build_dedup_local_order_short_reply(
        db,
        tenant_id=tenant_ctx.tenant_id,
        phone=tenant_ctx.phone,
        conversation_id=tenant_ctx.conversation_id,
        inbound_text=inbound,
        previous_outbound=previous_outbound,
    )


class TestLocalOrderStatusInquiryDetection:
    def test_track_and_order_number_phrases_detected(self) -> None:
        assert is_local_order_status_inquiry("وين طلبي")
        assert is_local_order_status_inquiry("كم رقم الطلب")
        assert is_local_order_status_inquiry(f"كم رقم الطلب {_OPTION_A_REF}")

    def test_social_greeting_not_order_inquiry(self) -> None:
        assert not is_local_order_status_inquiry("صباح الخير")
        assert not is_local_order_status_inquiry("شكراً")


class TestDedupLocalOrderShortReply:
    def test_repeated_track_order_with_local_evidence_not_empty(
        self, db, tenant_ctx,
    ) -> None:
        _seed_local_order(db, tenant_ctx, external_order_number="ORD-TRK-501")
        reply = _short_reply(db, tenant_ctx, "وين طلبي")
        assert reply
        assert "ORD-TRK-501" in reply
        assert "تتبع" not in reply.lower()

    def test_repeated_wain_talbi_returns_shorter_non_duplicate_alt(
        self, db, tenant_ctx,
    ) -> None:
        _seed_local_order(db, tenant_ctx, external_order_number="ORD-TRK-502")
        reply = _short_reply(db, tenant_ctx, "وين طلبي")
        assert reply
        assert reply != _LONG_TRACK_TEMPLATE
        assert _max_outbound_overlap(reply, [{"direction": "outbound", "body": _LONG_TRACK_TEMPLATE}]) < 0.85
        assert "نفس الطلب" in reply or "طلبك هو" in reply

    def test_repeated_order_number_with_explicit_ref(
        self, db, tenant_ctx,
    ) -> None:
        _seed_local_order(
            db,
            tenant_ctx,
            external_order_number=_OPTION_A_REF,
            status="payment_pending",
            source="manual",
        )
        reply = _short_reply(
            db,
            tenant_ctx,
            f"كم رقم الطلب {_OPTION_A_REF}",
        )
        assert reply
        assert _OPTION_A_REF in reply
        assert "رقم طلبك" in reply
        assert "قيد إكمال الدفع" in reply

    def test_no_local_evidence_returns_empty_for_hard_dedup_path(
        self, db, tenant_ctx,
    ) -> None:
        assert _short_reply(db, tenant_ctx, "وين طلبي") == ""

    def test_social_duplicate_still_not_restored(self) -> None:
        assert not should_restore_brain_reply_after_dedup_silence(
            current_inbound="صباح الخير",
            candidate_reply="صباح النور! 👋",
            previous_outbound=_SOCIAL_ONLY,
        )
        assert not is_local_order_status_inquiry("صباح الخير")

    def test_no_fake_tracking_number_in_short_reply(
        self, db, tenant_ctx,
    ) -> None:
        _seed_local_order(
            db,
            tenant_ctx,
            external_order_number="ORD-NO-TRK-1",
            status="processing",
        )
        reply = _short_reply(db, tenant_ctx, "وين طلبي")
        assert reply
        assert "رقم التتبع" not in reply
        assert "tracking" not in reply.lower()

    def test_generic_merchant_manual_source_not_salla_only(
        self, db, tenant_ctx,
    ) -> None:
        _seed_local_order(
            db,
            tenant_ctx,
            external_order_number="GEN-MAN-9001",
            status="under_review",
            source="manual",
        )
        reply = _short_reply(db, tenant_ctx, "وين طلبي")
        assert reply
        assert "GEN-MAN-9001" in reply
        assert "تحت المراجعة" in reply


class TestOptionARegression:
    """Option A msgs 39628–39630 must not end reply_len=0 when local order exists."""

    @pytest.mark.parametrize(
        "inbound",
        [
            "وين طلبي",
            f"كم رقم الطلب {_OPTION_A_REF}",
            f"وين طلبي رقم {_OPTION_A_REF}",
        ],
    )
    def test_option_a_followups_produce_non_empty_short_reply(
        self, db, tenant_ctx, inbound: str,
    ) -> None:
        _seed_local_order(
            db,
            tenant_ctx,
            external_order_number=_OPTION_A_REF,
            status="payment_pending",
            source="manual",
        )
        reply = _short_reply(
            db,
            tenant_ctx,
            inbound,
            previous_outbound=_LONG_TRACK_TEMPLATE,
        )
        assert reply
        assert len(reply) > 0
        assert _OPTION_A_REF in reply
        assert "قيد إكمال الدفع" in reply
