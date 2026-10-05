"""Salla event ordering never trusts a time in the future.

The ordering guard discards a product read or webhook older than the row's
``source_event_at``. A time beyond now (a clock ahead of ours, or a local
wall-clock string read as UTC) would make every correct later update look
older and be discarded until that time passed, for every Salla tenant. Event
times beyond now + ``SOURCE_EVENT_FUTURE_TOLERANCE`` are therefore not
provable: an incoming one triggers a fresh store read, a stored one guards
nothing. Ordinary ordering (an older event never replaces a newer state) is
unchanged.

Generic merchant data (متجر تجريبي عام, «حذاء رياضي أبيض»). Real clock;
the store is a mocked adapter. Asserts persisted state only.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

from database.models import Base, Product, Tenant, WhatsAppConnection
from services.store_sync import (
    SOURCE_EVENT_AT_KEY,
    SOURCE_EVENT_FUTURE_TOLERANCE,
    StoreSyncService,
    _normalise_product,
    source_event_time,
    stored_source_event_time,
)
from store_adapters.salla_adapter import SallaAdapter

PRODUCT_ID = "910200"


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    session.add(tenant)
    session.commit()
    session.add(WhatsAppConnection(
        tenant_id=tenant.id, whatsapp_business_account_id="WABA-1", phone_number_id="PN-1",
        access_token="EAAB-test", meta_catalog_id="CAT-GENERIC-001", catalog_enabled=True, extra_metadata={},
    ))
    session.commit()
    return session, tenant.id, engine


def _body(price, *, quantity=4, updated_at=None):
    body = {
        "id": int(PRODUCT_ID),
        "name": "حذاء رياضي أبيض",
        "description": "حذاء جري",
        "price": {"amount": float(price), "currency": "SAR"},
        "sale_price": {"amount": 0, "currency": "SAR"},
        "regular_price": {"amount": float(price), "currency": "SAR"},
        "quantity": quantity,
        "status": "sale",
        "images": [{"url": "https://cdn.example/shoe.jpg", "main": True}],
        "urls": {"customer": "https://store.example/p/shoe"},
        "skus": [{"id": 88001, "price": {"amount": float(price), "currency": "SAR"}, "stock_quantity": quantity}],
    }
    if updated_at is not None:
        body["updated_at"] = updated_at
    return body


def _service(session, tenant_id, store_state):
    """StoreSyncService whose Salla adapter returns *store_state* (a dict holding the current body)."""
    svc = StoreSyncService(session, tenant_id)
    adapter = MagicMock()
    adapter.platform = "salla"
    adapter.get_raw_variants = AsyncMock(return_value=[])
    real = SallaAdapter.__new__(SallaAdapter)
    adapter._normalize_variant = lambda raw, opts=None: SallaAdapter._normalize_variant(real, raw, opts)
    adapter.get_product = AsyncMock(
        side_effect=lambda pid: SallaAdapter._normalize_product(real, store_state["body"]).dict()
    )
    svc._adapter = adapter
    return svc


def _riyadh_wall_clock_naive(now):
    """``updated_at`` as a flat string with no zone, in Asia/Riyadh wall-clock time."""
    return (now + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")


def _row(session, tenant_id):
    session.expire_all()
    return session.query(Product).filter_by(tenant_id=tenant_id, external_id=PRODUCT_ID).one()


# ── Unit: provable times ──────────────────────────────────────────────────

def test_future_times_beyond_the_tolerance_are_not_provable():
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    near = (now + SOURCE_EVENT_FUTURE_TOLERANCE - timedelta(seconds=1)).isoformat()
    far = (now + SOURCE_EVENT_FUTURE_TOLERANCE + timedelta(seconds=1)).isoformat()
    past = (now - timedelta(minutes=5)).isoformat()
    assert SOURCE_EVENT_FUTURE_TOLERANCE <= timedelta(minutes=5)
    assert source_event_time({"updated_at": past}, now=now) is not None
    assert source_event_time({"updated_at": near}, now=now) is not None
    assert source_event_time({"updated_at": far}, now=now) is None
    # a future product time falls back to a provable envelope time
    assert source_event_time({"updated_at": far}, past, now=now) == datetime.fromisoformat(past)
    # both in the future: order unknown
    assert source_event_time({"updated_at": far}, far, now=now) is None
    row = Product(tenant_id=1, external_id=PRODUCT_ID, title="x", extra_metadata={SOURCE_EVENT_AT_KEY: far})
    assert stored_source_event_time(row, now=now) is None
    row.extra_metadata = {SOURCE_EVENT_AT_KEY: past}
    assert stored_source_event_time(row, now=now) == datetime.fromisoformat(past)


# ── Flow: the reviewer's lockout scenario ─────────────────────────────────

def test_local_wall_clock_updated_at_does_not_lock_out_later_updates():
    session, tid, engine = _db()
    try:
        now = datetime.now(timezone.utc)
        store = {"body": _body(150)}
        svc = _service(session, tid, store)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(
                _body(150, updated_at=_riyadh_wall_clock_naive(now)), webhook_event_type="product.created"))
            store["body"] = _body(140)
            asyncio.run(svc.handle_product_webhook(
                _body(140, updated_at=_riyadh_wall_clock_naive(now)), webhook_event_type="product.updated"))
        row = _row(session, tid)
        assert row.price == "140.0"
        stamp = datetime.fromisoformat(row.extra_metadata[SOURCE_EVENT_AT_KEY])
        assert stamp <= datetime.now(timezone.utc) + SOURCE_EVENT_FUTURE_TOLERANCE
        # the periodic store sync reads a newer price: applied, not "stale"
        res = svc._apply_normalised_product(_normalise_product(_body(99)), "salla")
        session.commit()
        assert res.get("action") != "skipped_stale"
        assert _row(session, tid).price == "99.0"
        # a webhook timed by a correct envelope a moment later still applies
        store["body"] = _body(88)
        envelope = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(
                _body(88), webhook_event_type="product.updated", envelope_created_at=envelope))
        assert _row(session, tid).price == "88.0"
    finally:
        session.close(); engine.dispose()


def test_an_already_stored_future_stamp_does_not_lock_out_valid_updates():
    """A row written before future times were refused carries a stamp three
    hours ahead. It must not make the next read or webhook look older."""
    session, tid, engine = _db()
    try:
        store = {"body": _body(120)}
        svc = _service(session, tid, store)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(_body(120), webhook_event_type="product.created"))
        row = _row(session, tid)
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        row.extra_metadata = {**(row.extra_metadata or {}), SOURCE_EVENT_AT_KEY: future}
        session.commit()
        res = svc._apply_normalised_product(_normalise_product(_body(95)), "salla")
        session.commit()
        assert res.get("action") != "skipped_stale"
        row = _row(session, tid)
        assert row.price == "95.0"
        assert datetime.fromisoformat(row.extra_metadata[SOURCE_EVENT_AT_KEY]) <= datetime.now(timezone.utc)
        # and a webhook with a correct own time applies too
        store["body"] = _body(91)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(
                _body(91, updated_at=datetime.now(timezone.utc).isoformat()), webhook_event_type="product.updated"))
        assert _row(session, tid).price == "91.0"
    finally:
        session.close(); engine.dispose()


def test_an_older_event_still_never_replaces_a_newer_state():
    session, tid, engine = _db()
    try:
        now = datetime.now(timezone.utc)
        store = {"body": _body(130)}
        svc = _service(session, tid, store)
        with patch("services.whatsapp_catalog_sync.schedule_whatsapp_catalog_drain"):
            asyncio.run(svc.handle_product_webhook(
                _body(130, updated_at=(now - timedelta(minutes=1)).isoformat()), webhook_event_type="product.created"))
            asyncio.run(svc.handle_product_webhook(
                _body(170, updated_at=(now - timedelta(minutes=30)).isoformat()), webhook_event_type="product.updated"))
        assert _row(session, tid).price == "130.0"
    finally:
        session.close(); engine.dispose()
