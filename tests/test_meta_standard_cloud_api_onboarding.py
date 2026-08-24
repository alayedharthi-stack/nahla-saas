"""AD-META-T1-1: Meta onboarding requires an explicit mode choice."""
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
    _apply_embedded_state,
    _assign_embedded_phone_id,
    _build_embedded_status_payload,
    _finalize_coexistence_exchange,
    _is_coexistence_conn,
    _project_phone_sync_state,
    _reset_metadata_for_standard_exchange,
    confirm_standard_cloud_api,
    sync_embedded_connection_from_meta,
)
from services.meta_coexistence import (  # noqa: E402
    COEXISTENCE_NOT_ELIGIBLE,
    clear_obsolete_coexistence_state,
    invalidate_identity_scoped_proof,
    maybe_fail_sync_deadline,
    persist_provider_phone_truth,
    project_coexistence_sync_state,
    provider_is_on_biz_app,
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


def test_meta_entry_requires_explicit_onboarding_mode_choice():
    login = (REPO_ROOT / "dashboard" / "src" / "lib" / "metaEmbeddedSignupLogin.ts").read_text(encoding="utf-8")
    page = (REPO_ROOT / "dashboard" / "src" / "pages" / "WhatsAppConnect.tsx").read_text(encoding="utf-8")
    assert "feature: 'whatsapp_embedded_signup'" in login
    assert "featureType: 'whatsapp_business_app_onboarding'" in login
    compact = page.split("Compact card CTA")[1]
    assert "onClick={openOnboardingModeChoice}" in compact
    assert "onClick={launchSignup}" not in compact
    assert "onChooseCoexistence={launchCoexistenceSignup}" in compact
    assert "onChooseCloudApi={launchSignup}" in compact
    assert compact.index("onChooseCoexistence={launchCoexistenceSignup}") < compact.index("onChooseCloudApi={launchSignup}")
    choice = page.split("function MetaOnboardingModeChoice")[1].split("function MetaEmbeddedOptionCard")[0]
    assert "Choice 1" in choice
    assert "Choice 2" in choice
    assert choice.index("Choice 1") < choice.index("Choice 2")
    assert "openOnboardingModeChoice" in page


def test_builders_are_not_swapped_across_onboarding_paths():
    page = (REPO_ROOT / "dashboard" / "src" / "pages" / "WhatsAppConnect.tsx").read_text(encoding="utf-8")
    coex_start = page.index("const launchCoexistenceSignup = useCallback")
    cloud_start = page.index("const launchSignup = useCallback")
    coex_fn = page[coex_start:cloud_start]
    cloud_fn = page[cloud_start:page.index("const confirmStandardCloudApi = useCallback")]
    assert "buildCoexistenceEmbeddedSignupFbLoginOptions" in coex_fn
    assert "buildEmbeddedSignupFbLoginOptions(" not in coex_fn
    assert "buildEmbeddedSignupFbLoginOptions" in cloud_fn
    assert "buildCoexistenceEmbeddedSignupFbLoginOptions" not in cloud_fn


def test_explicit_coexistence_choice_remains_available():
    page = (REPO_ROOT / "dashboard" / "src" / "pages" / "WhatsAppConnect.tsx").read_text(encoding="utf-8")
    assert "launchCoexistenceSignup" in page
    assert "connectionMode: 'coexistence'" in page
    assert "buildCoexistenceEmbeddedSignupFbLoginOptions" in page
    assert "coexistenceChoiceTitle" in page
    assert "Choice 1 — Coexistence" in page
    assert "no silent Cloud API conversion" in page


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
            trusted_phone_id=PHONE_ID,
            finish_event="FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
        ))
    db.refresh(conn)
    assert smb.call_count == 0
    assert payload["status"] == COEXISTENCE_NOT_ELIGIBLE
    assert payload["standard_cloud_api_available"] is True
    assert payload["connected"] is False
    assert conn.sending_enabled is False
    assert conn.status != "connected"
    assert (conn.extra_metadata or {}).get("connection_mode") == "coexistence"
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
            trusted_phone_id=BIZ_APP_PHONE_ID,
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


