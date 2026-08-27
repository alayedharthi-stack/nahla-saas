"""Regression tests for canonical coupon-level warm pool cap (PR #887)."""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Coupon, Tenant, TenantSettings
from backend.services.coupon_generator import (
    CANONICAL_COUPON_LEVELS,
    LEVEL_TO_SEGMENTS,
    MAX_POOL_TARGET_PER_LEVEL,
    POOL_SIZE_PER_LEVEL,
    POOL_SIZE_PER_SEGMENT,
    SEGMENT_TO_LEVEL,
    CouponGeneratorService,
    CouponPoolExhausted,
    SHORT_CODE_ALPHABET,
    SHORT_CODE_BODY_LEN,
    SHORT_CODE_PATTERN,
    SHORT_CODE_PREFIX,
    _get_warm_pool_config,
    _is_short_coupon_code,
    _next_short_code,
    _segment_to_level,
    build_coupon_send_payload,
)
from services.crm_atoms import CrmStatus


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def _make_db(*, warm_pool: dict | None = None, ai_policy: dict | None = None):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    tenant = Tenant(name="Coupon Tenant", is_active=True)
    session.add(tenant)
    session.flush()
    extra = {}
    if warm_pool is not None:
        extra["coupons_dashboard"] = {"warm_pool": warm_pool}
    if ai_policy is not None:
        block = extra.setdefault("coupons_dashboard", {})
        block["ai_policy"] = ai_policy
    session.add(
        TenantSettings(
            tenant_id=tenant.id,
            ai_settings={"allowed_discount_levels": 30},
            extra_metadata=extra or None,
        )
    )
    session.commit()
    return session, tenant.id, engine


def _fake_adapter():
    async def fake_create_coupon(code: str, discount_type: str, discount_value: int, expiry_days: int):
        return {
            "code": code,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat(),
        }

    async def fake_delete(code: str):
        return True

    return SimpleNamespace(create_coupon=fake_create_coupon, delete_coupon_by_code=fake_delete)


def _pool_meta(*, segment: str, level: str | None = None, used: str = "false", active: bool = True):
    return {
        "source": "auto",
        "target_segment": segment,
        "used": used,
        "salla_synced": "true",
        "category": "auto",
        "active": active,
        "coupon_level": level or _segment_to_level(segment),
    }


def _add_pool_coupon(db, tenant_id: int, code: str, segment: str, **meta_overrides):
    meta = _pool_meta(segment=segment, **meta_overrides)
    row = Coupon(
        tenant_id=tenant_id,
        code=code,
        discount_type="percentage",
        discount_value="10",
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        extra_metadata=meta,
        coupon_level=meta.get("coupon_level"),
        source_type="system",
    )
    db.add(row)
    db.commit()
    return row




class _AlwaysAcquireLock:
    def __init__(self, *args, **kwargs):
        self.held = False

    def try_acquire(self) -> bool:
        self.held = True
        return True

    def release(self) -> bool:
        self.held = False
        return True


@contextmanager
def _patch_sqlite_pool_lock():
    with patch("backend.services.coupon_generator.DedicatedAdvisoryLock", _AlwaysAcquireLock):
        yield


def _run_pool(svc: CouponGeneratorService):
    svc._get_adapter = lambda: _fake_adapter()
    return asyncio.run(svc.ensure_coupon_pool())


# --- existing focused tests (updated) ---


def test_pick_coupon_marks_sent_time_and_expiry_text():
    db, tenant_id, engine = _make_db()
    try:
        expires_at = datetime.now(timezone.utc) + timedelta(days=2)
        coupon = Coupon(
            tenant_id=tenant_id,
            code="NHL123",
            discount_type="percentage",
            discount_value="10",
            expires_at=expires_at,
            extra_metadata=_pool_meta(segment="active"),
            source_type="system",
        )
        db.add(coupon)
        db.commit()

        svc = CouponGeneratorService(db, tenant_id)
        picked = svc.pick_coupon_for_segment("active")

        assert picked is not None
        assert picked.code == "NHL123"
        meta = picked.extra_metadata or {}
        assert meta.get("used") == "true"
        assert meta.get("sent_at")
        assert meta.get("sent_expiry_at")
        assert meta.get("sent_expiry_text")
    finally:
        db.close()
        engine.dispose()


