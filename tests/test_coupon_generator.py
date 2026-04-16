import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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
    POOL_SIZE_PER_SEGMENT,
    CouponGeneratorService,
    build_coupon_send_payload,
)


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
    tenant = Tenant(name="Coupon Tenant", is_active=True)
    session.add(tenant)
    session.flush()
    session.add(TenantSettings(tenant_id=tenant.id, ai_settings={"allowed_discount_levels": 10}))
    session.commit()
    return session, tenant.id, engine


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
            extra_metadata={
                "source": "auto",
                "target_segment": "active",
                "used": "false",
                "salla_synced": "true",
                "category": "auto",
                "active": True,
            },
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


def test_ensure_coupon_pool_targets_fifteen_per_segment():
    db, tenant_id, engine = _make_db()
    try:
        svc = CouponGeneratorService(db, tenant_id)

        calls = []

        async def fake_create_coupon(code: str, discount_type: str, discount_value: int, expiry_days: int):
            calls.append((code, discount_type, discount_value, expiry_days))
            return {"code": code, "expires_at": (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat()}

        async def fake_delete(code: str):
            return True

        svc._get_adapter = lambda: SimpleNamespace(
            create_coupon=fake_create_coupon,
            delete_coupon_by_code=fake_delete,
        )

        import asyncio
        import re as _re

        created = asyncio.run(svc.ensure_coupon_pool())

        assert POOL_SIZE_PER_SEGMENT == 15
        assert all(count == 15 for count in created.values())
        rows = db.query(Coupon).filter(Coupon.tenant_id == tenant_id).all()
        assert len(rows) == 15 * 5

        # New format: NH + 3 alphanumeric chars (length 5).
        NEW_PATTERN = _re.compile(r"^NH[A-Z0-9]{3}$")
        assert all(NEW_PATTERN.match(c.code) for c in rows), (
            "All newly generated coupons must match the NH*** spec "
            f"— got {[c.code for c in rows if not NEW_PATTERN.match(c.code)]}"
        )

        # Uniqueness: no two coupons share a code.
        codes = [c.code for c in rows]
        assert len(codes) == len(set(codes)), "Generated duplicate coupon codes"
    finally:
        db.close()
        engine.dispose()


# ─────────────────────────────────────────────────────────────────────────────
# NH*** format + collision/compensation tests (Phase G)
# ─────────────────────────────────────────────────────────────────────────────

def test_short_code_regex_accepts_new_and_legacy_formats():
    """
    _is_short_coupon_code must accept both the new NH*** codes and the
    grandfathered NHL### codes so existing reporting keeps working while
    new issuance uses the NH*** space.
    """
    from backend.services.coupon_generator import _is_short_coupon_code

    assert _is_short_coupon_code("NH7K2") is True
    assert _is_short_coupon_code("NH3A9") is True
    assert _is_short_coupon_code("NHL042") is True   # legacy kept alive
    assert _is_short_coupon_code("NH12") is False     # too short
    assert _is_short_coupon_code("NH7KAB") is False   # too long
    assert _is_short_coupon_code("XX123") is False    # wrong prefix
    assert _is_short_coupon_code(None) is False


def test_next_short_code_avoids_reserved_codes_and_raises_when_exhausted():
    """
    _next_short_code must never reuse a reserved code and must raise
    CouponPoolExhausted when the alphabet is saturated.
    """
    from backend.services.coupon_generator import (
        CouponPoolExhausted,
        SHORT_CODE_ALPHABET,
        SHORT_CODE_BODY_LEN,
        SHORT_CODE_PREFIX,
        _next_short_code,
    )

    reserved: set[str] = set()
    seen: set[str] = set()
    for _ in range(50):
        code = _next_short_code(reserved)
        assert code.startswith(SHORT_CODE_PREFIX)
        assert len(code) == len(SHORT_CODE_PREFIX) + SHORT_CODE_BODY_LEN
        assert code not in seen
        seen.add(code)

    # Saturate the space with every possible NH*** code, then assert exhaustion.
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
    """
    Scenario: Salla creation succeeds, local INSERT hits IntegrityError (the
    unique index on (tenant_id, code) fires because of a concurrent writer).
    The generator must:
      • call delete_coupon_by_code(code) to un-orphan the Salla side
      • generate a new code and try again
      • eventually return a Coupon with a fresh, non-colliding code
    """
    import asyncio as _asyncio
    from types import SimpleNamespace

    from backend.services.coupon_generator import (
        CouponGeneratorService,
        SHORT_CODE_PATTERN,
    )
    from database.models import Coupon

    db, tenant_id, engine = _make_db()
    try:
        svc = CouponGeneratorService(db, tenant_id)

        # Pre-populate the DB with the next code the generator will try so the
        # first local insert attempt collides. We mock the RNG deterministically
        # by patching _next_short_code to hand out a fixed sequence.
        taken_code = "NHZZZ"
        db.add(Coupon(
            tenant_id=tenant_id,
            code=taken_code,
            discount_type="percentage",
            discount_value="10",
            extra_metadata={"source": "manual"},
        ))
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

        # Force the first attempt to use the already-taken code, then a fresh one.
        import backend.services.coupon_generator as cg

        original_next = cg._next_short_code
        call_counter = {"n": 0}

        def scripted_next(reserved, *args, **kwargs):
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                # Deliberately pick the colliding code even though it is reserved.
                return taken_code
            # Any subsequent call delegates to the real generator.
            return original_next(reserved, *args, **kwargs)

        cg._next_short_code = scripted_next
        try:
            reserved_codes = svc._reserved_codes()
            coupon = _asyncio.run(svc._create_one_coupon(
                segment="active",
                discount=5,
                expiry_days=3,
                reserved_codes=reserved_codes,
                adapter=svc._get_adapter(),
            ))
        finally:
            cg._next_short_code = original_next

        assert coupon is not None
        assert coupon.code != taken_code
        assert SHORT_CODE_PATTERN.match(coupon.code)
        assert taken_code in created_in_salla, "first attempt should have called Salla"
        assert taken_code in deleted_in_salla, "collision must trigger compensation delete"
        # Exactly one live row for the colliding code (our pre-seeded manual one).
        live = db.query(Coupon).filter(Coupon.code == taken_code, Coupon.tenant_id == tenant_id).all()
        assert len(live) == 1
    finally:
        db.close()
        engine.dispose()


def test_create_one_coupon_compensates_when_salla_succeeds_but_db_fails_hard():
    """
    If the local insert raises a non-IntegrityError exception after Salla
    already accepted the coupon, _create_one_coupon must call
    delete_coupon_by_code so the two systems do not drift apart.
    """
    import asyncio as _asyncio
    from types import SimpleNamespace

    from backend.services.coupon_generator import CouponGeneratorService

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

        # Force db.commit() to raise something that is NOT an IntegrityError.
        class _BoomDbError(Exception):
            pass

        original_commit = svc.db.commit

        def boom_commit():
            raise _BoomDbError("disk on fire")

        svc.db.commit = boom_commit  # type: ignore[assignment]
        try:
            result = _asyncio.run(svc._create_one_coupon(
                segment="new",
                discount=15,
                expiry_days=1,
                reserved_codes=set(),
                adapter=svc._get_adapter(),
            ))
        finally:
            svc.db.commit = original_commit  # type: ignore[assignment]

        assert result is None
        assert len(created_in_salla) == 1
        assert created_in_salla == deleted_in_salla, "must compensate 1:1 on hard DB failure"
    finally:
        db.close()
        engine.dispose()


def test_generate_for_customer_picks_from_pool_when_available():
    """
    Event-driven coupon handover: if the pool already has a fresh NH*** coupon
    for the target segment, generate_for_customer returns it without hitting
    Salla at all.
    """
    import asyncio as _asyncio
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    from backend.services.coupon_generator import CouponGeneratorService
    from database.models import Coupon

    db, tenant_id, engine = _make_db()
    try:
        expires = _dt.now(_tz.utc) + _td(days=3)
        db.add(Coupon(
            tenant_id=tenant_id,
            code="NHA12",
            discount_type="percentage",
            discount_value="20",
            expires_at=expires,
            extra_metadata={
                "source": "auto",
                "target_segment": "vip",
                "used": "false",
                "salla_synced": "true",
                "category": "auto",
                "active": True,
            },
        ))
        db.commit()

        svc = CouponGeneratorService(db, tenant_id)

        # Adapter must not be touched when the pool has a candidate.
        def _boom(*_a, **_kw):
            raise AssertionError("Salla adapter must not be called when pool has stock")

        from types import SimpleNamespace
        svc._get_adapter = lambda: SimpleNamespace(
            create_coupon=_boom,
            delete_coupon_by_code=_boom,
        )

        coupon = _asyncio.run(svc.generate_for_customer(
            customer_id=1,
            segment="vip",
            reason="status_change",
        ))

        assert coupon is not None
        assert coupon.code == "NHA12"
    finally:
        db.close()
        engine.dispose()
