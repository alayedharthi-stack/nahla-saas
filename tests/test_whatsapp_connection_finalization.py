"""Canonical WhatsApp connection finalization — trial lifecycle (WA-LIFE-*)."""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR, REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import (  # noqa: E402
    Base,
    BillingPlan,
    BillingSubscription,
    Tenant,
    WhatsAppConnection,
)
from core.trial_lifecycle import (  # noqa: E402
    TRIAL_STATUS_ACTIVE,
    TRIAL_STATUS_PENDING_WHATSAPP,
    init_new_tenant_trial_state,
    resolve_billing_lifecycle,
)
from core.whatsapp_connection_finalization import (  # noqa: E402
    WhatsAppConnectionFinalizationError,
    finalize_successful_whatsapp_connection,
)


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
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _tenant(db, *, name="متجر تجريبي عام"):
    t = Tenant(name=name, is_active=True)
    init_new_tenant_trial_state(t)
    db.add(t)
    db.flush()
    return t


def _conn(db, tenant_id, **overrides):
    now = datetime.now(timezone.utc)
    kwargs = {
        "tenant_id": tenant_id,
        "status": "configuring",
        "phone_number_id": "pn-generic-1",
        "whatsapp_business_account_id": "waba-generic-1",
        "provider": "meta",
        "connection_type": "embedded",
        "webhook_verified": True,
        "sending_enabled": False,
        "extra_metadata": {"connection_mode": "coexistence"},
        "last_webhook_received_at": now,
    }
    kwargs.update(overrides)
    row = WhatsAppConnection(**kwargs)
    db.add(row)
    db.flush()
    return row


def _paid(db, tenant_id):
    now = datetime.now(timezone.utc)
    plan = BillingPlan(
        tenant_id=None,
        slug=f"starter-life-{tenant_id}",
        name="Starter",
        description="",
        currency="SAR",
        price_sar=349,
        billing_cycle="monthly",
        features=[],
        limits={},
        extra_metadata={},
    )
    db.add(plan)
    db.flush()
    sub = BillingSubscription(
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="active",
        started_at=(now - timedelta(days=2)).replace(tzinfo=None),
        ends_at=(now + timedelta(days=28)).replace(tzinfo=None),
        extra_metadata={"paid_at": (now - timedelta(days=2)).isoformat()},
    )
    db.add(sub)
    db.commit()
    return sub


def test_wa_life_01_first_successful_connection_starts_trial_once(db):
    t = _tenant(db, name="قميص قطني أزرق")
    conn = _conn(db, t.id, status="pending")
    db.commit()

    first = finalize_successful_whatsapp_connection(db, conn)
    db.refresh(t)
    db.refresh(conn)
    assert first is True
    assert conn.status == "connected"
    assert conn.connected_at is not None
    assert t.subscription_status == TRIAL_STATUS_ACTIVE
    assert t.trial_started_at is not None
    assert t.first_whatsapp_connected_at is not None
    lifecycle = resolve_billing_lifecycle(db, t.id, t, active_sub=None)
    assert lifecycle["lifecycle_status"] == "trial_active"

    second = finalize_successful_whatsapp_connection(db, conn)
    db.refresh(t)
    assert second is False
    assert t.trial_started_at is not None