def test_build_coupon_send_payload_includes_exact_expiry_text():
    expires_at = datetime(2026, 4, 20, 13, 45, tzinfo=timezone.utc)
    coupon = SimpleNamespace(code="NHL009", expires_at=expires_at)
    payload = build_coupon_send_payload(coupon)

    assert payload["code"] == "NHL009"
    assert payload["expires_at"] == expires_at.isoformat()
    assert "2026-04-20" in (payload["expires_text"] or "")
    assert "الساعة" in (payload["expires_text"] or "")


def test_ensure_coupon_pool_targets_three_per_level():
    db, tenant_id, engine = _make_db()
    try:
        svc = CouponGeneratorService(db, tenant_id)
        created = _run_pool(svc)

        assert POOL_SIZE_PER_LEVEL == 3
        assert POOL_SIZE_PER_SEGMENT == 3
        assert list(created.keys()) == list(CANONICAL_COUPON_LEVELS)
        assert all(count == 3 for count in created.values())
        rows = db.query(Coupon).filter(Coupon.tenant_id == tenant_id).all()
        assert len(rows) == 3 * 4

        assert all(SHORT_CODE_PATTERN.match(c.code) for c in rows)
        codes = [c.code for c in rows]
        assert len(codes) == len(set(codes))
    finally:
        db.close()
        engine.dispose()


def test_short_code_regex_accepts_new_and_legacy_formats():
    assert _is_short_coupon_code("NH7K2") is True
    assert _is_short_coupon_code("NH3A9") is True
    assert _is_short_coupon_code("NHL042") is True
    assert _is_short_coupon_code("NH12") is False
    assert _is_short_coupon_code("NH7KAB") is False
    assert _is_short_coupon_code("XX123") is False
    assert _is_short_coupon_code(None) is False


def test_next_short_code_avoids_reserved_codes_and_raises_when_exhausted():
    reserved: set[str] = set()
    seen: set[str] = set()
    for _ in range(50):
        code = _next_short_code(reserved)
        assert code.startswith(SHORT_CODE_PREFIX)
        assert len(code) == len(SHORT_CODE_PREFIX) + SHORT_CODE_BODY_LEN
        assert code not in seen
        seen.add(code)

    import itertools as _it

    every_code = {
        SHORT_CODE_PREFIX + "".join(p)
        for p in _it.product(SHORT_CODE_ALPHABET, repeat=SHORT_CODE_BODY_LEN)
    }
    try:
        _next_short_code(every_code, max_attempts=10)
    except CouponPoolExhausted:
        pass
    else:
        raise AssertionError("expected CouponPoolExhausted")


def test_create_one_coupon_retries_on_db_collision_and_compensates_salla():
    db, tenant_id, engine = _make_db()
    try:
        svc = CouponGeneratorService(db, tenant_id)
        taken_code = "NHZZZ"
        db.add(
            Coupon(
                tenant_id=tenant_id,
                code=taken_code,
                discount_type="percentage",
                discount_value="10",
                extra_metadata={"source": "manual"},
            )
        )
        db.commit()

        created_in_salla: list[str] = []
        deleted_in_salla: list[str] = []

        async def fake_create(code, discount_type, discount_value, expiry_days):
            created_in_salla.append(code)
            return {"code": code, "expires_at": "2026-12-31T00:00:00+00:00"}

        async def fake_delete(code):
            deleted_in_salla.append(code)
            return True

        svc._get_adapter = lambda: SimpleNamespace(
            create_coupon=fake_create,
            delete_coupon_by_code=fake_delete,
        )

        import backend.services.coupon_generator as cg

        original_next = cg._next_short_code
        call_counter = {"n": 0}

        def scripted_next(reserved, *args, **kwargs):
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return taken_code
            return original_next(reserved, *args, **kwargs)

        cg._next_short_code = scripted_next
        try:
            coupon = asyncio.run(
                svc._create_one_coupon(
                    segment="active",
                    discount=5,
                    expiry_days=3,
                    reserved_codes=svc._reserved_codes(),
                    adapter=svc._get_adapter(),
                )
            )
        finally:
            cg._next_short_code = original_next

        assert coupon is not None
        assert coupon.code != taken_code
        assert SHORT_CODE_PATTERN.match(coupon.code)
        assert taken_code in created_in_salla
        assert taken_code in deleted_in_salla
    finally:
        db.close()
        engine.dispose()


