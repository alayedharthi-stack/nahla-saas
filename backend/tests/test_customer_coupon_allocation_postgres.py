"""PostgreSQL allocation and same-customer customer-request concurrency."""
from __future__ import annotations

import asyncio
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session, sessionmaker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"
_DATABASE = _REPO_ROOT / "database"
for _entry in (str(_REPO_ROOT), str(_BACKEND), str(_DATABASE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from database.models import Coupon, Customer, Order, Tenant, TenantSettings
from routers.coupons import DEFAULT_COUPON_LEVELS, _normalise_levels
from services.coupon_generator import CouponGeneratorService
from services.customer_intelligence import normalize_phone
from services import customer_request_coupon_service as coupon_request_mod
from services.customer_request_coupon_service import (
    ISSUED_REASON_CUSTOMER_REQUEST,
    issue_customer_coupon,
)
from tests.order_customer_identity_postgres_fixtures import (
    _connect_engine,
    _ensure_a1_schema,
    _integration_required,
)

TEST_TENANT = 991_880
TEST_TENANT_SAME_CUSTOMER_POOL = 991_881
TEST_TENANT_SAME_CUSTOMER_ON_DEMAND = 991_882
PHONE_POOL = "+966500000881"
PHONE_ON_DEMAND = "+966500000882"

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


def _fake_adapter():
    async def fake_create_coupon(code: str, discount_type: str, discount_value: int, expiry_days: int):
        return {
            "code": code,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat(),
        }

    async def fake_delete(code: str):
        return True

    return SimpleNamespace(create_coupon=fake_create_coupon, delete_coupon_by_code=fake_delete)


def _dashboard(*, pool_mode: str = "pool_first") -> dict:
    return {
        "levels": _normalise_levels(DEFAULT_COUPON_LEVELS),
        "ai_policy": {
            "enabled": True,
            "allowed_levels": ["bronze", "silver"],
            "min_remaining_hours": 0,
            "pool_mode": pool_mode,
        },
        "rules": [{"id": "first_purchase", "enabled": False}],
        "global_defaults": {"min_order_amount": 0},
    }


def _seed(session: Session, tenant_id: int) -> None:
    if session.get(Tenant, tenant_id) is None:
        session.add(Tenant(id=tenant_id, name=f"Customer coupon PG {tenant_id}"))
    settings = session.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
    if settings is None:
        settings = TenantSettings(tenant_id=tenant_id)
        session.add(settings)
    session.query(Coupon).filter(Coupon.tenant_id == tenant_id).delete()
    session.commit()


def _seed_customer_request_tenant(
    session: Session,
    tenant_id: int,
    *,
    pool_mode: str,
    countable: int,
    phone: str,
) -> Customer:
    if session.get(Tenant, tenant_id) is None:
        session.add(Tenant(id=tenant_id, name=f"Customer coupon PG {tenant_id}"))
        session.flush()
    settings = session.query(TenantSettings).filter_by(tenant_id=tenant_id).first()
    dash = _dashboard(pool_mode=pool_mode)
    if settings is None:
        settings = TenantSettings(
            tenant_id=tenant_id,
            extra_metadata={"coupons_dashboard": dash},
        )
        session.add(settings)
    else:
        meta = dict(settings.extra_metadata or {})
        meta["coupons_dashboard"] = dash
        settings.extra_metadata = meta
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(settings, "extra_metadata")
    session.query(Coupon).filter(Coupon.tenant_id == tenant_id).delete()
    session.query(Order).filter(Order.tenant_id == tenant_id).delete()
    normalized = normalize_phone(phone) or phone
    customer = (
        session.query(Customer)
        .filter(Customer.tenant_id == tenant_id, Customer.normalized_phone == normalized)
        .first()
    )
    if customer is None:
        customer = Customer(
            tenant_id=tenant_id,
            name="أحمد سالم",
            phone=phone,
            normalized_phone=normalized,
        )
        session.add(customer)
        session.flush()
    for _ in range(countable):
        session.add(
            Order(
                tenant_id=tenant_id,
                status="delivered",
                total="50",
                customer_info={"mobile": phone, "name": "أحمد سالم"},
                is_abandoned=False,
            )
        )
    session.commit()
    session.refresh(customer)
    return customer


def _add_pool_coupon(session: Session, tenant_id: int, code: str, level: str) -> Coupon:
    row = Coupon(
        tenant_id=tenant_id,
        code=code,
        discount_type="percentage",
        discount_value="20",
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        extra_metadata={
            "source": "auto",
            "target_segment": "new",
            "used": "false",
            "salla_synced": "true",
            "category": "auto",
            "active": True,
            "coupon_level": level,
        },
        coupon_level=level,
        source_type="system",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


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

    results: list[Optional[int]] = [None, None]
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


def test_same_customer_pool_concurrency_reuses_one_assignment(postgres_engine) -> None:
    setup = sessionmaker(bind=postgres_engine, expire_on_commit=False)()
    try:
        customer = _seed_customer_request_tenant(
            setup,
            TEST_TENANT_SAME_CUSTOMER_POOL,
            pool_mode="pool_first",
            countable=1,
            phone=PHONE_POOL,
        )
        customer_id = int(customer.id)
        c1 = _add_pool_coupon(setup, TEST_TENANT_SAME_CUSTOMER_POOL, "NHP01", "bronze")
        c2 = _add_pool_coupon(setup, TEST_TENANT_SAME_CUSTOMER_POOL, "NHP02", "bronze")
        pool_ids = {int(c1.id), int(c2.id)}
    finally:
        setup.close()

    results: list[Optional[int]] = [None, None]
    reasons: list[Optional[str]] = [None, None]
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(index: int) -> None:
        connection = postgres_engine.connect()
        session = sessionmaker(bind=connection, expire_on_commit=False)()
        try:
            barrier.wait(timeout=10)
            issued = asyncio.run(
                issue_customer_coupon(
                    session,
                    TEST_TENANT_SAME_CUSTOMER_POOL,
                    customer_id,
                    allow_issuance=True,
                )
            )
            results[index] = issued.coupon_id
            reasons[index] = issued.reason_code
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()
            connection.close()

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=40)
    t2.join(timeout=40)
    assert not errors, errors
    assert results[0] is not None and results[1] is not None, reasons
    assert results[0] == results[1]
    assert results[0] in pool_ids

    verify = sessionmaker(bind=postgres_engine, expire_on_commit=False)()
    try:
        owned = (
            verify.query(Coupon)
            .filter(Coupon.tenant_id == TEST_TENANT_SAME_CUSTOMER_POOL)
            .all()
        )
        assigned = [
            row
            for row in owned
            if str((row.extra_metadata or {}).get("issued_reason") or "")
            == ISSUED_REASON_CUSTOMER_REQUEST
            and str((row.extra_metadata or {}).get("customer_id")) == str(customer_id)
        ]
        assert len(assigned) == 1
        assert int(assigned[0].id) == results[0]
        unassigned = [row for row in owned if row.id != assigned[0].id]
        assert len(unassigned) == 1
        assert (unassigned[0].extra_metadata or {}).get("customer_id") in (None, "", "null")
    finally:
        verify.close()


def test_same_customer_on_demand_concurrency_creates_at_most_one(postgres_engine) -> None:
    setup = sessionmaker(bind=postgres_engine, expire_on_commit=False)()
    try:
        customer = _seed_customer_request_tenant(
            setup,
            TEST_TENANT_SAME_CUSTOMER_ON_DEMAND,
            pool_mode="on_demand_only",
            countable=1,
            phone=PHONE_ON_DEMAND,
        )
        customer_id = int(customer.id)
    finally:
        setup.close()

    results: list[Optional[int]] = [None, None]
    reasons: list[Optional[str]] = [None, None]
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(index: int) -> None:
        connection = postgres_engine.connect()
        session = sessionmaker(bind=connection, expire_on_commit=False)()
        try:
            barrier.wait(timeout=10)
            issued = asyncio.run(
                issue_customer_coupon(
                    session,
                    TEST_TENANT_SAME_CUSTOMER_ON_DEMAND,
                    customer_id,
                    allow_issuance=True,
                )
            )
            results[index] = issued.coupon_id
            reasons[index] = issued.reason_code
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()
            connection.close()

    with patch.object(
        coupon_request_mod.CouponGeneratorService,
        "_get_adapter",
        lambda self: _fake_adapter(),
    ):
        t1 = threading.Thread(target=worker, args=(0,))
        t2 = threading.Thread(target=worker, args=(1,))
        t1.start()
        t2.start()
        t1.join(timeout=40)
        t2.join(timeout=40)

    assert not errors, errors
    assert results[0] is not None and results[1] is not None, reasons
    assert results[0] == results[1]

    verify = sessionmaker(bind=postgres_engine, expire_on_commit=False)()
    try:
        created = (
            verify.query(Coupon)
            .filter(Coupon.tenant_id == TEST_TENANT_SAME_CUSTOMER_ON_DEMAND)
            .all()
        )
        customer_request_rows = [
            row
            for row in created
            if str((row.extra_metadata or {}).get("issued_reason") or "")
            == ISSUED_REASON_CUSTOMER_REQUEST
        ]
        assert len(customer_request_rows) == 1
        assert int(customer_request_rows[0].id) == results[0]
        assert int((customer_request_rows[0].extra_metadata or {}).get("customer_id")) == customer_id
    finally:
        verify.close()
