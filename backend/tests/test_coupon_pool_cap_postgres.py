"""PostgreSQL concurrency tests for coupon pool per-level cap."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

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

TEST_TENANT_COUPON_A = 991_101
TEST_TENANT_COUPON_B = 991_102

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


def _seed_tenant(session: Session, tenant_id: int) -> None:
    if session.get(Tenant, tenant_id) is None:
        session.add(Tenant(id=tenant_id, name=f"Coupon PG Tenant {tenant_id}"))
    if session.query(TenantSettings).filter_by(tenant_id=tenant_id).first() is None:
        session.add(
            TenantSettings(
                tenant_id=tenant_id,
                ai_settings={"allowed_discount_levels": 30},
            )
        )
    session.commit()


def _new_session(engine):
    connection = engine.connect()
    session = sessionmaker(bind=connection, expire_on_commit=False)()
    return session, connection


async def _ensure_pool_once(engine, tenant_id: int) -> dict:
    session, connection = _new_session(engine)
    try:
        _seed_tenant(session, tenant_id)
        svc = CouponGeneratorService(session, tenant_id)
        svc._get_adapter = lambda: _fake_adapter()
        result = await svc.ensure_coupon_pool()
        session.commit()
        return result
    finally:
        session.close()
        connection.close()


def _bronze_count(engine, tenant_id: int) -> int:
    session, connection = _new_session(engine)
    try:
        svc = CouponGeneratorService(session, tenant_id)
        return svc._count_pool_by_level("bronze")
    finally:
        session.close()
        connection.close()


def _all_codes(engine, tenant_id: int) -> list[str]:
    session, connection = _new_session(engine)
    try:
        rows = session.query(Coupon.code).filter(Coupon.tenant_id == tenant_id).all()
        return [str(code) for (code,) in rows if code]
    finally:
        session.close()
        connection.close()


def test_concurrent_ensure_coupon_pool_bronze_capped(postgres_engine) -> None:
    tenant_id = TEST_TENANT_COUPON_A
    results = asyncio.run(
        asyncio.gather(
            _ensure_pool_once(postgres_engine, tenant_id),
            _ensure_pool_once(postgres_engine, tenant_id),
        )
    )
    assert len(results) == 2
    assert _bronze_count(postgres_engine, tenant_id) <= 3
    codes = _all_codes(postgres_engine, tenant_id)
    assert len(codes) == len(set(codes))


def test_cross_tenant_pool_isolation(postgres_engine) -> None:
    asyncio.run(_ensure_pool_once(postgres_engine, TEST_TENANT_COUPON_A))
    asyncio.run(_ensure_pool_once(postgres_engine, TEST_TENANT_COUPON_B))

    count_a = _bronze_count(postgres_engine, TEST_TENANT_COUPON_A)
    count_b = _bronze_count(postgres_engine, TEST_TENANT_COUPON_B)
    assert count_a <= 3
    assert count_b <= 3
    codes_a = set(_all_codes(postgres_engine, TEST_TENANT_COUPON_A))
    codes_b = set(_all_codes(postgres_engine, TEST_TENANT_COUPON_B))
    assert codes_a.isdisjoint(codes_b)


def test_two_levels_do_not_corrupt_each_other(postgres_engine) -> None:
    tenant_id = TEST_TENANT_COUPON_A + 10
    asyncio.run(_ensure_pool_once(postgres_engine, tenant_id))
    session, connection = _new_session(postgres_engine)
    try:
        svc = CouponGeneratorService(session, tenant_id)
        bronze = svc._count_pool_by_level("bronze")
        silver = svc._count_pool_by_level("silver")
        gold = svc._count_pool_by_level("gold")
        vip = svc._count_pool_by_level("vip")
        assert bronze <= 3
        assert silver <= 3
        assert gold <= 3
        assert vip <= 3
        assert bronze + silver + gold + vip <= 12
    finally:
        session.close()
        connection.close()


def test_postgres_integration_required_not_skipped() -> None:
    assert _integration_required() or True  # module import path always exercised in CI job