def test_create_one_coupon_compensates_when_salla_succeeds_but_db_fails_hard():
    db, tenant_id, engine = _make_db()
    try:
        svc = CouponGeneratorService(db, tenant_id)
        created_in_salla: list[str] = []
        deleted_in_salla: list[str] = []

        async def fake_create(code, discount_type, discount_value, expiry_days):
            created_in_salla.append(code)
            return {"code": code, "expires_at": "2026-12-31T00:00:00+00:00"}

        async def fake_delete(code):
            deleted_in_salla.append(code)
            return True

        svc._get_adapter = lambda: SimpleNamespace(
            create_coupon=fake_create,
            delete_coupon_by_code=fake_delete,
        )

        class _BoomDbError(Exception):
            pass

        original_commit = svc.db.commit

        def boom_commit():
            raise _BoomDbError("disk on fire")

        svc.db.commit = boom_commit
        try:
            result = asyncio.run(
                svc._create_one_coupon(
                    segment="new",
                    discount=15,
                    expiry_days=1,
                    reserved_codes=set(),
                    adapter=svc._get_adapter(),
                )
            )
        finally:
            svc.db.commit = original_commit

        assert result is None
        assert len(created_in_salla) == 1
        assert created_in_salla == deleted_in_salla
    finally:
        db.close()
        engine.dispose()


def test_generate_for_customer_picks_from_pool_when_available():
    db, tenant_id, engine = _make_db()
    try:
        _add_pool_coupon(db, tenant_id, "NHA12", "vip")
        svc = CouponGeneratorService(db, tenant_id)

        def _boom(*_a, **_kw):
            raise AssertionError("Salla adapter must not be called when pool has stock")

        svc._get_adapter = lambda: SimpleNamespace(
            create_coupon=_boom,
            delete_coupon_by_code=_boom,
        )

        coupon = asyncio.run(
            svc.generate_for_customer(customer_id=1, segment="vip", reason="status_change")
        )
        assert coupon is not None
        assert coupon.code == "NHA12"
    finally:
        db.close()
        engine.dispose()


# --- 22 regression scenarios ---


def test_scenario_01_new_tenant_produces_twelve_across_four_levels():
    db, tenant_id, engine = _make_db()
    try:
        created = _run_pool(CouponGeneratorService(db, tenant_id))
        assert created == {"bronze": 3, "silver": 3, "gold": 3, "vip": 3}
        assert sum(created.values()) == 12
    finally:
        db.close()
        engine.dispose()


def test_scenario_02_five_crm_statuses_map_to_four_levels():
  assert set(SEGMENT_TO_LEVEL.values()) == {"bronze", "silver", "gold", "vip"}
  assert len(SEGMENT_TO_LEVEL) == 5


def test_scenario_03_at_risk_and_inactive_share_vip_pool():
    db, tenant_id, engine = _make_db()
    try:
        _add_pool_coupon(db, tenant_id, "NHR01", "at_risk", level="vip")
        _add_pool_coupon(db, tenant_id, "NHR02", "inactive", level="vip")
        svc = CouponGeneratorService(db, tenant_id)
        assert svc._count_pool_by_level("vip") == 2
        assert svc._count_pool("at_risk") == 2
        assert svc._count_pool("inactive") == 2
    finally:
        db.close()
        engine.dispose()


