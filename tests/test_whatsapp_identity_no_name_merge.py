"""WhatsApp inbound must not merge customers by profile name alone."""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, Customer, Tenant  # noqa: E402
from services.customer_intelligence import CustomerIntelligenceService  # noqa: E402

PHONE_A = "+966505263377"
PHONE_B = "+966551786669"
SHARED_NAME = "فاطمة العتيبي"


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


class TestWhatsAppIdentityNoNameMerge:
    def test_whatsapp_inbound_does_not_merge_different_phones_by_name(self):
        db, _ = _make_db()
        t = Tenant(name="T", is_active=True)
        db.add(t)
        db.commit()
        db.refresh(t)

        svc = CustomerIntelligenceService(db, t.id)
        first = svc.upsert_customer_identity(
            phone=PHONE_B,
            name=SHARED_NAME,
            source="whatsapp_inbound",
        )
        db.commit()
        assert first is not None

        second = svc.upsert_customer_identity(
            phone=PHONE_A,
            name=SHARED_NAME,
            source="whatsapp_inbound",
        )
        db.commit()
        assert second is not None
        assert second.id != first.id
        assert second.normalized_phone == PHONE_A
        assert first.normalized_phone == PHONE_B

        assert db.query(Customer).filter(Customer.tenant_id == t.id).count() == 2

    def test_salla_sync_may_still_match_by_name(self):
        db, _ = _make_db()
        t = Tenant(name="T", is_active=True)
        db.add(t)
        db.commit()
        db.refresh(t)

        svc = CustomerIntelligenceService(db, t.id)
        first = svc.upsert_customer_identity(
            phone=PHONE_B,
            name=SHARED_NAME,
            source="salla_sync",
        )
        db.commit()

        second = svc.upsert_customer_identity(
            phone=PHONE_A,
            name=SHARED_NAME,
            source="order_webhook",
        )
        db.commit()
        assert second is not None
        assert second.id == first.id
