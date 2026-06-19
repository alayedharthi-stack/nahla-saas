"""Contract tests for scripts/recompute_all.py profile backfill helper."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, Customer, CustomerProfile, Order, Tenant  # noqa: E402
from scripts.recompute_all import recompute_tenant_profiles  # noqa: E402


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)(), engine


class TestRecomputeTenantProfiles:
    def test_recomputes_all_customers_and_reports_counts(self):
        db, engine = _make_db()
        try:
            tenant = Tenant(name="T", is_active=True)
            db.add(tenant)
            db.commit()
            db.refresh(tenant)

            now = datetime.now(timezone.utc)
            lead = Customer(
                tenant_id=tenant.id,
                name="Lead",
                phone="+966500000010",
                normalized_phone="+966500000010",
            )
            buyer = Customer(
                tenant_id=tenant.id,
                name="Buyer",
                phone="+966500000011",
                normalized_phone="+966500000011",
            )
            db.add_all([lead, buyer])
            db.commit()

            db.add(
                Order(
                    tenant_id=tenant.id,
                    external_id="old-order",
                    status="completed",
                    total="100",
                    customer_info={"mobile": "+966500000011"},
                    line_items=[],
                    extra_metadata={"created_at": (now - timedelta(days=120)).isoformat()},
                )
            )
            db.commit()

            db.add(
                CustomerProfile(
                    customer_id=lead.id,
                    tenant_id=tenant.id,
                    segment="inactive",
                    customer_status="inactive",
                    rfm_segment="lost_customers",
                    last_seen_at=now,
                )
            )
            db.commit()

            stats = recompute_tenant_profiles(
                db,
                tenant.id,
                reason="test_recompute_all",
                commit=True,
                emit_event=False,
            )

            assert stats["total"] == 2
            assert stats["success"] == 2
            assert stats["failed"] == 0

            lead_profile = db.query(CustomerProfile).filter_by(customer_id=lead.id).one()
            buyer_profile = db.query(CustomerProfile).filter_by(customer_id=buyer.id).one()

            assert lead_profile.customer_status == "lead"
            assert buyer_profile.customer_status == "inactive"
        finally:
            db.close()
            engine.dispose()

    def test_continues_after_single_customer_failure(self):
        db, engine = _make_db()
        try:
            tenant = Tenant(name="T", is_active=True)
            db.add(tenant)
            db.commit()
            db.refresh(tenant)

            bad = Customer(
                tenant_id=tenant.id,
                name="Bad",
                phone="+966500000021",
                normalized_phone="+966500000021",
            )
            ok_customer = Customer(
                tenant_id=tenant.id,
                name="OK",
                phone="+966500000020",
                normalized_phone="+966500000020",
            )
            db.add_all([bad, ok_customer])
            db.commit()

            from services.customer_intelligence import CustomerIntelligenceService

            original = CustomerIntelligenceService.recompute_profile_for_customer

            def _fake_recompute(self, customer_id, *args, **kwargs):
                if customer_id == bad.id:
                    raise RuntimeError("boom")
                return original(self, customer_id, *args, **kwargs)

            with patch.object(
                CustomerIntelligenceService,
                "recompute_profile_for_customer",
                _fake_recompute,
            ):
                stats = recompute_tenant_profiles(
                    db,
                    tenant.id,
                    reason="test_recompute_all",
                    commit=True,
                    emit_event=False,
                )

            assert stats["total"] == 2
            assert stats["success"] == 1
            assert stats["failed"] == 1
            assert stats["errors"][0][0] == bad.id
        finally:
            db.close()
            engine.dispose()
