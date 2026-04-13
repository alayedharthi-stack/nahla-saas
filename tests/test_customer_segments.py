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

from database.models import Base, Customer, CustomerProfile, Order, Tenant
from backend.services.store_sync import StoreSyncService


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


def test_recent_order_does_not_become_churned():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        tenant = Tenant(name="Segment Tenant", is_active=True)
        db.add(tenant)
        db.flush()

        customer = Customer(
            tenant_id=tenant.id,
            name="عميل حديث",
            phone="555999001",
            extra_metadata={"source": "salla", "salla_id": "cust-1"},
        )
        db.add(customer)
        db.flush()

        db.add(CustomerProfile(
            customer_id=customer.id,
            tenant_id=tenant.id,
            segment="churned",
            first_seen_at=datetime.now(timezone.utc) - timedelta(days=100),
        ))

        recent_order_dt = datetime.now(timezone.utc) - timedelta(days=1)
        db.add(Order(
            tenant_id=tenant.id,
            external_id="order-1",
            status="completed",
            total="120",
            customer_info={"name": "عميل حديث", "mobile": "0555999001"},
            line_items=[],
            extra_metadata={"created_at": recent_order_dt.isoformat()},
        ))
        db.commit()

        svc = StoreSyncService(db, tenant.id)
        rebuilt = svc._build_customer_profiles()
        assert rebuilt >= 1

        profile = db.query(CustomerProfile).filter_by(customer_id=customer.id, tenant_id=tenant.id).first()
        assert profile is not None
        assert profile.total_orders == 1
        assert profile.last_order_at is not None
        assert profile.segment in {"new", "active"}
        assert profile.segment != "churned"
    finally:
        db.close()
        engine.dispose()
