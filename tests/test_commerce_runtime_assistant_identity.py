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

from core.tenant import DEFAULT_AI
from models import Base, Customer, Integration, Tenant, TenantSettings
from core.commerce_runtime import agent_contracts as ac
from core.commerce_runtime import agent_provider as ap
from core.commerce_runtime import runtime_entry as entry
from routers import settings as settings_router
from services import commerce_runtime_pilot as seam


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    tables = [Tenant.__table__, TenantSettings.__table__, Integration.__table__,
              Customer.__table__]
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

    def run(db, tenant_id=701, language="ar", customer_id=None,
            recipient="+966500000001"):
        asyncio.run(seam._own_turn(
            db=db, tenant_id=tenant_id, phone_id="test-connection", to=recipient,
            text="من أنت؟", convo=SimpleNamespace(id=901, customer_id=customer_id, language=language),
            wa_msg_id="test-inbound", inbound_metadata={}, trace=None,
            decision=SimpleNamespace(connection_ref="wa:test-connection", connection_id="7",
                                     recipient=recipient, model="configured-test-model")))
        call = calls[-1]
        assert call["audit_context"]["model"] == "configured-test-model"
        block = call["messages"][0]["content"][0]["text"]
        facts = json.loads(block.split("\n", 1)[1].rsplit("\n", 1)[0])
        # The saved name is data beside the customer's turn, never written into
        # the instructions.
        if facts.get("assistant_name") not in (None, DEFAULT_AI["assistant_name"]):
            assert facts["assistant_name"] not in call["system"]
        assert "assistant_name" not in call["system"]
        if facts.get("verified_customer_name"):
            assert facts["verified_customer_name"] not in call["system"]
        return facts

    return run


def test_only_a_phone_and_tenant_bound_approved_customer_name_reaches_the_provider(
        sessions, run_input):
    with sessions() as db:
        db.add_all([
            Customer(id=801, tenant_id=701, name="نورة عبدالله",
                     phone="0500000001", normalized_phone="+966500000001",
                     extra_metadata={"customer_name_source": "salla_order",
                                     "customer_name_status": "verified"}),
            Customer(id=802, tenant_id=702, name="أحمد سالم",
                     phone="0500000001", normalized_phone="+966500000001",
                     extra_metadata={"customer_name_source": "salla_order",
                                     "customer_name_status": "verified"}),
            Customer(id=803, tenant_id=701, name="ملف واتساب",
                     phone="0500000002", normalized_phone="+966500000002",
                     extra_metadata={"customer_name_source": "whatsapp_profile",
                                     "customer_name_status": "proposed"}),
        ])
        db.commit()

    with sessions() as db:
        # A returning clothing-store customer can be greeted by the name that
        # is already verified in the store, even when the webhook sends no
        # separate name string to the runtime.
        assert run_input(db, customer_id=801)["verified_customer_name"] == "نورة عبدالله"
        assert run_input(db, tenant_id=702, customer_id=802)["verified_customer_name"] == "أحمد سالم"
        assert "verified_customer_name" not in run_input(
            db, customer_id=801, recipient="+966500000099")
        assert "verified_customer_name" not in run_input(db, customer_id=802)
        assert "verified_customer_name" not in run_input(
            db, customer_id=803, recipient="+966500000002")
        assert "verified_customer_name" not in run_input(db, customer_id=None)


def test_a_legacy_row_without_status_metadata_is_judged_by_its_recorded_source(
        sessions, run_input):
    """Rows synced before the identity resolver carry no status, only a source
    or an acquisition channel. The existing resolver infers the status from it;
    nothing here adds a rule. A synced ecommerce name is approved, a WhatsApp
    lead's name is a proposal, and a row with no source is not approved."""
    with sessions() as db:
        db.add_all([
            # perfume-store customer synced from the store; number stored only locally
            Customer(id=811, tenant_id=701, name="نورة عبدالله", phone="0500000001",
                     acquisition_channel="salla", extra_metadata={}),
            Customer(id=812, tenant_id=701, name="Nora 🌸", phone="0500000002",
                     acquisition_channel="whatsapp_inbound", extra_metadata={}),
            Customer(id=813, tenant_id=701, name="أحمد سالم", phone="0500000003",
                     extra_metadata={}),
            Customer(id=814, tenant_id=701, name="سارة محمد", phone="0500000004",
                     extra_metadata={"name_source": "salla_sync"}),
        ])
        db.commit()
    with sessions() as db:
        assert run_input(db, customer_id=811)["verified_customer_name"] == "نورة عبدالله"
        assert "verified_customer_name" not in run_input(
            db, customer_id=812, recipient="+966500000002")
        assert "verified_customer_name" not in run_input(
            db, customer_id=813, recipient="+966500000003")
        assert run_input(db, customer_id=814,
                         recipient="+966500000004")["verified_customer_name"] == "سارة محمد"


