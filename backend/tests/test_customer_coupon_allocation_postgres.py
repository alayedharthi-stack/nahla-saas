"""PostgreSQL SKIP LOCKED allocation for customer-request coupons."""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_DATABASE = _REPO_ROOT / "database"
for _entry in (str(_REPO_ROOT), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from database.models import Coupon, Tenant, TenantSettings
from services.coupon_generator import CouponGeneratorService
from tests.order_customer_identity_postgres_fixtures import (
    _connect_engine,
    _ensure_a1_schema,
    _integration_required,
)

TEST_TENANT = 991_880

if not _integration_required():
    pytest.skip(
        "PostgreSQL integration tests require A1_PG_INTEGRATION_REQUIRED=1",
        allow_module_level=True,
    )

pytestmark = pytest.mark.usefixtures("postgres_engine")


@pytest.fixture(scope="module")
def postgres_engine():
    engine = _connect_engine()
    _ensure_a1_schema(engine)
    yield engine
    engine.dispose()


def _seed(session: Session, tenant_id: int) -> None:
    if session.get(Tenant, tenant_id) is None:
        session.add(Tenant(id=tenant_id, name=f"Customer coupon PG {tenant_id}"))
    settings = session.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
    if settings is None:
        settings = TenantSettings(tenant_id=tenant_id)
        session.add(settings)
    session.query(Coupon).filter(Coupon.tenant_id == tenant_id).delete()
    session.commit()


def test_concurrent_pick_coupon_for_level_does_not_double_assign(postgres_engine) -> None:
    setup = sessionmaker(bind=postgres_engine, expire_on_commit=False)()
    try:
        _seed(setup, TEST_TENANT)
        setup.add(
            Coupon(
                tenant_id=TEST_TENANT,
                code="NHLK1",
                discount_type="percentage",
                discount_value="20",
                expires_at=datetime.now(timezone.utc) + timedelta(days=3),
                extra_metadata={
                    "source": "auto",
                    "target_segment": "vip",
                    "used": "false",
                    "salla_synced": "true",
                    "active": True,
                    "coupon_level": "gold",
                },
                coupon_level="gold",
                source_type="system",
            )
        )
        setup.commit()
    finally:
        setup.close()

    results: list[int | None] = [None, None]
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(index: int, customer_id: int) -> None:
        connection = postgres_engine.connect()
        session = sessionmaker(bind=connection, expire_on_commit=False)()
        try:
            barrier.wait(timeout=10)
            svc = CouponGeneratorService(session, TEST_TENANT)
            coupon = svc.pick_coupon_for_level("gold", customer_id, for_channel="campaign")
            results[index] = int(coupon.id) if coupon is not None else None
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()
            connection.close()

    t1 = threading.Thread(target=worker, args=(0, 101))
    t2 = threading.Thread(target=worker, args=(1, 202))
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)
    assert not errors, errors
    assigned = [row for row in results if row is not None]
    assert len(assigned) == 1
    assert results.count(None) == 1

    verify = sessionmaker(bind=postgres_engine, expire_on_commit=False)()
    try:
        row = verify.query(Coupon).filter(Coupon.tenant_id == TEST_TENANT, Coupon.code == "NHLK1").one()
        owner = (row.extra_metadata or {}).get("customer_id")
        assert int(owner) in {101, 202}
    finally:
        verify.close()