def _already_standard_conn(db, tenant_id, **overrides):
    kwargs = {
        "extra_metadata": {"is_on_biz_app": False, "recommended_mode": "cloud_api"},
        "status": "connected",
        "sending_enabled": True,
        "webhook_verified": True,
        "connected_at": datetime.now(timezone.utc),
    }
    kwargs.update(overrides)
    return _conn(db, tenant_id, **kwargs)


def test_confirm_already_standard_graph_error_fails_closed(monkeypatch, db):
    tenant = _tenant(db, name="قميص قطني أزرق")
    conn = _already_standard_conn(db, tenant.id)
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
    assert conn.status == "connected"
    assert conn.sending_enabled is True


def test_confirm_already_standard_live_business_app_refused(monkeypatch, db):
    tenant = _tenant(db, name="عطر ورد 100ml")
    conn = _already_standard_conn(db, tenant.id)
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
    assert conn.status == "connected"
    assert (conn.extra_metadata or {}).get("is_on_biz_app") is True


def test_confirm_already_standard_missing_graph_flag_fails_closed(monkeypatch, db):
    tenant = _tenant(db, name="حذاء رياضي أبيض")
    conn = _already_standard_conn(db, tenant.id)
    db.commit()
    phone = dict(_t1_shape_phone())
    phone.pop("is_on_biz_app")
    _patch_phone(monkeypatch, {**phone, "status": "CONNECTED"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(confirm_standard_cloud_api(
            ConfirmStandardCloudApiRequest(confirm_standard_cloud_api=True),
            _req(tenant.id),
            db,
        ))
    assert exc.value.status_code == 502
    db.refresh(conn)
    assert conn.status == "connected"
    assert conn.sending_enabled is True


def test_confirm_already_standard_null_graph_flag_fails_closed(monkeypatch, db):
    tenant = _tenant(db, name="متجر تجريبي عام")
    conn = _already_standard_conn(db, tenant.id)
    db.commit()
    _patch_phone(monkeypatch, {**_t1_shape_phone(), "is_on_biz_app": None, "status": "CONNECTED"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(confirm_standard_cloud_api(
            ConfirmStandardCloudApiRequest(confirm_standard_cloud_api=True),
            _req(tenant.id),
            db,
        ))
    assert exc.value.status_code == 502
    db.refresh(conn)
    assert conn.status == "connected"
    assert conn.sending_enabled is True


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


def test_multi_phone_coexistence_keeps_mode_until_selection(db):
    tenant = _tenant(db, name="متجر تجريبي عام")
    conn = _conn(db, tenant.id, status="pending", extra_metadata={})
    db.commit()
    phones = [
        {"id": "pn-coex-a", "display_phone_number": "+966500000001", "verified_name": "فرع أ"},
        {"id": "pn-coex-b", "display_phone_number": "+966500000002", "verified_name": "فرع ب"},
    ]
    payload = asyncio.run(_finalize_coexistence_exchange(
        conn,
        db,
        tenant_id=tenant.id,
        waba_id=WABA_ID,
        user_token="tok",
        phones=phones,
        trusted_phone_id="",
        finish_event="FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
    ))
    db.refresh(conn)
    assert payload["status"] == "pending"
    assert payload["connected"] is False
    assert (conn.extra_metadata or {}).get("connection_mode") == "coexistence"
    assert _is_coexistence_conn(conn) is True


def test_ineligible_coexistence_status_sync_does_not_register(monkeypatch, db):
    tenant = _tenant(db, name="قميص قطني أزرق")
    _ai_settings(db, tenant.id)
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={
            "connection_mode": "coexistence",
            "is_on_biz_app": False,
            "last_coexistence_outcome": COEXISTENCE_NOT_ELIGIBLE,
        },
        status="failed",
    )
    db.commit()
    register_calls = []

    async def _register(*_a, **_k):
        register_calls.append(1)
        return {"success": True}, "platform"

    _patch_phone(monkeypatch, {**_t1_shape_phone(), "status": "CONNECTED"})
    monkeypatch.setattr("routers.whatsapp_embedded._register_phone_with_fallback", _register)
    monkeypatch.setattr(
        "services.whatsapp_connection_service.subscribe_phone_webhook",
        lambda *a, **k: (True, None),
    )
    asyncio.run(sync_embedded_connection_from_meta(
        conn,
        db,
        attempt_register=not _is_coexistence_conn(conn),
        allow_demotion=not _is_coexistence_conn(conn),
    ))
    db.refresh(conn)
    assert register_calls == []
    assert (conn.extra_metadata or {}).get("connection_mode") == "coexistence"
    assert conn.status != "connected"
    assert conn.sending_enabled is False


def test_standard_pending_is_not_labeled_coexistence_ineligible(db):
    tenant = _tenant(db, name="حذاء رياضي أبيض")
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={"is_on_biz_app": False},
        status="otp_pending",
    )
    db.commit()
    payload = _build_embedded_status_payload(conn)
    assert payload.get("coexistence_not_eligible") is not True
    assert payload["status"] == "otp_pending"


def test_waba_replace_clears_stale_webhook_verified(monkeypatch, db):
    from services import whatsapp_connection_service as wa_svc  # noqa: PLC0415

    monkeypatch.setattr(wa_svc, "evict_waba_id_from_other_tenants", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "assert_waba_id_not_claimed", lambda *_a, **_kw: None, raising=False)
    tenant = _tenant(db, name="متجر تجريبي عام")
    conn = _conn(
        db,
        tenant.id,
        whatsapp_business_account_id="waba-old-generic",
        webhook_verified=True,
        status="connected",
        sending_enabled=True,
        extra_metadata={
            "connection_mode": "coexistence",
            "is_on_biz_app": True,
            "platform_type": "CLOUD_API",
            "smb_sync": {
                "smb_app_state_sync": {"accepted": True, "request_id": "old-a"},
                "history": {"accepted": True, "request_id": "old-b"},
            },
            "smb_sync_deadline_at": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
            "readiness_phone_number_id": PHONE_ID,
            "readiness_waba_id": "waba-old-generic",
            "last_coexistence_outcome": "ready",
            "status_projection_reason": "ready",
            "embedded_status_message": "stale",
            "last_meta_sync_at": "2026-01-01T00:00:00+00:00",
            "failure_code": "not_eligible",
        },
    )
    db.commit()
    wa_svc.begin_waba_session(
        db,
        tenant_id=tenant.id,
        waba_id="waba-new-generic",
        access_token="tok",
    )
    db.refresh(conn)
    assert conn.webhook_verified is False
    assert conn.phone_number_id is None
    assert conn.status == "pending"
    assert conn.sending_enabled is False
    meta = dict(conn.extra_metadata or {})
    assert "smb_sync" not in meta
    assert "smb_sync_deadline_at" not in meta
    assert "readiness_phone_number_id" not in meta
    assert "readiness_waba_id" not in meta
    assert "is_on_biz_app" not in meta
    assert "platform_type" not in meta
    assert "last_coexistence_outcome" not in meta
    assert "status_projection_reason" not in meta
    assert "embedded_status_message" not in meta
    assert "last_meta_sync_at" not in meta
    assert "failure_code" not in meta


def test_assigning_new_phone_clears_webhook_verified(db):
    tenant = _tenant(db)
    conn = _conn(
        db,
        tenant.id,
        webhook_verified=True,
        phone_number_id=PHONE_ID,
        extra_metadata={
            "connection_mode": "coexistence",
            "is_on_biz_app": True,
            "platform_type": "CLOUD_API",
            "smb_sync": {"history": {"accepted": True, "request_id": "old"}},
            "smb_sync_deadline_at": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
            "readiness_phone_number_id": PHONE_ID,
            "last_coexistence_outcome": "ready",
            "status_projection_reason": "ready",
            "embedded_status_message": "stale",
            "last_meta_sync_at": "2026-01-01T00:00:00+00:00",
            "failure_code": "not_eligible",
        },
    )
    db.commit()
    _assign_embedded_phone_id(conn, "pn-new-identity")
    assert conn.webhook_verified is False
    assert conn.phone_number_id == "pn-new-identity"
    meta = dict(conn.extra_metadata or {})
    assert "smb_sync" not in meta
    assert "smb_sync_deadline_at" not in meta
    assert "is_on_biz_app" not in meta
    assert "readiness_phone_number_id" not in meta
    assert "last_coexistence_outcome" not in meta
    assert "status_projection_reason" not in meta
    assert "failure_code" not in meta


def test_connected_coexistence_live_false_surfaces_standard_confirm(monkeypatch, db):
    tenant = _tenant(db, name="عطر ورد 100ml")
    _ai_settings(db, tenant.id)
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={"connection_mode": "coexistence", "is_on_biz_app": True},
        status="connected",
        sending_enabled=True,
        webhook_verified=True,
        connected_at=datetime.now(timezone.utc),
    )
    db.commit()
    _patch_phone(monkeypatch, {**_t1_shape_phone(), "status": "CONNECTED"})
    payload = asyncio.run(sync_embedded_connection_from_meta(
        conn,
        db,
        attempt_register=False,
        allow_demotion=False,
    ))
    db.refresh(conn)
    assert conn.sending_enabled is False
    assert payload["connected"] is False
    assert payload.get("coexistence_not_eligible") is True
    assert payload["status"] == COEXISTENCE_NOT_ELIGIBLE
    assert payload.get("standard_cloud_api_available") is True


def test_confirm_graph_ambiguity_fails_closed(monkeypatch, db):
    tenant = _tenant(db, name="قميص قطني أزرق")
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={"is_on_biz_app": False},
        status="configuring",
    )
    db.commit()
    phone = dict(_t1_shape_phone())
    phone.pop("is_on_biz_app")
    _patch_phone(monkeypatch, {**phone, "status": "CONNECTED"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(confirm_standard_cloud_api(
            ConfirmStandardCloudApiRequest(confirm_standard_cloud_api=True),
            _req(tenant.id),
            db,
        ))
    assert exc.value.status_code == 502
    db.refresh(conn)
    assert conn.status != "connected"
    assert conn.sending_enabled is False


def test_confirm_resets_stale_webhook_and_resubscribes(monkeypatch, db):
    tenant = _tenant(db, name="متجر تجريبي عام")
    _ai_settings(db, tenant.id)
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={"connection_mode": "coexistence", "is_on_biz_app": False},
        status="configuring",
        webhook_verified=True,
    )
    db.commit()
    _patch_phone(monkeypatch, {**_t1_shape_phone(), "status": "CONNECTED"})
    monkeypatch.setattr(
        "routers.whatsapp_embedded._register_phone_with_fallback",
        AsyncMock(return_value=({"success": True}, "platform")),
    )
    captured = []

    def _subscribe(*a, **k):
        captured.append(k)
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
    db.refresh(conn)
    assert captured
    assert captured[0].get("prefer_waba") is True
    assert conn.webhook_verified is True
    assert conn.status == "connected"


def test_graph_null_is_not_explicit_false():
    assert provider_is_on_biz_app({"is_on_biz_app": None}, None) is None
    assert provider_is_on_biz_app({"is_on_biz_app": False}, None) is False
    assert provider_is_on_biz_app({"is_on_biz_app": True}, None) is True
    assert provider_is_on_biz_app({}, {"is_on_biz_app": None}) is None


def test_confirm_rejects_graph_null_is_on_biz_app(monkeypatch, db):
    tenant = _tenant(db, name="حذاء رياضي أبيض")
    conn = _conn(db, tenant.id, extra_metadata={"is_on_biz_app": False}, status="configuring")
    db.commit()
    _patch_phone(monkeypatch, {**_t1_shape_phone(), "is_on_biz_app": None, "status": "CONNECTED"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(confirm_standard_cloud_api(
            ConfirmStandardCloudApiRequest(confirm_standard_cloud_api=True),
            _req(tenant.id),
            db,
        ))
    assert exc.value.status_code == 502
    db.refresh(conn)
    assert conn.status != "connected"
    assert conn.sending_enabled is False


def test_unknown_phone_status_does_not_project_connected(db):
    tenant = _tenant(db)
    conn = _conn(db, tenant.id, extra_metadata={"is_on_biz_app": False})
    db.commit()
    phone = dict(_t1_shape_phone())
    phone.pop("status", None)
    projected = _project_phone_sync_state(conn, phone)
    assert projected["connected"] is False
    assert projected["sending_enabled"] is False
    assert projected["db_status"] != "connected"


def test_graph_null_is_not_persisted_as_false(db):
    tenant = _tenant(db)
    conn = _conn(db, tenant.id, extra_metadata={"is_on_biz_app": True})
    db.commit()
    persist_provider_phone_truth(conn, {"is_on_biz_app": None})
    assert "is_on_biz_app" not in dict(conn.extra_metadata or {})
    _apply_embedded_state(
        conn,
        {"is_on_biz_app": None, "platform_type": "CLOUD_API"},
        {
            "connected": False,
            "sending_enabled": False,
            "db_status": "activation_pending",
            "verification_status": "VERIFIED",
            "name_status": "APPROVED",
            "meta_phone_status": None,
            "quality_rating": "GREEN",
            "message": "pending",
        },
    )
    assert "is_on_biz_app" not in dict(conn.extra_metadata or {})
    assert provider_is_on_biz_app(None, dict(conn.extra_metadata or {})) is None


def test_identity_invalidation_clears_outcome_and_sending(db):
    tenant = _tenant(db)
    conn = _conn(
        db,
        tenant.id,
        webhook_verified=True,
        sending_enabled=True,
        last_verified_at=datetime.now(timezone.utc),
        extra_metadata={
            "connection_mode": "coexistence",
            "last_coexistence_outcome": "ready",
            "status_projection_reason": "ready",
            "embedded_status_message": "stale-ready",
            "last_meta_sync_at": "2026-01-01T00:00:00+00:00",
            "failure_code": "not_eligible",
            "meta_verification_unavailable": True,
            "meta_fetch_failure_kind": "transient",
            "webhook_subscription_error": "old",
            "recommended_mode": "cloud_api",
            "standard_cloud_api_available": True,
            "smb_sync": {"history": {"accepted": True, "request_id": "old"}},
        },
    )
    db.commit()
    invalidate_identity_scoped_proof(conn)
    meta = dict(conn.extra_metadata or {})
    assert conn.webhook_verified is False
    assert conn.sending_enabled is False
    assert conn.last_verified_at is None
    assert "smb_sync" not in meta
    assert "last_coexistence_outcome" not in meta
    assert "status_projection_reason" not in meta
    assert "embedded_status_message" not in meta
    assert "last_meta_sync_at" not in meta
    assert "failure_code" not in meta
    assert "meta_verification_unavailable" not in meta
    assert "meta_fetch_failure_kind" not in meta
    assert "webhook_subscription_error" not in meta
    assert "recommended_mode" not in meta
    assert "standard_cloud_api_available" not in meta


def test_standard_exchange_keeps_coexistence_consent(db):
    tenant = _tenant(db)
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={
            "connection_mode": "coexistence",
            "oauth_keep": "keep-me",
            "is_on_biz_app": True,
        },
    )
    db.commit()
    _reset_metadata_for_standard_exchange(conn)
    meta = dict(conn.extra_metadata or {})
    assert meta.get("connection_mode") == "coexistence"
    assert meta.get("oauth_keep") == "keep-me"
    assert _is_coexistence_conn(conn) is True


def test_standard_exchange_clears_non_coexistence_metadata(db):
    tenant = _tenant(db)
    conn = _conn(db, tenant.id, extra_metadata={"stale_standard": "drop-me"})
    db.commit()
    _reset_metadata_for_standard_exchange(conn)
    assert dict(conn.extra_metadata or {}) == {}


def test_first_waba_assignment_clears_stale_verification_proof(monkeypatch, db):
    from services import whatsapp_connection_service as wa_svc  # noqa: PLC0415

    monkeypatch.setattr(wa_svc, "evict_waba_id_from_other_tenants", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "assert_waba_id_not_claimed", lambda *_a, **_kw: None, raising=False)
    tenant = _tenant(db)
    conn = _conn(
        db,
        tenant.id,
        whatsapp_business_account_id=None,
        webhook_verified=True,
        sending_enabled=True,
        last_verified_at=datetime.now(timezone.utc),
        extra_metadata={
            "meta_verification_unavailable": True,
            "meta_fetch_failure_kind": "transient",
            "webhook_subscription_error": "old",
        },
    )
    db.commit()
    wa_svc.begin_waba_session(
        db,
        tenant_id=tenant.id,
        waba_id="waba-new-generic",
        access_token="tok",
    )
    db.refresh(conn)
    meta = dict(conn.extra_metadata or {})
    assert conn.webhook_verified is False
    assert conn.sending_enabled is False
    assert conn.last_verified_at is None
    assert "meta_verification_unavailable" not in meta
    assert "meta_fetch_failure_kind" not in meta
    assert "webhook_subscription_error" not in meta


def test_preserved_coexistence_blocks_standard_exchange_register(monkeypatch, db):
    tenant = _tenant(db, name="حذاء رياضي أبيض")
    conn = _conn(
        db,
        tenant.id,
        extra_metadata={
            "connection_mode": "coexistence",
            "is_on_biz_app": False,
            "platform_type": "NOT_APPLICABLE",
        },
    )
    db.commit()
    _reset_metadata_for_standard_exchange(conn)
    assert _is_coexistence_conn(conn) is True
    _patch_phone(monkeypatch, {**_t1_shape_phone(), "status": "CONNECTED"})
    register_calls = []

    async def _register(*_a, **_k):
        register_calls.append(1)
        return {"success": True}, "platform"

    monkeypatch.setattr("routers.whatsapp_embedded._register_phone_with_fallback", _register)
    monkeypatch.setattr(
        "services.whatsapp_connection_service.subscribe_phone_webhook",
        lambda *a, **k: (True, None),
    )
    asyncio.run(sync_embedded_connection_from_meta(
        conn, db, attempt_register=not _is_coexistence_conn(conn),
    ))
    db.refresh(conn)
    assert register_calls == []
    assert (conn.extra_metadata or {}).get("connection_mode") == "coexistence"
    assert conn.sending_enabled is False


def test_same_waba_provider_change_invalidates_identity_proof(monkeypatch, db):
    from services import whatsapp_connection_service as wa_svc  # noqa: PLC0415

    monkeypatch.setattr(wa_svc, "evict_waba_id_from_other_tenants", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "assert_waba_id_not_claimed", lambda *_a, **_kw: None, raising=False)
    tenant = _tenant(db, name="قميص قطني أزرق")
    conn = _conn(
        db,
        tenant.id,
        whatsapp_business_account_id=WABA_ID,
        provider="meta",
        connection_type="embedded",
        webhook_verified=True,
        sending_enabled=True,
        last_verified_at=datetime.now(timezone.utc),
        extra_metadata={
            "recommended_mode": "cloud_api",
            "standard_cloud_api_available": True,
            "status_projection_reason": "ready",
            "webhook_subscription_error": "old",
        },
    )
    db.commit()
    wa_svc.begin_waba_session(
        db,
        tenant_id=tenant.id,
        waba_id=WABA_ID,
        access_token="tok",
        connection_type="cloud_api",
        provider="meta",
    )
    db.refresh(conn)
    meta = dict(conn.extra_metadata or {})
    assert conn.phone_number_id == PHONE_ID
    assert conn.webhook_verified is False
    assert conn.sending_enabled is False
    assert conn.last_verified_at is None
    assert "recommended_mode" not in meta
    assert "standard_cloud_api_available" not in meta
    assert "status_projection_reason" not in meta
    assert "webhook_subscription_error" not in meta
