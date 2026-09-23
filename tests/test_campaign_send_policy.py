"""Merchant template campaigns remain independent of automatic AI replies.

Exercise the wire gate and real store-mode reader; only persistence lookups,
quota results, credentials and external provider IO are stubbed.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.meta_errors import classify_meta_error
from services.whatsapp_platform import service


@pytest.fixture
def wire(monkeypatch):
    settings = SimpleNamespace(ai_settings={
        "store_ai_mode": "test", "ai_test_allowed_numbers": ["966500000001"],
    })
    conversations = [SimpleNamespace(id=1, customer_id=7, ai_paused=False)]
    blocked = MagicMock(return_value=(False, None))
    lookup = MagicMock(side_effect=lambda *a: conversations)
    quota = MagicMock(return_value=SimpleNamespace(
        allowed=True, used_total=0, limit=200, reason="",
    ))
    token = AsyncMock(return_value=MagicMock(token="test", source="test"))
    post = AsyncMock(return_value={"messages": [{"id": "wamid.test"}]})
    monkeypatch.setattr("core.tenant.get_or_create_settings", lambda *a: settings)
    monkeypatch.setattr("core.automation_send_guard.is_internal_or_blocked", blocked)
    monkeypatch.setattr("core.ai_disabled_gate._find_conversations_for_phone", lookup)
    monkeypatch.setattr("core.wa_usage.check_limit", quota)
    monkeypatch.setattr(service, "get_token_for_operation", token)
    monkeypatch.setattr(service, "provider_post_with_context", post)
    db, conn = MagicMock(), SimpleNamespace(extra_metadata={}, provider="meta")

    def send(operation="campaign_send", message_type="template", tenant_id=91):
        payload = {"messaging_product": "whatsapp", "to": "966500000002",
                   "type": message_type,
                   "template": {"name": "seasonal_offer", "language": {"code": "ar"}}}
        return asyncio.run(service.provider_send_message(
            db, conn, tenant_id=tenant_id, operation=operation,
            phone_id="PH1", payload=payload,
        ))[0]

    return SimpleNamespace(send=send, settings=settings, conversations=conversations,
                           blocked=blocked, lookup=lookup, quota=quota, token=token,
                           post=post, db=db)


@pytest.mark.parametrize("mode", ["test", "off", "on"])
@pytest.mark.parametrize("tenant_id", [91, 192])
def test_campaign_templates_send_without_changing_ai_mode(wire, mode, tenant_id):
    wire.settings.ai_settings["store_ai_mode"] = mode
    before = deepcopy(wire.settings.ai_settings)
    assert "messages" in wire.send(tenant_id=tenant_id)
    wire.post.assert_awaited_once()
    wire.quota.assert_called_once()
    wire.lookup.assert_called_once_with(wire.db, tenant_id, "966500000002")
    assert wire.settings.ai_settings == before
    assert not wire.conversations[0].ai_paused


@pytest.mark.parametrize("mode", ["test", "off"])
@pytest.mark.parametrize("operation,message_type", [
    ("send_message", "text"), ("send_message", "template"),
    ("campaign_send", "text"),
])
def test_ai_and_non_template_sends_keep_store_mode_guard(wire, mode, operation, message_type):
    wire.settings.ai_settings["store_ai_mode"] = mode
    result = wire.send(operation, message_type)
    assert result["_nahla_classification"] == "automation_blocked"
    assert result["_nahla_block_reason"] == (
        "store_ai_disabled" if mode == "off" else "store_ai_test_mode_not_allowed"
    )
    wire.token.assert_not_awaited()
    wire.post.assert_not_awaited()


def test_campaign_preserves_pause_on_any_sibling_conversation(wire):
    wire.conversations.append(SimpleNamespace(id=2, customer_id=7, ai_paused=True))
    result = wire.send()
    assert result["_nahla_block_reason"] == "ai_disabled"
    assert wire.conversations[1].ai_paused
    wire.token.assert_not_awaited()
    wire.post.assert_not_awaited()


def test_campaign_keeps_internal_and_blocked_number_guard(wire):
    wire.blocked.return_value = (True, "internal_number")
    assert wire.send()["_nahla_block_reason"] == "blocked_number"
    wire.token.assert_not_awaited()
    wire.post.assert_not_awaited()


def test_campaign_keeps_monthly_quota(wire):
    wire.quota.return_value = SimpleNamespace(
        allowed=False, used_total=200, limit=200, reason="conversation_limit_reached",
    )
    assert wire.send()["_nahla_classification"] == "conversation_quota_blocked"
    wire.token.assert_not_awaited()
    wire.post.assert_not_awaited()


@pytest.mark.parametrize("failing_reader", ["lookup", "blocked"])
def test_campaign_safety_lookup_failure_does_not_send(wire, failing_reader):
    getattr(wire, failing_reader).side_effect = RuntimeError("database unavailable")
    assert wire.send()["_nahla_block_reason"] == "campaign_safety_unavailable"
    wire.token.assert_not_awaited()
    wire.post.assert_not_awaited()


@pytest.mark.parametrize("message", [
    "Outbound send blocked: conversation under human supervision or AI disabled",
    "Outbound send blocked by Nahla safety policy: ai_disabled",
    "Outbound send blocked by Nahla safety policy: campaign_safety_unavailable",
])
def test_internal_guard_error_is_not_unknown_meta_or_automatically_retryable(message):
    error = classify_meta_error(code="automation_blocked", error_type="AutomationBlocked",
                                message=message)
    assert error.key == "automation_blocked"
    assert not error.retryable
    assert error.quality_tier == "harmless"
    assert not error.suppress_on_repeat
    assert not error.provider_billing_block


@pytest.mark.parametrize("paused_owner", [None, "same_tenant", "other_tenant"])
def test_persisted_sibling_pause_and_tenant_isolation(paused_owner):
    from sqlalchemy import JSON, create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker

    from core.automation_send_guard import evaluate_campaign_send
    from models import Base, Conversation, Customer, Tenant

    engine = create_engine("sqlite:///:memory:")
    saved = []
    try:
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if isinstance(column.type, JSONB):
                    saved.append((column, column.type))
                    column.type = JSON()
        Base.metadata.create_all(engine)
    finally:
        for column, original in saved:
            column.type = original
    try:
        with sessionmaker(bind=engine)() as db:
            tenants = [Tenant(name="Generic clothing store"), Tenant(name="Generic perfume store")]
            db.add_all(tenants)
            db.flush()
            for index, tenant in enumerate(tenants):
                customer = Customer(tenant_id=tenant.id, phone="966500000002",
                                    normalized_phone="+966500000002")
                db.add(customer)
                db.flush()
                for sibling in range(2):
                    paused = sibling == 1 and paused_owner == (
                        "same_tenant" if index == 0 else "other_tenant"
                    )
                    db.add(Conversation(tenant_id=tenant.id, customer_id=customer.id,
                                        status="active", ai_paused=paused))
            db.commit()
            decision = evaluate_campaign_send(
                db, tenant_id=tenants[0].id, customer_phone="966500000002",
                blocked_path="campaign_send",
            )
            assert decision.block == (paused_owner == "same_tenant")
            assert decision.reason == ("ai_disabled" if decision.block else "")
    finally:
        engine.dispose()
