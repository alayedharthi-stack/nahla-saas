"""Salla tracking ingestion: monotonic updates and tenant isolation."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

_REPO = Path(__file__).resolve().parents[2]
for path in (_REPO, _REPO / "backend", _REPO / "database"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from models import Base, Customer, Order, OrderShipment, Tenant  # noqa: E402
from core.order_shipment_service import get_order_shipment  # noqa: E402
from services.salla_shipment_tracking import (  # noqa: E402
    TRACKING_AVAILABLE,
    TRACKING_STORED_REFRESH_FAILED,
    refresh_order_tracking,
)


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(type_, compiler, **kw):
    """SQLite has no JSONB, so render it as JSON when SQLite emits the DDL.

    This used to be a ``before_create`` listener on ``Base.metadata`` that
    rewrote ``column.type`` in place. Two things made that reach far outside
    this module: the listener is registered at import, so merely collecting
    this file arms it for the whole session, and it fired for every engine
    rather than only SQLite — with no restore. After one in-memory database
    was built, ``Coupon.metadata`` and every other JSONB column stayed
    ``JSON()`` for the rest of the process, so any PostgreSQL database another
    suite created afterwards silently lost ``jsonb``: ``?``, ``@>`` and GIN
    indexes stopped existing on it.

    Compiling the type for one dialect touches no shared column object, so a
    PostgreSQL ``create_all`` in the same process still gets ``jsonb``.
    """
    return "JSON"


class _SallaTrackingAdapter:
    def __init__(self, tracking: dict, *, fail_list: bool = False):
        self.tracking = tracking
        self.fail_list = fail_list

    async def get_shipments(self, *, order_id=None, from_date=None):
        if self.fail_list:
            raise RuntimeError("salla_unavailable")
        return [{"id": self.tracking["id"], "order_id": order_id}]

    async def get_shipment_tracking(self, shipment_id):
        assert str(shipment_id) == str(self.tracking["id"])
        return dict(self.tracking)


def _run(coro):
    return asyncio.run(coro)


def _tracking(*, status: str, created_at=None, station=None, city=None) -> dict:
    event = {"status": status, "note": f"{status} note"}
    if created_at is not None:
        event["create_at"] = created_at
    if station is not None:
        event["station"] = station
    if city is not None:
        event["city"] = city
    return {
        "id": "salla-shipment-1",
        "order_id": "salla-order-1",
        "type": "shipment",
        "courier_name": "DHL",
        "tracking_number": "TRACK-ONLY-AS-DATA",
        "tracking_link": "https://carrier.example/track/TRACK-ONLY-AS-DATA",
        "status": status,
        "history": [event],
    }


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    first = Tenant(name="Tenant 1", is_active=True)
    second = Tenant(name="Tenant 2", is_active=True)
    db.add_all([first, second])
    db.flush()
    one = Order(tenant_id=first.id, external_id="salla-order-1", status="paid")
    two = Order(tenant_id=second.id, external_id="salla-order-1", status="paid")
    db.add_all([one, two])
    db.commit()
    return db, engine, first, second, one, two


def test_updates_latest_event_without_inferred_location_or_time():
    db, engine, first, second, one, two = _db()
    try:
        adapter = _SallaTrackingAdapter(_tracking(status="delivering"))
        result = _run(refresh_order_tracking(
            db, tenant_id=first.id, order=one, adapter=adapter,
            observed_via="test",
        ))
        db.commit()

        shipment = db.query(OrderShipment).filter_by(tenant_id=first.id, order_id=one.id).one()
        assert result.state == TRACKING_AVAILABLE
        assert shipment.status == "delivering"
        assert shipment.carrier == "DHL"
        assert shipment.tracking_number == "TRACK-ONLY-AS-DATA"
        assert shipment.latest_event == {
            "status": "delivering", "note": "delivering note", "occurred_at": None,
        }
        assert shipment.source_event_at is None
        assert shipment.last_verified_at is not None
        # A same-looking order in another tenant is not read or written.
        assert db.query(OrderShipment).filter_by(tenant_id=second.id, order_id=two.id).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_duplicate_and_out_of_order_events_cannot_regress_newer_status():
    db, engine, first, _, one, _ = _db()
    try:
        adapter = _SallaTrackingAdapter(_tracking(
            status="delivered",
            created_at={"date": "2026-09-20 15:00:00", "timezone": "Asia/Riyadh"},
            station="Central depot",
            city="Riyadh",
        ))
        _run(refresh_order_tracking(db, tenant_id=first.id, order=one, adapter=adapter, observed_via="test"))
        db.commit()

        # Same shipment arrives later through a duplicate delivery path with
        # an equal source timestamp. It must not replace delivered.
        adapter.tracking = _tracking(
            status="delivering",
            created_at={"date": "2026-09-20 15:00:00", "timezone": "Asia/Riyadh"},
            station="Older depot",
            city="Jeddah",
        )
        _run(refresh_order_tracking(db, tenant_id=first.id, order=one, adapter=adapter, observed_via="delayed_webhook"))
        # And an explicitly older source event is equally unable to regress it.
        adapter.tracking = _tracking(
            status="delivering",
            created_at={"date": "2026-09-20 10:00:00", "timezone": "Asia/Riyadh"},
            station="Older depot",
            city="Jeddah",
        )
        _run(refresh_order_tracking(db, tenant_id=first.id, order=one, adapter=adapter, observed_via="delayed_poller"))
        db.commit()

        shipment = db.query(OrderShipment).filter_by(tenant_id=first.id, order_id=one.id).one()
        assert shipment.status == "delivered"
        assert shipment.latest_event["station"] == "Central depot"
        assert shipment.latest_event["city"] == "Riyadh"
        assert shipment.latest_event["status"] == "delivered"
    finally:
        db.close()
        engine.dispose()


def test_source_failure_keeps_stored_data_and_never_marks_it_fresh():
    db, engine, first, _, one, _ = _db()
    try:
        working = _SallaTrackingAdapter(_tracking(
            status="delivering",
            created_at={"date": "2026-09-20 10:00:00", "timezone": "Asia/Riyadh"},
        ))
        _run(refresh_order_tracking(db, tenant_id=first.id, order=one, adapter=working, observed_via="test"))
        db.commit()
        shipment = db.query(OrderShipment).filter_by(tenant_id=first.id, order_id=one.id).one()
        verified_at = shipment.last_verified_at

        failed = _SallaTrackingAdapter(working.tracking, fail_list=True)
        result = _run(refresh_order_tracking(
            db, tenant_id=first.id, order=one, adapter=failed,
            observed_via="poller",
        ))
        db.commit()
        db.refresh(shipment)
        db.refresh(one)

        assert result.state == TRACKING_STORED_REFRESH_FAILED
        assert shipment.status == "delivering"
        assert shipment.last_verified_at == verified_at
        assert one.extra_metadata["salla_tracking"]["state"] == TRACKING_STORED_REFRESH_FAILED
        assert one.extra_metadata["salla_tracking"]["last_refresh_failed_at"]
    finally:
        db.close()
        engine.dispose()


def test_tenant_or_customer_mismatch_cannot_read_or_refresh_tracking():
    db, engine, first, second, one, _ = _db()
    try:
        adapter = _SallaTrackingAdapter(_tracking(status="delivering"))
        with pytest.raises(ValueError, match="tenant_order_mismatch"):
            _run(refresh_order_tracking(
                db, tenant_id=second.id, order=one, adapter=adapter,
                observed_via="wrong_tenant",
            ))
        # The rejected tenant reaches neither the shipment list nor tracking API.
        assert db.query(OrderShipment).count() == 0

        owner = Customer(tenant_id=first.id, name="Owner")
        another_customer = Customer(tenant_id=first.id, name="Another")
        db.add_all([owner, another_customer])
        db.flush()
        one.customer_id = owner.id
        _run(refresh_order_tracking(
            db, tenant_id=first.id, order=one, adapter=adapter,
            observed_via="owner_refresh",
        ))
        db.commit()

        assert get_order_shipment(db, first.id, one.id, customer_id=owner.id) is not None
        assert get_order_shipment(db, first.id, one.id, customer_id=another_customer.id) is None
    finally:
        db.close()
        engine.dispose()


def test_building_the_sqlite_schema_leaves_jsonb_intact_for_postgresql():
    """Collecting this module must not cost another suite its ``jsonb``.

    The SQLite shim lives on a type shared by every model in the process. When
    it rewrote ``column.type`` in place, the first in-memory database built
    here turned every JSONB column into ``JSON`` for good — so a PostgreSQL
    database another suite created later came up with ``json`` columns, and
    ``metadata ? 'key'`` stopped existing on it. That surfaced as five
    unrelated failures in the coupon eligibility trace, which is the only
    suite that asks for a jsonb-only operator.

    Asserting on the shared column after a SQLite ``create_all`` is what holds
    the shim inside SQLite.
    """
    from models import Coupon

    _db()

    assert isinstance(Coupon.__table__.c["metadata"].type, JSONB)
    rendered = Coupon.__table__.c["metadata"].type.compile(
        dialect=postgresql.dialect())
    assert rendered.upper() == "JSONB"
