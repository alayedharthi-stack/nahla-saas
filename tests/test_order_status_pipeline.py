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
    SOURCE_LABELS_AR,
    _build_payment_reminder_text,
    _build_store_url,
    _build_timeline,
    _classify_status,
    _compute_needs_action,
    _lookup_order,
    _parse_corrupt_status,
    _read_created_at,
    _resolve_customer_display,
    _resolve_order_number,
    _resolve_source,
    _serialise_order,
    _to_float_sar,
)
from services.customer_intelligence import (  # noqa: E402
    CustomerIntelligenceService,
    is_countable_order,
    order_status_key,
)
from services.store_sync import _normalise_order as _ss_normalise_order  # noqa: E402
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

    def test_reference_id_extracted_for_dashboard_display(self):
        """Salla returns BOTH `id` (internal) and `reference_id` (human-visible).
        The adapter must surface `reference_id` so the dashboard can render
        the same number the merchant sees in their Salla store."""
        raw = {
            "id": 566146469,
            "reference_id": 1585297702,
            "status": "delivered",
            "amounts": {"total": {"amount": 174}},
            "customer": {"name": "تركي", "mobile": "+966555906901"},
            "items": [],
        }
        normalized = self.adapter._normalize_order(raw, None)
        assert normalized.id == "566146469"
        assert normalized.reference_id == "1585297702"
        assert normalized.source == "salla"

    def test_reference_id_falls_back_to_id_when_missing(self):
        raw = {
            "id": 100,
            "status": "delivered",
            "amounts": {},
            "customer": {},
            "items": [],
        }
        normalized = self.adapter._normalize_order(raw, None)
        assert normalized.reference_id == "100"
        assert normalized.source == "salla"


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


# ── C2. store_sync normaliser writes the dashboard fields ─────────────────

class TestStoreSyncNormaliserDashboardFields:
    """Once an adapter returns a NormalizedOrder, the store_sync layer
    must propagate `reference_id`, `customer_name`, and `source` into the
    DB upsert so the dashboard can render them without re-deriving."""

    def test_normaliser_extracts_external_order_number_from_reference_id(self):
        n = _ss_normalise_order({
            "id": "566146469",
            "reference_id": "1585297702",
            "status": "delivered",
            "total": 174,
            "customer": {"name": "تركي", "phone": "+966555906901"},
            "items": [],
            "source": "salla",
        })
        assert n["external_id"] == "566146469"
        assert n["external_order_number"] == "1585297702"
        assert n["customer_name"] == "تركي"
        assert n["source"] == "salla"

    def test_normaliser_falls_back_to_external_id_when_no_reference(self):
        n = _ss_normalise_order({
            "id": "ABC-7", "status": "pending",
            "total": 0, "customer": {}, "items": [],
        })
        assert n["external_id"] == "ABC-7"
        assert n["external_order_number"] == "ABC-7"

    def test_normaliser_supports_zid_code_and_shopify_name(self):
        # Zid uses `code`, Shopify uses `name` for the human number.
        zid = _ss_normalise_order({
            "id": "z1", "code": "ZID-1024", "status": "paid",
            "total": 100, "customer": {"name": "X"}, "items": [],
        })
        shopify = _ss_normalise_order({
            "id": "s1", "name": "#1099", "status": "paid",
            "total": 100, "customer": {"name": "Y"}, "items": [],
        })
        assert zid["external_order_number"] == "ZID-1024"
        assert shopify["external_order_number"] == "#1099"


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

    def test_dashboard_resolvers_use_first_class_columns(self):
        """The /orders endpoint must prefer Order.external_order_number,
        Order.customer_name, and Order.source when present so the merchant
        sees the platform's real values, not Nahla's internal pk."""
        order = Order(
            id=11,
            tenant_id=1,
            external_id="566146469",
            external_order_number="1585297702",
            status="delivered",
            total="174",
            customer_name="تركي البخاري",
            customer_info={"name": "تركي البخاري", "phone": "+966555906901"},
            line_items=[],
            source="salla",
        )
        assert _resolve_order_number(order) == "#1585297702"
        assert _resolve_customer_display(order) == "تركي البخاري"
        assert _resolve_source(order) == "salla"
        assert SOURCE_LABELS_AR[_resolve_source(order)] == "سلة"

    def test_dashboard_customer_falls_back_to_phone(self):
        order = Order(
            id=2, tenant_id=1, external_id="x",
            status="delivered", total="0",
            customer_info={"phone": "+966555000000"},
            line_items=[],
        )
        assert _resolve_customer_display(order) == "+966555000000"

    def test_dashboard_order_number_falls_back_to_external_id(self):
        order = Order(
            id=99, tenant_id=1, external_id="ABC-7",
            status="delivered", total="0",
            customer_info={}, line_items=[],
        )
        assert _resolve_order_number(order) == "#ABC-7"

    def test_dashboard_source_legacy_ai_metadata_resolves_whatsapp(self):
        # Legacy rows wrote source into extra_metadata before the dedicated
        # column existed. The resolver must still classify them as
        # "whatsapp" so they show up under the WhatsApp source filter.
        order = Order(
            id=3, tenant_id=1, external_id="x",
            status="pending_confirmation", total="0",
            customer_info={}, line_items=[],
            extra_metadata={"source": "ai_sales_agent"},
        )
        assert _resolve_source(order) == "whatsapp"

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


