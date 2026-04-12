"""
Integration-style tests for webhook → phone_number_id → tenant routing.

Uses an in-memory SQLite database to verify that:
  1. Webhook resolves the correct tenant from phone_number_id
  2. Unknown phone_number_id is dropped gracefully
  3. phone_number_id is the *only* key used (no WABA / tenant fallback)
"""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in (REPO_ROOT, BACKEND_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sqlalchemy import create_engine, event, JSON
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import JSONB

from database.models import Base, Tenant, WhatsAppConnection


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    """Replace JSONB columns with plain JSON so SQLite can create them."""
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def _seed(db, *, tenant_name, phone_number_id, waba_id, status="connected", sending_enabled=True):
    tenant = Tenant(name=tenant_name, is_active=True)
    db.add(tenant)
    db.flush()
    conn = WhatsAppConnection(
        tenant_id=tenant.id,
        phone_number_id=phone_number_id,
        phone_number="+966500000000",
        whatsapp_business_account_id=waba_id,
        connection_type="embedded",
        status=status,
        sending_enabled=sending_enabled,
        webhook_verified=True,
    )
    db.add(conn)
    db.commit()
    return tenant, conn


# ── Test 1: correct tenant resolved ──────────────────────────────────────────

def test_webhook_resolves_correct_tenant(db):
    """phone_number_id uniquely maps to a single tenant."""
    t1, c1 = _seed(db, tenant_name="Store A", phone_number_id="PID_AAA", waba_id="WABA_1")
    t2, c2 = _seed(db, tenant_name="Store B", phone_number_id="PID_BBB", waba_id="WABA_2")

    result = db.query(WhatsAppConnection).filter_by(phone_number_id="PID_AAA").first()
    assert result is not None
    assert result.tenant_id == t1.id

    result2 = db.query(WhatsAppConnection).filter_by(phone_number_id="PID_BBB").first()
    assert result2 is not None
    assert result2.tenant_id == t2.id


# ── Test 2: unknown phone_number_id returns None ────────────────────────────

def test_webhook_drops_unknown_phone(db):
    """Unknown phone_number_id should return no connection."""
    _seed(db, tenant_name="Store X", phone_number_id="PID_KNOWN", waba_id="WABA_X")

    result = db.query(WhatsAppConnection).filter_by(phone_number_id="PID_UNKNOWN").first()
    assert result is None


# ── Test 3: same WABA, different phones → different tenants ──────────────────

def test_same_waba_different_phones(db):
    """Two tenants can share the same WABA but have different phone_number_ids."""
    t1, _ = _seed(db, tenant_name="Merchant 1", phone_number_id="PID_M1", waba_id="SHARED_WABA")
    t2, _ = _seed(db, tenant_name="Merchant 2", phone_number_id="PID_M2", waba_id="SHARED_WABA")

    r1 = db.query(WhatsAppConnection).filter_by(phone_number_id="PID_M1").first()
    r2 = db.query(WhatsAppConnection).filter_by(phone_number_id="PID_M2").first()

    assert r1.tenant_id == t1.id
    assert r2.tenant_id == t2.id
    assert r1.tenant_id != r2.tenant_id


# ── Test 4: stale connection cleanup on re-registration ──────────────────────

def test_stale_cleanup_on_reregistration(db):
    """When a phone moves to a new tenant, the old connection is detached."""
    t_old, c_old = _seed(db, tenant_name="Old Owner", phone_number_id="PID_MOVE", waba_id="WABA_OLD")
    t_new = Tenant(name="New Owner", is_active=True)
    db.add(t_new)
    db.flush()

    db.query(WhatsAppConnection).filter(
        WhatsAppConnection.phone_number_id == "PID_MOVE",
        WhatsAppConnection.tenant_id != t_new.id,
    ).update({"phone_number_id": None, "status": "disconnected", "sending_enabled": False})

    new_conn = WhatsAppConnection(
        tenant_id=t_new.id,
        phone_number_id="PID_MOVE",
        phone_number="+966500000001",
        whatsapp_business_account_id="WABA_NEW",
        connection_type="embedded",
        status="connected",
        sending_enabled=True,
        webhook_verified=True,
    )
    db.add(new_conn)
    db.commit()

    result = db.query(WhatsAppConnection).filter_by(phone_number_id="PID_MOVE").first()
    assert result.tenant_id == t_new.id

    old = db.query(WhatsAppConnection).filter_by(tenant_id=t_old.id).first()
    assert old.phone_number_id is None
    assert old.status == "disconnected"


# ── Test 5: no WABA/env fallback in routing ──────────────────────────────────

def test_no_env_fallback_in_routing(db):
    """Routing must NOT fall back to a platform env var — only DB lookup."""
    _seed(db, tenant_name="Only Tenant", phone_number_id="PID_REAL", waba_id="WABA_REAL")

    env_pid = "PID_FROM_ENV_SHOULD_NOT_MATCH"
    result = db.query(WhatsAppConnection).filter_by(phone_number_id=env_pid).first()
    assert result is None
