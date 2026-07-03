"""Phase 1 — Customer Commerce Ledger (local orders, all sources)."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.customer_commerce_answerer import (  # noqa: E402
    TOPIC_LATEST_ORDER_SUMMARY,
    TOPIC_ORDER_HISTORY_COUNT,
    render_customer_commerce_reply,
    resolve_customer_commerce_reply,
)
from core.customer_commerce_ledger import (  # noqa: E402
    resolve_customer_commerce_profile,
)
from modules.ai.brain.decision.actions import ACTION_CUSTOMER_LEDGER_REPLY  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    INTENT_LATEST_ORDER_SUMMARY,
    INTENT_ORDER_HISTORY_COUNT,
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
GENERIC_PRODUCT = "حذاء رياضي أبيض"


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


def _profile(db, ctx, **kwargs):
    return resolve_customer_commerce_profile(
        db,
        tenant_id=ctx.tenant_id,
        customer_id=ctx.customer_id,
        phone=ctx.phone,
        **kwargs,
    )


class TestIntentRules:
    @pytest.mark.parametrize(
        "message,expected",
        (
            ("طلباتي السابقة كم؟", INTENT_ORDER_HISTORY_COUNT),
            ("كم طلب لي؟", INTENT_ORDER_HISTORY_COUNT),
            ("عندي طلبات سابقة؟", INTENT_ORDER_HISTORY_COUNT),
            ("وش آخر طلباتي؟", INTENT_LATEST_ORDER_SUMMARY),
            ("آخر طلب لي وش هو؟", INTENT_LATEST_ORDER_SUMMARY),
        ),
    )
    def test_history_intents_detected(self, message: str, expected: str) -> None:
        matched = rules.match(message)
        assert matched is not None
        assert matched.name == expected


class TestLedgerCounts:
    def test_order_history_count_with_mixed_sources(self, db, tenant_ctx) -> None:
        specs = [
            ("whatsapp", "wa-1", "NHL-101", "pending_payment"),
            ("salla", "sal-1", "SAL-201", "processing"),
            ("shopify", "shp-1", "SHP-301", "paid"),
            ("zid", "zid-1", "ZID-401", "shipped"),
            ("manual", "man-1", "MAN-501", "delivered"),
        ]
        for source, ext_id, ref, status in specs:
            seed_order(
                db,
                tenant_ctx.tenant_id,
                source=source,
                external_id=ext_id,
                external_order_number=ref,
                status=status,
                customer_info={"phone": tenant_ctx.phone},
                line_items=[{"title": GENERIC_PRODUCT, "quantity": 1}],
            )

        profile = _profile(db, tenant_ctx)
        assert profile.order_counts.total_orders == 5
        assert profile.order_counts.open_orders >= 1
        assert profile.latest_order is not None
        assert profile.latest_order.display_reference == "MAN-501"
        assert profile.sources.get("whatsapp") == 1
        assert profile.sources.get("salla") == 1
        assert profile.sources.get("shopify") == 1
        assert profile.sources.get("zid") == 1
        assert profile.sources.get("manual") == 1

        reply = render_customer_commerce_reply(TOPIC_ORDER_HISTORY_COUNT, profile)
        assert "5 طلبات مسجلة" in reply
        assert "MAN-501" in reply
        assert "ما عندي معلومات" not in reply
        assert "دفعت" not in reply

    def test_latest_order_summary_arabic_status(self, db, tenant_ctx) -> None:
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            external_id="ord-latest",
            external_order_number="269866315",
            status="payment_pending",
            customer_info={"phone": tenant_ctx.phone},
        )
        profile = _profile(db, tenant_ctx)
        reply = render_customer_commerce_reply(TOPIC_LATEST_ORDER_SUMMARY, profile)
        assert "269866315" in reply
        assert "قيد إكمال الدفع" in reply
        assert "payment_pending" not in reply

    def test_cancelled_counted_separately_not_open(self, db, tenant_ctx) -> None:
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            external_id="open-1",
            external_order_number="OPEN-1",
            status="processing",
            customer_info={"phone": tenant_ctx.phone},
        )
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="salla",
            external_id="cancel-1",
            external_order_number="CXL-9",
            status="cancelled",
            customer_info={"phone": tenant_ctx.phone},
        )
        profile = _profile(db, tenant_ctx)
        assert profile.order_counts.total_orders == 2
        assert profile.order_counts.cancelled_orders == 1
        assert profile.order_counts.open_orders == 1

    def test_abandoned_excluded_from_total_by_default(self, db, tenant_ctx) -> None:
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="salla",
            external_id="ok-1",
            external_order_number="OK-1",
            status="paid",
            customer_info={"phone": tenant_ctx.phone},
        )
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="salla",
            external_id="ab-1",
            external_order_number="AB-1",
            status="abandoned",
            customer_info={"phone": tenant_ctx.phone},
            extra_metadata={"is_abandoned": True},
        )
        profile = _profile(db, tenant_ctx, include_abandoned=False)
        assert profile.order_counts.total_orders == 1
        assert profile.order_counts.abandoned_carts == 1

        profile_inc = _profile(db, tenant_ctx, include_abandoned=True)
        assert profile_inc.order_counts.total_orders == 2
        assert profile_inc.order_counts.abandoned_carts == 1

    def test_no_orders_honest_reply(self, db, tenant_ctx) -> None:
        profile = _profile(db, tenant_ctx)
        assert profile.order_counts.total_orders == 0
        reply = resolve_customer_commerce_reply(
            db,
            topic=TOPIC_ORDER_HISTORY_COUNT,
            tenant_id=tenant_ctx.tenant_id,
            phone=tenant_ctx.phone,
        )
        assert reply == "ما ظهر لي طلبات مسجلة على هذا الرقم."

    def test_unknown_status_honest_fallback(self, db, tenant_ctx) -> None:
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            external_id="unk-1",
            external_order_number="UNK-77",
            status="mystery_status_xyz",
            customer_info={"phone": tenant_ctx.phone},
        )
        profile = _profile(db, tenant_ctx)
        reply = render_customer_commerce_reply(TOPIC_LATEST_ORDER_SUMMARY, profile)
        assert "UNK-77" in reply
        assert "mystery_status_xyz" in reply

    def test_tenant_isolation(self, db, tenant_ctx) -> None:
        other = seed_tenant(db, name="متجر آخر")
        seed_order(
            db,
            other.id,
            source="manual",
            external_id="other-1",
            external_order_number="OTHER-9",
            status="paid",
            customer_info={"phone": tenant_ctx.phone},
        )
        profile = _profile(db, tenant_ctx)
        assert profile.order_counts.total_orders == 0

    @pytest.mark.parametrize("phone_variant", ("966555000111", "+966555000111", "0555000111"))
    def test_phone_normalization_variants(self, db, tenant_ctx, phone_variant: str) -> None:
        customer = seed_customer(
            db,
            tenant_ctx.tenant_id,
            phone=phone_variant,
            name=GENERIC_CUSTOMER,
        )
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            external_id="ph-1",
            external_order_number="PH-100",
            status="paid",
            customer_info={"phone": phone_variant},
        )
        profile = resolve_customer_commerce_profile(
            db,
            tenant_id=tenant_ctx.tenant_id,
            customer_id=customer.id,
            phone=phone_variant,
        )
        assert profile.order_counts.total_orders == 1


class TestDecisionWiring:
    def test_decision_engine_routes_to_customer_ledger(self) -> None:
        intent = rules.match("طلباتي السابقة كم؟")
        assert intent is not None
        ctx = BrainContext(
            tenant_id=99,
            customer_phone=DEFAULT_PHONE_E164,
            message="طلباتي السابقة كم؟",
            intent=intent,
            state=MerchantConversationState(),
            facts=CommerceFacts(store_name=GENERIC_MERCHANT),
        )
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_CUSTOMER_LEDGER_REPLY
        assert decision.args.get("ledger_topic") == INTENT_ORDER_HISTORY_COUNT

    def test_evidence_quality_phase1_no_payment_totals(self, db, tenant_ctx) -> None:
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="salla",
            external_id="paid-1",
            external_order_number="PAID-1",
            status="paid",
            customer_info={"phone": tenant_ctx.phone},
            extra_metadata={"payment_status": "paid"},
        )
        profile = _profile(db, tenant_ctx)
        assert profile.evidence_quality.payment_totals_verified is False
