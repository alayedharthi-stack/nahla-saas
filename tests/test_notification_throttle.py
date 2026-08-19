"""Compat tests: merchant email is first-contact only (no 24h returning spam)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_BACKEND = Path(__file__).parent.parent / "backend"
_DB_DIR = Path(__file__).parent.parent / "database"
for _p in (str(_BACKEND), str(_DB_DIR), str(_BACKEND.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models import Base, Customer, Tenant  # noqa: E402
from routers.whatsapp_webhook import _should_notify_merchant_email  # noqa: E402
from services.merchant_first_contact import STAMP_KEY  # noqa: E402

NOW = datetime.now(timezone.utc)


def _session():
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)()


def _cust(db, *, channel="whatsapp_inbound", notified=None, last=None):
    t = Tenant(name="متجر تجريبي عام", is_active=True)
    db.add(t)
    db.flush()
    row = Customer(
        tenant_id=t.id,
        phone="+966500000001",
        normalized_phone="+966500000001",
        acquisition_channel=channel,
        first_seen_at=NOW - timedelta(minutes=2),
        last_interaction_at=last or NOW - timedelta(minutes=2),
        extra_metadata=(
            {STAMP_KEY: notified.isoformat()} if notified is not None else None
        ),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return db, t, row


def test_new_whatsapp_customer_is_claimed_once():
    db, t, cust = _cust(_session())
    first = _should_notify_merchant_email(db=db, tenant_id=t.id, customer=cust)
    second = _should_notify_merchant_email(db=db, tenant_id=t.id, customer=cust)
    assert first["send"] is True
    assert first["reason"] == "first_customer_contact"
    assert second["send"] is False
    db.close()


def test_returning_after_silence_does_not_email():
    db, t, cust = _cust(
        _session(),
        notified=NOW - timedelta(days=10),
        last=NOW - timedelta(hours=25),
    )
    result = _should_notify_merchant_email(db=db, tenant_id=t.id, customer=cust)
    assert result["send"] is False
    db.close()


def test_no_customer_skip():
    db = _session()
    result = _should_notify_merchant_email(db=db, tenant_id=1, customer=None)
    assert result["send"] is False
    assert result["reason"] == "no_customer"
    db.close()
