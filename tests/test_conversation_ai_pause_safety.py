"""Conversation pause durability and last-mile admission; no model or network."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from models import Conversation, Customer, Tenant, TenantSettings
from core import ai_disabled_gate as gate
from core.ai_pause_guard import pause_ai
from routers import conversations as router
from services.whatsapp_platform import service

PHONE = "+12025550101"
TENANT = 701


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(_type, _compiler, **_kw):
    return "JSON"


@pytest.fixture()
def local_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pause.sqlite'}")
    for model in (Tenant, TenantSettings, Customer, Conversation):
        model.__table__.create(engine)
    with Session(engine) as db:
        db.add_all([Tenant(id=TENANT, name="Clothing store"),
                    Tenant(id=702, name="Stationery store")])
        db.flush()
        db.add(TenantSettings(tenant_id=TENANT, ai_settings={"store_ai_mode": "on"}))
        db.add(Customer(id=1, tenant_id=TENANT, phone=PHONE, normalized_phone=PHONE))
        db.flush()
        db.add_all([
            Conversation(id=1, tenant_id=TENANT, customer_id=1, status="active"),
            Conversation(id=2, tenant_id=TENANT, customer_id=1, status="active"),
            Conversation(id=3, tenant_id=702, status="active",
                         ai_paused=True, ai_paused_reason="manual_pause",
                         extra_metadata={"phone": PHONE}),
        ])
        db.commit()
    with Session(engine, autoflush=False) as db:
        yield engine, db
    engine.dispose()


def _pause_in_other_session(engine):
    with Session(engine) as db:
        for row in router._find_conversations_for_phone(db, TENANT, PHONE):
            pause_ai(db, row, reason="manual_pause", commit=False)
        db.commit()


def _fresh(db, **kwargs):
    return gate.evaluate_fresh_ai_disabled_send_block(
        db, tenant_id=TENANT, customer_phone=PHONE, **kwargs)


def test_fresh_read_sees_pause_while_worker_holds_old_objects(local_db):
    engine, worker = local_db
    held = router._find_conversations_for_phone(worker, TENANT, PHONE)
    assert len(held) == 2 and not any(row.ai_paused for row in held)
    _pause_in_other_session(engine)
    # Re-querying alone demonstrably reuses the old ORM objects.
    assert not any(row.ai_paused for row in router._find_conversations_for_phone(worker, TENANT, PHONE))
    blocked, decision = _fresh(worker)
    assert blocked and decision.reason == "manual_pause"
    assert not any(row.ai_paused for row in held)


def test_connection_bound_worker_uses_independent_connection(local_db):
    engine, _ = local_db
    with engine.connect() as connection, Session(bind=connection) as worker:
        held = router._find_conversations_for_phone(worker, TENANT, PHONE)
        _pause_in_other_session(engine)
        assert not any(row.ai_paused for row in held)
        assert _fresh(worker)[0]


def test_other_tenant_pause_does_not_block_and_check_does_not_flush_worker(local_db):
    engine, worker = local_db
    held = worker.get(Conversation, 1)
    held.ai_paused = True  # Uncommitted worker state is not authoritative.
    blocked, _ = _fresh(worker)
    assert not blocked
    with Session(engine) as db:
        assert db.get(Conversation, 1).ai_paused is False


def test_fresh_read_failure_blocks(local_db, monkeypatch):
    _, db = local_db
    monkeypatch.setattr(db, "get_bind", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    blocked, decision = _fresh(db)
    assert blocked and decision.reason == "ai_state_unavailable"


def test_fresh_query_failure_blocks(local_db):
    engine, db = local_db
    def fail_read(*_args):
        raise RuntimeError("injected SELECT failure")
    event.listen(engine, "before_cursor_execute", fail_read)
    try:
        blocked, decision = _fresh(db)
    finally:
        event.remove(engine, "before_cursor_execute", fail_read)
    assert blocked and decision.reason == "ai_state_unavailable"


def test_campaign_store_mode_exception_still_honors_pause(local_db):
    engine, db = local_db
    with Session(engine) as settings_db:
        settings = settings_db.query(TenantSettings).filter_by(tenant_id=TENANT).one()
        settings.ai_settings = {"store_ai_mode": "off", "store_ai_enabled": False}
        settings_db.commit()
    assert _fresh(db)[0]
    assert not _fresh(db, check_store_mode=False)[0]
    _pause_in_other_session(engine)
    assert _fresh(db, check_store_mode=False)[0]


def test_endpoint_commits_all_siblings_and_is_idempotent(local_db, monkeypatch):
    engine, db = local_db
    monkeypatch.setattr(router, "resolve_tenant_id", lambda _r: TENANT)
    body = router.AIPauseIn(customer_phone=PHONE, reason="manual_pause")
    first = asyncio.run(router.pause_conversation_ai(body, SimpleNamespace(), db))
    second = asyncio.run(router.pause_conversation_ai(body, SimpleNamespace(), db))
    assert first["ok"] and first["aiPaused"] and second["aiPaused"]
    with Session(engine) as read:
        rows = router._find_conversations_for_phone(read, TENANT, PHONE)
        assert len(rows) == 2 and all(row.ai_paused for row in rows)


@pytest.mark.parametrize("failure", ["second_flush", "commit"])
def test_endpoint_failure_rolls_back_all_siblings(local_db, monkeypatch, caplog, failure):
    engine, db = local_db
    monkeypatch.setattr(router, "resolve_tenant_id", lambda _r: TENANT)
    flush = db.flush
    calls = 0

    def failing_flush(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second flush")
        return flush(*args, **kwargs)

    if failure == "second_flush":
        monkeypatch.setattr(db, "flush", failing_flush)
    else:
        monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("injected commit")))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(router.pause_conversation_ai(
            router.AIPauseIn(customer_phone=PHONE, reason="manual_pause"), SimpleNamespace(), db))
    assert exc.value.status_code == 503
    assert exc.value.detail == "ai_pause_persistence_failed"
    with Session(engine) as read:
        assert not any(row.ai_paused for row in router._find_conversations_for_phone(read, TENANT, PHONE))
    assert "[AI_PAUSE_API] tenant=" not in caplog.text
    assert "[ai_pause] PAUSED " not in caplog.text


def test_pause_helper_propagates_commit_failure(local_db, monkeypatch):
    engine, db = local_db
    row = db.get(Conversation, 1)
    def fail_commit():
        raise RuntimeError("injected commit")
    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="injected commit"):
        pause_ai(db, row, reason="manual_pause")
    with Session(engine) as read:
        assert read.get(Conversation, 1).ai_paused is False


@pytest.fixture()
def fake_transport(monkeypatch):
    # Stub only preparations/transport; final fresh pause read remains real.
    token = AsyncMock(return_value=SimpleNamespace(source="offline"))
    post = AsyncMock(return_value={"messages": [{"id": "offline-wamid"}]})
    monkeypatch.setattr(service, "_resolve_token", token)
    monkeypatch.setattr(service, "provider_post_with_context", post)
    monkeypatch.setattr(service, "_scrub_outbound_payload", lambda payload: payload)
    monkeypatch.setattr("core.automation_send_guard.evaluate_automation_send",
                        lambda *a, **kw: SimpleNamespace(block=False))
    return token, post


async def _send(db, **kwargs):
    return await service.provider_send_message(
        db, None, tenant_id=TENANT, operation=kwargs.pop("operation", "send_message"),
        phone_id="offline-phone-id",
        payload={"to": PHONE, "type": "text", "text": {"body": "Candidate from model"}},
        **kwargs)


def test_pause_during_awaited_preparation_blocks_wire(local_db, fake_transport):
    engine, db = local_db
    token, post = fake_transport

    async def pause_while_resolving(*args, **kwargs):
        _pause_in_other_session(engine)
        return SimpleNamespace(source="offline")

    token.side_effect = pause_while_resolving
    result, _ = asyncio.run(_send(db))
    assert result["_nahla_classification"] == "automation_blocked"
    post.assert_not_awaited()


@pytest.mark.parametrize("next_operation", ["send_message", "send_message_retry"])
def test_each_part_and_retry_rechecks_pause(local_db, fake_transport, next_operation):
    engine, db = local_db
    _, post = fake_transport
    assert "error" not in asyncio.run(_send(db))[0]
    _pause_in_other_session(engine)
    result, _ = asyncio.run(_send(db, operation=next_operation))
    assert result["_nahla_classification"] == "automation_blocked"
    assert post.await_count == 1


@pytest.mark.parametrize("exception_policy", [{"allow_manual": True}, {"automation_guard": False}])
def test_manual_and_lifecycle_exceptions_unchanged(local_db, fake_transport, exception_policy):
    engine, db = local_db
    _, post = fake_transport
    _pause_in_other_session(engine)
    assert "error" not in asyncio.run(_send(db, **exception_policy))[0]
    post.assert_awaited_once()


def test_early_guard_error_is_not_permission_to_send(local_db, fake_transport, monkeypatch):
    _, db = local_db
    token, post = fake_transport

    def failed(*args, **kwargs):
        raise RuntimeError("injected verification failure")

    monkeypatch.setattr("core.automation_send_guard.evaluate_automation_send", failed)
    result, _ = asyncio.run(_send(db))
    assert result["_nahla_block_reason"] == "ai_state_unavailable"
    token.assert_not_awaited()
    post.assert_not_awaited()


def test_final_verification_error_does_not_send(local_db, fake_transport, monkeypatch):
    _, db = local_db
    _, post = fake_transport
    monkeypatch.setattr(db, "get_bind", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    result, _ = asyncio.run(_send(db))
    assert result["_nahla_block_reason"] == "ai_state_unavailable"
    post.assert_not_awaited()


def test_merchant_send_without_database_is_blocked(fake_transport):
    _, post = fake_transport
    result, _ = asyncio.run(_send(None))
    assert result["_nahla_block_reason"] == "ai_state_unavailable"
    post.assert_not_awaited()


def test_allowed_candidate_reaches_transport_unchanged(local_db, fake_transport):
    _, db = local_db
    _, post = fake_transport
    assert "error" not in asyncio.run(_send(db))[0]
    assert post.await_args.kwargs["json"]["text"] == {"body": "Candidate from model"}


def test_final_check_is_read_only(local_db):
    engine, db = local_db
    statements = []
    def collect(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lstrip().split()[0].upper())
    event.listen(engine, "before_cursor_execute", collect)
    try:
        assert not _fresh(db)[0]
    finally:
        event.remove(engine, "before_cursor_execute", collect)
    assert statements and set(statements) == {"SELECT"}