def test_wa_life_02_coexistence_finalization_starts_trial_once(db):
    from routers.whatsapp_embedded import _finalize_coexistence_exchange  # noqa: PLC0415

    t = _tenant(db, name="عطر ورد 100ml")
    conn = _conn(db, t.id, status="pending", extra_metadata={"connection_mode": "coexistence"})
    db.commit()
    smb = {
        "smb_app_state_sync": {"accepted": True, "request_id": "a"},
        "history": {"accepted": True, "request_id": "b"},
    }
    phones = [{
        "id": "pn-generic-1",
        "display_phone_number": "+966500000010",
        "verified_name": "متجر تجريبي عام",
    }]
    with patch("core.tenant_integrity.assert_phone_id_not_claimed"), patch(
        "core.tenant_integrity.evict_phone_id_from_other_tenants",
    ), patch(
        "services.meta_coexistence.verify_coexistence_phone",
        return_value=(True, {"display_phone_number": "+966500000010", "verified_name": "متجر تجريبي عام"}, None),
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
            tenant_id=t.id,
            waba_id="waba-generic-1",
            user_token="tok",
            phones=phones,
            hinted_phone_id="pn-generic-1",
            finish_event="FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
        ))
    db.refresh(t)
    db.refresh(conn)
    assert payload.get("connected") is True or conn.status == "connected"
    assert conn.status == "connected"
    assert t.subscription_status == TRIAL_STATUS_ACTIVE
    started = t.trial_started_at
    first_wa = t.first_whatsapp_connected_at
    with patch("core.tenant_integrity.assert_phone_id_not_claimed"), patch(
        "core.tenant_integrity.evict_phone_id_from_other_tenants",
    ), patch(
        "services.meta_coexistence.verify_coexistence_phone",
        return_value=(True, {}, None),
    ), patch(
        "services.whatsapp_connection_service.subscribe_phone_webhook",
        return_value=(True, None),
    ), patch(
        "services.meta_coexistence.initiate_smb_app_data",
        return_value=smb,
    ):
        asyncio.run(_finalize_coexistence_exchange(
            conn,
            db,
            tenant_id=t.id,
            waba_id="waba-generic-1",
            user_token="tok",
            phones=phones,
            hinted_phone_id="pn-generic-1",
            finish_event="FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING",
        ))
    db.refresh(t)
    assert t.trial_started_at == started
    assert t.first_whatsapp_connected_at == first_wa


def test_wa_life_03_and_04_guardian_promotion_starts_trial_once(db):
    from core.webhook_guardian import _inspect_connection  # noqa: PLC0415

    t = _tenant(db, name="حذاء رياضي أبيض")
    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=12)).isoformat()
    conn = _conn(
        db,
        t.id,
        status="configuring",
        sending_enabled=False,
        webhook_verified=True,
        last_webhook_received_at=now,
        extra_metadata={
            "connection_mode": "coexistence",
            "smb_sync_deadline_at": future,
            "smb_sync": {},
        },
    )
    db.commit()
    results = {
        "smb_app_state_sync": {"accepted": True, "request_id": "a"},
        "history": {"accepted": True, "request_id": "b"},
    }
    with patch(
        "services.whatsapp_platform.wa_connection_secrets.read_access_token",
        return_value="tok",
    ), patch(
        "services.meta_coexistence.initiate_smb_app_data",
        return_value=results,
    ):
        health = asyncio.run(_inspect_connection(db, conn, now, now - timedelta(minutes=15)))
    db.refresh(t)
    db.refresh(conn)
    assert health == "active"
    assert conn.status == "connected"
    assert t.subscription_status == TRIAL_STATUS_ACTIVE
    started = t.trial_started_at
    ended = t.trial_ends_at
    first_wa = t.first_whatsapp_connected_at
    connected_at = conn.connected_at

    with patch(
        "services.whatsapp_platform.wa_connection_secrets.read_access_token",
        return_value="tok",
    ), patch(
        "services.meta_coexistence.initiate_smb_app_data",
        return_value=results,
    ) as mock_sync:
        asyncio.run(_inspect_connection(db, conn, now, now - timedelta(minutes=15)))
    db.refresh(t)
    db.refresh(conn)
    assert mock_sync.called is False
    assert t.trial_started_at == started
    assert t.trial_ends_at == ended
    assert t.first_whatsapp_connected_at == first_wa
    assert conn.connected_at == connected_at


def test_wa_life_05_reconnect_does_not_restart_trial(db):
    t = _tenant(db, name="أحمد سالم")
    conn = _conn(db, t.id, status="pending")
    db.commit()
    first_at = datetime.now(timezone.utc) - timedelta(days=4)
    assert finalize_successful_whatsapp_connection(db, conn, connected_at=first_at) is True
    db.refresh(t)
    db.refresh(conn)
    started = t.trial_started_at
    ended = t.trial_ends_at
    first_wa = t.first_whatsapp_connected_at
    connected_at = conn.connected_at

    conn.status = "disconnected"
    db.commit()
    later = datetime.now(timezone.utc)
    assert finalize_successful_whatsapp_connection(db, conn, connected_at=later) is False
    db.refresh(t)
    db.refresh(conn)
    assert conn.status == "connected"
    assert t.trial_started_at == started
    assert t.trial_ends_at == ended
    assert t.first_whatsapp_connected_at == first_wa
    assert conn.connected_at == connected_at


