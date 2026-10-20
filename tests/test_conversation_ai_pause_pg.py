"""Real PostgreSQL proof for pause visibility and the final send-admission race.

The dedicated CI job requires all cases to pass without skips. Ordinary unit
runs skip this module unless the explicit disposable, loopback-only DSN is set.
No application database URL, provider credentials, model or real send is used.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psycopg2
from psycopg2 import sql
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from models import Conversation, Customer, Tenant, TenantSettings
from core.ai_disabled_gate import evaluate_fresh_ai_disabled_send_block
from core.ai_pause_guard import pause_ai
from routers.conversations import _find_conversations_for_phone
from services.whatsapp_platform import service

PHONE = "+12025550101"
TENANT = 701
REAL_PROVIDER_POST = service.provider_post_with_context


@pytest.fixture()
def pg_case(monkeypatch):
    dsn = os.getenv("NAHLA_AI_PAUSE_PG_ADMIN_DSN", "")
    if not dsn:
        if os.getenv("NAHLA_AI_PAUSE_PG_REQUIRED") == "1":
            pytest.fail("required disposable PostgreSQL DSN is missing")
        pytest.skip("disposable PostgreSQL proof runs in its dedicated CI job")
    url = make_url(dsn)
    if url.host not in {"127.0.0.1", "localhost"} or url.username != "nahla_pause_test":
        pytest.fail("pause proof requires its dedicated loopback test server/user")

    # Refuse any accidental real HTTP call, even if a transport mock is lost.
    monkeypatch.setattr("httpx.AsyncClient.post", AsyncMock(
        side_effect=AssertionError("external transport forbidden in pause proof")))
    name = "nahla_pause_test_" + uuid.uuid4().hex
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    engine = create_engine(url.set(database=name), echo=False, pool_pre_ping=True)
    try:
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
                Conversation(id=3, tenant_id=702, status="active", ai_paused=True,
                             ai_paused_reason="manual_pause", extra_metadata={"phone": PHONE}),
            ])
            db.commit()
        yield engine
    finally:
        engine.dispose()
        # Only this fixture-created database is removed. No shared schema.
        assert name.startswith("nahla_pause_test_") and len(name) == 49
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
        admin.close()


def _pause(engine):
    with Session(engine) as control:
        pid = control.scalar(text("select pg_backend_pid()"))
        rows = _find_conversations_for_phone(control, TENANT, PHONE)
        assert {row.id for row in rows} == {1, 2}
        for row in rows:
            pause_ai(control, row, reason="manual_pause", commit=False)
        control.commit()
        return pid


@pytest.mark.parametrize("isolation", ["READ COMMITTED", "REPEATABLE READ"])
def test_two_sessions_see_pause_despite_stale_worker_snapshot(pg_case, isolation):
    engine = pg_case.execution_options(isolation_level=isolation)
    with Session(engine, autoflush=False) as worker:
        held = _find_conversations_for_phone(worker, TENANT, PHONE)
        worker_pid = worker.scalar(text("select pg_backend_pid()"))
        # The other tenant's paused orphan must not stop this tenant.
        assert not evaluate_fresh_ai_disabled_send_block(
            worker, tenant_id=TENANT, customer_phone=PHONE)[0]
        control_pid = _pause(engine)
        assert worker_pid != control_pid
        assert not any(row.ai_paused for row in held)
        assert not any(row.ai_paused for row in _find_conversations_for_phone(worker, TENANT, PHONE))
        blocked, decision = evaluate_fresh_ai_disabled_send_block(
            worker, tenant_id=TENANT, customer_phone=PHONE)
        assert blocked and decision.reason == "manual_pause"


@pytest.fixture()
def preparation(monkeypatch):
    monkeypatch.setattr("core.automation_send_guard.evaluate_automation_send",
                        lambda *a, **kw: SimpleNamespace(block=False))
    monkeypatch.setattr("core.wa_usage.check_limit",
                        lambda *a, **kw: SimpleNamespace(allowed=True))
    monkeypatch.setattr(service, "_scrub_outbound_payload", lambda value: value)
    token = AsyncMock(return_value=SimpleNamespace(source="offline"))
    post = AsyncMock(return_value={"messages": [{"id": "offline-wamid"}]})
    monkeypatch.setattr(service, "_resolve_token", token)
    monkeypatch.setattr(service, "provider_post_with_context", post)
    return token, post


async def _send(worker):
    return await service.provider_send_message(
        worker, None, tenant_id=TENANT, operation="send_message",
        phone_id="offline-phone", payload={
            "to": PHONE, "type": "text", "text": {"body": "Model candidate"},
        })


def test_pause_committed_during_preparation_prevents_transport(pg_case, preparation):
    token, post = preparation
    with Session(pg_case, autoflush=False) as worker:
        held = _find_conversations_for_phone(worker, TENANT, PHONE)
        worker_pid = worker.scalar(text("select pg_backend_pid()"))

        async def finish_preparation(*_args, **_kw):
            assert _pause(pg_case) != worker_pid
            return SimpleNamespace(source="offline")

        token.side_effect = finish_preparation
        result, _ = asyncio.run(_send(worker))
        assert not any(row.ai_paused for row in held)
        assert result["_nahla_classification"] == "automation_blocked"
        assert result["_nahla_block_reason"] == "manual_pause"
        post.assert_not_awaited()


def test_pause_during_client_entry_prevents_http_post(pg_case, preparation, monkeypatch):
    sent = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            await asyncio.sleep(0)
            _pause(pg_case)
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            sent.append(kwargs)
            raise AssertionError("paused send reached HTTP transport")

    monkeypatch.setattr(service, "provider_post_with_context", REAL_PROVIDER_POST)
    monkeypatch.setattr(service.httpx, "AsyncClient", Client)
    monkeypatch.setattr(service, "_provider_url", lambda *args: "https://offline.invalid/messages")
    monkeypatch.setattr(service, "_provider_headers", lambda *args: {})
    monkeypatch.setattr(service, "_record_provider_attempt", lambda **kwargs: None)
    with Session(pg_case, autoflush=False) as worker:
        held = _find_conversations_for_phone(worker, TENANT, PHONE)
        result, _ = asyncio.run(_send(worker))
        assert not any(row.ai_paused for row in held)
        assert result["_nahla_block_reason"] == "manual_pause"
        assert not sent


def test_pause_after_http_invocation_is_not_a_drain_barrier(pg_case, preparation, monkeypatch):
    """Pause may commit inside the HTTP library after its post was invoked."""
    monkeypatch.setattr(service, "provider_post_with_context", REAL_PROVIDER_POST)
    monkeypatch.setattr(service, "_provider_url", lambda *args: "https://offline.invalid/messages")
    monkeypatch.setattr(service, "_provider_headers", lambda *args: {})
    monkeypatch.setattr(service, "_record_provider_attempt", lambda **kwargs: None)
    with Session(pg_case, autoflush=False) as worker:
        _find_conversations_for_phone(worker, TENANT, PHONE)

        async def scenario():
            entered = asyncio.Event()
            finish = asyncio.Event()

            class Client:
                def __init__(self, **kwargs):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

                async def post(self, *_args, **_kw):
                    entered.set()  # The inner admission passed; post is now invoked.
                    await finish.wait()  # Models a library pool/connect wait before bytes.
                    return SimpleNamespace(status_code=200, text="{}", json=lambda: {
                        "messages": [{"id": "offline-in-flight"}]})

            monkeypatch.setattr(service.httpx, "AsyncClient", Client)
            task = asyncio.create_task(_send(worker))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                _pause(pg_case)  # Separate committed control transaction.
                with Session(pg_case) as read:
                    assert all(row.ai_paused for row in _find_conversations_for_phone(
                        read, TENANT, PHONE))
                finish.set()
                result, _ = await asyncio.wait_for(task, timeout=5)
                assert result["messages"][0]["id"] == "offline-in-flight"
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        asyncio.run(scenario())