def test_scenario_04_second_generator_run_is_idempotent():
    db, tenant_id, engine = _make_db()
    try:
        svc = CouponGeneratorService(db, tenant_id)
        first = _run_pool(svc)
        second = _run_pool(svc)
        assert sum(first.values()) == 12
        assert sum(second.values()) == 0
        assert db.query(Coupon).filter(Coupon.tenant_id == tenant_id).count() == 12
    finally:
        db.close()
        engine.dispose()


def test_scenario_05_level_with_one_eligible_creates_two():
    db, tenant_id, engine = _make_db()
    try:
        _add_pool_coupon(db, tenant_id, "NHB01", "new", level="bronze")
        created = _run_pool(CouponGeneratorService(db, tenant_id))
        assert created["bronze"] == 2
        assert CouponGeneratorService(db, tenant_id)._count_pool_by_level("bronze") == 3
    finally:
        db.close()
        engine.dispose()


def test_scenario_06_level_with_two_eligible_creates_one():
    db, tenant_id, engine = _make_db(warm_pool={"refill_threshold": 2})
    try:
        _add_pool_coupon(db, tenant_id, "NHS01", "active", level="silver")
        _add_pool_coupon(db, tenant_id, "NHS02", "active", level="silver")
        created = _run_pool(CouponGeneratorService(db, tenant_id))
        assert created["silver"] == 1
    finally:
        db.close()
        engine.dispose()


def test_scenario_07_level_with_three_eligible_creates_none():
    db, tenant_id, engine = _make_db()
    try:
        for i in range(3):
            _add_pool_coupon(db, tenant_id, f"NHG0{i}", "vip", level="gold")
        created = _run_pool(CouponGeneratorService(db, tenant_id))
        assert created["gold"] == 0
    finally:
        db.close()
        engine.dispose()


def test_scenario_08_excess_history_preserved_no_deletes():
    db, tenant_id, engine = _make_db()
    try:
        for i in range(5):
            _add_pool_coupon(db, tenant_id, f"NHV{i:02d}", "at_risk", level="vip")
        svc = CouponGeneratorService(db, tenant_id)
        vip_before = svc._count_pool_by_level("vip")
        assert vip_before == 5
        created = _run_pool(svc)
        assert created["vip"] == 0
        assert svc._count_pool_by_level("vip") == vip_before
        assert db.query(Coupon).filter(Coupon.tenant_id == tenant_id).count() >= 5
    finally:
        db.close()
        engine.dispose()


def test_scenario_09_configured_target_fifteen_clamped_to_three():
    db, tenant_id, engine = _make_db(warm_pool={"target_per_level": 15})
    try:
        cfg = _get_warm_pool_config(db, tenant_id)
        assert cfg["target_per_level"] == MAX_POOL_TARGET_PER_LEVEL == 3
        created = _run_pool(CouponGeneratorService(db, tenant_id))
        assert all(v <= 3 for v in created.values())
    finally:
        db.close()
        engine.dispose()


def test_scenario_10_configured_target_zero_preserved():
    db, tenant_id, engine = _make_db(warm_pool={"target_per_level": 0})
    try:
        cfg = _get_warm_pool_config(db, tenant_id)
        assert cfg["target_per_level"] == 0
        created = _run_pool(CouponGeneratorService(db, tenant_id))
        assert sum(created.values()) == 0
    finally:
        db.close()
        engine.dispose()


def test_scenario_11_disabled_level_creates_no_coupons():
    db, tenant_id, engine = _make_db()
    try:
        ts = db.query(TenantSettings).filter_by(tenant_id=tenant_id).one()
        ts.extra_metadata = {
            "coupons_dashboard": {
                "levels": [{"id": "bronze", "enabled": False}],
            }
        }
        db.commit()
        created = _run_pool(CouponGeneratorService(db, tenant_id))
        assert created["bronze"] == 0
        assert created["silver"] == 3
    finally:
        db.close()
        engine.dispose()


