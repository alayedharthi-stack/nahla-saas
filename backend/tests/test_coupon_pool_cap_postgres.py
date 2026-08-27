"""PostgreSQL concurrency tests for coupon pool per-level cap."""
from __future__ import annotations

import asyncio
import threading
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

from core.pg_advisory_lock import DedicatedAdvisoryLock
from database.models import Coupon, Tenant, TenantSettings
from services.coupon_generator import (
    LEVEL_TO_REPRESENTATIVE_SEGMENT,
    POOL_LOCK_NAMESPACE,
    CouponGeneratorService,
)
from services.crm_atoms import CrmStatus
from tests.order_customer_identity_postgres_fixtures import (
    _connect_engine,
    _ensure_a1_schema,
    _integration_required,
)

TEST_TENANT_COUPON_A = 991_101
TEST_TENANT_COUPON_B = 991_102
TEST_TENANT_COUPON_TWO_LEVEL = 991_110

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


def _make_fake_adapter(adapter_calls: list[dict], calls_lock: threading.Lock):
    counter = {"n": 0}

    async def fake_create_coupon(code: str, discount_type: str, discount_value: int, expiry_days: int):
        with calls_lock:
            counter["n"] += 1
            coupon_id = f"salla-{counter['n']}"
            adapter_calls.append({"code": code, "id": coupon_id})
        return {
            "id": coupon_id,
            "code": code,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat(),
        }

    async def fake_delete_coupon_by_id(coupon_id: str):
        return True

    async def fake_delete_coupon_by_code(code: str):
        return True

    return SimpleNamespace(
        create_coupon=fake_create_coupon,
        delete_coupon_by_id=fake_delete_coupon_by_id,
        delete_coupon_by_code=fake_delete_coupon_by_code,
    )


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


def _clear_tenant_coupons(session: Session, tenant_id: int) -> None:
    """Delete all coupons for a tenant so pool tests start from a clean slate."""
    session.query(Coupon).filter(Coupon.tenant_id == tenant_id).delete()
    session.commit()


def _new_session(engine):
    connection = engine.connect()
    session = sessionmaker(bind=connection, expire_on_commit=False)()
    return session, connection


async def _ensure_pool_once(
    engine,
    tenant_id: int,
    adapter_calls: list[dict],
    calls_lock: threading.Lock,
) -> tuple[dict, dict]:
    session, connection = _new_session(engine)
    try:
        _seed_tenant(session, tenant_id)
        svc = CouponGeneratorService(session, tenant_id)
        svc._get_adapter = lambda: _make_fake_adapter(adapter_calls, calls_lock)
        result = await svc.ensure_coupon_pool()
        session.commit()
        return result, svc._last_pool_outcomes
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


def _level_count(engine, tenant_id: int, level: str) -> int:
    session, connection = _new_session(engine)
    try:
        svc = CouponGeneratorService(session, tenant_id)
        return svc._count_pool_by_level(level)
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




def _add_pool_coupon(session: Session, tenant_id: int, level: str, code: str) -> None:
    """Insert a warm-pool coupon that counts toward _count_pool_by_level."""
    segment = LEVEL_TO_REPRESENTATIVE_SEGMENT[level]
    now = datetime.now(timezone.utc)
    metadata = {
        "source": "auto",
        "target_segment": segment,
        "used": "false",
        "salla_synced": "true",
        "active": "true",
        "coupon_level": level,
    }
    session.add(
        Coupon(
            tenant_id=tenant_id,
            code=code,
            discount_type="percentage",
            discount_value="10",
            expires_at=now + timedelta(days=2),
            extra_metadata=metadata,
            source_type="system",
            coupon_level=level,
        )
    )
def _pool_lock_key(tenant_id: int, level: str = "bronze") -> int:
    suffix = {"bronze": 0, "silver": 1, "gold": 2, "vip": 3}[level]
    return int(tenant_id) * 10 + suffix


def test_concurrent_ensure_coupon_pool_bronze_capped(postgres_engine) -> None:
    """Thread B skips bronze refill while Thread A holds the per-level advisory lock."""
    tenant_id = TEST_TENANT_COUPON_A
    session, connection = _new_session(postgres_engine)
    try:
        _seed_tenant(session, tenant_id)
        _clear_tenant_coupons(session, tenant_id)
        for level, codes in (
            ("silver", ["NHSL1", "NHSL2", "NHSL3"]),
            ("gold", ["NHGD1", "NHGD2", "NHGD3"]),
            ("vip", ["NHVP1", "NHVP2", "NHVP3"]),
        ):
            for code in codes:
                _add_pool_coupon(session, tenant_id, level, code)
        session.commit()
        assert _bronze_count(postgres_engine, tenant_id) == 0
        for level in ("silver", "gold", "vip"):
            assert _level_count(postgres_engine, tenant_id, level) == 3
    finally:
        session.close()
        connection.close()

    adapter_calls: list[dict] = []
    calls_lock = threading.Lock()
    a_holds = threading.Event()
    b_may_run = threading.Event()
    b_done = threading.Event()
    thread_b_outcomes: dict[str, str] = {}

    def _thread_a() -> None:
        hold_session, hold_conn = _new_session(postgres_engine)
        lock = DedicatedAdvisoryLock(
            hold_session,
            namespace=POOL_LOCK_NAMESPACE,
            level_key=_pool_lock_key(tenant_id, "bronze"),
        )
        try:
            assert lock.try_acquire() is True
            a_holds.set()
            b_may_run.wait(timeout=30)
            b_done.wait(timeout=30)
        finally:
            if lock.held:
                lock.release()
            hold_session.close()
            hold_conn.close()

    def _thread_b() -> None:
        a_holds.wait(timeout=30)
        b_may_run.set()
        _created, outcomes = asyncio.run(
            _ensure_pool_once(postgres_engine, tenant_id, adapter_calls, calls_lock)
        )
        thread_b_outcomes.update(outcomes)
        b_done.set()

    t_a = threading.Thread(target=_thread_a)
    t_b = threading.Thread(target=_thread_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=60)
    t_b.join(timeout=60)

    assert thread_b_outcomes.get("bronze") == "lock_held"
    assert len(adapter_calls) == 0

    asyncio.run(_ensure_pool_once(postgres_engine, tenant_id, adapter_calls, calls_lock))
    assert _bronze_count(postgres_engine, tenant_id) == 3
    codes = [row["code"] for row in adapter_calls]
    assert len(codes) == len(set(codes))
    assert len(adapter_calls) == 3