def test_wa_life_06_paid_tenant_remains_paid(db):
    t = _tenant(db, name="نورة عبدالله")
    conn = _conn(db, t.id, status="pending")
    db.commit()
    _paid(db, t.id)
    t.subscription_status = "active"
    db.commit()

    started = finalize_successful_whatsapp_connection(db, conn)
    db.refresh(t)
    assert started is False
    assert t.trial_started_at is None
    assert t.subscription_status == "active"
    assert t.first_whatsapp_connected_at is not None
    assert conn.status == "connected"


def test_wa_life_07_existing_active_trial_timestamps_stable(db):
    t = _tenant(db, name="الرياض-RRRD1234")
    conn = _conn(db, t.id, status="connected")
    db.commit()
    original_start = (datetime.now(timezone.utc) - timedelta(days=3)).replace(tzinfo=None)
    original_end = original_start + timedelta(days=14)
    original_first = original_start
    t.trial_started_at = original_start
    t.trial_ends_at = original_end
    t.first_whatsapp_connected_at = original_first
    t.subscription_status = TRIAL_STATUS_ACTIVE
    original_connected = datetime.now(timezone.utc) - timedelta(days=3)
    conn.connected_at = original_connected
    db.commit()

    assert finalize_successful_whatsapp_connection(db, conn) is False
    db.refresh(t)
    db.refresh(conn)
    assert t.trial_started_at == original_start
    assert t.trial_ends_at == original_end
    assert t.first_whatsapp_connected_at == original_first
    got = conn.connected_at
    if got is not None and got.tzinfo is None and original_connected.tzinfo is not None:
        got = got.replace(tzinfo=original_connected.tzinfo)
    assert got == original_connected


def test_wa_life_01_commit_connection_ordinary_path_starts_trial(monkeypatch, db):
    from services import whatsapp_connection_service as wa_svc  # noqa: PLC0415
    from services.whatsapp_platform.wa_token_validation import classify_debug_info  # noqa: PLC0415

    monkeypatch.setattr(wa_svc, "validate_phone_waba_match", lambda *_a, **_kw: (True, None, None))
    monkeypatch.setattr(
        "services.whatsapp_platform.wa_token_validation.validate_meta_access_token_sync",
        lambda _token: classify_debug_info({
            "is_valid": True,
            "type": "SYSTEM_USER",
            "expires_at": 0,
            "scopes": ["whatsapp_business_messaging"],
            "app_id": "123",
        }),
    )
    monkeypatch.setattr(wa_svc, "evict_phone_id_from_other_tenants", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "evict_waba_id_from_other_tenants", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "assert_phone_id_not_claimed", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "assert_waba_id_not_claimed", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "fetch_phone_metadata", lambda *_a, **_kw: {})
    monkeypatch.setattr(wa_svc, "register_phone_number", lambda *_a, **_kw: (True, None))
    monkeypatch.setattr(wa_svc, "subscribe_phone_webhook", lambda *_a, **_kw: (True, None))

    t = _tenant(db, name="Ordinary Cloud API")
    db.commit()
    wa_svc.commit_connection(
        db,
        tenant_id=t.id,
        phone_number_id="PHONE-ORD-1",
        waba_id="WABA-ORD-1",
        access_token="tok",
        connection_type="cloud_api",
        phone_number="+966500000020",
        display_name="متجر تجريبي عام",
    )
    db.refresh(t)
    assert t.subscription_status == TRIAL_STATUS_ACTIVE
    started = t.trial_started_at
    first_wa = t.first_whatsapp_connected_at
    row = db.query(WhatsAppConnection).filter_by(tenant_id=t.id).first()
    connected_at = row.connected_at
    wa_svc.commit_connection(
        db,
        tenant_id=t.id,
        phone_number_id="PHONE-ORD-1",
        waba_id="WABA-ORD-1",
        access_token="tok",
        connection_type="cloud_api",
        phone_number="+966500000020",
        display_name="متجر تجريبي عام",
    )
    db.refresh(t)
    db.refresh(row)
    assert t.trial_started_at == started
    assert t.first_whatsapp_connected_at == first_wa
    assert row.connected_at == connected_at


