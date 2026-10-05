"""Salla → Nahla product truth: money objects, availability, source status, duplicate and late events.

Generic merchant data (متجر تجريبي عام). Asserts persisted state and queue
behaviour, never Arabic wording.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Product, ProductVariant, Tenant, WhatsAppConnection  # noqa: E402
from core.catalog import is_hidden_at_source, is_whatsapp_channel_publish_eligible  # noqa: E402
from services.store_sync import StoreSyncService, _normalise_product  # noqa: E402
from store_adapters.salla_adapter import SallaAdapter  # noqa: E402


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    session.add(tenant)
    session.commit()
    session.add(
        WhatsAppConnection(
            tenant_id=tenant.id,
            whatsapp_business_account_id="WABA-1",
            phone_number_id="PN-1",
            access_token="EAAB-test",
            meta_catalog_id="CAT-GENERIC-001",
            catalog_enabled=True,
            extra_metadata={},
        )
    )
    session.commit()
    return session, tenant.id, engine


def _stamp(hour, minute):
    return {"date": f"2026-10-01 {hour:02d}:{minute:02d}:00.000000", "timezone_type": 3, "timezone": "Asia/Riyadh"}


_SEEN_BODIES: list = []


def _raw_webhook_product(**overrides):
    body = _build_raw_webhook_product(**overrides)
    _TRUTH_OVERRIDES[str(body.get("id"))] = body
    _SEEN_BODIES.append(body)
    return body


def _build_raw_webhook_product(**overrides):
    body = {
        "id": 900100,
        "name": "عطر ورد 100ml",
        "description": "عطر زهري",
        "price": {"amount": 199.0, "currency": "SAR"},
        "sale_price": {"amount": 0, "currency": "SAR"},
        "regular_price": {"amount": 199.0, "currency": "SAR"},
        "quantity": 0,
        "status": "sale",
        "images": [{"url": "https://cdn.example/rose.jpg", "main": True}],
        "urls": {"customer": "https://store.example/p/rose"},
        "skus": [{"id": 77001, "price": {"amount": 199.0, "currency": "SAR"}, "stock_quantity": 0}],
    }
    body.update(overrides)
    return body


# ── Normaliser truth ──────────────────────────────────────────────────────

def test_webhook_money_objects_never_become_dict_text():
    norm = _normalise_product(_raw_webhook_product())
    assert norm["price"] == "199.0"
    assert norm["regular_price"] == "199.0"
    assert norm["currency"] == "SAR"
    assert "{" not in norm["price"]


def test_webhook_quantity_zero_is_out_of_stock_and_unlimited_is_in_stock():
    assert _normalise_product(_raw_webhook_product(quantity=0))["in_stock"] is False
    assert _normalise_product(_raw_webhook_product(quantity=5))["in_stock"] is True
    assert _normalise_product(_raw_webhook_product(quantity=0, unlimited_quantity=True))["in_stock"] is True
    # adapter-normalised payloads keep their explicit flag
    assert _normalise_product({"id": "1", "title": "x", "price": "5", "in_stock": False, "quantity": 9})["in_stock"] is False


def test_live_salla_statuses_stay_orderable_for_the_ai_and_hidden_is_not():
    """``sale``/``out`` are live listings: the platform lifecycle status must stay
    ``active`` (AI orderability reads it), while the raw value is kept apart."""
    from modules.ai.brain.postprocess.availability_context_builder import _can_checkout_from_row

    for raw_status in ("sale", "out", {"slug": "sale"}):
        norm = _normalise_product(_raw_webhook_product(status=raw_status, quantity=4))
        assert norm["status"] == "active"
        assert norm["source_status"] in ("sale", "out")
        row = Product(tenant_id=1, external_id="900100", title="عطر ورد 100ml", price="199", source="salla",
                      in_stock=True, stock_quantity=4, catalog_status="active", extra_metadata=norm)
        assert _can_checkout_from_row(row, variants_ok=True) is True
        assert is_whatsapp_channel_publish_eligible(row) is True
    hidden_norm = _normalise_product(_raw_webhook_product(status="hidden", quantity=4))
    assert hidden_norm["status"] == "hidden" and hidden_norm["source_status"] == "hidden"
    hidden_row = Product(tenant_id=1, external_id="900100", title="x", price="199", source="salla",
                         in_stock=True, stock_quantity=4, catalog_status="active", extra_metadata=hidden_norm)
    assert _can_checkout_from_row(hidden_row, variants_ok=True) is False
    assert is_whatsapp_channel_publish_eligible(hidden_row) is False
    # adapter-normalised products (no status) keep the historical default
    assert _normalise_product({"id": "1", "title": "x", "price": "5"})["status"] == "active"


def test_source_status_carried_and_hidden_blocks_channel_publish():
    hidden = _normalise_product(_raw_webhook_product(status="hidden"))
    assert hidden["status"] == "hidden"
    row = Product(
        tenant_id=1, external_id="900100", title="عطر ورد 100ml", price="199", source="salla",
        catalog_status="active", extra_metadata={"status": "hidden"},
    )
    assert is_hidden_at_source(row) is True
    assert is_whatsapp_channel_publish_eligible(row) is False
    row.extra_metadata = {"status": "sale"}
    assert is_whatsapp_channel_publish_eligible(row) is True
    native = Product(tenant_id=1, title="x", price="5", source="manual", catalog_status="active", extra_metadata={"status": "hidden"})
    assert is_hidden_at_source(native) is False


def test_adapter_product_carries_source_status():
    adapter = SallaAdapter.__new__(SallaAdapter)
    normalized = SallaAdapter._normalize_product(adapter, _raw_webhook_product(status={"slug": "hidden"}))
    assert normalized.status == "hidden"
    assert normalized.in_stock is False
    assert normalized.price == 199.0


# ── Webhook flow on real rows ─────────────────────────────────────────────

def _svc(session, tenant_id):
    svc = StoreSyncService(session, tenant_id)
    adapter = MagicMock()
    adapter.platform = "salla"
    adapter.get_raw_variants = AsyncMock(return_value=[
        {"id": 77001, "price": {"amount": 199.0, "currency": "SAR"}, "quantity": 0},
    ])
    real = SallaAdapter.__new__(SallaAdapter)
    adapter._normalize_variant = lambda raw, opts=None: SallaAdapter._normalize_variant(real, raw, opts)
    # A product.created with no local row is verified against the store before
    # the row is created; by default the store agrees with the event body.
    adapter.get_product = AsyncMock(side_effect=lambda pid: _adapter_truth_for(pid))
    svc._adapter = adapter
    return svc


_TRUTH_OVERRIDES: dict = {}


@pytest.fixture(autouse=True)
def _synthetic_read_clock(monkeypatch):
    """Store reads in these tests happen in the synthetic timeline of the
    fixtures: a read is stamped from the ``updated_at`` of the last body the
    store would return (minus the skew margin), not from the wall clock.
    An explicit ``now`` still goes through the real stamp function."""
    from services import store_sync as ss
    from services.salla_datetime import parse_salla_datetime_to_utc

    real = ss.source_read_stamp

    def _fake(now=None):
        if now is not None:
            return real(now)
        stamps = [
            parse_salla_datetime_to_utc(b.get("updated_at"))
            for b in _SEEN_BODIES
            if b.get("updated_at") is not None
        ]
        stamps = [x for x in stamps if x is not None]
        # a monotonic synthetic clock: the store is at least as fresh as the newest body seen
        return real(max(stamps)) if stamps else real()

    monkeypatch.setattr(ss, "source_read_stamp", _fake)
    _TRUTH_OVERRIDES.clear(); _SEEN_BODIES.clear()
    yield
    _TRUTH_OVERRIDES.clear(); _SEEN_BODIES.clear()


def _adapter_truth_for(product_id):
    """Store read used by the default test adapter: the last body seen for this id."""
    body = _TRUTH_OVERRIDES.get(str(product_id)) or _raw_webhook_product()
    adapter = SallaAdapter.__new__(SallaAdapter)
    return SallaAdapter._normalize_product(adapter, body).dict()


def test_created_then_duplicate_event_marks_pending_once():
    session, tid, engine = _make_db()
    try:
        svc = _svc(session, tid)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(_raw_webhook_product(updated_at=_stamp(8, 0)), webhook_event_type="product.created"))
        row = session.query(Product).filter_by(tenant_id=tid, external_id="900100").one()
        assert row.price == "199.0"
        assert row.in_stock is False
        assert row.sync_status == "pending"
        gen1 = row.extra_metadata["sync_meta"]["content_generation"]
        variants = session.query(ProductVariant).filter_by(product_id=row.id).all()
        assert [v.salla_variant_id for v in variants] == ["77001"]
        assert variants[0].retailer_id == "900100-77001"
        # same event delivered twice (Salla retries): no new generation
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(_raw_webhook_product(updated_at=_stamp(8, 0)), webhook_event_type="product.created"))
        session.refresh(row)
        assert row.extra_metadata["sync_meta"]["content_generation"] == gen1
    finally:
        session.close(); engine.dispose()


def test_price_image_and_stock_updates_bump_generation_and_hidden_requests_retirement():
    session, tid, engine = _make_db()
    try:
        svc = _svc(session, tid)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(_raw_webhook_product(quantity=3, updated_at=_stamp(8, 0)), webhook_event_type="product.created"))
            row = session.query(Product).filter_by(tenant_id=tid, external_id="900100").one()
            gen = row.extra_metadata["sync_meta"]["content_generation"]
            # price change
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 179.0, "currency": "SAR"}, updated_at=_stamp(8, 1)),
                webhook_event_type="product.price.updated",
            ))
            session.refresh(row)
            assert row.price == "179.0"
            assert row.extra_metadata["sync_meta"]["content_generation"] == gen + 1
            # image change
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 179.0, "currency": "SAR"}, updated_at=_stamp(8, 2),
                                     images=[{"url": "https://cdn.example/rose-2.jpg", "main": True}]),
                webhook_event_type="product.image.updated",
            ))
            session.refresh(row)
            assert row.extra_metadata["sync_meta"]["content_generation"] == gen + 2
            # stock to zero
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=0, price={"amount": 179.0, "currency": "SAR"}, updated_at=_stamp(8, 3),
                                     images=[{"url": "https://cdn.example/rose-2.jpg", "main": True}]),
                webhook_event_type="product.quantity.low",
            ))
            session.refresh(row)
            assert row.in_stock is False
            assert row.extra_metadata["sync_meta"]["content_generation"] == gen + 3
            assert is_whatsapp_channel_publish_eligible(row) is True  # out of stock still publishes availability
            # pretend it was published, then Salla hides it
            row.sync_status = "synced"
            row.last_synced_at = datetime.now(timezone.utc)
            session.commit()
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=0, status="hidden", price={"amount": 179.0, "currency": "SAR"}, updated_at=_stamp(8, 4),
                                     images=[{"url": "https://cdn.example/rose-2.jpg", "main": True}]),
                webhook_event_type="product.status.updated",
            ))
            session.refresh(row)
            assert row.extra_metadata["status"] == "hidden"
            assert row.sync_status == "synced"
            assert row.extra_metadata["sync_meta"]["retire_pending"] is True
            assert row.extra_metadata["sync_meta"]["retire_reason"] == "source_hidden"
    finally:
        session.close(); engine.dispose()


def _at(hour, minute):
    return {"date": f"2026-10-01 {hour:02d}:{minute:02d}:00.000000", "timezone_type": 3, "timezone": "Asia/Riyadh"}


def test_stale_event_never_replaces_the_newer_state_price_stock_or_hidden():
    """Events carry Salla's ``updated_at``; one older than the stored stamp
    is ignored for every field and never re-queues old content for Meta."""
    session, tid, engine = _make_db()
    try:
        svc = _svc(session, tid)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 150.0, "currency": "SAR"}, updated_at=_at(10, 5)),
                webhook_event_type="product.created",
            ))
            row = session.query(Product).filter_by(tenant_id=tid, external_id="900100").one()
            gen = row.extra_metadata["sync_meta"]["content_generation"]
            assert row.extra_metadata["source_event_at"]
            # delayed events describing an OLDER state: price 120, stock 0, hidden
            for stale in (
                dict(quantity=3, price={"amount": 120.0, "currency": "SAR"}, updated_at=_at(10, 0)),
                dict(quantity=0, price={"amount": 150.0, "currency": "SAR"}, updated_at=_at(10, 1)),
                dict(quantity=3, status="hidden", price={"amount": 150.0, "currency": "SAR"}, updated_at=_at(10, 2)),
            ):
                asyncio.run(svc.handle_product_webhook(_raw_webhook_product(**stale), webhook_event_type="product.updated"))
                session.refresh(row)
                assert row.price == "150.0"
                assert row.in_stock is True and row.stock_quantity == 3
                assert row.extra_metadata["status"] == "active"
                assert row.extra_metadata["sync_meta"]["content_generation"] == gen
                assert not row.extra_metadata["sync_meta"].get("retire_pending")
            # a genuinely newer event applies
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 130.0, "currency": "SAR"}, updated_at=_at(10, 9)),
                webhook_event_type="product.price.updated",
            ))
            session.refresh(row)
            assert row.price == "130.0"
            assert row.extra_metadata["sync_meta"]["content_generation"] == gen + 1
            # a late event behind the newest one is ignored again (envelope time only)
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 120.0, "currency": "SAR"}),
                webhook_event_type="product.updated",
                envelope_created_at="Thu Oct 01 2026 10:07:00 GMT+0300",
            ))
            session.refresh(row)
            assert row.price == "130.0"
            assert row.extra_metadata["sync_meta"]["content_generation"] == gen + 1
    finally:
        session.close(); engine.dispose()


def test_event_with_unprovable_order_reads_current_truth_from_salla():
    """No ``updated_at`` and no envelope time: the body is not trusted; the
    product is re-read from Salla and that state is stored."""
    session, tid, engine = _make_db()
    try:
        svc = _svc(session, tid)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 150.0, "currency": "SAR"}, updated_at=_at(10, 5)),
                webhook_event_type="product.created",
            ))
            row = session.query(Product).filter_by(tenant_id=tid, external_id="900100").one()
            svc._adapter.get_product = AsyncMock(return_value=_raw_webhook_product(
                quantity=5, price={"amount": 175.0, "currency": "SAR"}, updated_at=_at(11, 0)))
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=0, price={"amount": 120.0, "currency": "SAR"}),
                webhook_event_type="product.updated",
            ))
            session.refresh(row)
            svc._adapter.get_product.assert_awaited_once_with("900100")
            assert row.price == "175.0" and row.stock_quantity == 5 and row.in_stock is True
            # the store-side read is the freshest state, so it stamps after the created event
            assert row.extra_metadata["source_event_at"] > "2026-10-01T07:05:00"
            # when the read fails, the event is retried instead of applying the unordered body
            svc._adapter.get_product = AsyncMock(side_effect=RuntimeError("salla down"))
            with pytest.raises(RuntimeError, match="product_hydration_failed"):
                asyncio.run(svc.handle_product_webhook(
                    _raw_webhook_product(quantity=0, price={"amount": 120.0, "currency": "SAR"}),
                    webhook_event_type="product.updated",
                ))
            session.rollback(); session.refresh(row)
            assert row.price == "175.0"
    finally:
        session.close(); engine.dispose()


def test_partial_event_hydration_prefers_the_store_read_over_the_event_body():
    session, tid, engine = _make_db()
    try:
        svc = _svc(session, tid)
        svc._adapter.get_product = AsyncMock(return_value=_raw_webhook_product(
            quantity=2, price={"amount": 199.0, "currency": "SAR"}, updated_at=_at(12, 0)))
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook({"id": 900100, "price": {"amount": 90.0, "currency": "SAR"}},
                                                   webhook_event_type="product.price.updated"))
        row = session.query(Product).filter_by(tenant_id=tid, external_id="900100").one()
        assert row.price == "199.0" and row.stock_quantity == 2
    finally:
        session.close(); engine.dispose()


def test_periodic_store_sync_stamps_fetch_time_so_older_webhooks_are_ignored():
    session, tid, engine = _make_db()
    try:
        svc = _svc(session, tid)
        from services.store_sync import _normalise_product as norm

        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            result = svc._apply_normalised_product(
                norm(_raw_webhook_product(quantity=3, price={"amount": 150.0, "currency": "SAR"})), "salla",
            )
            session.commit()
            assert result["action"] == "created"
            row = session.query(Product).filter_by(tenant_id=tid, external_id="900100").one()
            assert row.extra_metadata["source_event_at"]
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 120.0, "currency": "SAR"}, updated_at=_at(9, 0)),
                webhook_event_type="product.updated",
            ))
            session.refresh(row)
            assert row.price == "150.0"
    finally:
        session.close(); engine.dispose()


def _adapter_truth(**overrides):
    """The real adapter shape (NormalizedProduct.dict()): stock_quantity/in_stock, no `quantity`."""
    adapter = SallaAdapter.__new__(SallaAdapter)
    return SallaAdapter._normalize_product(adapter, _raw_webhook_product(**overrides)).dict()


def test_unprovable_order_and_partial_events_take_stock_from_the_real_adapter_shape():
    """Review finding: with the adapter's real shape (``stock_quantity``, no
    ``quantity``) the untrusted body's quantity must not leak into the row."""
    session, tid, engine = _make_db()
    try:
        svc = _svc(session, tid)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 150.0, "currency": "SAR"}, updated_at=_stamp(10, 5)),
                webhook_event_type="product.created",
            ))
            row = session.query(Product).filter_by(tenant_id=tid, external_id="900100").one()
            # unprovable order: body says quantity 0 / price 120; Salla truth says stock 5 / price 175
            svc._adapter.get_product = AsyncMock(return_value=_adapter_truth(
                quantity=5, price={"amount": 175.0, "currency": "SAR"}, updated_at=_stamp(11, 0)))
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=0, price={"amount": 120.0, "currency": "SAR"}),
                webhook_event_type="product.updated",
            ))
            session.refresh(row)
            assert row.price == "175.0" and row.stock_quantity == 5 and row.in_stock is True
            assert row.extra_metadata.get("stock_qty") == 5
            # partial event: body quantity 0; truth stock 2
            svc._adapter.get_product = AsyncMock(return_value=_adapter_truth(
                quantity=2, price={"amount": 175.0, "currency": "SAR"}, updated_at=_stamp(11, 5)))
            asyncio.run(svc.handle_product_webhook({"id": 900100, "quantity": 0},
                                                   webhook_event_type="product.quantity.low"))
            session.refresh(row)
            assert row.stock_quantity == 2 and row.in_stock is True and row.price == "175.0"
    finally:
        session.close(); engine.dispose()


