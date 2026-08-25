"""Basic tests for coupon retention reporting helpers."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Coupon, Tenant
from services.coupon_retention_report import (
    build_coupon_retention_report,
    build_nahla_auto_retention_dry_run,
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
    tenant = Tenant(name="Retention Tenant", is_active=True)
    session.add(tenant)
    session.commit()
    return session, tenant.id


def test_build_coupon_retention_report_groups_by_source():
    db, tenant_id = _make_db()
    db.add(Coupon(tenant_id=tenant_id, code="IMP1", source_type="imported", extra_metadata={"source": "salla"}))
    db.add(Coupon(tenant_id=tenant_id, code="SYS1", source_type="system", extra_metadata={"source": "auto", "used": "true"}))
    db.commit()
    report = build_coupon_retention_report(db, tenant_id)
    assert report["total"] == 2
    assert report["groups"]["imported"]["total"] == 1
    assert report["groups"]["system"]["used"] == 1


def test_build_nahla_auto_retention_dry_run_lists_eligible_only():
    db, tenant_id = _make_db()
    expired_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.add(Coupon(tenant_id=tenant_id, code="OLD1", source_type="system", expires_at=expired_at, extra_metadata={"source": "auto"}))
    db.add(Coupon(tenant_id=tenant_id, code="LIVE1", source_type="system", expires_at=datetime.now(timezone.utc) + timedelta(days=2), extra_metadata={"source": "auto"}))
    db.commit()
    dry_run = build_nahla_auto_retention_dry_run(db, tenant_id)
    assert dry_run["eligible_count"] == 1
    assert dry_run["expired_count"] == 1
    assert dry_run["eligible"][0]["code"] == "OLD1"