# ── G. Order detail page contract (clickable order #) ────────────────────

class TestStoreUrlBuilders:
    """Each adapter source must produce a deep-link the merchant can open."""

    def test_salla_uses_external_id(self):
        assert _build_store_url("salla", "566146469", "1585297702") == (
            "https://salla.sa/dashboard/orders/566146469"
        )

    def test_salla_falls_back_to_order_number(self):
        assert _build_store_url("salla", None, "#1585297702") == (
            "https://salla.sa/dashboard/orders/1585297702"
        )

    def test_zid_uses_zid_panel(self):
        assert _build_store_url("zid", "Z-1", "Z-1") == (
            "https://web.zid.sa/orders/Z-1"
        )

    def test_shopify_uses_admin_panel(self):
        assert _build_store_url("shopify", "1234567890", "#1001") == (
            "https://admin.shopify.com/orders/1234567890"
        )

    def test_whatsapp_has_no_store_url(self):
        assert _build_store_url("whatsapp", "x", "y") is None

    def test_manual_has_no_store_url(self):
        assert _build_store_url("manual", "x", "y") is None

    def test_missing_identifiers_returns_none(self):
        assert _build_store_url("salla", None, None) is None


class TestSerialiseOrderDetailedPayload:
    """The detail-mode serialiser must expose links + line items + AI flag."""

    def _order(self, **overrides):
        defaults = dict(
            id=11, tenant_id=1,
            external_id="566146469",
            external_order_number="1585297702",
            status="delivered",
            total="174",
            customer_name="تركي البخاري",
            customer_info={
                "name": "تركي البخاري",
                "phone": "+966555906901",
                "city": "الرياض",
                "district": "النخيل",
            },
            line_items=[
                {"product_id": "p1", "name": "عسل سدر", "quantity": 2, "unit_price": "50"},
            ],
            source="salla",
            extra_metadata={"created_at": "2026-04-15T12:00:00+03:00"},
        )
        defaults.update(overrides)
        return Order(**defaults)

    def test_detailed_payload_exposes_store_link(self):
        payload = _serialise_order(
            self._order(),
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=True,
        )
        assert payload["links"]["store"] == "https://salla.sa/dashboard/orders/566146469"
        assert payload["links"]["store_label"] == "فتح الطلب في سلة"

    def test_detailed_payload_exposes_whatsapp_and_conversation_links(self):
        payload = _serialise_order(
            self._order(),
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=True,
        )
        assert payload["links"]["whatsapp"] == "https://wa.me/966555906901"
        assert payload["links"]["conversation"] == "/conversations?phone=+966555906901"

    def test_detailed_payload_returns_line_items(self):
        payload = _serialise_order(
            self._order(),
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=True,
        )
        assert len(payload["line_items"]) == 1
        item = payload["line_items"][0]
        assert item["name"] == "عسل سدر"
        assert item["quantity"] == 2
        assert item["unit_price"] == 50.0

    def test_detailed_payload_returns_address(self):
        payload = _serialise_order(
            self._order(),
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=True,
        )
        assert payload["customer_address"]["city"] == "الرياض"
        assert payload["customer_address"]["district"] == "النخيل"

    def test_whatsapp_order_is_marked_as_ai_created(self):
        payload = _serialise_order(
            self._order(source="whatsapp", external_id="wa-1"),
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=True,
        )
        assert payload["is_ai_created"] is True
        # WhatsApp orders should NOT get a store deep-link.
        assert payload["links"]["store"] is None

    def test_legacy_ai_metadata_marks_ai_created(self):
        payload = _serialise_order(
            self._order(extra_metadata={"source": "ai_sales_agent"}, source=None),
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=True,
        )
        assert payload["is_ai_created"] is True

    def test_store_order_is_not_ai_created(self):
        payload = _serialise_order(
            self._order(),
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=True,
        )
        assert payload["is_ai_created"] is False

    def test_list_payload_omits_detail_only_keys(self):
        payload = _serialise_order(
            self._order(),
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=False,
        )
        assert "links" not in payload
        assert "line_items" not in payload
        assert "customer_address" not in payload