def test_bulk_sync_read_started_before_a_newer_webhook_does_not_win_and_redelivery_still_applies():
    """Review finding: the hourly read is stamped with the time the read
    STARTED; a webhook applied while the read was in flight is newer and
    stays; a redelivery of that webhook is not rejected."""
    session, tid, engine = _make_db()
    try:
        svc = _svc(session, tid)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 150.0, "currency": "SAR"}, updated_at=_stamp(10, 0)),
                webhook_event_type="product.created",
            ))
            row = session.query(Product).filter_by(tenant_id=tid, external_id="900100").one()
            # bulk sync "read" began at 10:15 (Riyadh) and returned the old price 150 ...
            from services.store_sync import SOURCE_EVENT_AT_KEY, _normalise_product as norm
            from services.salla_datetime import parse_salla_datetime_to_utc

            read_started = parse_salla_datetime_to_utc(_stamp(10, 15))
            # ... meanwhile a webhook for 10:30 landed and applied
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 130.0, "currency": "SAR"}, updated_at=_stamp(10, 30)),
                webhook_event_type="product.price.updated",
            ))
            stale_read = norm(_raw_webhook_product(quantity=3, price={"amount": 150.0, "currency": "SAR"}))
            stale_read[SOURCE_EVENT_AT_KEY] = read_started.isoformat()
            result = svc._apply_normalised_product(stale_read, "salla")
            session.commit(); session.refresh(row)
            assert result["action"] == "skipped_stale"
            assert row.price == "130.0"
            # the 10:30 event redelivered is equal to the stamp → applies idempotently, not rejected
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 130.0, "currency": "SAR"}, updated_at=_stamp(10, 30)),
                webhook_event_type="product.price.updated",
            ))
            session.refresh(row)
            assert row.price == "130.0"
            # a read that started AFTER the webhook wins (it is newer truth)
            fresh_read = norm(_raw_webhook_product(quantity=3, price={"amount": 140.0, "currency": "SAR"}))
            fresh_read[SOURCE_EVENT_AT_KEY] = parse_salla_datetime_to_utc(_stamp(10, 45)).isoformat()
            assert svc._apply_normalised_product(fresh_read, "salla")["action"] == "updated"
            session.commit(); session.refresh(row)
            assert row.price == "140.0"
    finally:
        session.close(); engine.dispose()


