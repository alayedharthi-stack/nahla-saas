"""AD-META-T1-1: standard Cloud API is the default Meta onboarding path."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR, REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, Tenant, TenantSettings, WhatsAppConnection  # noqa: E402
from core.trial_lifecycle import init_new_tenant_trial_state  # noqa: E402
from routers.whatsapp_embedded import (  # noqa: E402
    ConfirmStandardCloudApiRequest,
    _finalize_coexistence_exchange,
    _project_phone_sync_state,
    confirm_standard_cloud_api,
    sync_embedded_connection_from_meta,
)
from services.meta_coexistence import (  # noqa: E402
    COEXISTENCE_NOT_ELIGIBLE,
    clear_obsolete_coexistence_state,
    maybe_fail_sync_deadline,
    project_coexistence_sync_state,
    should_project_as_coexistence,
)

PHONE_ID = "pn-generic-cloud-1"
WABA_ID = "waba-generic-cloud-1"
BIZ_APP_PHONE_ID = "pn-generic-biz-app-1"
BIZ_APP_WABA_ID = "waba-generic-biz-app-1"


def _make_db():
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
    return sessionmaker(bind=engine)


@pytest.fixture
def db():
    Session = _make_db()
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _tenant(db, *, name="متجر تجريبي عام"):
    tenant = Tenant(name=name, is_active=True)
    init_new_tenant_trial_state(tenant)
    db.add(tenant)
    db.flush()
    return tenant


def _ai_settings(db, tenant_id):
    row = TenantSettings(
        tenant_id=tenant_id,
        ai_settings={"store_ai_mode": "on", "store_ai_enabled": True},
        store_settings={"store_name": "متجر تجريبي عام"},
        whatsapp_settings={},
    )
    db.add(row)
    db.flush()
    return row


def _conn(db, tenant_id, **overrides):
    kwargs = {
        "tenant_id": tenant_id,
        "status": "failed",
        "phone_number_id": PHONE_ID,
        "whatsapp_business_account_id": WABA_ID,
        "provider": "meta",
        "connection_type": "embedded",
        "webhook_verified": False,
        "sending_enabled": False,
        "extra_metadata": {},
        "phone_number": "+966500000099",
        "business_display_name": "متجر تجريبي عام",
    }
    kwargs.update(overrides)
    row = WhatsAppConnection(**kwargs)
    db.add(row)
    db.flush()
    return row


def _t1_shape_phone(*, phone_id=PHONE_ID):
    return {
        "id": phone_id,
        "display_phone_number": "+966500000099",
        "verified_name": "متجر تجريبي عام",
        "code_verification_status": "VERIFIED",
        "name_status": "APPROVED",
        "status": "PENDING",
        "is_on_biz_app": False,
        "platform_type": "NOT_APPLICABLE",
        "quality_rating": "GREEN",
    }


def _biz_app_phone(*, phone_id=BIZ_APP_PHONE_ID):
    return {
        "id": phone_id,
        "display_phone_number": "+966500000035",
        "verified_name": "متجر تجريبي عام",
        "code_verification_status": "VERIFIED",
        "name_status": "APPROVED",
        "status": "CONNECTED",
        "is_on_biz_app": True,
        "platform_type": "CLOUD_API",
        "quality_rating": "GREEN",
    }


def _req(tenant_id: int):
    return SimpleNamespace(state=SimpleNamespace(tenant_id=tenant_id), headers={})


def _patch_phone(monkeypatch, phone):
    async def _fake(_conn, _db, phone_number_id=None):
        payload = dict(phone)
        if phone_number_id:
            payload["id"] = phone_number_id
        return payload, "platform"

    monkeypatch.setattr(
        "routers.whatsapp_embedded._get_phone_details_with_fallback",
        _fake,
    )


def test_default_meta_path_uses_standard_cloud_api_feature():
    login = (REPO_ROOT / "dashboard" / "src" / "lib" / "metaEmbeddedSignupLogin.ts").read_text(encoding="utf-8")
    page = (REPO_ROOT / "dashboard" / "src" / "pages" / "WhatsAppConnect.tsx").read_text(encoding="utf-8")
    assert "feature: 'whatsapp_embedded_signup'" in login
    assert "featureType: 'whatsapp_business_app_onboarding'" in login
    compact = page.split("Compact card CTA")[1]
    assert compact.index("onClick={launchSignup}") < compact.index("onClick={launchCoexistenceSignup}")
    assert "Main CTA — standard Cloud API" in page


def test_explicit_coexistence_choice_remains_available():
    page = (REPO_ROOT / "dashboard" / "src" / "pages" / "WhatsAppConnect.tsx").read_text(encoding="utf-8")
    assert "launchCoexistenceSignup" in page
    assert "connectionMode: 'coexistence'" in page
    assert "buildCoexistenceEmbeddedSignupFbLoginOptions" in page
    assert "coexistenceChoiceLabel" in page
    assert "Explicit Coexistence" in page


def test_business_app_safety_copy_not_on_generic_syncing_stage():
    page = (REPO_ROOT / "dashboard" / "src" / "pages" / "WhatsAppConnect.tsx").read_text(encoding="utf-8")
    syncing = page.split("stage === 'syncing-phone'")[1].split("Compact card CTA")[0]
    assert "coexistenceSafetyNote" not in syncing
    assert "simp.coexistenceSafetyNote" in page


def test_real_business_app_keeps_coexistence_projection():
    conn = SimpleNamespace(
        phone_number_id=BIZ_APP_PHONE_ID,
        whatsapp_business_account_id=BIZ_APP_WABA_ID,
        webhook_verified=True,
        extra_metadata={
            "connection_mode": "coexistence",
            "is_on_biz_app": True,
            "platform_type": "CLOUD_API",
            "readiness_phone_number_id": BIZ_APP_PHONE_ID,
            "readiness_waba_id": BIZ_APP_WABA_ID,
            "smb_sync": {},
        },
        last_error=None,
        status="configuring",
        sending_enabled=False,
    )
    assert should_project_as_coexistence(conn, _biz_app_phone()) is True
    projected = project_coexistence_sync_state(
        conn,
        phone_data=_biz_app_phone(),
        cloud_state={"connected": False, "db_status": "activation_pending"},
    )
    assert projected["projection_reason"] == "smb_incomplete"
    assert projected["db_status"] == "configuring"
    assert projected["connected"] is False


def test_non_business_app_does_not_enter_smb_wait():
    conn = SimpleNamespace(
        phone_number_id=PHONE_ID,
        whatsapp_business_account_id=WABA_ID,
        webhook_verified=False,
        extra_metadata={
            "connection_mode": "coexistence",
            "is_on_biz_app": False,
            "platform_type": "NOT_APPLICABLE",
            "smb_sync": {},
            "smb_sync_deadline_at": (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat(),
        },
        last_error=None,
        status="configuring",
        sending_enabled=False,
    )
    phone = _t1_shape_phone()
    assert should_project_as_coexistence(conn, phone) is False
    projected = _project_phone_sync_state(conn, phone)
    assert projected["projection_reason"] == COEXISTENCE_NOT_ELIGIBLE
    assert projected["db_status"] != "configuring"
    assert projected["connected"] is False
    assert projected.get("coexistence_not_eligible") is True


def test_no_smb_deadline_for_non_business_app_phone():
    past = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    conn = SimpleNamespace(
        status="configuring",
        sending_enabled=False,
        last_error=None,
        extra_metadata={
            "connection_mode": "coexistence",
            "is_on_biz_app": False,
            "smb_sync_deadline_at": past,
            "smb_sync": {},
        },
    )
    assert maybe_fail_sync_deadline(conn) is False
    assert conn.status == "configuring"


def test_finalize_rejects_non_business_app_without_smb_wait(db):
    tenant = _tenant(db, name="قميص قطني أزرق")
    conn = _conn(db, tenant.id, status="pending", extra_metadata={})
    db.commit()
    phones = [{
        "id": PHONE_ID,
        "display_phone_number": "+966500000099",
        "verified_name": "متجر تجريبي عام",
    }]
    with patch("core.tenant_integrity.assert_phone_id_not_claimed"), patch(
        "core.tenant_integrity.evict_phone_id_from_other_tenants",
    ), patch(
        "services.meta_coexistence.verify_coexistence_phone",
        return_value=(False, _t1_shape_phone(), "not eligible"),
    ), patch(
        "services.meta_coexistence.initiate_smb_app_data",
    ) as smb:
        payload = asyncio.run(_finalize_coexistence_exchange(
            conn,
            db,
            tenant_id=tenant.id,
            waba_id=WABA_ID,
            user_token="tok",
            phones=phones,
            hinted_phone_id=PHONE_ID,
            finish_event="FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
        ))
    db.refresh(conn)
    assert smb.call_count == 0
    assert payload["status"] == COEXISTENCE_NOT_ELIGIBLE
    assert payload["standard_cloud_api_available"] is True
    assert payload["connected"] is False
    assert conn.sending_enabled is False
    assert conn.status != "connected"
    assert (conn.extra_metadata or {}).get("connection_mode") != "coexistence"
    assert (conn.extra_metadata or {}).get("smb_sync_deadline_at") is None


def test_finalize_keeps_eligible_business_app_coexistence(db):
    tenant = _tenant(db, name="عطر ورد 100ml")
    conn = _conn(
        db,
        tenant.id,
        status="pending",
        phone_number_id=BIZ_APP_PHONE_ID,
        whatsapp_business_account_id=BIZ_APP_WABA_ID,
        extra_metadata={},
    )
    db.commit()
    smb = {
        "smb_app_state_sync": {"accepted": True, "request_id": "sync-a"},
        "history": {"accepted": True, "request_id": "hist-b"},
    }
    phones = [{
        "id": BIZ_APP_PHONE_ID,
        "display_phone_number": "+966500000035",
        "verified_name": "متجر تجريبي عام",
    }]
    with patch("core.tenant_integrity.assert_phone_id_not_claimed"), patch(
        "core.tenant_integrity.evict_phone_id_from_other_tenants",
    ), patch(
        "services.meta_coexistence.verify_coexistence_phone",
        return_value=(True, _biz_app_phone(), None),
    ), patch(
        "services.whatsapp_connection_service.subscribe_phone_webhook",
        return_value=(True, None),
    ), patch(
        "services.meta_coexistence.initiate_smb_app_data",
        return_value=smb,
    ):
        payload = asyncio.run(_finalize_coexistence_exchange(
            conn,
            db,
            tenant_id=tenant.id,
            waba_id=BIZ_APP_WABA_ID,
            user_token="tok",
            phones=phones,
            hinted_phone_id=BIZ_APP_PHONE_ID,
            finish_event="FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
        ))
    db.refresh(conn)
    assert (conn.extra_metadata or {}).get("connection_mode") == "coexistence"
    assert (conn.extra_metadata or {}).get("smb_sync_deadline_at")
    assert conn.status == "connected"
    assert payload["connected"] is True or conn.sending_enabled is True


def test_standard_register_and_subscription_on_confirm(monkeypatch, db):
    tenant = _tenant(db)
    _ai_settings(db, tenant.id)
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={
            "connection_mode": "coexistence",
            "is_on_biz_app": False,
            "platform_type": "NOT_APPLICABLE",
            "smb_sync_deadline_at": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
            "smb_sync": {},
            "oauth_keep": "keep-me",
        },
    )
    db.commit()
    phone_state = {"status": "PENDING"}

    async def _fake(_conn, _db, phone_number_id=None):
        payload = {**_t1_shape_phone(), "status": phone_state["status"]}
        if phone_number_id:
            payload["id"] = phone_number_id
        return payload, "platform"

    monkeypatch.setattr("routers.whatsapp_embedded._get_phone_details_with_fallback", _fake)
    register_calls = []

    async def _register(conn_row, _db, pin):
        register_calls.append({"tenant_id": conn_row.tenant_id, "phone": conn_row.phone_number_id, "pin": pin})
        phone_state["status"] = "CONNECTED"
        return {"success": True}, "platform"

    monkeypatch.setattr("routers.whatsapp_embedded._register_phone_with_fallback", _register)
    monkeypatch.setattr(
        "services.whatsapp_connection_service.subscribe_phone_webhook",
        lambda *a, **k: (True, None),
    )
    payload = asyncio.run(confirm_standard_cloud_api(
        ConfirmStandardCloudApiRequest(confirm_standard_cloud_api=True),
        _req(tenant.id),
        db,
    ))
    db.refresh(conn)
    assert register_calls, "standard finalization must own /register"
    assert register_calls[0]["tenant_id"] == tenant.id
    assert (conn.extra_metadata or {}).get("connection_mode") != "coexistence"
    assert (conn.extra_metadata or {}).get("smb_sync_deadline_at") is None
    assert (conn.extra_metadata or {}).get("oauth_keep") == "keep-me"
    assert conn.status == "connected"
    assert conn.sending_enabled is True
    assert conn.webhook_verified is True
    assert payload["connected"] is True


def test_register_failure_fails_closed(monkeypatch, db):
    tenant = _tenant(db, name="حذاء رياضي أبيض")
    conn = _conn(db, tenant.id, extra_metadata={"is_on_biz_app": False})
    db.commit()
    _patch_phone(monkeypatch, _t1_shape_phone())

    async def _register(_conn, _db, _pin):
        return {"error": {"code": 133010, "message": "Account not registered"}}, "platform"

    monkeypatch.setattr("routers.whatsapp_embedded._register_phone_with_fallback", _register)
    payload = asyncio.run(sync_embedded_connection_from_meta(conn, db, attempt_register=True))
    db.refresh(conn)
    assert payload["connected"] is False
    assert conn.status != "connected"
    assert conn.sending_enabled is False
    assert conn.status in {"error", "activation_pending", "failed"}


def test_no_connected_before_provider_readiness(monkeypatch, db):
    tenant = _tenant(db)
    conn = _conn(db, tenant.id, extra_metadata={"is_on_biz_app": False})
    db.commit()
    _patch_phone(monkeypatch, _t1_shape_phone())
    monkeypatch.setattr(
        "routers.whatsapp_embedded._register_phone_with_fallback",
        AsyncMock(return_value=({"success": True}, "platform")),
    )
    payload = asyncio.run(sync_embedded_connection_from_meta(conn, db, attempt_register=True))
    db.refresh(conn)
    assert payload["connected"] is False
    assert conn.status != "connected"
    assert conn.sending_enabled is False


def test_stale_coexistence_cannot_force_smb_wait_after_standard_mode(db):
    tenant = _tenant(db)
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={
            "connection_mode": "coexistence",
            "is_on_biz_app": False,
            "smb_sync": {},
            "smb_sync_deadline_at": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
            "status_projection_reason": "smb_incomplete",
        },
    )
    db.commit()
    projected = _project_phone_sync_state(conn, _t1_shape_phone())
    assert projected["projection_reason"] != "smb_incomplete"
    clear_obsolete_coexistence_state(conn)
    db.commit()
    db.refresh(conn)
    assert should_project_as_coexistence(conn, _t1_shape_phone()) is False
    assert (conn.extra_metadata or {}).get("connection_mode") is None
    projected_after = _project_phone_sync_state(conn, _t1_shape_phone())
    assert projected_after.get("projection_reason") != "smb_incomplete"
    assert projected_after["connected"] is False


def test_confirm_is_tenant_scoped(monkeypatch, db):
    owner = _tenant(db, name="متجر تجريبي عام")
    other = _tenant(db, name="متجر تجريبي آخر")
    _ai_settings(db, owner.id)
    _ai_settings(db, other.id)
    owner_conn = _conn(db, owner.id, extra_metadata={"connection_mode": "coexistence", "is_on_biz_app": False})
    other_conn = _conn(
        db,
        other.id,
        phone_number_id="pn-other-tenant",
        whatsapp_business_account_id="waba-other-tenant",
        extra_metadata={"connection_mode": "coexistence", "is_on_biz_app": True, "keep": True},
        status="configuring",
    )
    db.commit()
    _patch_phone(monkeypatch, {**_t1_shape_phone(), "status": "CONNECTED"})
    monkeypatch.setattr(
        "routers.whatsapp_embedded._register_phone_with_fallback",
        AsyncMock(return_value=({"success": True}, "platform")),
    )
    monkeypatch.setattr(
        "services.whatsapp_connection_service.subscribe_phone_webhook",
        lambda *a, **k: (True, None),
    )
    asyncio.run(confirm_standard_cloud_api(
        ConfirmStandardCloudApiRequest(confirm_standard_cloud_api=True),
        _req(owner.id),
        db,
    ))
    db.refresh(owner_conn)
    db.refresh(other_conn)
    assert (owner_conn.extra_metadata or {}).get("connection_mode") != "coexistence"
    assert (other_conn.extra_metadata or {}).get("connection_mode") == "coexistence"
    assert other_conn.phone_number_id == "pn-other-tenant"
    assert other_conn.status == "configuring"


def test_confirm_refuses_live_business_app_number(monkeypatch, db):
    tenant = _tenant(db, name="عطر ورد 100ml")
    conn = _conn(
        db,
        tenant.id,
        phone_number_id=BIZ_APP_PHONE_ID,
        whatsapp_business_account_id=BIZ_APP_WABA_ID,
        extra_metadata={"connection_mode": "coexistence", "is_on_biz_app": True},
        status="configuring",
    )
    db.commit()
    _patch_phone(monkeypatch, _biz_app_phone())
    with pytest.raises(HTTPException) as exc:
        asyncio.run(confirm_standard_cloud_api(
            ConfirmStandardCloudApiRequest(confirm_standard_cloud_api=True),
            _req(tenant.id),
            db,
        ))
    assert exc.value.status_code == 409
    db.refresh(conn)
    assert (conn.extra_metadata or {}).get("connection_mode") == "coexistence"


def test_guardian_skips_smb_retry_for_non_business_app(monkeypatch, db):
    from core.webhook_guardian import _inspect_connection  # noqa: PLC0415

    tenant = _tenant(db)
    now = datetime.now(timezone.utc)
    conn = _conn(
        db,
        tenant.id,
        status="configuring",
        extra_metadata={
            "connection_mode": "coexistence",
            "is_on_biz_app": False,
            "smb_sync": {},
            "smb_sync_deadline_at": (now + timedelta(hours=12)).isoformat(),
        },
    )
    db.commit()
    called = {"smb": False}

    def _smb(*_a, **_k):
        called["smb"] = True
        return {}

    monkeypatch.setattr("services.meta_coexistence.initiate_smb_app_data", _smb)
    monkeypatch.setattr(
        "services.whatsapp_platform.wa_connection_secrets.read_access_token",
        lambda *_a, **_k: "tok",
    )
    asyncio.run(_inspect_connection(db, conn, now, now - timedelta(minutes=15)))
    assert called["smb"] is False


def test_tenant1_shape_routes_to_standard_completion_not_smb(monkeypatch, db):
    tenant = _tenant(db, name="متجر تجريبي عام")
    _ai_settings(db, tenant.id)
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={
            "connection_mode": "coexistence",
            "is_on_biz_app": False,
            "platform_type": "NOT_APPLICABLE",
            "subscribed_apps": [],
            "smb_sync": {},
            "smb_sync_deadline_at": (datetime.now(timezone.utc) + timedelta(hours=23)).isoformat(),
            "failure_code": "not_eligible",
        },
        status="configuring",
    )
    db.commit()
    phone = _t1_shape_phone()
    projected = _project_phone_sync_state(conn, phone)
    assert projected["projection_reason"] != "smb_incomplete"
    assert projected["connected"] is False
    _patch_phone(monkeypatch, {**phone, "status": "CONNECTED"})
    monkeypatch.setattr(
        "routers.whatsapp_embedded._register_phone_with_fallback",
        AsyncMock(return_value=({"success": True}, "platform")),
    )
    monkeypatch.setattr(
        "services.whatsapp_connection_service.subscribe_phone_webhook",
        lambda *a, **k: (True, None),
    )
    payload = asyncio.run(confirm_standard_cloud_api(
        ConfirmStandardCloudApiRequest(confirm_standard_cloud_api=True),
        _req(tenant.id),
        db,
    ))
    db.refresh(conn)
    assert payload["connected"] is True
    assert conn.status == "connected"
    assert (conn.extra_metadata or {}).get("connection_mode") != "coexistence"
    assert "smb_sync_deadline_at" not in (conn.extra_metadata or {})


def test_confirm_refuses_stale_false_when_live_graph_is_business_app(monkeypatch, db):
    tenant = _tenant(db, name="قميص قطني أزرق")
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={"connection_mode": "coexistence", "is_on_biz_app": False},
        status="configuring",
    )
    db.commit()
    _patch_phone(monkeypatch, {**_biz_app_phone(), "id": PHONE_ID})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(confirm_standard_cloud_api(
            ConfirmStandardCloudApiRequest(confirm_standard_cloud_api=True),
            _req(tenant.id),
            db,
        ))
    assert exc.value.status_code == 409
    db.refresh(conn)
    assert (conn.extra_metadata or {}).get("connection_mode") == "coexistence"
    assert conn.status == "configuring"
    assert conn.sending_enabled is False


def test_confirm_graph_error_fails_closed(monkeypatch, db):
    tenant = _tenant(db, name="عطر ورد 100ml")
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={"connection_mode": "coexistence", "is_on_biz_app": False},
        status="configuring",
    )
    db.commit()

    async def _graph_error(_conn, _db, phone_number_id=None):
        return {"error": {"code": 2, "message": "Service temporarily unavailable"}}, "platform"

    monkeypatch.setattr(
        "routers.whatsapp_embedded._get_phone_details_with_fallback",
        _graph_error,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(confirm_standard_cloud_api(
            ConfirmStandardCloudApiRequest(confirm_standard_cloud_api=True),
            _req(tenant.id),
            db,
        ))
    assert exc.value.status_code == 502
    db.refresh(conn)
    assert (conn.extra_metadata or {}).get("connection_mode") == "coexistence"
    assert conn.status == "configuring"


def test_confirm_already_connected_standard_is_idempotent(monkeypatch, db):
    tenant = _tenant(db, name="متجر تجريبي عام")
    _ai_settings(db, tenant.id)
    connected_at = datetime.now(timezone.utc)
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={"is_on_biz_app": False, "recommended_mode": "cloud_api"},
        status="connected",
        sending_enabled=True,
        webhook_verified=True,
        connected_at=connected_at,
    )
    db.commit()
    _patch_phone(monkeypatch, {**_t1_shape_phone(), "status": "CONNECTED"})
    payload = asyncio.run(confirm_standard_cloud_api(
        ConfirmStandardCloudApiRequest(confirm_standard_cloud_api=True),
        _req(tenant.id),
        db,
    ))
    db.refresh(conn)
    assert payload["connected"] is True
    assert conn.status == "connected"
    assert conn.sending_enabled is True
    assert conn.connected_at is not None
    assert conn.webhook_verified is True
    assert conn.status != "activation_pending"


def test_confirm_webhook_failure_does_not_enable_sending(monkeypatch, db):
    tenant = _tenant(db, name="حذاء رياضي أبيض")
    _ai_settings(db, tenant.id)
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={"connection_mode": "coexistence", "is_on_biz_app": False},
        status="configuring",
    )
    db.commit()
    _patch_phone(monkeypatch, {**_t1_shape_phone(), "status": "CONNECTED"})
    monkeypatch.setattr(
        "routers.whatsapp_embedded._register_phone_with_fallback",
        AsyncMock(return_value=({"success": True}, "platform")),
    )
    monkeypatch.setattr(
        "services.whatsapp_connection_service.subscribe_phone_webhook",
        lambda *a, **k: (False, "Unsupported post request"),
    )
    payload = asyncio.run(confirm_standard_cloud_api(
        ConfirmStandardCloudApiRequest(confirm_standard_cloud_api=True),
        _req(tenant.id),
        db,
    ))
    db.refresh(conn)
    assert payload["connected"] is False
    assert conn.sending_enabled is False
    assert conn.webhook_verified is False
    assert conn.status != "connected"
    assert conn.status == "activation_pending"


def test_confirm_standard_subscribe_prefers_waba(monkeypatch, db):
    tenant = _tenant(db, name="متجر تجريبي عام")
    _ai_settings(db, tenant.id)
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={"is_on_biz_app": False},
        status="configuring",
    )
    db.commit()
    _patch_phone(monkeypatch, {**_t1_shape_phone(), "status": "CONNECTED"})
    monkeypatch.setattr(
        "routers.whatsapp_embedded._register_phone_with_fallback",
        AsyncMock(return_value=({"success": True}, "platform")),
    )
    captured = {}

    def _subscribe(*a, **k):
        captured.update(k)
        return True, None

    monkeypatch.setattr(
        "services.whatsapp_connection_service.subscribe_phone_webhook",
        _subscribe,
    )
    asyncio.run(confirm_standard_cloud_api(
        ConfirmStandardCloudApiRequest(confirm_standard_cloud_api=True),
        _req(tenant.id),
        db,
    ))
    assert captured.get("prefer_waba") is True
    assert captured.get("waba_id") == WABA_ID
