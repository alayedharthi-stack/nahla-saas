"""Persisted merchant identity reaches the existing runtime data channel.

The save handler, SQL read, WhatsApp seam and provider serialization are real.
The runtime ledger/transport is replaced at its entry; inference is a recording
double. These tests prove input delivery, never model wording or live delivery.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from models import Base, Integration, Tenant, TenantSettings
from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_provider as ap
from core.commerce_runtime import runtime_entry as entry
from routers import settings as settings_router
from services import commerce_runtime_pilot as seam


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    tables = [Tenant.__table__, TenantSettings.__table__, Integration.__table__]
    swapped = []
    for table in tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                swapped.append((column, column.type))
                column.type = JSON()
    try:
        Base.metadata.create_all(engine, tables=tables)
    finally:
        for column, original in swapped:
            column.type = original
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory() as db:
        db.add_all([Tenant(id=701, name="متجر تجريبي عام"),
                    Tenant(id=702, name="متجر ملابس تجريبي")])
        db.commit()
    # Unrelated settings response enrichments are outside the identity path.
    monkeypatch.setattr(settings_router, "_sales_channel_availability", lambda *_: {})
    monkeypatch.setattr("core.merchant_payment_methods.load_merchant_payment_methods",
                        lambda *_: SimpleNamespace(to_dict=lambda: {}))
    yield factory
    engine.dispose()


def save_name(sessions, tenant_id, name):
    request = Request({"type": "http", "path": "/settings",
                       "state": {"jwt_payload": {"tenant_id": tenant_id}}})
    with sessions() as db:
        result = asyncio.run(settings_router.update_settings(
            settings_router.AllSettingsIn(ai=settings_router.AISettingsIn(assistant_name=name)),
            request=request, db=db, _no_support={}))
        assert result["ai"]["assistant_name"] == name
    with sessions() as db:
        assert db.query(TenantSettings.ai_settings).filter(
            TenantSettings.tenant_id == tenant_id).scalar()["assistant_name"] == name


@pytest.fixture
def run_input(monkeypatch):
    calls = []

    class RecordingInference:
        def call_single_step(self, **kwargs):
            calls.append(kwargs)
            return {"status": "ok", "model": "configured-test-model",
                    "stop_reason": "tool_use", "blocks": [{
                        "type": "tool_use", "id": "reply-1", "name": "submit_reply",
                        "input": {"text": "test reply", "claims_commerce_facts": False},
                    }]}

    def recording_runtime(**kwargs):
        reasoner = ap.AnthropicReasoningProvider(
            instructions=kwargs["instructions"], tools_provider=RecordingInference(),
            context_preamble=kwargs["context_preamble"], history=kwargs["history"],
            audit_context={"model": kwargs["model"]})
        request = ac.ProviderRequest(
            step_no=1,
            context=ac.AuthorizedContext(
                tenant_id=kwargs["tenant_id"], namespace="live", conversation_id=901,
                turn_id=1, inbound={"text": kwargs["inbound_text"]}, state_payload={}),
            tools=(), observations=(), feedback=(),
            budget=ac.BudgetView(remaining_steps=2, remaining_tool_calls=3, remaining_seconds=20))
        reasoner.step(request)
        return entry.TurnReport(reason=entry.ALREADY_TERMINAL,
                                tenant_id=kwargs["tenant_id"], conversation_id=901)

    def no_send(*args, **kwargs):
        raise AssertionError("identity tests must never send")

    monkeypatch.setattr(entry, "run_commerce_runtime_turn", recording_runtime)
    for factory in ("_send_factory", "_send_list_factory", "_send_card_factory"):
        monkeypatch.setattr(seam, factory, lambda *a, **kw: no_send)
    monkeypatch.setattr(seam, "_prior_turns", lambda *a, **kw: [])
    monkeypatch.setattr(seam, "_settle_deferred", lambda *a, **kw: None)

    def run(db, tenant_id=701, language="ar"):
        asyncio.run(seam._own_turn(
            db=db, tenant_id=tenant_id, phone_id="test-connection", to="test-recipient",
            text="من أنت؟", convo=SimpleNamespace(id=901, customer_id=None, language=language),
            wa_msg_id="test-inbound", inbound_metadata={}, trace=None,
            decision=SimpleNamespace(connection_ref="wa:test-connection", connection_id="7",
                                     recipient="test-recipient", model="configured-test-model"),
            customer_name=""))
        call = calls[-1]
        assert call["system"] == seam._instructions().strip()
        assert call["audit_context"]["model"] == "configured-test-model"
        block = call["messages"][0]["content"][0]["text"]
        return json.loads(block.split("\n", 1)[1].rsplit("\n", 1)[0])

    return run


def test_saved_custom_name_reaches_the_provider_without_cross_tenant_leakage(sessions, run_input):
    save_name(sessions, 701, "وردة")
    save_name(sessions, 702, "Atlas")
    with sessions() as db:
        assert run_input(db, 701)["assistant_name"] == "وردة"
        assert run_input(db, 702)["assistant_name"] == "Atlas"
        assert run_input(db, 701, "en")["assistant_name"] == "وردة"


@pytest.mark.parametrize("stored", [None, {}, {"assistant_name": ""}, {"assistant_name": "  "}])
@pytest.mark.parametrize("language,expected", [("ar", "نحلة"), ("en", "NAHLAH")])
def test_missing_and_blank_names_use_existing_conversation_language(
        sessions, run_input, stored, language, expected):
    with sessions() as db:
        if stored is not None:
            db.add(TenantSettings(tenant_id=701, ai_settings=stored))
            db.commit()
        assert run_input(db, language=language)["assistant_name"] == expected


def test_next_turn_reads_rename_even_with_a_preloaded_orm_object(sessions, run_input):
    save_name(sessions, 701, "وردة")
    with sessions() as db:
        stale = db.query(TenantSettings).filter(TenantSettings.tenant_id == 701).one()
        assert run_input(db)["assistant_name"] == "وردة"
        db.commit()  # End the turn's transaction; keep the session and identity map.
        save_name(sessions, 701, "ياسمين")
        assert stale.ai_settings["assistant_name"] == "وردة"
        assert run_input(db)["assistant_name"] == "ياسمين"


def test_settings_read_failure_is_not_misreported_as_a_missing_name(sessions, run_input, monkeypatch):
    with sessions() as db:
        def unreadable(*args, **kwargs):
            raise RuntimeError("synthetic settings read failure")
        monkeypatch.setattr(db, "query", unreadable)
        with pytest.raises(RuntimeError, match="synthetic settings read failure"):
            run_input(db)
