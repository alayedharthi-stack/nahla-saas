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

        svc._get_adapter = lambda: SimpleNamespace(create_coupon=fake_create_coupon)

        import asyncio

        created = asyncio.run(svc.ensure_coupon_pool())

        assert POOL_SIZE_PER_SEGMENT == 15
        assert all(count == 15 for count in created.values())
        rows = db.query(Coupon).filter(Coupon.tenant_id == tenant_id).all()
        assert len(rows) == 15 * 5
        assert all(c.code.startswith("NHL") and len(c.code) == 6 and c.code[3:].isdigit() for c in rows)
    finally:
        db.close()
        engine.dispose()