class TestLookupOrder:
    """The detail endpoint must accept any of: pk, external_id, ref number."""

    def test_lookup_by_internal_pk(self):
        db, tenant_id, engine = _make_db()
        try:
            order = Order(
                tenant_id=tenant_id,
                external_id="ext-1",
                external_order_number="100",
                status="delivered", total="50",
                customer_info={}, line_items=[],
                source="salla",
            )
            db.add(order); db.commit()

            hit = _lookup_order(db, tenant_id, str(order.id))
            assert hit is not None and hit.id == order.id
        finally:
            db.close(); engine.dispose()

    def test_lookup_by_external_id(self):
        db, tenant_id, engine = _make_db()
        try:
            order = Order(
                tenant_id=tenant_id,
                external_id="ext-1",
                external_order_number="100",
                status="delivered", total="50",
                customer_info={}, line_items=[], source="salla",
            )
            db.add(order); db.commit()
            hit = _lookup_order(db, tenant_id, "ext-1")
            assert hit is not None
        finally:
            db.close(); engine.dispose()

    def test_lookup_by_external_order_number_with_hash(self):
        db, tenant_id, engine = _make_db()
        try:
            order = Order(
                tenant_id=tenant_id,
                external_id="ext-1",
                external_order_number="1585297702",
                status="delivered", total="50",
                customer_info={}, line_items=[], source="salla",
            )
            db.add(order); db.commit()
            hit = _lookup_order(db, tenant_id, "#1585297702")
            assert hit is not None
        finally:
            db.close(); engine.dispose()

    def test_lookup_does_not_cross_tenant(self):
        db, tenant_id, engine = _make_db()
        try:
            other = Tenant(name="Other", is_active=True)
            db.add(other); db.commit()
            order = Order(
                tenant_id=other.id,
                external_id="ext-1",
                external_order_number="100",
                status="delivered", total="50",
                customer_info={}, line_items=[], source="salla",
            )
            db.add(order); db.commit()
            hit = _lookup_order(db, tenant_id, str(order.id))
            assert hit is None
        finally:
            db.close(); engine.dispose()

    def test_lookup_unknown_returns_none(self):
        db, tenant_id, engine = _make_db()
        try:
            assert _lookup_order(db, tenant_id, "no-such-thing") is None
        finally:
            db.close(); engine.dispose()


# ── H. Operational layer (needs_action, timeline, reminder draft) ────────

