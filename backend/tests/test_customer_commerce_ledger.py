"""Phase 1 — Customer Commerce Ledger (local orders, all sources)."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
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
    _ledger_phone_sql_keys,
    list_recent_order_snapshots,
    resolve_customer_commerce_profile,
)
from models import Order  # noqa: E402
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
OTHER_CUSTOMER_PHONE = "+966500000099"


def _bulk_insert_orders(
    db,
    tenant_id: int,
    *,
    count: int,
    phone: str,
    ref_prefix: str = "NOISE",
    status: str = "paid",
    customer_id: int | None = None,
    created_at_base: datetime | None = None,
) -> None:
    base = created_at_base or datetime.now(timezone.utc)
    mappings = [
        {
            "tenant_id": tenant_id,
            "external_id": f"{ref_prefix}-ext-{idx}",
            "external_order_number": f"{ref_prefix}-{idx:04d}",
            "status": status,
            "source": "manual",
            "customer_info": {"phone": phone},
            "line_items": [{"title": GENERIC_PRODUCT, "quantity": 1}],
            "metadata": {
                "created_at": (base + timedelta(seconds=idx)).isoformat(),
            },
            "customer_id": customer_id,
        }
        for idx in range(count)
    ]
    db.bulk_insert_mappings(Order, mappings)
    db.commit()


def _iso_at(base: datetime, **delta_kwargs) -> str:
    return (base + timedelta(**delta_kwargs)).isoformat()


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
        base = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
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
                extra_metadata={"created_at": _iso_at(base, hours=idx)},
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

    def test_sqlite_json_phone_accessor_finds_stored_phone(self, db, list_ctx) -> None:
        seed_order(
            db,
            list_ctx.tenant_id,
            source="manual",
            external_id="json-ext",
            external_order_number="GEN-JSON-501",
            status="paid",
            customer_info={"mobile": list_ctx.phone},
        )
        profile = resolve_customer_commerce_profile(
            db,
            tenant_id=list_ctx.tenant_id,
            customer_id=list_ctx.customer_id,
            phone=list_ctx.phone,
        )
        assert profile.order_counts.total_orders == 1
        assert profile.latest_order is not None
        assert profile.latest_order.display_reference == "GEN-JSON-501"

    def test_busy_tenant_target_orders_survive_two_hundred_noise_rows(
        self, db, list_ctx,
    ) -> None:
        noise_base = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        target_base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        started = time.perf_counter()

        _bulk_insert_orders(
            db,
            list_ctx.tenant_id,
            count=210,
            phone=OTHER_CUSTOMER_PHONE,
            ref_prefix="NOISE",
            created_at_base=noise_base,
        )
        target_refs = [f"GEN-TGT-{i:02d}" for i in range(1, 9)]
        for idx, ref in enumerate(target_refs, start=1):
            seed_order(
                db,
                list_ctx.tenant_id,
                source="manual",
                external_id=f"tgt-ext-{idx}",
                external_order_number=ref,
                status="paid",
                customer_info={"phone": list_ctx.phone},
                extra_metadata={"created_at": _iso_at(target_base, days=idx)},
            )

        other = seed_tenant(db, name="متجر آخر")
        seed_order(
            db,
            other.id,
            source="manual",
            external_id="leak-ext",
            external_order_number="LEAK-OTHER-TENANT",
            status="paid",
            customer_info={"phone": list_ctx.phone},
        )

        profile = resolve_customer_commerce_profile(
            db,
            tenant_id=list_ctx.tenant_id,
            customer_id=list_ctx.customer_id,
            phone=list_ctx.phone,
        )
        snapshots = self._list_snapshots(db, list_ctx, limit=5)
        elapsed = time.perf_counter() - started

        assert profile.order_counts.total_orders == 8
        assert profile.latest_order is not None
        assert profile.latest_order.display_reference == "GEN-TGT-08"
        assert [s.display_reference for s in snapshots] == list(reversed(target_refs[-5:]))
        assert all(ref.startswith("GEN-TGT-") for ref in (s.display_reference for s in snapshots))
        assert "LEAK-OTHER-TENANT" not in {s.display_reference for s in snapshots}
        assert "NOISE-" not in "".join(s.display_reference for s in snapshots)
        assert elapsed < 15.0, f"bulk scenario too slow: {elapsed:.2f}s"

    def test_identical_created_at_orders_by_id_tiebreaker(self, db, list_ctx) -> None:
        same_ts = _iso_at(datetime(2026, 3, 5, 9, 0, tzinfo=timezone.utc))
        first = seed_order(
            db,
            list_ctx.tenant_id,
            source="manual",
            external_id="tie-a",
            external_order_number="GEN-TIE-A",
            status="paid",
            customer_info={"phone": list_ctx.phone},
            extra_metadata={"created_at": same_ts},
        )
        second = seed_order(
            db,
            list_ctx.tenant_id,
            source="manual",
            external_id="tie-b",
            external_order_number="GEN-TIE-B",
            status="paid",
            customer_info={"phone": list_ctx.phone},
            extra_metadata={"created_at": same_ts},
        )
        snapshots = self._list_snapshots(db, list_ctx, limit=2)
        assert len(snapshots) == 2
        assert int(second.id) > int(first.id)
        assert snapshots[0].display_reference == "GEN-TIE-B"
        assert snapshots[1].display_reference == "GEN-TIE-A"

    def test_cancelled_visible_abandoned_hidden_in_reference_list(
        self, db, list_ctx,
    ) -> None:
        seed_order(
            db,
            list_ctx.tenant_id,
            source="manual",
            external_id="open-live",
            external_order_number="GEN-OPEN-1",
            status="processing",
            customer_info={"phone": list_ctx.phone},
            extra_metadata={"created_at": _iso_at(datetime(2026, 2, 1, tzinfo=timezone.utc))},
        )
        seed_order(
            db,
            list_ctx.tenant_id,
            source="salla",
            external_id="cancelled-live",
            external_order_number="GEN-CXL-2",
            status="cancelled",
            customer_info={"phone": list_ctx.phone},
            extra_metadata={"created_at": _iso_at(datetime(2026, 2, 2, tzinfo=timezone.utc))},
        )
        seed_order(
            db,
            list_ctx.tenant_id,
            source="salla",
            external_id="abandoned-hidden",
            external_order_number="GEN-AB-3",
            status="abandoned",
            customer_info={"phone": list_ctx.phone},
            extra_metadata={
                "created_at": _iso_at(datetime(2026, 2, 3, tzinfo=timezone.utc)),
                "is_abandoned": True,
            },
        )
        profile = resolve_customer_commerce_profile(
            db,
            tenant_id=list_ctx.tenant_id,
            customer_id=list_ctx.customer_id,
            phone=list_ctx.phone,
        )
        snapshots = self._list_snapshots(db, list_ctx)
        reply = self._list_reply(db, list_ctx)

        assert profile.order_counts.total_orders == 2
        assert profile.order_counts.abandoned_carts == 1
        assert profile.order_counts.cancelled_orders == 1
        refs = [s.display_reference for s in snapshots]
        assert "GEN-CXL-2" in refs
        assert "GEN-AB-3" not in refs
        assert "GEN-OPEN-1" in refs
        assert "ملغ" in reply or "ملغى" in reply or "ملغي" in reply
        assert "GEN-AB-3" not in reply

    @pytest.mark.parametrize(
        "phone_input,expected_subset",
        (
            ("0555906901", {"0555906901", "966555906901", "+966555906901"}),
            ("966555906901", {"966555906901", "+966555906901", "0555906901"}),
            ("+966555906901", {"+966555906901", "966555906901", "0555906901"}),
        ),
    )
    def test_ledger_phone_sql_keys_cover_mandated_formats(
        self, phone_input: str, expected_subset: set[str],
    ) -> None:
        keys = set(_ledger_phone_sql_keys(phone_input))
        assert expected_subset.issubset(keys)


class TestLedgerOrdering:
    """SQLite-backed ordering semantics (NULLs-last + tie-break)."""

    def test_null_metadata_date_sorts_last(self, db, tenant_ctx) -> None:
        base = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            external_id="dated-new",
            external_order_number="ORD-DATED-NEW",
            status="paid",
            customer_info={"phone": tenant_ctx.phone},
            extra_metadata={"created_at": _iso_at(base, days=2)},
        )
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            external_id="dated-old",
            external_order_number="ORD-DATED-OLD",
            status="paid",
            customer_info={"phone": tenant_ctx.phone},
            extra_metadata={"created_at": _iso_at(base, days=1)},
        )
        db.add(
            Order(
                tenant_id=tenant_ctx.tenant_id,
                external_id="null-date",
                external_order_number="ORD-NULL-DATE",
                status="paid",
                source="manual",
                customer_info={"phone": tenant_ctx.phone},
                line_items=[{"title": GENERIC_PRODUCT, "quantity": 1}],
                extra_metadata={"created_via": "test"},
            )
        )
        db.add(
            Order(
                tenant_id=tenant_ctx.tenant_id,
                external_id="empty-date",
                external_order_number="ORD-EMPTY-DATE",
                status="paid",
                source="manual",
                customer_info={"phone": tenant_ctx.phone},
                line_items=[{"title": GENERIC_PRODUCT, "quantity": 1}],
                extra_metadata={"created_at": None},
            )
        )
        db.commit()

        snapshots = list_recent_order_snapshots(
            db,
            tenant_id=tenant_ctx.tenant_id,
            customer_id=tenant_ctx.customer_id,
            phone=tenant_ctx.phone,
            limit=10,
        )
        refs = [s.display_reference for s in snapshots]
        assert refs[:2] == ["ORD-DATED-NEW", "ORD-DATED-OLD"]
        assert set(refs[2:]) == {"ORD-NULL-DATE", "ORD-EMPTY-DATE"}

        profile = resolve_customer_commerce_profile(
            db,
            tenant_id=tenant_ctx.tenant_id,
            customer_id=tenant_ctx.customer_id,
            phone=tenant_ctx.phone,
        )
        assert profile.latest_order is not None
        assert profile.latest_order.display_reference == "ORD-DATED-NEW"

    def test_identical_created_at_orders_by_id_tiebreaker_sqlite(
        self, db, tenant_ctx,
    ) -> None:
        same_ts = _iso_at(datetime(2026, 3, 5, 9, 0, tzinfo=timezone.utc))
        first = seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            external_id="tie-a",
            external_order_number="ORD-TIE-A",
            status="paid",
            customer_info={"phone": tenant_ctx.phone},
            extra_metadata={"created_at": same_ts},
        )
        second = seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            external_id="tie-b",
            external_order_number="ORD-TIE-B",
            status="paid",
            customer_info={"phone": tenant_ctx.phone},
            extra_metadata={"created_at": same_ts},
        )
        snapshots = list_recent_order_snapshots(
            db,
            tenant_id=tenant_ctx.tenant_id,
            customer_id=tenant_ctx.customer_id,
            phone=tenant_ctx.phone,
            limit=2,
        )
        assert int(second.id) > int(first.id)
        assert [s.display_reference for s in snapshots] == ["ORD-TIE-B", "ORD-TIE-A"]


try:
    from tests.order_customer_identity_postgres_fixtures import (  # noqa: E402
        pg_session,
        postgres_engine,
        seed_tenant as pg_seed_tenant,
    )

    _PG_FIXTURES_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PG_FIXTURES_AVAILABLE = False

_LEDGER_PG_TENANT = 990_150
_LEDGER_PG_PHONE = "+966500000777"


def _pg_seed_ledger_order(
    session,
    tenant_id: int,
    *,
    ref: str,
    created_at: str | None = None,
    phone: str = _LEDGER_PG_PHONE,
    include_created_at_key: bool = True,
) -> Order:
    if not include_created_at_key:
        meta: dict | None = {}
    else:
        meta = {"created_at": created_at}
    row = Order(
        tenant_id=int(tenant_id),
        external_id=f"pg-ext-{ref}",
        external_order_number=ref,
        status="paid",
        source="manual",
        customer_info={"phone": phone},
        line_items=[{"title": GENERIC_PRODUCT, "quantity": 1}],
        extra_metadata=meta or None,
    )
    session.add(row)
    session.flush()
    return row


@pytest.mark.skipif(not _PG_FIXTURES_AVAILABLE, reason="postgres fixtures unavailable")
@pytest.mark.usefixtures("postgres_engine")
class TestLedgerOrderingPostgres:
    """PostgreSQL proof for NULLs-last and true chronological ordering."""

    def test_null_metadata_date_sorts_last_pg(self, pg_session) -> None:
        pg_seed_tenant(pg_session, tenant_id=_LEDGER_PG_TENANT)
        _pg_seed_ledger_order(
            pg_session,
            _LEDGER_PG_TENANT,
            ref="PG-DATED-NEW",
            created_at="2026-05-10T12:00:00+03:00",
        )
        _pg_seed_ledger_order(
            pg_session,
            _LEDGER_PG_TENANT,
            ref="PG-DATED-OLD",
            created_at="2026-05-09T12:00:00+03:00",
        )
        _pg_seed_ledger_order(
            pg_session,
            _LEDGER_PG_TENANT,
            ref="PG-NULL-DATE",
            created_at=None,
        )
        _pg_seed_ledger_order(
            pg_session,
            _LEDGER_PG_TENANT,
            ref="PG-MISSING-KEY",
            include_created_at_key=False,
        )
        pg_session.flush()

        snapshots = list_recent_order_snapshots(
            pg_session,
            tenant_id=_LEDGER_PG_TENANT,
            phone=_LEDGER_PG_PHONE,
            limit=10,
        )
        refs = [s.display_reference for s in snapshots]
        assert refs[:2] == ["PG-DATED-NEW", "PG-DATED-OLD"]
        assert set(refs[2:]) == {"PG-NULL-DATE", "PG-MISSING-KEY"}

        profile = resolve_customer_commerce_profile(
            pg_session,
            tenant_id=_LEDGER_PG_TENANT,
            phone=_LEDGER_PG_PHONE,
        )
        assert profile.latest_order is not None
        assert profile.latest_order.display_reference == "PG-DATED-NEW"

    def test_mixed_iso_formats_order_chronologically_pg(self, pg_session) -> None:
        pg_seed_tenant(pg_session, tenant_id=_LEDGER_PG_TENANT)
        # Text DESC would rank +03:00 T10 above Z T08; chronology is the reverse.
        _pg_seed_ledger_order(
            pg_session,
            _LEDGER_PG_TENANT,
            ref="PG-OFF-OLDER",
            created_at="2026-07-27T10:00:00+03:00",  # 07:00 UTC
        )
        _pg_seed_ledger_order(
            pg_session,
            _LEDGER_PG_TENANT,
            ref="PG-Z-NEWER",
            created_at="2026-07-27T08:00:00Z",  # 08:00 UTC
        )
        _pg_seed_ledger_order(
            pg_session,
            _LEDGER_PG_TENANT,
            ref="PG-USEC-MID",
            created_at="2026-07-27T07:30:00.123456+03:00",  # 04:30 UTC
        )
        pg_session.flush()

        snapshots = list_recent_order_snapshots(
            pg_session,
            tenant_id=_LEDGER_PG_TENANT,
            phone=_LEDGER_PG_PHONE,
            limit=3,
        )
        assert [s.display_reference for s in snapshots] == [
            "PG-Z-NEWER",
            "PG-OFF-OLDER",
            "PG-USEC-MID",
        ]