def test_a_rejected_or_cleared_name_never_reaches_the_provider(sessions, run_input):
    with sessions() as db:
        db.add_all([
            Customer(id=821, tenant_id=701, name="الحمد لله", phone="0500000001",
                     extra_metadata={"customer_name_source": "customer_message",
                                     "customer_name_status": "rejected"}),
            Customer(id=822, tenant_id=701, name=None, phone="0500000002",
                     extra_metadata={"customer_name_source": "salla_order",
                                     "customer_name_status": "verified",
                                     "manual_name_cleared": True}),
        ])
        db.commit()
    with sessions() as db:
        assert "verified_customer_name" not in run_input(db, customer_id=821)
        assert "verified_customer_name" not in run_input(
            db, customer_id=822, recipient="+966500000002")


def test_a_failed_name_read_keeps_the_turn_and_its_session(sessions, run_input, monkeypatch):
    """The name is one fact; losing it must not cost the turn, and the session
    that holds the inbound turn stays usable with its pending work intact."""
    import core.customer_identity_resolver as resolver

    with sessions() as db:
        db.add(Customer(id=831, tenant_id=701, name="نورة", phone="0500000001",
                        normalized_phone="+966500000001",
                        extra_metadata={"customer_name_status": "verified"}))
        db.commit()

    def broken(_customer):
        raise RuntimeError("identity unreadable")

    monkeypatch.setattr(resolver, "read_customer_identity", broken)
    with sessions() as db:
        pending = Customer(id=832, tenant_id=701, name="عميل جديد", phone="0500000009")
        db.add(pending)
        facts = run_input(db, customer_id=831)
        assert "verified_customer_name" not in facts
        assert facts["assistant_name"]                       # the rest of the context still went
        db.commit()
    with sessions() as db:
        assert db.get(Customer, 832) is not None             # nothing pending was lost


def test_saved_custom_name_reaches_the_provider_without_cross_tenant_leakage(sessions, run_input):
    save_name(sessions, 701, "وردة")
    save_name(sessions, 702, "Atlas")
    with sessions() as db:
        assert run_input(db, 701)["assistant_name"] == "وردة"
        assert run_input(db, 702)["assistant_name"] == "Atlas"
        assert run_input(db, 701, "en")["assistant_name"] == "وردة"


@pytest.mark.parametrize("stored", [None, {}, {"assistant_name": ""}, {"assistant_name": "  "}])
@pytest.mark.parametrize("language", ["ar", "en", ""])
def test_missing_and_blank_names_use_the_platforms_own_default(sessions, run_input, stored, language):
    with sessions() as db:
        if stored is not None:
            db.add(TenantSettings(tenant_id=701, ai_settings=stored))
            db.commit()
        assert run_input(db, language=language)["assistant_name"] == DEFAULT_AI["assistant_name"]


def test_next_turn_reads_rename_even_with_a_preloaded_orm_object(sessions, run_input):
    save_name(sessions, 701, "وردة")
    with sessions() as db:
        stale = db.query(TenantSettings).filter(TenantSettings.tenant_id == 701).one()
        assert run_input(db)["assistant_name"] == "وردة"
        db.commit()  # End the turn's transaction; keep the session and identity map.
        save_name(sessions, 701, "ياسمين")
        assert stale.ai_settings["assistant_name"] == "وردة"
        assert run_input(db)["assistant_name"] == "ياسمين"


def _logged_errors(monkeypatch):
    """The pilot's own error log, observed directly rather than through logging
    configuration another suite may have changed."""
    errors = []
    monkeypatch.setattr(seam.logger, "error", lambda message, *args: errors.append(message % args))
    return errors


def test_settings_read_failure_is_not_misreported_and_the_turn_still_goes(
        sessions, run_input, monkeypatch):
    """A failed read is neither a missing name nor a dropped turn: the model is
    reached without the name, and the failure is logged."""
    errors = _logged_errors(monkeypatch)
    with sessions() as db:
        real_query = db.query

        def unreadable(*args, **kwargs):
            if args and "ai_settings" in str(args[0]):
                raise RuntimeError("synthetic settings read failure")
            return real_query(*args, **kwargs)
        monkeypatch.setattr(db, "query", unreadable)
        facts = run_input(db)
        assert "assistant_name" not in facts
        assert any("assistant name unreadable" in line for line in errors)


@pytest.mark.parametrize("stored", ["وردة", ["وردة"], '{"assistant_name": "وردة"}',
                                    {"assistant_name": {"ar": "وردة"}}, {"assistant_name": 7}])
def test_settings_that_are_not_an_object_leave_the_name_out(sessions, run_input, stored, monkeypatch):
    errors = _logged_errors(monkeypatch)
    with sessions() as db:
        db.add(TenantSettings(tenant_id=701, ai_settings=stored))
        db.commit()
        facts = run_input(db)
        assert "assistant_name" not in facts
        assert any("not an object with a text name" in line for line in errors)