def test_pool_provenance_jsonb_postgres(postgres_engine) -> None:
    """PostgreSQL JSONB provenance filters for warm-pool counting."""
    tenant_id = TEST_TENANT_COUPON_A + 50
    session, connection = _new_session(postgres_engine)
    try:
        _seed_tenant(session, tenant_id)
        now = datetime.now(timezone.utc)
        base_meta = {
            "source": "auto",
            "target_segment": CrmStatus.NEW,
            "used": "false",
            "salla_synced": "true",
            "active": "true",
            "coupon_level": "bronze",
        }

        rows = [
            Coupon(
                tenant_id=tenant_id,
                code="NHM01",
                discount_type="percentage",
                discount_value="10",
                expires_at=now + timedelta(days=2),
                extra_metadata={**base_meta, "source": "auto"},
                source_type="manual",
                coupon_level="bronze",
            ),
            Coupon(
                tenant_id=tenant_id,
                code="NHM02",
                discount_type="percentage",
                discount_value="10",
                expires_at=now + timedelta(days=2),
                extra_metadata={**base_meta, "source": "salla"},
                source_type="imported",
                coupon_level="bronze",
            ),
            Coupon(
                tenant_id=tenant_id,
                code="NHM03",
                discount_type="percentage",
                discount_value="10",
                expires_at=now + timedelta(days=2),
                extra_metadata=dict(base_meta),
                source_type="system",
                coupon_level="bronze",
            ),
            Coupon(
                tenant_id=tenant_id,
                code="NHM04",
                discount_type="percentage",
                discount_value="10",
                expires_at=now + timedelta(days=2),
                extra_metadata={**base_meta, "source": "auto"},
                source_type="",
                coupon_level="bronze",
            ),
            Coupon(
                tenant_id=tenant_id,
                code="NHM05",
                discount_type="percentage",
                discount_value="10",
                expires_at=now + timedelta(days=2),
                extra_metadata={**base_meta, "source": "merchant_campaign"},
                source_type="manual",
                coupon_level="bronze",
            ),
        ]
        session.add_all(rows)
        session.commit()

        svc = CouponGeneratorService(session, tenant_id)
        assert svc._count_pool_by_level("bronze") == 2
    finally:
        session.close()
        connection.close()


def test_cross_tenant_pool_isolation(postgres_engine) -> None:
    session, connection = _new_session(postgres_engine)
    try:
        for tenant_id in (TEST_TENANT_COUPON_A, TEST_TENANT_COUPON_B):
            _seed_tenant(session, tenant_id)
            _clear_tenant_coupons(session, tenant_id)
    finally:
        session.close()
        connection.close()

    adapter_a: list[dict] = []
    adapter_b: list[dict] = []
    lock_a = threading.Lock()
    lock_b = threading.Lock()

    asyncio.run(_ensure_pool_once(postgres_engine, TEST_TENANT_COUPON_A, adapter_a, lock_a))
    asyncio.run(_ensure_pool_once(postgres_engine, TEST_TENANT_COUPON_B, adapter_b, lock_b))

    assert _bronze_count(postgres_engine, TEST_TENANT_COUPON_A) == 3
    assert _bronze_count(postgres_engine, TEST_TENANT_COUPON_B) == 3
    for tenant_id in (TEST_TENANT_COUPON_A, TEST_TENANT_COUPON_B):
        for level in ("silver", "gold", "vip"):
            assert _level_count(postgres_engine, tenant_id, level) == 3

    codes_a = set(_all_codes(postgres_engine, TEST_TENANT_COUPON_A))
    codes_b = set(_all_codes(postgres_engine, TEST_TENANT_COUPON_B))
    assert len(codes_a) == 12
    assert len(codes_b) == 12
    assert codes_a.isdisjoint(codes_b)


def test_two_levels_do_not_corrupt_each_other(postgres_engine) -> None:
    tenant_id = TEST_TENANT_COUPON_TWO_LEVEL
    session, connection = _new_session(postgres_engine)
    try:
        _seed_tenant(session, tenant_id)
        _clear_tenant_coupons(session, tenant_id)
    finally:
        session.close()
        connection.close()

    adapter_calls: list[dict] = []
    calls_lock = threading.Lock()
    asyncio.run(_ensure_pool_once(postgres_engine, tenant_id, adapter_calls, calls_lock))

    session, connection = _new_session(postgres_engine)
    try:
        svc = CouponGeneratorService(session, tenant_id)
        bronze = svc._count_pool_by_level("bronze")
        silver = svc._count_pool_by_level("silver")
        gold = svc._count_pool_by_level("gold")
        vip = svc._count_pool_by_level("vip")
        assert bronze == 3
        assert silver == 3
        assert gold == 3
        assert vip == 3
        assert bronze + silver + gold + vip == 12
        assert len(adapter_calls) == 12
    finally:
        session.close()
        connection.close()