def test_begin_waba_session_does_not_start_trial(monkeypatch, db):
    from services import whatsapp_connection_service as wa_svc  # noqa: E402

    monkeypatch.setattr(wa_svc, "evict_waba_id_from_other_tenants", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "assert_waba_id_not_claimed", lambda *_a, **_kw: None, raising=False)
    t = _tenant(db, name="Pending WABA only")
    db.commit()
    wa_svc.begin_waba_session(
        db,
        tenant_id=t.id,
        waba_id="WABA-PEND-1",
        access_token="tok",
    )
    db.refresh(t)
    row = db.query(WhatsAppConnection).filter_by(tenant_id=t.id).first()
    assert row.status == "pending"
    assert t.trial_started_at is None
    assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP


def _changed_files() -> set[str]:
    names: set[str] = set()
    for args in (
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
        ["git", "diff", "--name-only", "origin/main...HEAD"],
    ):
        proc = subprocess.run(
            args,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        names.update(line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip())
    return names


def test_wa_life_08_no_trial_banner_workaround():
    banner = (REPO_ROOT / "dashboard" / "src" / "components" / "ui" / "TrialBanner.tsx").read_text(
        encoding="utf-8",
    )
    assert "lifecycle_status" in banner
    assert "whatsapp/connection" not in banner
    assert "store_ai_mode" not in banner
    changed = _changed_files()
    assert "dashboard/src/components/ui/TrialBanner.tsx" not in changed


def test_wa_life_09_and_10_no_ai_settings_or_customer_ai_files():
    changed = _changed_files()
    forbidden = [
        "dashboard/src/pages/Intelligence.tsx",
        "backend/modules/ai/brain/pipeline.py",
        "backend/modules/ai/brain/compose/responder.py",
        "backend/routers/whatsapp_webhook.py",
    ]
    for path in forbidden:
        assert path not in changed, path
    for path in changed:
        if path.startswith("backend/modules/ai/"):
            raise AssertionError(f"Customer AI file changed: {path}")
    text = (BACKEND_DIR / "core" / "whatsapp_connection_finalization.py").read_text(encoding="utf-8")
    assert "store_ai_mode" not in text
    assert "DEFAULT_AI" not in text
    assert "ai_test_allowed_numbers" not in text


def test_wa_life_11_no_tenant_or_phone_specific_code():
    text = (BACKEND_DIR / "core" / "whatsapp_connection_finalization.py").read_text(encoding="utf-8")
    assert "966555906901" not in text
    assert "tenant_id == 35" not in text
    assert "tenant_id=35" not in text


def test_canonical_owner_is_single_trial_caller():
    service = (BACKEND_DIR / "services" / "whatsapp_connection_service.py").read_text(encoding="utf-8")
    guardian = (BACKEND_DIR / "core" / "webhook_guardian.py").read_text(encoding="utf-8")
    embedded = (BACKEND_DIR / "routers" / "whatsapp_embedded.py").read_text(encoding="utf-8")
    connect = (BACKEND_DIR / "routers" / "whatsapp_connect.py").read_text(encoding="utf-8")
    assert "start_trial_on_whatsapp_connect" not in service
    assert "start_trial_on_whatsapp_connect" not in guardian
    assert "start_trial_on_whatsapp_connect" not in embedded
    assert "start_trial_on_whatsapp_connect" not in connect
    assert "finalize_successful_whatsapp_connection" in service
    assert "finalize_successful_whatsapp_connection" in guardian
    assert "finalize_successful_whatsapp_connection" in embedded
    assert "finalize_successful_whatsapp_connection" in connect
    assert 'conn.status                   = "connected"' not in service
    assert 'conn.status = "connected"' not in service


def _patch_commit_connection(monkeypatch, *, register=(True, None), subscribe=(True, None)):
    from services import whatsapp_connection_service as wa_svc  # noqa: PLC0415
    from services.whatsapp_platform.wa_token_validation import classify_debug_info  # noqa: PLC0415

    monkeypatch.setattr(wa_svc, "validate_phone_waba_match", lambda *_a, **_kw: (True, None, None))
    monkeypatch.setattr(
        "services.whatsapp_platform.wa_token_validation.validate_meta_access_token_sync",
        lambda _token: classify_debug_info({
            "is_valid": True,
            "type": "SYSTEM_USER",
            "expires_at": 0,
            "scopes": ["whatsapp_business_messaging"],
            "app_id": "123",
        }),
    )
    monkeypatch.setattr(wa_svc, "evict_phone_id_from_other_tenants", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "evict_waba_id_from_other_tenants", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "assert_phone_id_not_claimed", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "assert_waba_id_not_claimed", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "fetch_phone_metadata", lambda *_a, **_kw: {})
    monkeypatch.setattr(wa_svc, "register_phone_number", lambda *_a, **_kw: register)
    monkeypatch.setattr(wa_svc, "subscribe_phone_webhook", lambda *_a, **_kw: subscribe)
    return wa_svc


def _ready_sync_state(**overrides):
    state = {
        "connected": True,
        "sending_enabled": True,
        "db_status": "connected",
        "message": "ready",
        "verification_status": "VERIFIED",
        "name_status": "APPROVED",
        "meta_phone_status": "CONNECTED",
        "quality_rating": "GREEN",
    }
    state.update(overrides)
    return state


def test_wa_life_12_credential_persistence_alone_does_not_start_trial(monkeypatch, db):
    wa_svc = _patch_commit_connection(
        monkeypatch, register=(False, "register skipped"), subscribe=(False, "webhook skipped"),
    )
    t = _tenant(db, name="متجر تجريبي عام")
    db.commit()
    result = wa_svc.commit_connection(
        db,
        tenant_id=t.id,
        phone_number_id="PHONE-CRED-1",
        waba_id="WABA-CRED-1",
        access_token="tok",
        connection_type="cloud_api",
        phone_number="+966500000030",
        display_name="متجر تجريبي عام",
    )
    db.refresh(t)
    row = db.query(WhatsAppConnection).filter_by(tenant_id=t.id).first()
    assert result.credentials_saved is True
    assert result.inbound_usable is False
    assert row.status == "pending"
    assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP
    assert t.trial_started_at is None
    assert t.first_whatsapp_connected_at is None


def test_wa_life_13_register_failure_does_not_finalize(monkeypatch, db):
    wa_svc = _patch_commit_connection(
        monkeypatch, register=(False, "graph register failed"), subscribe=(True, None),
    )
    t = _tenant(db, name="حذاء رياضي أبيض")
    db.commit()
    result = wa_svc.commit_connection(
        db,
        tenant_id=t.id,
        phone_number_id="PHONE-REG-1",
        waba_id="WABA-REG-1",
        access_token="tok",
        connection_type="cloud_api",
        phone_number="+966500000031",
        display_name="حذاء رياضي أبيض",
    )
    db.refresh(t)
    row = db.query(WhatsAppConnection).filter_by(tenant_id=t.id).first()
    assert result.phone_registered is False
    assert result.inbound_usable is False
    assert row.status == "pending"
    assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP
    assert t.trial_started_at is None
    assert t.first_whatsapp_connected_at is None


def test_wa_life_14_webhook_failure_does_not_finalize(monkeypatch, db):
    wa_svc = _patch_commit_connection(
        monkeypatch, register=(True, None), subscribe=(False, "subscribe failed"),
    )
    t = _tenant(db, name="قميص قطني أزرق")
    db.commit()
    result = wa_svc.commit_connection(
        db,
        tenant_id=t.id,
        phone_number_id="PHONE-WH-1",
        waba_id="WABA-WH-1",
        access_token="tok",
        connection_type="cloud_api",
        phone_number="+966500000032",
        display_name="قميص قطني أزرق",
    )
    db.refresh(t)
    row = db.query(WhatsAppConnection).filter_by(tenant_id=t.id).first()
    assert result.phone_registered is True
    assert result.webhook_subscribed is False
    assert result.inbound_usable is False
    assert row.status == "pending"
    assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP
    assert t.trial_started_at is None
    assert t.first_whatsapp_connected_at is None


def test_wa_life_15_normal_meta_fully_ready_finalizes_once(monkeypatch, db):
    wa_svc = _patch_commit_connection(monkeypatch)
    calls = []
    real = finalize_successful_whatsapp_connection

    def wrapped(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(
        "core.whatsapp_connection_finalization.finalize_successful_whatsapp_connection",
        wrapped,
    )
    t = _tenant(db, name="عطر ورد 100ml")
    db.commit()
    result = wa_svc.commit_connection(
        db,
        tenant_id=t.id,
        phone_number_id="PHONE-READY-1",
        waba_id="WABA-READY-1",
        access_token="tok",
        connection_type="cloud_api",
        phone_number="+966500000033",
        display_name="عطر ورد 100ml",
    )
    db.refresh(t)
    row = db.query(WhatsAppConnection).filter_by(tenant_id=t.id).first()
    assert result.inbound_usable is True
    assert calls == [1]
    assert row.status == "connected"
    assert t.subscription_status == TRIAL_STATUS_ACTIVE
    started = t.trial_started_at
    first_wa = t.first_whatsapp_connected_at
    connected_at = row.connected_at
    wa_svc.commit_connection(
        db,
        tenant_id=t.id,
        phone_number_id="PHONE-READY-1",
        waba_id="WABA-READY-1",
        access_token="tok",
        connection_type="cloud_api",
        phone_number="+966500000033",
        display_name="عطر ورد 100ml",
    )
    db.refresh(t)
    db.refresh(row)
    assert calls == [1, 1]
    assert t.trial_started_at == started
    assert t.first_whatsapp_connected_at == first_wa
    assert row.connected_at == connected_at


def test_wa_life_16_embedded_sync_cannot_persist_connected_before_finalizer(db):
    from routers.whatsapp_embedded import _apply_embedded_state  # noqa: PLC0415

    t = _tenant(db, name="متجر تجريبي عام")
    conn = _conn(db, t.id, status="configuring")
    db.commit()
    _apply_embedded_state(
        conn,
        {"display_phone_number": "+966500000034", "verified_name": "متجر تجريبي عام"},
        _ready_sync_state(),
    )
    db.commit()
    db.refresh(conn)
    db.refresh(t)
    assert conn.status == "configuring"
    assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP
    assert t.trial_started_at is None


def test_wa_life_17_indirect_db_status_connected_routes_through_finalizer(db):
    from routers.whatsapp_embedded import _apply_embedded_state  # noqa: PLC0415

    t = _tenant(db, name="أحمد سالم")
    conn = _conn(db, t.id, status="activation_pending")
    db.commit()
    _apply_embedded_state(
        conn,
        {"display_phone_number": "+966500000035", "verified_name": "أحمد سالم"},
        _ready_sync_state(connected=False, db_status="connected"),
    )
    db.commit()
    db.refresh(conn)
    assert conn.status == "activation_pending"
    assert finalize_successful_whatsapp_connection(db, conn) is True
    db.refresh(conn)
    db.refresh(t)
    assert conn.status == "connected"
    assert t.subscription_status == TRIAL_STATUS_ACTIVE


def test_wa_life_18_reconcile_has_no_independent_connected_writer():
    src = (BACKEND_DIR / "routers" / "whatsapp_connect.py").read_text(encoding="utf-8")
    start = src.index("def _reconcile_coexistence_status(")
    end = src.index("\ndef ", start + 1)
    body = src[start:end]
    assert 'conn.status = "connected"' not in body
    assert "db: Session | None" not in body
    assert "db: Optional[Session]" not in body
    assert "if db is None" not in body
    assert "finalize_successful_whatsapp_connection" in body


def test_wa_life_19_finalizer_persist_failure_raises(db):
    t = _tenant(db, name="نورة عبدالله")
    conn = _conn(db, t.id, status="pending")
    db.commit()
    original = db.commit

    def boom():
        if str(getattr(conn, "status", "") or "") == "connected":
            raise RuntimeError("db persist failed")
        return original()

    db.commit = boom  # type: ignore[method-assign]
    with pytest.raises(WhatsAppConnectionFinalizationError, match="failed to persist"):
        finalize_successful_whatsapp_connection(db, conn)
    db.commit = original  # type: ignore[method-assign]
    db.refresh(conn)
    db.refresh(t)
    assert conn.status != "connected"
    assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP
    assert t.trial_started_at is None
    assert t.first_whatsapp_connected_at is None


def test_wa_life_20_caller_does_not_convert_persist_failure_into_success(monkeypatch, db):
    from services.whatsapp_connection_service import WhatsAppConnectionError  # noqa: PLC0415

    wa_svc = _patch_commit_connection(monkeypatch)

    def boom(*_a, **_k):
        raise WhatsAppConnectionFinalizationError("failed to persist successful WhatsApp connection")

    monkeypatch.setattr(
        "core.whatsapp_connection_finalization.finalize_successful_whatsapp_connection",
        boom,
    )
    t = _tenant(db, name="الرياض-RRRD1234")
    db.commit()
    with pytest.raises(WhatsAppConnectionError, match="finalization failed"):
        wa_svc.commit_connection(
            db,
            tenant_id=t.id,
            phone_number_id="PHONE-FAIL-1",
            waba_id="WABA-FAIL-1",
            access_token="tok",
            connection_type="cloud_api",
            phone_number="+966500000036",
            display_name="الرياض-RRRD1234",
        )
    db.refresh(t)
    row = db.query(WhatsAppConnection).filter_by(tenant_id=t.id).first()
    assert row.status != "connected"
    assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP
    connect = (BACKEND_DIR / "routers" / "whatsapp_connect.py").read_text(encoding="utf-8")
    assert "except HTTPException:\n            raise" in connect
    assert "except HTTPException:\n        raise" in connect


def test_wa_life_21_trial_failure_cannot_leave_connected_and_pending(monkeypatch, db):
    from core.trial_lifecycle import start_trial_on_whatsapp_connect as real_start  # noqa: PLC0415

    t = _tenant(db, name="متجر تجريبي عام")
    conn = _conn(db, t.id, status="pending")
    db.commit()

    def boom(*args, **kwargs):
        real_start(*args, **kwargs)
        raise RuntimeError("trial persist failed")

    monkeypatch.setattr(
        "core.whatsapp_connection_finalization.start_trial_on_whatsapp_connect",
        boom,
    )
    with pytest.raises(WhatsAppConnectionFinalizationError, match="trial lifecycle failed"):
        finalize_successful_whatsapp_connection(db, conn)
    db.refresh(conn)
    db.refresh(t)
    assert conn.status != "connected"
    assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP
    assert t.trial_started_at is None
    assert t.first_whatsapp_connected_at is None


def test_wa_life_22_guardian_finalization_failure_is_retryable(monkeypatch, db):
    from core.webhook_guardian import _inspect_connection  # noqa: PLC0415

    t = _tenant(db, name="حذاء رياضي أبيض")
    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=12)).isoformat()
    conn = _conn(
        db,
        t.id,
        status="configuring",
        sending_enabled=False,
        webhook_verified=True,
        last_webhook_received_at=now,
        extra_metadata={
            "connection_mode": "coexistence",
            "smb_sync_deadline_at": future,
            "smb_sync": {},
        },
    )
    db.commit()
    monkeypatch.setattr(
        "core.whatsapp_connection_finalization.finalize_successful_whatsapp_connection",
        lambda *_a, **_k: (_ for _ in ()).throw(WhatsAppConnectionFinalizationError("persist failed")),
    )
    with patch(
        "services.whatsapp_platform.wa_connection_secrets.read_access_token",
        return_value="tok",
    ), patch(
        "services.meta_coexistence.initiate_smb_app_data",
        return_value={
            "smb_app_state_sync": {"accepted": True, "request_id": "a"},
            "history": {"accepted": True, "request_id": "b"},
        },
    ):
        health = asyncio.run(_inspect_connection(db, conn, now, now - timedelta(minutes=15)))
    db.refresh(conn)
    db.refresh(t)
    assert health == "critical"
    assert conn.status != "connected"
    assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP


