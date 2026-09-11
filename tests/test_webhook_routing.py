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


def _session_local_factory(db):
    """Mirror production: ``_handle_360dialog_body`` opens ``SessionLocal()``,
    not ``get_db()``. Bind a factory to the fixture's in-memory engine."""
    return sessionmaker(bind=db.get_bind())


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
    headers = {"x_nahla_coexistence_secret": "secret-123"}

    with patch.object(wa_webhook, "SessionLocal", _session_local_factory(db)), patch.object(
        wa_webhook, "_dispatch_message", new=AsyncMock()
    ) as mock_dispatch:
        asyncio.run(wa_webhook._handle_360dialog_body(payload, headers))

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
    headers = {"x_nahla_coexistence_secret": "secret-123"}

    with patch.object(wa_webhook, "SessionLocal", _session_local_factory(db)), patch.object(
        wa_webhook, "_dispatch_message", new=AsyncMock()
    ) as mock_dispatch:
        asyncio.run(wa_webhook._handle_360dialog_body(payload, headers))

    mock_dispatch.assert_not_awaited()
    stored = db.query(MessageEvent).filter(
        MessageEvent.tenant_id == tenant_id,
        MessageEvent.event_type == "smb_message_echo",
    ).all()
    assert len(stored) == 1
    assert stored[0].direction == "outbound"
    assert stored[0].body == "رسالة من التاجر"


def test_dispatch_message_closes_db_session_after_merchant_route(db):
    """Merchant routing used to return before `_dispatch_message` reached any
    cleanup block, leaking the SQLAlchemy session. That left transactions open
    on `whatsapp_connections`, later blocking webhook delivery and even auth
    requests behind a row lock."""
    _tenant, conn = _seed(
        db,
        tenant_name="Merchant Route Tenant",
        phone_number_id="PID_CLOSE",
        waba_id="WABA_CLOSE",
    )
    import routers.whatsapp_webhook as wa_webhook  # noqa: PLC0415

    close_calls = {"count": 0}
    original_close = db.close

    def tracked_close():
        close_calls["count"] += 1
        return original_close()

    db.close = tracked_close
    msg = {
        "from": "966500000123",
        "id": "wamid.close-check",
        "type": "text",
        "text": {"body": "مرحبا"},
    }
    value = {
        "metadata": {"phone_number_id": "PID_CLOSE"},
        "contacts": [{"wa_id": "966500000123", "profile": {"name": "Tester"}}],
    }

    with patch.object(wa_webhook, "get_db", return_value=iter([db])), patch.object(
        wa_webhook, "_is_platform_tenant", return_value=False
    ), patch.object(
        wa_webhook, "_handle_merchant_message", new=AsyncMock()
    ) as mock_merchant:
        asyncio.run(wa_webhook._dispatch_message("PID_CLOSE", msg, value))

    mock_merchant.assert_awaited_once()
    assert close_calls["count"] == 1, "_dispatch_message leaked its DB session on merchant route"


@pytest.mark.parametrize("body", ["ابي جاكيت", "أريد صورة حذاء رياضي أبيض"])
def test_dispatch_uses_persisted_dedup_after_worker_cache_reset(db, body):
    """A provider retry on a fresh worker must stop before merchant execution."""
    from core.conversation_engine import ConversationState, IdempotencyGuard
    from core.inbound_dedup import reset_cache
    import routers.whatsapp_webhook as webhook

    tenant, _ = _seed(db, tenant_name="Generic store", phone_number_id="PID_RETRY",
                      waba_id="WABA_RETRY")
    tenant_id = tenant.id
    sender = "966500000123"
    msg_id = "wamid.persisted-retry"
    state = ConversationState(phone=sender)
    IdempotencyGuard.mark_processed(state, msg_id)
    reset_cache()
    with (
        patch.object(webhook, "get_db", return_value=iter([db])),
        patch.object(webhook.StateManager, "load", return_value=state) as load,
        patch.object(webhook.StateManager, "save") as save,
        patch.object(webhook, "_handle_merchant_message", new=AsyncMock()) as merchant,
        patch.object(webhook, "_post_wa", new=AsyncMock()) as send,
    ):
        asyncio.run(webhook._dispatch_message("PID_RETRY", {
            "from": sender, "id": msg_id, "type": "text", "text": {"body": body},
        }, {}))
    load.assert_called_once_with(db, phone=sender, tenant_id=tenant_id)
    save.assert_not_called()
    merchant.assert_not_awaited()
    send.assert_not_awaited()


def test_dispatch_persists_fresh_id_and_delivers_unchanged_customer_text(db):
    """The restored guard must allow a new turn with the correct tenant/text."""
    from core.conversation_engine import ConversationState
    from core.inbound_dedup import reset_cache
    import routers.whatsapp_webhook as webhook

    tenant, _ = _seed(db, tenant_name="Generic clothing store", phone_number_id="PID_FRESH",
                      waba_id="WABA_FRESH")
    tenant_id = tenant.id
    sender = "966500000123"
    state = ConversationState(phone=sender)
    body = "أريد صورة قميص قطني أزرق"
    reset_cache()
    with (
        patch.object(webhook, "get_db", return_value=iter([db])),
        patch.object(webhook.StateManager, "load", return_value=state) as load,
        patch.object(webhook.StateManager, "save") as save,
        patch.object(webhook, "_is_platform_tenant", return_value=False),
        patch.object(webhook, "_handle_merchant_message", new=AsyncMock()) as merchant,
        patch.object(webhook, "_post_wa", new=AsyncMock()) as send,
    ):
        asyncio.run(webhook._dispatch_message("PID_FRESH", {
            "from": sender, "id": "wamid.fresh", "type": "text", "text": {"body": body},
        }, {}))
    load.assert_called_once_with(db, phone=sender, tenant_id=tenant_id)
    save.assert_called_once_with(db, state, tenant_id=tenant_id)
    assert state.processed_ids == ["wamid.fresh"]
    merchant.assert_awaited_once()
    assert merchant.await_args.kwargs["tenant_id"] == tenant_id
    assert merchant.await_args.kwargs["text"] == body
    assert merchant.await_args.kwargs["wa_msg_id"] == "wamid.fresh"
    send.assert_not_awaited()


