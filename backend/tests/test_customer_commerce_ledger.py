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
    TOPIC_ORDER_REFERENCE_LIST,
    _NO_ORDERS_REPLY,
    render_customer_commerce_reply,
    render_order_reference_list_reply,
    resolve_customer_commerce_reply,
)
from core.customer_commerce_ledger import (  # noqa: E402
    list_recent_order_snapshots,
    resolve_customer_commerce_profile,
)
from core.local_order_resolver import (  # noqa: E402
    _fetch_tenant_orders_for_customer,
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


class TestOrderReferenceList:
    """Issue #709 — multi-reference listing (DATA + ANSWER layer)."""

    LIST_PHONE = "966555906901"
    LIST_PHONE_E164 = "+966555906901"

    def test_matched_row_without_id_still_returned(self) -> None:
        """Regression: dedup must not drop matched rows with falsy id."""
        row = SimpleNamespace(
            id=None,
            customer_id=None,
            customer_info={"phone": self.LIST_PHONE_E164},
            extra_metadata={},
        )

        class _FakeQuery:
            def filter(self, *_args, **_kwargs):
                return self

            def order_by(self, *_args, **_kwargs):
                return self

            def limit(self, *_args, **_kwargs):
                return self

            def all(self):
                return [row]

        class _FakeDb:
            def query(self, _model):
                return _FakeQuery()

        matched = _fetch_tenant_orders_for_customer(
            _FakeDb(),
            tenant_id=1,
            phone=self.LIST_PHONE_E164,
            customer_id=None,
        )
        assert matched == [row]

    @pytest.fixture()
    def list_ctx(self, db):
        tenant = seed_tenant(db, name=GENERIC_MERCHANT)
        customer = seed_customer(
            db,
            tenant.id,
            phone=self.LIST_PHONE_E164,
            name=GENERIC_CUSTOMER,
        )
        conv = seed_conversation(db, tenant.id, customer_id=customer.id)
        return SimpleNamespace(
            tenant_id=tenant.id,
            customer_id=customer.id,
            conversation_id=conv.id,
            phone=self.LIST_PHONE_E164,
        )

    def _list_snapshots(self, db, ctx, **kwargs):
        return list_recent_order_snapshots(
            db,
            tenant_id=ctx.tenant_id,
            customer_id=ctx.customer_id,
            phone=ctx.phone,
            **kwargs,
        )

    def _list_reply(self, db, ctx, **kwargs):
        profile = resolve_customer_commerce_profile(
            db,
            tenant_id=ctx.tenant_id,
            customer_id=ctx.customer_id,
            phone=ctx.phone,
            **{k: v for k, v in kwargs.items() if k in ("include_abandoned", "include_cancelled")},
        )
        snapshots = self._list_snapshots(db, ctx, **kwargs)
        return render_order_reference_list_reply(profile, snapshots, **kwargs)

    def test_fk_linked_customer_orders_listed(self, db, list_ctx) -> None:
        order = seed_order(
            db,
            list_ctx.tenant_id,
            source="manual",
            external_id="fk-ext-1",
            external_order_number="GEN-FK-101",
            status="processing",
            customer_info={},
            line_items=[{"title": GENERIC_PRODUCT, "quantity": 1}],
        )
        order.customer_id = list_ctx.customer_id
        db.commit()

        snapshots = self._list_snapshots(db, list_ctx)
        assert len(snapshots) == 1
        assert snapshots[0].display_reference == "GEN-FK-101"

    def test_phone_matched_with_null_customer_id_fk(self, db, list_ctx) -> None:
        """Tenant-1 shape: phone in customer_info, orders.customer_id NULL."""
        seed_order(
            db,
            list_ctx.tenant_id,
            source="salla",
            external_id="sal-null-fk",
            external_order_number="GEN-PH-201",
            status="paid",
            customer_info={"phone": list_ctx.phone},
            line_items=[{"title": "قميص قطني أزرق", "quantity": 1}],
        )
        snapshots = self._list_snapshots(db, list_ctx)
        assert len(snapshots) == 1
        assert snapshots[0].display_reference == "GEN-PH-201"

    def test_cross_tenant_isolation_same_phone(self, db, list_ctx) -> None:
        other = seed_tenant(db, name="متجر آخر")
        seed_order(
            db,
            other.id,
            source="manual",
            external_id="other-tenant-ref",
            external_order_number="OTHER-TENANT-99",
            status="paid",
            customer_info={"phone": list_ctx.phone},
        )
        seed_order(
            db,
            list_ctx.tenant_id,
            source="manual",
            external_id="own-tenant-ref",
            external_order_number="OWN-TENANT-11",
            status="paid",
            customer_info={"phone": list_ctx.phone},
        )
        snapshots = self._list_snapshots(db, list_ctx)
        refs = {s.display_reference for s in snapshots}
        assert "OWN-TENANT-11" in refs
        assert "OTHER-TENANT-99" not in refs

    def test_zero_orders_uses_existing_no_orders_reply(self, db, list_ctx) -> None:
        profile = resolve_customer_commerce_profile(
            db,
            tenant_id=list_ctx.tenant_id,
            customer_id=list_ctx.customer_id,
            phone=list_ctx.phone,
        )
        assert profile.order_counts.total_orders == 0
        reply = resolve_customer_commerce_reply(
            db,
            topic=TOPIC_ORDER_REFERENCE_LIST,
            tenant_id=list_ctx.tenant_id,
            customer_id=list_ctx.customer_id,
            phone=list_ctx.phone,
        )
        assert reply == _NO_ORDERS_REPLY
        assert "ما أقدر" not in reply
        assert "لا أستطيع" not in reply

    def test_more_than_five_orders_capped_newest_first(self, db, list_ctx) -> None:
        refs = [f"GEN-CAP-{i:03d}" for i in range(1, 8)]
        for idx, ref in enumerate(refs, start=1):
            seed_order(
                db,
                list_ctx.tenant_id,
                source="manual",
                external_id=f"cap-ext-{idx}",
                external_order_number=ref,
                status="paid",
                customer_info={"phone": list_ctx.phone},
            )
        snapshots = self._list_snapshots(db, list_ctx, limit=5)
        assert len(snapshots) == 5
        assert [s.display_reference for s in snapshots] == list(reversed(refs[-5:]))

        reply = self._list_reply(db, list_ctx, limit=5)
        for ref in refs[-5:]:
            assert ref in reply
        assert refs[0] not in reply
        assert refs[1] not in reply

    def test_display_reference_only_no_internal_ids(self, db, list_ctx) -> None:
        # Letter-only refs (no digits) so auto-increment ids cannot be substrings.
        internal_ids: list[int] = []
        for ref in ("GEN-SAFE-AAA", "GEN-SAFE-BBB"):
            order = seed_order(
                db,
                list_ctx.tenant_id,
                source="manual",
                external_id=f"safe-ext-{ref}",
                external_order_number=ref,
                status="processing",
                customer_info={"phone": list_ctx.phone},
            )
            internal_ids.append(int(order.id))

        snapshots = self._list_snapshots(db, list_ctx)
        reply = self._list_reply(db, list_ctx)
        for snap in snapshots:
            assert snap.display_reference in reply
        for oid in internal_ids:
            assert str(oid) not in reply

    @pytest.mark.parametrize(
        "phone_variant",
        ("0555906901", "966555906901", "+966555906901"),
    )
    def test_phone_format_variants_match_same_order(
        self, db, list_ctx, phone_variant: str,
    ) -> None:
        seed_order(
            db,
            list_ctx.tenant_id,
            source="salla",
            external_id="var-ext-1",
            external_order_number="GEN-VAR-301",
            status="paid",
            customer_info={"phone": self.LIST_PHONE_E164},
        )
        snapshots = list_recent_order_snapshots(
            db,
            tenant_id=list_ctx.tenant_id,
            customer_id=list_ctx.customer_id,
            phone=phone_variant,
        )
        assert len(snapshots) == 1
        assert snapshots[0].display_reference == "GEN-VAR-301"

    def test_orders_without_display_reference_honest_reply(self, db, list_ctx) -> None:
        seed_order(
            db,
            list_ctx.tenant_id,
            source="salla",
            external_id="",
            external_order_number="",
            status="paid",
            customer_info={"phone": list_ctx.phone},
        )
        profile = resolve_customer_commerce_profile(
            db,
            tenant_id=list_ctx.tenant_id,
            customer_id=list_ctx.customer_id,
            phone=list_ctx.phone,
        )
        snapshots = self._list_snapshots(db, list_ctx)
        reply = render_order_reference_list_reply(profile, snapshots)
        assert profile.order_counts.total_orders == 1
        assert "أرقام مرجعية" in reply
        assert reply != _NO_ORDERS_REPLY

    def test_resolve_customer_commerce_reply_dispatches_reference_list(
        self, db, list_ctx,
    ) -> None:
        seed_order(
            db,
            list_ctx.tenant_id,
            source="manual",
            external_id="dispatch-ext",
            external_order_number="GEN-DSP-401",
            status="paid",
            customer_info={"phone": list_ctx.phone},
        )
        reply = resolve_customer_commerce_reply(
            db,
            topic=TOPIC_ORDER_REFERENCE_LIST,
            tenant_id=list_ctx.tenant_id,
            customer_id=list_ctx.customer_id,
            phone=list_ctx.phone,
        )
        assert "GEN-DSP-401" in reply
        assert render_customer_commerce_reply(
            TOPIC_ORDER_REFERENCE_LIST,
            resolve_customer_commerce_profile(
                db,
                tenant_id=list_ctx.tenant_id,
                customer_id=list_ctx.customer_id,
                phone=list_ctx.phone,
            ),
            snapshots=self._list_snapshots(db, list_ctx),
        ) == reply