class TestComputeNeedsAction:
    """The needs_action chip-list must reflect concrete operational gaps."""

    def test_pending_with_link_is_awaiting_payment_only(self):
        reasons = _compute_needs_action(
            status="pending", source_key="salla", payment_link="https://pay/x",
            is_vip_customer=False, has_open_conv=False, is_ai_created=False,
        )
        keys = [r["key"] for r in reasons]
        assert "awaiting_payment" in keys
        assert "no_payment_link" not in keys

    def test_pending_without_link_adds_no_payment_link(self):
        reasons = _compute_needs_action(
            status="pending", source_key="salla", payment_link=None,
            is_vip_customer=False, has_open_conv=False, is_ai_created=False,
        )
        keys = [r["key"] for r in reasons]
        assert "awaiting_payment" in keys
        assert "no_payment_link" in keys

    def test_paid_order_has_no_action(self):
        reasons = _compute_needs_action(
            status="paid", source_key="salla", payment_link="x",
            is_vip_customer=False, has_open_conv=False, is_ai_created=False,
        )
        assert reasons == []

    def test_vip_always_chips_in(self):
        reasons = _compute_needs_action(
            status="paid", source_key="salla", payment_link="x",
            is_vip_customer=True, has_open_conv=False, is_ai_created=False,
        )
        assert any(r["key"] == "vip" for r in reasons)
        assert all(r["level"] in {"amber", "red", "blue", "purple"} for r in reasons)

    def test_open_conversation_chips_in(self):
        reasons = _compute_needs_action(
            status="paid", source_key="salla", payment_link="x",
            is_vip_customer=False, has_open_conv=True, is_ai_created=False,
        )
        assert any(r["key"] == "open_conversation" for r in reasons)

    def test_whatsapp_ai_order_without_followup_flagged(self):
        reasons = _compute_needs_action(
            status="paid", source_key="whatsapp", payment_link="x",
            is_vip_customer=False, has_open_conv=False, is_ai_created=True,
        )
        assert any(r["key"] == "whatsapp_unfollowed" for r in reasons)

    def test_whatsapp_ai_order_with_open_conv_not_flagged(self):
        reasons = _compute_needs_action(
            status="paid", source_key="whatsapp", payment_link="x",
            is_vip_customer=False, has_open_conv=True, is_ai_created=True,
        )
        assert all(r["key"] != "whatsapp_unfollowed" for r in reasons)


class TestPaymentReminderDraft:

    def test_draft_includes_order_number_and_link(self):
        text = _build_payment_reminder_text(
            customer_name="نشمي",
            order_number="#1585297702",
            payment_url="https://pay.example/abc",
        )
        assert "نشمي" in text
        assert "#1585297702" in text
        assert "https://pay.example/abc" in text

    def test_draft_falls_back_when_no_link(self):
        text = _build_payment_reminder_text(
            customer_name="نشمي",
            order_number="#1",
            payment_url=None,
        )
        # Must NOT mention an empty link; should still be a friendly message.
        assert "https://" not in text
        assert "نشمي" in text

    def test_dash_customer_name_falls_back_to_polite_address(self):
        text = _build_payment_reminder_text(
            customer_name="—",
            order_number="#1",
            payment_url="https://x",
        )
        assert "عميلنا الكريم" in text