@pytest.mark.parametrize("product", ["قميص قطني أزرق", "عطر ورد 100ml"])
def test_first_contact_dedup_and_brain_share_one_customer_conversation(db, product):
    from core.conversation_engine import ConversationState, IdempotencyGuard, StateManager
    from core.order_flow import _load_brain_state
    from routers.conversations import _get_or_create_conversation
    from database.models import Conversation

    tenant, _ = _seed(db, tenant_name="Generic store", phone_number_id="PID_SINGLE",
                      waba_id="WABA_SINGLE")
    tenant_id = tenant.id
    phone = "966500000123"
    state = ConversationState(phone=phone)
    IdempotencyGuard.mark_processed(state, "wamid.first-contact")
    saved = StateManager.save(db, state, tenant_id=tenant_id)
    assert saved is not None
    saved_id = saved.id
    canonical = _get_or_create_conversation(db, tenant_id, phone)
    assert canonical.id == saved_id
    assert canonical.customer_id is not None
    assert db.query(Conversation).filter(Conversation.tenant_id == tenant_id).count() == 1

    brain_state = {"stage": "exploring", "current_product_focus": {"id": 42, "title": product}}
    canonical.extra_metadata = {**canonical.extra_metadata, "brain_state": brain_state,
                                "custom_metadata": {"keep": True}}
    db.commit()
    reloaded = StateManager.load(db, phone, tenant_id=tenant_id)
    assert IdempotencyGuard.is_duplicate(reloaded, "wamid.first-contact")
    IdempotencyGuard.mark_processed(reloaded, "wamid.second-turn")
    assert StateManager.save(db, reloaded, tenant_id=tenant_id).id == saved_id
    brain_conversation, retrieved_brain = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
    assert brain_conversation.id == saved_id
    assert retrieved_brain == brain_state
    assert brain_conversation.extra_metadata["custom_metadata"] == {"keep": True}


def test_state_lookup_uses_customer_identity_and_keeps_tenants_separate(db):
    from core.conversation_engine import ConversationState, IdempotencyGuard, StateManager
    from routers.conversations import _get_or_create_conversation

    tenant_a, _ = _seed(db, tenant_name="Clothing store", phone_number_id="PID_ISOLATION_A",
                        waba_id="WABA_ISOLATION_A")
    tenant_a_id = tenant_a.id
    tenant_b, _ = _seed(db, tenant_name="Footwear store", phone_number_id="PID_ISOLATION_B",
                        waba_id="WABA_ISOLATION_B")
    tenant_b_id = tenant_b.id
    phone = "966500000123"
    a = _get_or_create_conversation(db, tenant_a_id, phone)
    b = _get_or_create_conversation(db, tenant_b_id, phone)
    a_id, b_id = a.id, b.id
    state = ConversationState(phone=phone)
    IdempotencyGuard.mark_processed(state, "wamid.only-a")
    # A linked conversation can predate the legacy metadata phone key.
    a.extra_metadata = {**state.to_dict(), "brain_state": {"turn": 7}}
    a.extra_metadata.pop("phone")
    b.extra_metadata = {"brain_state": {"turn": 3}}
    db.commit()
    reloaded = StateManager.load(db, phone, tenant_id=tenant_a_id)
    assert IdempotencyGuard.is_duplicate(reloaded, "wamid.only-a")
    assert not IdempotencyGuard.is_duplicate(
        StateManager.load(db, phone, tenant_id=tenant_b_id), "wamid.only-a")
    # Restore the caller's delivery phone when the old state omitted it.
    assert reloaded.phone == phone
    assert StateManager.save(db, reloaded, tenant_id=tenant_a_id).id == a_id
    assert b.id == b_id
    assert b.extra_metadata == {"brain_state": {"turn": 3}}


def test_legacy_phone_only_state_is_linked_without_losing_context(db):
    from core.conversation_engine import ConversationState, StateManager
    from routers.conversations import _get_or_create_conversation
    from database.models import Conversation

    tenant, _ = _seed(db, tenant_name="Generic store", phone_number_id="PID_LEGACY",
                      waba_id="WABA_LEGACY")
    tenant_id = tenant.id
    state = ConversationState(phone="966500000123")
    brain = {"stage": "exploring", "turn": 4}
    legacy = Conversation(tenant_id=tenant_id, status="active",
                          extra_metadata={**state.to_dict(), "brain_state": brain})
    db.add(legacy)
    db.commit()
    legacy_id = legacy.id
    assert StateManager.save(db, state, tenant_id=tenant_id).id == legacy_id
    canonical = _get_or_create_conversation(db, tenant_id, state.phone)
    assert canonical.id == legacy_id
    assert canonical.customer_id is not None
    assert canonical.extra_metadata["brain_state"] == brain
    assert db.query(Conversation).filter(Conversation.tenant_id == tenant_id).count() == 1