def test_late_update_after_delete_does_not_resurrect_the_product():
    session, tid, engine = _make_db()
    try:
        svc = _svc(session, tid)
        svc._adapter.get_product = AsyncMock(return_value=None)  # Salla no longer has it
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 150.0, "currency": "SAR"}, updated_at=_stamp(9, 0)),
                webhook_event_type="product.updated",
            ))
        assert session.query(Product).filter_by(tenant_id=tid, external_id="900100").count() == 0
        svc._adapter.get_product.assert_awaited_once_with("900100")
        # a product the store does have (missed product.created) is created from the store read
        svc._adapter.get_product = AsyncMock(return_value=_adapter_truth(quantity=4, updated_at=_stamp(9, 30)))
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=0, price={"amount": 1.0, "currency": "SAR"}, updated_at=_stamp(9, 0)),
                webhook_event_type="product.updated",
            ))
        row = session.query(Product).filter_by(tenant_id=tid, external_id="900100").one()
        assert row.stock_quantity == 4 and row.price == "199.0"
    finally:
        session.close(); engine.dispose()


def test_change_made_in_the_same_second_as_the_read_start_is_not_lost():
    """Review N1: Salla's updated_at is second-precision; a read stamped a
    few hundred ms into the same second must not shadow a change made then."""
    from services.store_sync import SOURCE_EVENT_AT_KEY, source_read_stamp, _normalise_product as norm
    from services.salla_datetime import parse_salla_datetime_to_utc

    session, tid, engine = _make_db()
    try:
        svc = _svc(session, tid)
        base = parse_salla_datetime_to_utc(_stamp(10, 15))  # 10:15:00 Riyadh
        read_started = base.replace(microsecond=500_000)
        read = norm(_raw_webhook_product(quantity=3, price={"amount": 150.0, "currency": "SAR"}))
        read[SOURCE_EVENT_AT_KEY] = source_read_stamp(read_started).isoformat()
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            assert svc._apply_normalised_product(read, "salla")["action"] == "created"
            session.commit()
            row = session.query(Product).filter_by(tenant_id=tid, external_id="900100").one()
            # merchant changed the price at 10:15:00.9 → Salla says updated_at 10:15:00
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 130.0, "currency": "SAR"}, updated_at=_stamp(10, 15)),
                webhook_event_type="product.price.updated",
            ))
            session.refresh(row)
            assert row.price == "130.0"
            # but a change from well before the read is still ignored
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 120.0, "currency": "SAR"}, updated_at=_stamp(10, 10)),
                webhook_event_type="product.price.updated",
            ))
            session.refresh(row)
            assert row.price == "130.0"
    finally:
        session.close(); engine.dispose()


