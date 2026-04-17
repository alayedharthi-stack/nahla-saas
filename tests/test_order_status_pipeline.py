"""
tests/test_order_status_pipeline.py
───────────────────────────────────
Regression coverage for the 2026-04-17 root-cause fix.

Bug
───
The Salla adapter stringified the status DICT
(`status=str(raw.get("status", "pending"))`), producing values like
``"{'id': 566146469, 'name': 'بإنتظار المراجعة', 'slug': 'under_review'}"``
in the DB. Downstream:

  • dashboard /orders mapped any non-whitelisted status to "cancelled"
    → every order looked like ملغي
  • customer intelligence still counted them but the unrecognised string
    interacted badly with status displays + segment narratives

These tests pin down the contract end-to-end:

  1. SallaAdapter._normalize_order extracts `status.slug`, never repr.
  2. SallaAdapter._normalize_order recovers totals from every plausible
     Salla shape (amounts.total dict, raw.total flat, etc).
  3. routers/orders._classify_status maps real Salla slugs to UI buckets
     and never silently falls into "cancelled".
  4. customer_intelligence.order_status_key heals legacy corrupted rows
     at READ time (so segmentation works before backfill runs).
  5. The admin backfill endpoint repairs corrupted rows + rebuilds
     customer profiles.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Customer, CustomerProfile, Order, Tenant  # noqa: E402
# Re-import via the path used by services/customer_intelligence.py so
# ``isinstance(order, Order)`` checks pass — under sys.path duplication
# the two import paths can otherwise yield distinct class objects.
from models import Order as _ModelsOrder  # noqa: E402,F401
from routers.orders import (  # noqa: E402
    PAID_STATUSES,
    PENDING_STATUSES,
    CANCELLED_STATUSES,
    FAILED_STATUSES,
    _classify_status,
    _parse_corrupt_status,
    _read_created_at,
    _to_float_sar,
)
from services.customer_intelligence import (  # noqa: E402
    CustomerIntelligenceService,
    is_countable_order,
    order_status_key,
)
from store_adapters.salla_adapter import SallaAdapter  # noqa: E402


# ── SQLite shim for JSONB columns ─────────────────────────────────────────
@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    tenant = Tenant(name="Test Tenant", is_active=True)
    session.add(tenant)
    session.commit()
    return session, tenant.id, engine


# ── A. Adapter status extraction ──────────────────────────────────────────

class TestSallaAdapterStatusExtraction:
    """Salla returns status as a dict — adapter MUST extract slug."""

    def setup_method(self):
        self.adapter = SallaAdapter(api_key="test", store_id="1", tenant_id=1)

    def test_dict_status_extracts_slug(self):
        raw = {
            "id": 566146469,
            "status": {
                "id": 566146469,
                "name": "بإنتظار المراجعة",
                "slug": "under_review",
                "customized": {"id": 566146469, "name": "بإنتظار المراجعة"},
            },
            "amounts": {"total": {"amount": 100, "currency": "SAR"}},
            "customer": {"name": "نشمي", "mobile": "+966512345678"},
            "items": [],
            "created_at": "2026-04-15T12:00:00+03:00",
        }
        normalized = self.adapter._normalize_order(raw, None)
        assert normalized.status == "under_review"
        assert normalized.total == 100.0

    def test_dict_status_falls_back_to_name_then_code(self):
        # Some custom Salla statuses ship without `slug`.
        raw = {
            "id": 1,
            "status": {"name": "Custom Stage", "id": 99},
            "amounts": {},
            "customer": {},
            "items": [],
        }
        normalized = self.adapter._normalize_order(raw, None)
        assert normalized.status == "Custom Stage"

    def test_string_status_passes_through(self):
        raw = {
            "id": 1,
            "status": "delivered",
            "amounts": {"total": {"amount": 50}},
            "customer": {},
            "items": [],
        }
        normalized = self.adapter._normalize_order(raw, None)
        assert normalized.status == "delivered"

    def test_missing_status_defaults_to_pending(self):
        raw = {"id": 1, "customer": {}, "items": []}
        normalized = self.adapter._normalize_order(raw, None)
        assert normalized.status == "pending"

    def test_status_never_starts_with_brace(self):
        """The exact regression: status must never be a Python dict repr."""
        raw = {
            "id": 1,
            "status": {"id": 1, "slug": "processing", "name": "x"},
            "customer": {},
            "items": [],
        }
        normalized = self.adapter._normalize_order(raw, None)
        assert not normalized.status.startswith("{")
        assert not normalized.status.startswith("'")


# ── B. Adapter total recovery ─────────────────────────────────────────────

class TestSallaAdapterTotalRecovery:
    def setup_method(self):
        self.adapter = SallaAdapter(api_key="test", store_id="1", tenant_id=1)

    def _base(self, **overrides):
        raw = {"id": 1, "status": "completed", "customer": {}, "items": []}
        raw.update(overrides)
        return raw

    def test_amounts_total_dict(self):
        raw = self._base(amounts={"total": {"amount": 250.5, "currency": "SAR"}})
        n = self.adapter._normalize_order(raw, None)
        assert n.total == 250.5
        assert n.currency == "SAR"

    def test_amounts_total_flat_number(self):
        raw = self._base(amounts={"total": 99})
        n = self.adapter._normalize_order(raw, None)
        assert n.total == 99.0

    def test_falls_back_to_raw_total_when_amounts_empty(self):
        raw = self._base(amounts={}, total=500)
        n = self.adapter._normalize_order(raw, None)
        assert n.total == 500.0

    def test_zero_amounts_dont_short_circuit(self):
        raw = self._base(amounts={"total": {"amount": 0}}, total=42)
        n = self.adapter._normalize_order(raw, None)
        assert n.total == 42.0

    def test_no_total_anywhere_returns_zero(self):
        raw = self._base(amounts={})
        n = self.adapter._normalize_order(raw, None)
        assert n.total == 0.0


# ── C. Dashboard status classifier ────────────────────────────────────────

class TestDashboardStatusClassifier:

    @pytest.mark.parametrize("slug", sorted(PAID_STATUSES))
    def test_paid_buckets(self, slug):
        assert _classify_status(slug) == "paid"

    @pytest.mark.parametrize("slug", sorted(PENDING_STATUSES))
    def test_pending_buckets(self, slug):
        assert _classify_status(slug) == "pending"

    @pytest.mark.parametrize("slug", sorted(FAILED_STATUSES))
    def test_failed_buckets(self, slug):
        assert _classify_status(slug) == "failed"

    @pytest.mark.parametrize("slug", sorted(CANCELLED_STATUSES))
    def test_cancelled_buckets(self, slug):
        assert _classify_status(slug) == "cancelled"

    def test_unknown_status_defaults_to_pending_not_cancelled(self):
        # The ROOT CAUSE — previously the dashboard mapped everything
        # unknown to "cancelled" (ملغي). It must now default to "pending"
        # so unrecognised merchant-customised slugs stay visible.
        assert _classify_status("merchant_custom_step_42") == "pending"
        assert _classify_status("brand_new_salla_status") == "pending"

    def test_real_salla_slugs_dont_become_cancelled(self):
        # Sanity check: every status the user sees in real Salla orders must
        # NOT show up as ملغي in the dashboard. This is the regression test.
        real_slugs = [
            "under_review",      # بإنتظار المراجعة
            "in_progress",       # قيد التنفيذ
            "preparing",         # قيد التحضير
            "ready_for_shipment",
            "shipped",           # تم الشحن
            "delivering",        # قيد التوصيل
            "delivered",         # تم التسليم
            "completed",         # مكتمل
            "restored",          # مستعاد
            "payment_pending",
        ]
        for slug in real_slugs:
            bucket = _classify_status(slug)
            assert bucket != "cancelled", f"{slug} must not be classified as cancelled"

    def test_corrupt_repr_string_is_repaired_at_read_time(self):
        corrupt = (
            "{'id': 566146469, 'name': 'بإنتظار المراجعة', "
            "'slug': 'under_review', 'customized': {'id': 1}}"
        )
        # Even with the corrupt legacy value, the dashboard should classify
        # correctly via _parse_corrupt_status.
        assert _parse_corrupt_status(corrupt) == "under_review"
        assert _classify_status(corrupt) == "pending"  # under_review is pending bucket

    def test_empty_status_defaults_to_pending(self):
        assert _classify_status("") == "pending"
        assert _classify_status(None) == "pending"


# ── D. Amount + created_at extraction in dashboard ────────────────────────

class TestDashboardOrderFields:

    def test_to_float_sar_handles_dict(self):
        assert _to_float_sar({"amount": 99.5, "currency": "SAR"}) == 99.5
        assert _to_float_sar({"value": 12}) == 12.0

    def test_to_float_sar_strips_arabic_currency(self):
        assert _to_float_sar("250.00 ر.س") == 250.0
        assert _to_float_sar("1,250.50") == 1250.5
        assert _to_float_sar("99 SAR") == 99.0

    def test_to_float_sar_handles_garbage(self):
        assert _to_float_sar("nothing") == 0.0
        assert _to_float_sar(None) == 0.0

    def test_read_created_at_prefers_extra_metadata(self):
        # Order has no created_at column → endpoint must read from
        # extra_metadata so the dashboard never claims old orders are "today".
        order = Order(
            tenant_id=1,
            external_id="x",
            status="completed",
            total="100",
            customer_info={},
            line_items=[],
            extra_metadata={"created_at": "2026-04-10T08:00:00+00:00"},
        )
        fallback = datetime(2099, 1, 1, tzinfo=timezone.utc)
        result = _read_created_at(order, fallback=fallback)
        assert result.year == 2026
        assert result.month == 4
        assert result.day == 10

    def test_read_created_at_falls_back_when_no_metadata(self):
        order = Order(
            tenant_id=1, external_id="x", status="completed",
            total="100", customer_info={}, line_items=[],
            extra_metadata={},
        )
        fallback = datetime(2030, 6, 1, tzinfo=timezone.utc)
        assert _read_created_at(order, fallback=fallback) == fallback


# ── E. Customer intelligence heals corrupt status at read time ────────────

class TestCustomerIntelligenceHealsCorruptStatus:

    def test_order_status_key_heals_dict_repr(self):
        corrupt = "{'id': 1, 'name': 'X', 'slug': 'delivered'}"
        assert order_status_key(corrupt) == "delivered"

    def test_order_status_key_handles_clean_string(self):
        assert order_status_key("delivered") == "delivered"
        assert order_status_key("UNDER_REVIEW") == "under_review"

    def test_order_status_key_handles_real_order_object(self):
        # Use the same import path that customer_intelligence.py uses,
        # otherwise sys.path duplication creates two distinct Order classes
        # and isinstance(order, Order) returns False.
        order = _ModelsOrder(
            tenant_id=1, external_id="x",
            status="{'id': 1, 'slug': 'shipped', 'name': 'Y'}",
            total="100", customer_info={}, line_items=[],
        )
        assert order_status_key(order) == "shipped"

    def test_corrupt_status_still_counted_as_real_order(self):
        # is_countable_order should NOT exclude an order just because its
        # status is corrupt — the slug `processing` is in COUNTABLE.
        order = Order(
            tenant_id=1, external_id="x",
            status="{'id': 1, 'slug': 'processing', 'name': 'Y'}",
            total="100", customer_info={}, line_items=[],
        )
        assert is_countable_order(order) is True


# ── F. Backfill endpoint contract ─────────────────────────────────────────

class TestBackfillRepairsCorruption:

    def test_repaired_rows_recover_slug_and_classify_correctly(self):
        db, tenant_id, engine = _make_db()
        try:
            now = datetime.now(timezone.utc)
            customer = Customer(tenant_id=tenant_id, name="نشمي", phone="+966512345678")
            db.add(customer)
            db.commit()

            corrupt = (
                "{'id': 566146469, 'name': 'بإنتظار المراجعة', "
                "'slug': 'under_review'}"
            )
            db.add(Order(
                tenant_id=tenant_id,
                external_id="ord-1",
                status=corrupt,
                total="150.00",
                customer_info={"name": "نشمي", "phone": "+966512345678", "mobile": "+966512345678"},
                line_items=[{"product_id": "1", "name": "منتج", "quantity": 1}],
                extra_metadata={"created_at": now.isoformat()},
            ))
            db.commit()

            # Replicate the backfill code path inline (the endpoint itself
            # requires the FastAPI dependency stack which would over-couple
            # this test to auth setup).
            import ast as _ast
            order = db.query(Order).filter_by(tenant_id=tenant_id).first()
            parsed = _ast.literal_eval(order.status)
            order.status = parsed["slug"]
            db.commit()

            assert order.status == "under_review"

            # Now the customer profile rebuild should classify the customer
            # based on the recovered status.
            svc = CustomerIntelligenceService(db, tenant_id)
            n = svc.rebuild_profiles_for_tenant(
                reason="test_backfill", commit=True, emit_event=False,
            )
            assert n == 1

            profile = (
                db.query(CustomerProfile)
                .filter_by(tenant_id=tenant_id, customer_id=customer.id)
                .first()
            )
            assert profile is not None
            assert profile.total_orders == 1, "the recovered order must be counted"
            # First order today → status should be "new" (≤30 days from first order),
            # NOT "lead" or "inactive".
            assert profile.customer_status in {"new", "active"}, (
                f"customer with a recent recovered order must not be lead/inactive, "
                f"got {profile.customer_status!r}"
            )
        finally:
            db.close()
            engine.dispose()

    def test_old_order_still_classifies_as_inactive(self):
        # Sanity: backfill doesn't artificially make stale orders "active".
        db, tenant_id, engine = _make_db()
        try:
            old = datetime.now(timezone.utc) - timedelta(days=200)
            customer = Customer(tenant_id=tenant_id, name="X", phone="+966599999999")
            db.add(customer)
            db.commit()
            db.add(Order(
                tenant_id=tenant_id, external_id="old",
                status="delivered", total="100",
                customer_info={"phone": "+966599999999", "mobile": "+966599999999"},
                line_items=[],
                extra_metadata={"created_at": old.isoformat()},
            ))
            db.commit()
            CustomerIntelligenceService(db, tenant_id).rebuild_profiles_for_tenant(
                reason="t", commit=True, emit_event=False,
            )
            profile = db.query(CustomerProfile).first()
            assert profile.total_orders == 1
            assert profile.customer_status == "inactive"
        finally:
            db.close()
            engine.dispose()