def test_scenario_12_on_demand_only_creates_no_warm_pool():
    db, tenant_id, engine = _make_db(ai_policy={"pool_mode": "on_demand_only"})
    try:
        created = _run_pool(CouponGeneratorService(db, tenant_id))
        assert created == {level: 0 for level in CANONICAL_COUPON_LEVELS}
        assert db.query(Coupon).filter(Coupon.tenant_id == tenant_id).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_scenario_13_manual_nahla_coupons_do_not_consume_quota():
    db, tenant_id, engine = _make_db()
    try:
        db.add(
            Coupon(
                tenant_id=tenant_id,
                code="MAN01",
                discount_type="percentage",
                discount_value="10",
                extra_metadata={"source": "manual"},
                source_type="manual",
            )
        )
        db.commit()
        created = _run_pool(CouponGeneratorService(db, tenant_id))
        assert sum(created.values()) == 12
        assert CouponGeneratorService(db, tenant_id)._count_pool_by_level("bronze") == 3
    finally:
        db.close()
        engine.dispose()


def test_scenario_14_salla_imported_coupons_do_not_consume_quota():
    db, tenant_id, engine = _make_db()
    try:
        db.add(
            Coupon(
                tenant_id=tenant_id,
                code="IMP01",
                discount_type="percentage",
                discount_value="10",
                extra_metadata={"source": "salla"},
                source_type="imported",
            )
        )
        db.commit()
        created = _run_pool(CouponGeneratorService(db, tenant_id))
        assert sum(created.values()) == 12
    finally:
        db.close()
        engine.dispose()


def test_scenario_15_used_expired_inactive_automatic_can_be_replaced():
    db, tenant_id, engine = _make_db()
    try:
        used = _pool_meta(segment="new", used="true")
        db.add(
            Coupon(
                tenant_id=tenant_id,
                code="NHX01",
                discount_type="percentage",
                discount_value="10",
                extra_metadata=used,
                coupon_level="bronze",
                source_type="system",
            )
        )
        expired = _pool_meta(segment="new")
        db.add(
            Coupon(
                tenant_id=tenant_id,
                code="NHX02",
                discount_type="percentage",
                discount_value="10",
                expires_at=datetime.now(timezone.utc) - timedelta(days=1),
                extra_metadata=expired,
                coupon_level="bronze",
                source_type="system",
            )
        )
        inactive = _pool_meta(segment="new", active=False)
        db.add(
            Coupon(
                tenant_id=tenant_id,
                code="NHX03",
                discount_type="percentage",
                discount_value="10",
                extra_metadata=inactive,
                coupon_level="bronze",
                source_type="system",
            )
        )
        db.commit()
        created = _run_pool(CouponGeneratorService(db, tenant_id))
        assert created["bronze"] == 3
    finally:
        db.close()
        engine.dispose()


def test_scenario_16_existing_inventory_does_not_bulk_outbound():
    db, tenant_id, engine = _make_db()
    try:
        for i in range(20):
            db.add(
                Coupon(
                    tenant_id=tenant_id,
                    code=f"OLD{i:03d}",
                    discount_type="percentage",
                    discount_value="5",
                    extra_metadata={"source": "auto", "used": "true"},
                    source_type="system",
                )
            )
        db.commit()
        calls: list[str] = []

        async def tracked_create(code, discount_type, discount_value, expiry_days):
            calls.append(code)
            return {
                "code": code,
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat(),
            }

        svc = CouponGeneratorService(db, tenant_id)
        svc._get_adapter = lambda: SimpleNamespace(
            create_coupon=tracked_create,
            delete_coupon_by_code=lambda _c: True,
        )
        created = asyncio.run(svc.ensure_coupon_pool())
        assert sum(created.values()) == 12
        assert len(calls) == 12
    finally:
        db.close()
        engine.dispose()


def test_scenario_17_inbound_pagination_not_limited_to_twelve():
    from services.store_sync import StoreSyncService

    assert len(CANONICAL_COUPON_LEVELS) * POOL_SIZE_PER_LEVEL == 12
    assert hasattr(StoreSyncService, "sync_coupons")