def test_redelivered_product_created_after_delete_does_not_resurrect():
    """Review N2: a product.created redelivered after product.deleted asks the
    store; a product that is gone is dropped, one that exists is created from
    the store read."""
    from services.store_sync import StoreSyncService

    session, tid, engine = _make_db()
    try:
        svc = _svc(session, tid)
        svc._adapter.get_product = AsyncMock(return_value=_adapter_truth(quantity=3, updated_at=_stamp(9, 0)))
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 150.0, "currency": "SAR"}, updated_at=_stamp(9, 0)),
                webhook_event_type="product.created",
            ))
            assert session.query(Product).filter_by(tenant_id=tid, external_id="900100").count() == 1
            asyncio.run(StoreSyncService(session, tid).handle_product_deleted("900100"))
            assert session.query(Product).filter_by(tenant_id=tid, external_id="900100").count() == 0
            svc._adapter.get_product = AsyncMock(return_value=None)
            asyncio.run(svc.handle_product_webhook(
                _raw_webhook_product(quantity=3, price={"amount": 150.0, "currency": "SAR"}, updated_at=_stamp(9, 0)),
                webhook_event_type="product.created",
            ))
        assert session.query(Product).filter_by(tenant_id=tid, external_id="900100").count() == 0
        svc._adapter.get_product.assert_awaited_once_with("900100")
    finally:
        session.close(); engine.dispose()
