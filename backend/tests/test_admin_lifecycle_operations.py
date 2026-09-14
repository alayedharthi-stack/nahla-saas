"""Security and recovery contracts for platform lifecycle operations."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.auth import create_token, require_admin, require_not_support_impersonation
from core.commerce_lifecycle.dispatch import commerce_lifecycle_send_audit_schema_status
from core.commerce_lifecycle.operations import (
    build_lifecycle_preflight,
    build_order_recovery_preflight,
    retry_post_cod_final_confirmation,
)
from core.database import get_db
from models import (
    CommerceLifecycleNotificationLedger,
    Order,
    TenantSettings,
    WaConversationWindow,
    WhatsAppTemplate,
)
from routers.admin_lifecycle_operations import router


PHONE = "+966500111222"
ORDER_ID = 707
TENANT_ID = 9


def _make_db(*models):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved = []
    for model in models:
        table = model.__table__
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
        table.create(engine, checkfirst=True)
    for col, original in saved:
        col.type = original
    return sessionmaker(bind=engine)(), engine


def _complete_db():
    return _make_db(
        Order,
        WhatsAppTemplate,
        CommerceLifecycleNotificationLedger,
        TenantSettings,
        WaConversationWindow,
    )


def _order(**overrides):
    metadata = {
        "payment_method": "cod",
        "is_cod": True,
        "cod_webhook_triggered": True,
        "nahla_cod_confirmation_sent": True,
        "nahla_cod_confirmation_sent_at": "2026-09-14T18:00:00+00:00",
        "nahla_cod_confirmation_wamid": "wamid.initial",
        "nahla_cod_confirmation_execution_id": 9001,
        "cod_confirmed_at": "2026-09-14T18:05:00+00:00",
        "cod_pushed_external_id": "ext-707",
        "cod_previous_status": "in_progress",
    }
    metadata.update(overrides.pop("extra_metadata", {}))
    values = {
        "id": ORDER_ID,
        "tenant_id": TENANT_ID,
        "status": "under_review",
        "external_id": "ext-707",
        "external_order_number": "ORDER-707",
        "customer_name": "نورة عبدالله",
        "customer_info": {"name": "نورة عبدالله", "phone": PHONE},
        "line_items": [],
        "extra_metadata": metadata,
    }
    values.update(overrides)
    return Order(**values)


def _template():
    return WhatsAppTemplate(
        id=808,
        tenant_id=TENANT_ID,
        name="nahla_order_confirmation_r3_dc8a88",
        language="ar",
        category="UTILITY",
        status="APPROVED",
        components=[{"type": "BODY", "text": "{{1}} {{2}}"}],
        service_key="order_confirmation",
        is_active=True,
        is_hidden=False,
        revision=3,
    )


def _seed_eligible(db):
    db.add(_order())
    db.add(_template())
    db.commit()


def _route_dependency_names(path: str, method: str) -> set[str]:
    for route in router.routes:
        if route.path == path and method in route.methods:
            return {
                getattr(dep.call, "__name__", repr(dep.call))
                for dep in route.dependant.dependencies
            }
    return set()


def test_operator_routes_require_admin_and_block_support_impersonation():
    for path, method in (
        ("/admin/operations/commerce-lifecycle/preflight", "GET"),
        ("/admin/operations/commerce-lifecycle/orders/{order_id}/preflight", "GET"),
        ("/admin/operations/orders/{order_id}/retry-final-confirmation", "POST"),
    ):
        deps = _route_dependency_names(path, method)
        assert "require_admin" in deps
        assert "require_not_support_impersonation" in deps


def test_unauthenticated_and_merchant_requests_are_rejected():
    db, engine = _complete_db()
    app = FastAPI()
    app.include_router(router)

    def _db_override():
        yield db

    app.dependency_overrides[get_db] = _db_override
    client = TestClient(app)
    assert client.get("/admin/operations/commerce-lifecycle/preflight").status_code == 401

    merchant = create_token("merchant@example.test", "merchant", TENANT_ID)
    response = client.get(
        "/admin/operations/commerce-lifecycle/preflight",
        headers={"Authorization": f"Bearer {merchant}"},
    )
    assert response.status_code == 403
    db.close()
    engine.dispose()


def test_schema_preflight_reports_missing_0094_0095_columns():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE commerce_lifecycle_notification_ledger (id INTEGER PRIMARY KEY)"
        )
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('0093')")
    db = sessionmaker(bind=engine)()
    result = build_lifecycle_preflight(db)
    assert result["schema_ready"] is False
    assert result["ledger_table_present"] is True
    assert "send_state" in result["missing_columns"]
    assert "send_method" in result["missing_columns"]
    assert result["alembic_revisions"] == ["0093"]
    db.close()
    engine.dispose()


def test_schema_preflight_reports_complete_contract():
    db, engine = _complete_db()
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('0095')")
    result = build_lifecycle_preflight(db)
    assert result["schema_ready"] is True
    assert result["missing_columns"] == []
    assert set(result["required_columns_present"]) == set(result["required_columns"])
    assert result["alembic_revisions"] == ["0095"]
    db.close()
    engine.dispose()


def test_order_preflight_masks_phone_and_exposes_no_secrets(monkeypatch):
    db, engine = _complete_db()
    _seed_eligible(db)
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", str(TENANT_ID))
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST", PHONE)
    result = build_order_recovery_preflight(db, order_id=ORDER_ID)
    assert result["recipient_masked"] == "*1222"
    assert PHONE not in repr(result)
    assert "DATABASE_URL" not in repr(result)
    assert result["tenant_permitted"] is True
    assert result["recipient_permitted"] is True
    assert result["resolved_template_name"] == "nahla_order_confirmation_r3_dc8a88"
    db.close()
    engine.dispose()


@pytest.mark.parametrize(
    ("order_changes", "metadata_changes", "expected_reason"),
    [
        ({"status": "in_progress"}, {}, "status_not_under_review"),
        ({}, {"payment_method": "card", "is_cod": False}, "cod_evidence_missing"),
        (
            {},
            {"nahla_cod_confirmation_sent": False},
            "initial_cod_confirmation_evidence_missing",
        ),
    ],
)
def test_retry_rejects_ineligible_order(
    monkeypatch,
    order_changes,
    metadata_changes,
    expected_reason,
):
    db, engine = _complete_db()
    db.add(_order(**order_changes, extra_metadata=metadata_changes))
    db.add(_template())
    db.commit()
    send = AsyncMock()
    monkeypatch.setattr("services.cod_confirmation.send_order_confirmation_after_cod", send)
    result = asyncio.run(retry_post_cod_final_confirmation(db, order_id=ORDER_ID))
    assert result["sent"] is False
    assert expected_reason in result["eligibility"]["ineligibility_reasons"]
    send.assert_not_awaited()
    db.close()
    engine.dispose()


def test_retry_uses_canonical_path_and_never_imports_salla_or_brain(monkeypatch):
    db, engine = _complete_db()
    _seed_eligible(db)
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", str(TENANT_ID))
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST", PHONE)
    send = AsyncMock(
        return_value={
            "sent": True,
            "duplicate": False,
            "error": None,
            "provider_message_id": "wamid.final",
            "ledger_id": 303,
        }
    )
    monkeypatch.setattr("services.cod_confirmation.send_order_confirmation_after_cod", send)
    result = asyncio.run(retry_post_cod_final_confirmation(db, order_id=ORDER_ID))
    assert result == {
        "outcome": "sent",
        "sent": True,
        "duplicate": False,
        "service_key": "order_confirmation",
        "template_name": "nahla_order_confirmation_r3_dc8a88",
        "ledger_id": 303,
        "provider_message_id": "wamid.final",
        "eligibility": result["eligibility"],
    }
    send.assert_awaited_once()
    assert send.await_args.kwargs["tenant_id"] == TENANT_ID
    assert send.await_args.kwargs["order"].id == ORDER_ID

    source = __import__("inspect").getsource(retry_post_cod_final_confirmation)
    assert "update_order_status" not in source
    assert "store_integration.order_service" not in source
    assert "Brain" not in source
    db.close()
    engine.dispose()


def test_successful_retry_sends_once_and_second_attempt_is_duplicate(monkeypatch):
    db, engine = _complete_db()
    _seed_eligible(db)
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", str(TENANT_ID))
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST", PHONE)
    provider = AsyncMock(return_value=("sent", {"wa_message_id": "wamid.final"}))
    with patch(
        "core.automation_engine.send_lifecycle_whatsapp_template",
        provider,
    ), patch(
        "core.automation_engine.send_lifecycle_whatsapp_session_body",
        provider,
    ), patch(
        "core.commerce_lifecycle.window.lifecycle_service_window_is_open",
        return_value=(False, "test"),
    ), patch(
        "core.merchant_capabilities.resolve_merchant_capabilities",
        return_value=SimpleNamespace(to_dict=lambda: {}),
    ):
        first = asyncio.run(retry_post_cod_final_confirmation(db, order_id=ORDER_ID))
        second = asyncio.run(retry_post_cod_final_confirmation(db, order_id=ORDER_ID))

    assert first["outcome"] == "sent"
    assert first["duplicate"] is False
    assert first["provider_message_id"] == "wamid.final"
    assert second["outcome"] == "duplicate"
    assert second["duplicate"] is True
    provider.assert_awaited_once()
    assert db.query(CommerceLifecycleNotificationLedger).count() == 1
    db.close()
    engine.dispose()


@pytest.mark.parametrize(
    ("enabled", "tenants", "recipients", "expected"),
    [
        ("false", str(TENANT_ID), PHONE, "dispatch_disabled"),
        ("true", "99", PHONE, "tenant_not_allowlisted"),
        ("true", str(TENANT_ID), "+966500999888", "recipient_not_allowlisted"),
    ],
)
def test_canary_failures_remain_fail_closed(
    monkeypatch,
    enabled,
    tenants,
    recipients,
    expected,
):
    db, engine = _complete_db()
    _seed_eligible(db)
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", enabled)
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", tenants)
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST", recipients)
    result = asyncio.run(retry_post_cod_final_confirmation(db, order_id=ORDER_ID))
    assert result["outcome"] == expected
    assert result["sent"] is False
    assert db.query(CommerceLifecycleNotificationLedger).count() == 0
    db.close()
    engine.dispose()


def test_schema_status_is_the_same_runtime_inspection_used_by_dispatch():
    db, engine = _complete_db()
    status = commerce_lifecycle_send_audit_schema_status(db)
    assert status["schema_ready"] is True
    assert "send_method" in status["required_columns_present"]
    db.close()
    engine.dispose()