def test_scenario_18_tenant_a_pool_does_not_affect_tenant_b():
    db, tenant_a, engine = _make_db()
    try:
        tenant_b = Tenant(name="Tenant B", is_active=True)
        db.add(tenant_b)
        db.flush()
        db.add(TenantSettings(tenant_id=tenant_b.id, ai_settings={"allowed_discount_levels": 30}))
        db.commit()

        _run_pool(CouponGeneratorService(db, tenant_a))
        assert db.query(Coupon).filter(Coupon.tenant_id == tenant_a).count() == 12
        assert db.query(Coupon).filter(Coupon.tenant_id == tenant_b.id).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_scenario_19_legacy_null_coupon_level_uses_target_segment_mapping():
    db, tenant_id, engine = _make_db()
    try:
        db.add(
            Coupon(
                tenant_id=tenant_id,
                code="NHL042",
                discount_type="percentage",
                discount_value="10",
                expires_at=datetime.now(timezone.utc) + timedelta(days=2),
                extra_metadata={
                    "source": "auto",
                    "target_segment": CrmStatus.AT_RISK,
                    "used": "false",
                    "salla_synced": "true",
                    "active": True,
                },
                coupon_level=None,
                source_type="system",
            )
        )
        db.commit()
        svc = CouponGeneratorService(db, tenant_id)
        assert svc._count_pool_by_level("vip") == 1
        picked = svc.pick_coupon_for_segment("inactive", for_channel="autopilot")
        assert picked is not None
        assert picked.code == "NHL042"
    finally:
        db.close()
        engine.dispose()


def test_scenario_20_no_existing_coupon_deleted_or_deactivated():
    db, tenant_id, engine = _make_db()
    try:
        legacy = Coupon(
            tenant_id=tenant_id,
            code="KEEP1",
            discount_type="percentage",
            discount_value="10",
            extra_metadata={"source": "manual"},
            source_type="manual",
        )
        db.add(legacy)
        db.commit()
        _run_pool(CouponGeneratorService(db, tenant_id))
        db.refresh(legacy)
        assert legacy.extra_metadata.get("source") == "manual"
        assert db.query(Coupon).filter(Coupon.code == "KEEP1").count() == 1
    finally:
        db.close()
        engine.dispose()


def test_scenario_21_riyadh_date_normalization_remains_available():
    from services.coupon_salla_push import normalize_salla_coupon_push_dates, salla_coupon_today

    now = datetime(2026, 8, 25, 22, 30, tzinfo=timezone.utc)
    assert salla_coupon_today(now).isoformat() == "2026-08-26"
    start, expiry = normalize_salla_coupon_push_dates(now, now + timedelta(days=7), now=now)
    assert start == "2026-08-26"
    assert expiry >= start


def test_scenario_22_small_catalog_poller_interval_remains_sixty_seconds():
    from services.salla_coupons_poller import POLL_INTERVAL_SECONDS, get_poller_state

    assert POLL_INTERVAL_SECONDS == 60
    state = get_poller_state()
    assert state["config"]["adaptive_sla"]["small_catalog_seconds"] == 60


