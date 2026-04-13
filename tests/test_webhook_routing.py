"""
Integration-style tests for webhook → phone_number_id → tenant routing.

Uses an in-memory SQLite database to verify that:
  1. Webhook resolves the correct tenant from phone_number_id
  2. Unknown phone_number_id is dropped gracefully
  3. phone_number_id is the *only* key used (no WABA / tenant fallback)
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sqlalchemy import create_engine, event, JSON
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import JSONB

from database.models import Base, MessageEvent, Tenant, WhatsAppConnection


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


def _seed_coexistence(db, *, tenant_name, phone_number_id, status="connected"):
    tenant = Tenant(name=tenant_name, is_active=True)
    db.add(tenant)
    db.flush()
    conn = WhatsAppConnection(
        tenant_id=tenant.id,
        phone_number_id=phone_number_id,
        phone_number="+966511111111",
        whatsapp_business_account_id="WABA_COEX",
        connection_type="coexistence",
        provider="dialog360",
        access_token="d360_api_key",
        status=status,
        sending_enabled=status == "connected",
        webhook_verified=status == "connected",
        extra_metadata={"coexistence_internal_secret": "secret-123"},
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


def test_coexistence_provider_routing_uses_same_phone_number_id(db):
    """Coexistence keeps the same routing key: phone_number_id."""
    tenant, _conn = _seed_coexistence(db, tenant_name="Coex Tenant", phone_number_id="PID_COEX")
    result = db.query(WhatsAppConnection).filter_by(phone_number_id="PID_COEX").first()
    assert result is not None
    assert result.tenant_id == tenant.id
    assert result.provider == "dialog360"
    assert result.connection_type == "coexistence"


def test_coexistence_echoes_can_be_stored_without_reclassifying_tenant(db):
    """Merchant mobile echoes must stay on the resolved tenant and never require WABA fallback."""
    tenant, conn = _seed_coexistence(db, tenant_name="Echo Tenant", phone_number_id="PID_ECHO")
    payload_phone_id = "PID_ECHO"
    resolved = db.query(WhatsAppConnection).filter_by(phone_number_id=payload_phone_id).first()
    assert resolved is not None
    assert resolved.tenant_id == tenant.id
    assert resolved.provider == "dialog360"
    assert (resolved.extra_metadata or {}).get("coexistence_internal_secret") == "secret-123"


def test_360dialog_messages_field_dispatches_customer_message(db):
    """Customer-originated coexistence messages must continue into the existing dispatcher."""
    _tenant, _conn = _seed_coexistence(db, tenant_name="Webhook Tenant", phone_number_id="PID_360_MSG")
    import routers.whatsapp_webhook as wa_webhook  # noqa: PLC0415

    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA_360",
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": "PID_360_MSG", "display_phone_number": "+966511111111"},
                    "messages": [{
                        "from": "966500000001",
                        "id": "wamid.customer",
                        "type": "text",
                        "text": {"body": "مرحبا"},
                    }],
                },
            }],
        }],
    }
    request = SimpleNamespace(headers={"X-Nahla-Coexistence-Secret": "secret-123"})

    with patch.object(wa_webhook, "get_db", return_value=iter([db])), patch.object(
        wa_webhook, "_dispatch_message", new=AsyncMock()
    ) as mock_dispatch:
        asyncio.run(wa_webhook._handle_360dialog_body(payload, request))

    mock_dispatch.assert_awaited_once()
    dispatched_phone_id, dispatched_msg, _value = mock_dispatch.await_args.args
    assert dispatched_phone_id == "PID_360_MSG"
    assert dispatched_msg["id"] == "wamid.customer"


def test_360dialog_smb_echoes_are_stored_without_dispatch(db):
    """Merchant mobile echoes must never be treated as inbound AI-driving messages."""
    tenant, _conn = _seed_coexistence(db, tenant_name="Echo Webhook Tenant", phone_number_id="PID_360_ECHO")
    tenant_id = tenant.id
    import routers.whatsapp_webhook as wa_webhook  # noqa: PLC0415

    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA_360",
            "changes": [{
                "field": "smb_message_echoes",
                "value": {
                    "metadata": {"phone_number_id": "PID_360_ECHO", "display_phone_number": "+966511111111"},
                    "message_echoes": [{
                        "from": "+966511111111",
                        "to": "966500000099",
                        "id": "wamid.echo",
                        "type": "text",
                        "text": {"body": "رسالة من التاجر"},
                    }],
                },
            }],
        }],
    }
    request = SimpleNamespace(headers={"X-Nahla-Coexistence-Secret": "secret-123"})

    with patch.object(wa_webhook, "get_db", return_value=iter([db])), patch.object(
        wa_webhook, "_dispatch_message", new=AsyncMock()
    ) as mock_dispatch:
        asyncio.run(wa_webhook._handle_360dialog_body(payload, request))

    mock_dispatch.assert_not_awaited()
    stored = db.query(MessageEvent).filter(
        MessageEvent.tenant_id == tenant_id,
        MessageEvent.event_type == "smb_message_echo",
    ).all()
    assert len(stored) == 1
    assert stored[0].direction == "outbound"
    assert stored[0].body == "رسالة من التاجر"