class TestBuildTimeline:

    def _order(self, **overrides):
        defaults = dict(
            id=11, tenant_id=1,
            external_id="566146469",
            external_order_number="1585297702",
            status="pending_payment",
            total="174",
            customer_info={"phone": "+966555906901"},
            line_items=[],
            source="salla",
            extra_metadata={"created_at": "2026-04-15T12:00:00+00:00"},
        )
        defaults.update(overrides)
        return Order(**defaults)

    def test_timeline_always_has_creation_event(self):
        ev = _build_timeline(self._order(), has_open_conv=False, source_label="سلة")
        keys = [e["key"] for e in ev]
        assert "created" in keys
        assert "أُنشئ من المتجر" in next(e for e in ev if e["key"] == "created")["label"]

    def test_timeline_marks_ai_created_for_whatsapp(self):
        ev = _build_timeline(
            self._order(source="whatsapp"),
            has_open_conv=False, source_label="واتساب",
        )
        creation = next(e for e in ev if e["key"] == "created")
        assert "أنشأه الذكاء" in creation["label"]

    def test_timeline_includes_payment_link_event_when_checkout_url_present(self):
        order = self._order()
        order.checkout_url = "https://pay/x"
        ev = _build_timeline(order, has_open_conv=False, source_label="سلة")
        assert any(e["key"] == "payment_link_attached" for e in ev)

    def test_timeline_includes_payment_reminder_history(self):
        order = self._order(
            extra_metadata={
                "created_at": "2026-04-15T12:00:00+00:00",
                "payment_reminders": [
                    {"sent_at": "2026-04-16T09:00:00+00:00", "channel": "whatsapp"},
                    {"sent_at": "2026-04-16T18:00:00+00:00", "channel": "whatsapp"},
                ],
            },
        )
        ev = _build_timeline(order, has_open_conv=False, source_label="سلة")
        reminder_events = [e for e in ev if e["key"] == "payment_reminder_sent"]
        assert len(reminder_events) == 2

    def test_timeline_includes_open_conversation_marker(self):
        ev = _build_timeline(self._order(), has_open_conv=True, source_label="سلة")
        assert any(e["key"] == "conversation_open" for e in ev)

    def test_timeline_is_sorted_by_at(self):
        order = self._order(
            extra_metadata={
                "created_at": "2026-04-15T12:00:00+00:00",
                "payment_reminders": [{"sent_at": "2026-04-16T09:00:00+00:00"}],
                "status_changed_at": "2026-04-17T09:00:00+00:00",
            },
        )
        ev = _build_timeline(order, has_open_conv=False, source_label="سلة")
        ats = [e["at"] for e in ev if e["at"]]
        assert ats == sorted(ats)


class TestSerialiseOrderEmitsOperationalFields:
    """Both list and detail payloads must expose operational fields."""

    def _order(self, **overrides):
        defaults = dict(
            id=11, tenant_id=1,
            external_id="566146469",
            external_order_number="1585297702",
            status="pending_payment",
            total="174",
            customer_name="نشمي",
            customer_info={"name": "نشمي", "phone": "+966555906901"},
            line_items=[],
            source="salla",
            extra_metadata={"created_at": "2026-04-15T12:00:00+00:00"},
        )
        defaults.update(overrides)
        return Order(**defaults)

    def test_list_payload_includes_needs_action(self):
        payload = _serialise_order(
            self._order(),
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=False,
        )
        # pending order with no checkout_url → at least both pending+no_link.
        keys = [r["key"] for r in payload["needs_action"]]
        assert "awaiting_payment" in keys
        assert "no_payment_link" in keys

    def test_list_payload_marks_vip_when_phone_in_set(self):
        payload = _serialise_order(
            self._order(),
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=False,
            vip_phones={"966555906901"},
        )
        assert payload["is_vip"] is True
        assert any(r["key"] == "vip" for r in payload["needs_action"])

    def test_list_payload_marks_open_conversation(self):
        payload = _serialise_order(
            self._order(),
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=False,
            unread_phones={"966555906901"},
        )
        assert payload["has_open_conversation"] is True
        assert any(r["key"] == "open_conversation" for r in payload["needs_action"])

    def test_detail_payload_includes_timeline_and_draft(self):
        payload = _serialise_order(
            self._order(),
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=True,
        )
        assert "timeline" in payload
        assert any(e["key"] == "created" for e in payload["timeline"])
        # pending → draft must be filled.
        assert payload["payment_reminder_draft"]
        assert "نشمي" in payload["payment_reminder_draft"]
        assert "#1585297702" in payload["payment_reminder_draft"]

    def test_detail_payload_no_draft_for_paid_orders(self):
        order = self._order(status="paid")
        payload = _serialise_order(
            order,
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=True,
        )
        assert payload["payment_reminder_draft"] is None
        # Paid orders without VIP / conversation → no needs_action.
        assert payload["needs_action"] == []

    def test_paid_order_with_no_extra_signals_has_empty_needs_action(self):
        order = self._order(status="paid")
        order.checkout_url = "https://pay/x"
        payload = _serialise_order(
            order,
            customer_lookup={},
            now=datetime.now(timezone.utc),
            detailed=False,
        )
        assert payload["needs_action"] == []