def test_wa_life_23_backfill_finalization_failure_fails_operation(monkeypatch, db):
    import importlib.util

    t = _tenant(db, name="قميص قطني أزرق")
    conn = _conn(
        db,
        t.id,
        status="configuring",
        phone_number="+966500000037",
        extra_metadata={"provider_details": {"channel_id": "CH-1"}},
    )
    conn.access_token = "tok"
    db.commit()

    spec = importlib.util.spec_from_file_location(
        "backfill_coexistence_record",
        BACKEND_DIR / "scripts" / "backfill_coexistence_record.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _Session:
        def close(self):
            return None

        def __getattr__(self, name):
            return getattr(db, name)

    monkeypatch.setattr(mod, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(
        "core.whatsapp_connection_finalization.finalize_successful_whatsapp_connection",
        lambda *_a, **_k: (_ for _ in ()).throw(WhatsAppConnectionFinalizationError("persist failed")),
    )
    rc = mod.backfill(
        tenant_id=t.id,
        waba_id="waba-generic-1",
        channel_id="CH-1",
        phone_number="+966500000037",
        phone_number_id="pn-generic-1",
        display_name="قميص قطني أزرق",
        promote=True,
        dry_run=False,
    )
    assert rc == 1
    db.refresh(conn)
    db.refresh(t)
    assert conn.status != "connected"
    assert t.subscription_status == TRIAL_STATUS_PENDING_WHATSAPP


def test_wa_life_24_paid_tenant_remains_paid(db):
    test_wa_life_06_paid_tenant_remains_paid(db)


def test_wa_life_25_reconnect_does_not_restart_trial(db):
    test_wa_life_05_reconnect_does_not_restart_trial(db)


def test_wa_life_26_repeated_guardian_recovery_is_idempotent(db):
    test_wa_life_03_and_04_guardian_promotion_starts_trial_once(db)


def test_wa_life_27_first_whatsapp_connected_at_stable(db):
    t = _tenant(db, name="عطر ورد 100ml")
    conn = _conn(db, t.id, status="pending")
    db.commit()
    assert finalize_successful_whatsapp_connection(db, conn) is True
    db.refresh(t)
    db.refresh(conn)
    first_wa = t.first_whatsapp_connected_at
    connected_at = conn.connected_at
    assert first_wa is not None
    assert finalize_successful_whatsapp_connection(db, conn) is False
    db.refresh(t)
    db.refresh(conn)
    assert t.first_whatsapp_connected_at == first_wa
    assert conn.connected_at == connected_at


_CONNECTED_ASSIGN_RE = re.compile(
    r"""(?<![.\w])(?:conn|wa_conn|row)\.status\s*=\s*(['"])connected\1"""
)
_INDIRECT_STATUS_RE = re.compile(
    r"""\.status\s*=\s*(?:sync_state\[[\"']db_status[\"']\]|sync_state\.get\([\"']db_status[\"'])"""
)


def test_wa_life_28_production_writer_audit():
    allowed = {
        (BACKEND_DIR / "core" / "whatsapp_connection_finalization.py").resolve(),
    }
    scan_roots = [
        BACKEND_DIR / "core",
        BACKEND_DIR / "routers",
        BACKEND_DIR / "services",
        BACKEND_DIR / "scripts",
    ]
    writers = []
    indirect = []
    for root in scan_roots:
        for path in root.rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if path.resolve() not in allowed:
                for match in _CONNECTED_ASSIGN_RE.finditer(text):
                    writers.append(f"{path.relative_to(REPO_ROOT)}:{text[:match.start()].count(chr(10)) + 1}")
            for match in _INDIRECT_STATUS_RE.finditer(text):
                indirect.append(f"{path.relative_to(REPO_ROOT)}:{text[:match.start()].count(chr(10)) + 1}")
    assert writers == [], f"second successful-connection writers remain: {writers}"
    assert indirect == [], f"indirect db_status connected writers remain: {indirect}"
    finalizer = (BACKEND_DIR / "core" / "whatsapp_connection_finalization.py").read_text(encoding="utf-8")
    assert 'conn.status = "connected"' in finalizer
    assert "raise WhatsAppConnectionFinalizationError" in finalizer
    assert "return False" not in finalizer or "False is never a failed persist" in finalizer