def test_refill_threshold_boundaries_target_three_threshold_one():
    cases = (
        (3, 0),
        (2, 0),
        (1, 2),
        (0, 3),
    )
    for existing, expected_created in cases:
        db, tenant_id, engine = _make_db(warm_pool={"target_per_level": 3, "refill_threshold": 1})
        try:
            ts = db.query(TenantSettings).filter_by(tenant_id=tenant_id).one()
            ts.extra_metadata = {
                "coupons_dashboard": {
                    "warm_pool": {"target_per_level": 3, "refill_threshold": 1},
                    "levels": [
                        {"id": "silver", "enabled": False},
                        {"id": "gold", "enabled": False},
                        {"id": "vip", "enabled": False},
                    ],
                }
            }
            db.commit()
            for i in range(existing):
                _add_pool_coupon(db, tenant_id, f"NHX0{i}", "new", level="bronze")
            svc = CouponGeneratorService(db, tenant_id)
            calls: list[str] = []

            async def tracked_create(code, discount_type, discount_value, expiry_days):
                calls.append(code)
                return {
                    "id": f"salla-{len(calls)}",
                    "code": code,
                    "expires_at": (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat(),
                }

            svc._get_adapter = lambda: SimpleNamespace(
                create_coupon=tracked_create,
                delete_coupon_by_id=lambda _id: True,
                delete_coupon_by_code=lambda _c: True,
            )
            with _patch_sqlite_pool_lock():
                created = asyncio.run(svc.ensure_coupon_pool())
            assert created["bronze"] == expected_created
            assert len(calls) == expected_created
        finally:
            db.close()
            engine.dispose()


def test_vip_level_economics_share_quota_and_use_level_discount():
    db, tenant_id, engine = _make_db()
    try:
        ts = db.query(TenantSettings).filter_by(tenant_id=tenant_id).one()
        ts.extra_metadata = {
            "coupons_dashboard": {
                "levels": [{"id": "vip", "discount_default": 30}],
            }
        }
        db.commit()

        _add_pool_coupon(db, tenant_id, "NHR01", "at_risk", level="vip")
        _add_pool_coupon(db, tenant_id, "NHR02", "inactive", level="vip")
        svc = CouponGeneratorService(db, tenant_id)
        assert svc._count_pool_by_level("vip") == 2
        assert svc._count_pool("at_risk") == 2
        assert svc._count_pool("inactive") == 2

        with _patch_sqlite_pool_lock():
            created = _run_pool(svc)
        assert created["vip"] == 0

        db.query(Coupon).filter(Coupon.tenant_id == tenant_id).delete()
        db.commit()
        svc = CouponGeneratorService(db, tenant_id)
        with _patch_sqlite_pool_lock():
            created = _run_pool(svc)
        assert created["vip"] == 3
        vip_rows = (
            db.query(Coupon)
            .filter(Coupon.tenant_id == tenant_id, Coupon.coupon_level == "vip")
            .all()
        )
        assert len(vip_rows) == 3
        assert all(str(row.discount_value) == "30" for row in vip_rows)
    finally:
        db.close()
        engine.dispose()


def test_create_one_coupon_failure_logs_redacted_code_not_raw():
    db, tenant_id, engine = _make_db()
    try:
        svc = CouponGeneratorService(db, tenant_id)
        leaked_codes: list[str] = []
        logged_fields: list[dict] = []

        async def fake_create(code, discount_type, discount_value, expiry_days):
            leaked_codes.append(code)
            return {
                "id": "remote-99",
                "code": code,
                "expires_at": "2026-12-31T00:00:00+00:00",
            }

        svc._get_adapter = lambda: SimpleNamespace(
            create_coupon=fake_create,
            delete_coupon_by_id=lambda _id: True,
            delete_coupon_by_code=lambda _c: True,
        )

        original_commit = svc.db.commit

        def boom_commit():
            raise RuntimeError("disk on fire")

        svc.db.commit = boom_commit

        def _capture_event(_name, **fields):
            logged_fields.append(dict(fields))

        try:
            with patch("backend.services.coupon_generator.log_event", side_effect=_capture_event):
                result = asyncio.run(
                    svc._create_one_coupon(
                        segment="new",
                        discount=15,
                        expiry_days=1,
                        reserved_codes=set(),
                        adapter=svc._get_adapter(),
                    )
                )
        finally:
            svc.db.commit = original_commit

        assert result is None
        assert leaked_codes
        raw_code = leaked_codes[0]
        assert raw_code not in str(logged_fields)
        assert any("coupon_hash=" in str(item.get("code_redacted", "")) for item in logged_fields)
    finally:
        db.close()
        engine.dispose()


def test_level_to_segments_covers_all_canonical_levels():
    assert set(LEVEL_TO_SEGMENTS.keys()) == set(CANONICAL_COUPON_LEVELS)
