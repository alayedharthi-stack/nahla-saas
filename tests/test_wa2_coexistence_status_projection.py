"""WA-2: Coexistence vs Embedded Cloud status projection."""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
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

from models import Base, Tenant, TenantSettings, WhatsAppConnection  # noqa: E402
from core.trial_lifecycle import (  # noqa: E402
    TRIAL_STATUS_ACTIVE,
    init_new_tenant_trial_state,
)
from core.whatsapp_connection_finalization import (  # noqa: E402
    finalize_successful_whatsapp_connection,
)
from routers.whatsapp_embedded import (  # noqa: E402
    _GRAPH_OBJECT_MISSING_SUBCODE,
    _apply_embedded_state,
    _build_phone_sync_state,
    _meta_fetch_failure_kind,
    _project_phone_sync_state,
    sync_embedded_connection_from_meta,
)
from services.meta_coexistence import project_coexistence_sync_state  # noqa: E402

PHONE_ID = "pn-generic-1"
WABA_ID = "waba-generic-1"
SMB_READY = {
    "smb_app_state_sync": {"accepted": True, "request_id": "sync-a"},
    "history": {"accepted": True, "request_id": "hist-b"},
}


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


def _ai_settings(db, tenant_id, *, mode="on", enabled=True):
    row = TenantSettings(
        tenant_id=tenant_id,
        ai_settings={"store_ai_mode": mode, "store_ai_enabled": enabled},
        store_settings={"store_name": "متجر تجريبي عام"},
        whatsapp_settings={},
    )
    db.add(row)
    db.flush()
    return row


def _smb_meta(*, stamp_identity: bool = True, **extra):
    meta = {
        "connection_mode": "coexistence",
        "smb_sync": dict(SMB_READY),
    }
    if stamp_identity:
        meta["readiness_phone_number_id"] = PHONE_ID
        meta["readiness_waba_id"] = WABA_ID
    meta.update(extra)
    return meta


def _conn(db, tenant_id, **overrides):
    now = datetime.now(timezone.utc)
    kwargs = {
        "tenant_id": tenant_id,
        "status": "configuring",
        "phone_number_id": PHONE_ID,
        "whatsapp_business_account_id": WABA_ID,
        "provider": "meta",
        "connection_type": "embedded",
        "webhook_verified": True,
        "sending_enabled": False,
        "extra_metadata": _smb_meta(),
        "last_webhook_received_at": now,
        "phone_number": "+966500000001",
        "business_display_name": "متجر تجريبي عام",
    }
    kwargs.update(overrides)
    row = WhatsAppConnection(**kwargs)
    db.add(row)
    db.flush()
    return row


def _cloud_phone(*, verified=False, phone_id=PHONE_ID, status="CONNECTED"):
    return {
        "id": phone_id,
        "display_phone_number": "+966500000001",
        "verified_name": "متجر تجريبي عام",
        "code_verification_status": "VERIFIED" if verified else "NOT_VERIFIED",
        "name_status": "APPROVED",
        "status": status,
        "quality_rating": "GREEN",
    }


def _patch_phone(monkeypatch, phone):
    async def _fake(_conn, _db):
        return dict(phone), "platform"

    monkeypatch.setattr(
        "routers.whatsapp_embedded._get_phone_details_with_fallback",
        _fake,
    )


def _patch_phone_error(monkeypatch, err):
    async def _fake(_conn, _db):
        return {"error": dict(err)}, "platform"

    monkeypatch.setattr(
        "routers.whatsapp_embedded._get_phone_details_with_fallback",
        _fake,
    )


def _sync(conn, db, **kwargs):
    return asyncio.run(sync_embedded_connection_from_meta(conn, db, **kwargs))


def test_wa2_01_live_coexistence_ignores_cloud_otp_not_verified(monkeypatch, db):
    t = _tenant(db, name="قميص قطني أزرق")
    _ai_settings(db, t.id)
    conn = _conn(db, t.id, status="otp_pending", sending_enabled=False)
    first_at = datetime(2026, 8, 18, 18, 4, 30, 138454, tzinfo=timezone.utc)
    assert finalize_successful_whatsapp_connection(db, conn, connected_at=first_at) is True
    conn.status = "otp_pending"
    conn.sending_enabled = False
    db.commit()
    db.refresh(t)
    started = t.trial_started_at
    ended = t.trial_ends_at
    first_wa = t.first_whatsapp_connected_at
    connected_at = conn.connected_at
    ai_before = dict(db.query(TenantSettings).filter_by(tenant_id=t.id).one().ai_settings)
    token_before = conn.access_token
    phone_before = conn.phone_number_id
    waba_before = conn.whatsapp_business_account_id
    mode_before = (conn.extra_metadata or {}).get("connection_mode")

    _patch_phone(monkeypatch, _cloud_phone(verified=False))
    payload = _sync(conn, db, attempt_register=False, allow_demotion=False)
    db.refresh(conn)
    db.refresh(t)
    ai_after = dict(db.query(TenantSettings).filter_by(tenant_id=t.id).one().ai_settings)

    assert payload["status"] != "otp_pending"
    assert payload["otp_required"] is False
    assert payload["connection_mode"] == "coexistence"
    assert payload["connected"] is True
    assert payload["sending_enabled"] is True
    assert conn.status == "connected"
    assert conn.sending_enabled is True
    assert conn.webhook_verified is True
    assert conn.phone_number_id == phone_before
    assert conn.whatsapp_business_account_id == waba_before
    assert (conn.extra_metadata or {}).get("connection_mode") == mode_before
    assert conn.access_token == token_before
    assert t.subscription_status == TRIAL_STATUS_ACTIVE
    assert t.trial_started_at == started
    assert t.trial_ends_at == ended
    assert t.first_whatsapp_connected_at == first_wa
    assert conn.connected_at == connected_at
    assert ai_after == ai_before


def test_wa2_02_incomplete_coexistence_is_not_force_connected(monkeypatch, db):
    t = _tenant(db, name="حذاء رياضي أبيض")
    conn = _conn(
        db,
        t.id,
        extra_metadata={"connection_mode": "coexistence", "smb_sync": {}},
        webhook_verified=True,
        status="configuring",
    )
    db.commit()
    _patch_phone(monkeypatch, _cloud_phone(verified=False, status="CONNECTED"))
    payload = _sync(conn, db, attempt_register=True, allow_demotion=True)
    db.refresh(conn)
    db.refresh(t)
    assert payload["connected"] is False
    assert payload["otp_required"] is False
    assert conn.status != "connected"
    assert conn.status != "otp_pending"
    assert conn.sending_enabled is False
    assert t.trial_started_at is None
    assert t.first_whatsapp_connected_at is None


def test_wa2_03_embedded_cloud_still_requires_otp(monkeypatch, db):
    t = _tenant(db, name="عطر ورد 100ml")
    conn = _conn(
        db,
        t.id,
        extra_metadata={},
        webhook_verified=False,
        status="pending",
    )
    db.commit()
    cloud = _build_phone_sync_state(_cloud_phone(verified=False))
    assert cloud["db_status"] == "otp_pending"
    _patch_phone(monkeypatch, _cloud_phone(verified=False))
    payload = _sync(conn, db, attempt_register=True, allow_demotion=True)
    db.refresh(conn)
    assert payload["status"] == "otp_pending"
    assert payload["otp_required"] is True
    assert payload["connected"] is False
    assert conn.status == "otp_pending"
    assert conn.sending_enabled is False
    assert t.trial_started_at is None


def test_wa2_04_embedded_verified_and_ready_uses_finalizer(monkeypatch, db):
    t = _tenant(db, name="أحمد سالم")
    conn = _conn(
        db,
        t.id,
        extra_metadata={},
        webhook_verified=True,
        status="activation_pending",
        sending_enabled=False,
    )
    db.commit()
    _patch_phone(monkeypatch, _cloud_phone(verified=True, status="CONNECTED"))
    with patch(
        "routers.whatsapp_embedded.subscribe_phone_webhook",
        create=True,
    ):
        payload = _sync(conn, db, attempt_register=False, allow_demotion=True)
    db.refresh(conn)
    db.refresh(t)
    assert payload["status"] == "connected"
    assert payload["otp_required"] is False
    assert conn.status == "connected"
    assert conn.sending_enabled is True
    assert t.subscription_status == TRIAL_STATUS_ACTIVE
    assert t.trial_started_at is not None


def test_wa2_05_stale_cloud_otp_cannot_demote_live_coexistence(monkeypatch, db):
    t = _tenant(db, name="نورة عبدالله")
    conn = _conn(db, t.id)
    db.commit()
    assert finalize_successful_whatsapp_connection(db, conn) is True
    db.refresh(conn)
    assert conn.status == "connected"
    _patch_phone(monkeypatch, _cloud_phone(verified=False))
    payload = _sync(conn, db, attempt_register=True, allow_demotion=False)
    db.refresh(conn)
    assert payload["status"] == "connected"
    assert payload["otp_required"] is False
    assert conn.status == "connected"
    assert conn.sending_enabled is True


def test_wa2_06_get_status_flags_are_observe_safe():
    connect = (BACKEND_DIR / "routers" / "whatsapp_connect.py").read_text(encoding="utf-8")
    body = connect[connect.index("async def whatsapp_status("):connect.index("async def direct_status(")]
    assert "allow_demotion=False" in body
    assert "attempt_register=False" in body
    embedded = (BACKEND_DIR / "routers" / "whatsapp_embedded.py").read_text(encoding="utf-8")
    get_status = embedded[embedded.index("async def get_status("):embedded.index("async def add_phone(")]
    assert "allow_demotion=not _is_coexistence_conn(conn)" in get_status
    assert "attempt_register=not _is_coexistence_conn(conn)" in get_status


def test_wa2_06b_reopen_status_does_not_change_canonical_ready_row(monkeypatch, db):
    t = _tenant(db, name="الرياض-RRRD1234")
    conn = _conn(db, t.id, status="connected", sending_enabled=True)
    conn.connected_at = datetime.now(timezone.utc)
    db.commit()
    fingerprint = (
        conn.status,
        conn.sending_enabled,
        conn.phone_number_id,
        conn.whatsapp_business_account_id,
        conn.connection_type,
        (conn.extra_metadata or {}).get("connection_mode"),
        conn.webhook_verified,
    )
    _patch_phone(monkeypatch, _cloud_phone(verified=False))
    _sync(conn, db, attempt_register=False, allow_demotion=False)
    db.refresh(conn)
    assert (
        conn.status,
        conn.sending_enabled,
        conn.phone_number_id,
        conn.whatsapp_business_account_id,
        conn.connection_type,
        (conn.extra_metadata or {}).get("connection_mode"),
        conn.webhook_verified,
    ) == fingerprint


def test_wa2_07_old_readiness_does_not_grant_new_identity():
    conn = SimpleNamespace(
        phone_number_id=PHONE_ID,
        whatsapp_business_account_id=WABA_ID,
        webhook_verified=True,
        extra_metadata=_smb_meta(),
        last_error=None,
        status="connected",
    )
    projected = project_coexistence_sync_state(
        conn,
        phone_data=_cloud_phone(verified=True, phone_id="pn-new-identity"),
        cloud_state=_build_phone_sync_state(_cloud_phone(verified=True, phone_id="pn-new-identity")),
    )
    assert projected["connected"] is False
    assert projected["otp_required"] is False
    assert projected["projection_reason"] == "identity_mismatch"
    assert projected["db_status"] != "connected"


def test_wa2_07b_apply_does_not_rewrite_identity_from_foreign_phone(db):
    t = _tenant(db, name="قميص قطني أزرق")
    conn = _conn(db, t.id, status="connected", sending_enabled=True)
    conn.connected_at = datetime.now(timezone.utc)
    db.commit()
    foreign = _cloud_phone(verified=True, phone_id="pn-new-identity")
    projected = _project_phone_sync_state(conn, foreign)
    _apply_embedded_state(conn, foreign, projected, allow_demotion=True)
    db.commit()
    db.refresh(conn)
    assert conn.phone_number_id == PHONE_ID
    assert conn.status != "connected"


def test_wa2_07c_replaced_identity_cannot_reuse_unstamped_smb_and_webhook():
    conn = SimpleNamespace(
        phone_number_id="old-phone",
        whatsapp_business_account_id="old-waba",
        provider="meta",
        connection_type="embedded",
        webhook_verified=True,
        extra_metadata={
            "connection_mode": "coexistence",
            "smb_sync": {
                "smb_app_state_sync": {"accepted": True, "request_id": "old-sync"},
                "history": {"accepted": True, "request_id": "old-history"},
            },
        },
        last_error=None,
        status="connected",
    )
    conn.phone_number_id = "new-phone"
    conn.whatsapp_business_account_id = "new-waba"
    projected = project_coexistence_sync_state(
        conn,
        phone_data={
            "id": "new-phone",
            "code_verification_status": "NOT_VERIFIED",
            "status": "CONNECTED",
        },
    )
    assert projected["connected"] is False
    assert projected["otp_required"] is False
    assert projected["projection_reason"] == "identity_mismatch"
    assert projected["db_status"] != "connected"


def test_wa2_07e_stamped_old_identity_does_not_follow_new_phone():
    conn = SimpleNamespace(
        phone_number_id="new-phone",
        whatsapp_business_account_id="new-waba",
        webhook_verified=True,
        extra_metadata=_smb_meta(),
        last_error=None,
        status="connected",
        connected_at=datetime.now(timezone.utc),
    )
    projected = project_coexistence_sync_state(
        conn,
        phone_data=_cloud_phone(verified=False, phone_id="new-phone"),
    )
    assert projected["connected"] is False
    assert projected["projection_reason"] == "identity_mismatch"


def test_wa2_07d_unstamped_legacy_demotion_repairs_same_identity_only():
    conn = SimpleNamespace(
        phone_number_id=PHONE_ID,
        whatsapp_business_account_id=WABA_ID,
        webhook_verified=True,
        connected_at=datetime(2026, 8, 18, 18, 4, 30, 138454, tzinfo=timezone.utc),
        extra_metadata=_smb_meta(stamp_identity=False),
        last_error=None,
        status="otp_pending",
    )
    projected = project_coexistence_sync_state(
        conn,
        phone_data=_cloud_phone(verified=False),
    )
    assert projected["connected"] is True
    assert projected["otp_required"] is False


def test_wa2_08_reconnect_does_not_restart_trial(monkeypatch, db):
    t = _tenant(db, name="حذاء رياضي أبيض")
    conn = _conn(db, t.id)
    db.commit()
    first_at = datetime.now(timezone.utc) - timedelta(days=3)
    assert finalize_successful_whatsapp_connection(db, conn, connected_at=first_at) is True
    db.refresh(t)
    started = t.trial_started_at
    ended = t.trial_ends_at
    first_wa = t.first_whatsapp_connected_at
    connected_at = conn.connected_at
    conn.status = "disconnected"
    conn.sending_enabled = False
    db.commit()
    _patch_phone(monkeypatch, _cloud_phone(verified=False))
    _sync(conn, db, attempt_register=False, allow_demotion=False)
    db.refresh(t)
    db.refresh(conn)
    assert conn.status == "connected"
    assert t.trial_started_at == started
    assert t.trial_ends_at == ended
    assert t.first_whatsapp_connected_at == first_wa
    assert conn.connected_at == connected_at


def test_wa2_09_and_10_lifecycle_and_ai_settings_untouched_on_get_sync(monkeypatch, db):
    test_wa2_01_live_coexistence_ignores_cloud_otp_not_verified(monkeypatch, db)


def test_wa2_11_inbound_outbound_gates_stay_live(monkeypatch, db):
    t = _tenant(db, name="عطر ورد 100ml")
    conn = _conn(
        db,
        t.id,
        status="connected",
        sending_enabled=True,
        webhook_verified=True,
        last_webhook_received_at=datetime.now(timezone.utc),
    )
    conn.connected_at = datetime.now(timezone.utc)
    db.commit()
    _patch_phone(monkeypatch, _cloud_phone(verified=False))
    _sync(conn, db, attempt_register=False, allow_demotion=False)
    db.refresh(conn)
    assert conn.webhook_verified is True
    assert conn.sending_enabled is True
    assert conn.status == "connected"
    assert bool(conn.phone_number_id)


def test_wa2_12_no_new_connected_writers_outside_finalizer():
    allowed = {
        (BACKEND_DIR / "core" / "whatsapp_connection_finalization.py").resolve(),
    }
    assign_re = re.compile(
        r"""(?<![.\w])(?:conn|wa_conn|row)\.status\s*=\s*(['"])connected\1"""
    )
    writers = []
    for root in (BACKEND_DIR / "core", BACKEND_DIR / "routers", BACKEND_DIR / "services"):
        for path in root.rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            if path.resolve() in allowed:
                continue
            text = path.read_text(encoding="utf-8")
            for match in assign_re.finditer(text):
                writers.append(f"{path.relative_to(REPO_ROOT)}:{text[:match.start()].count(chr(10)) + 1}")
    assert writers == [], f"connected writers outside finalizer: {writers}"


def test_wa2_no_frontend_sms_hide_hack():
    page = (REPO_ROOT / "dashboard" / "src" / "pages" / "WhatsAppConnect.tsx").read_text(encoding="utf-8")
    assert "if (res.status === 'otp_pending')" in page
    assert "connection_mode === 'coexistence' &&" not in page
    assert 'connection_mode == "coexistence"' not in page


def test_wa2_cloud_otp_projection_not_used_as_coexistence_readiness():
    conn = SimpleNamespace(
        phone_number_id=PHONE_ID,
        whatsapp_business_account_id=WABA_ID,
        webhook_verified=True,
        extra_metadata=_smb_meta(),
        last_error=None,
        status="otp_pending",
    )
    cloud = _build_phone_sync_state(_cloud_phone(verified=False))
    assert cloud["db_status"] == "otp_pending"
    projected = _project_phone_sync_state(conn, _cloud_phone(verified=False))
    assert projected["db_status"] == "connected"
    assert projected["otp_required"] is False
    assert projected["connected"] is True


def _connected_coexistence(db):
    t = _tenant(db, name="متجر تجريبي عام")
    _ai_settings(db, t.id)
    conn = _conn(db, t.id)
    db.commit()
    assert finalize_successful_whatsapp_connection(db, conn) is True
    db.refresh(conn)
    db.refresh(t)
    conn.sending_enabled = True
    db.commit()
    return t, conn


def test_wa2_13_transient_meta_code_2_does_not_demote_connected(monkeypatch, db):
    t, conn = _connected_coexistence(db)
    started = t.trial_started_at
    ended = t.trial_ends_at
    first_wa = t.first_whatsapp_connected_at
    connected_at = conn.connected_at
    phone = conn.phone_number_id
    waba = conn.whatsapp_business_account_id
    sending = conn.sending_enabled
    ai_before = dict(db.query(TenantSettings).filter_by(tenant_id=t.id).one().ai_settings)
    _patch_phone_error(monkeypatch, {"code": 2, "message": "Service temporarily unavailable"})
    payload = _sync(conn, db, attempt_register=False, allow_demotion=False)
    db.refresh(conn)
    db.refresh(t)
    assert payload["status"] == "connected"
    assert payload["connected"] is True
    assert conn.status == "connected"
    assert conn.sending_enabled is sending
    assert conn.connected_at == connected_at
    assert conn.phone_number_id == phone
    assert conn.whatsapp_business_account_id == waba
    assert t.trial_started_at == started
    assert t.trial_ends_at == ended
    assert t.first_whatsapp_connected_at == first_wa
    assert dict(db.query(TenantSettings).filter_by(tenant_id=t.id).one().ai_settings) == ai_before
    assert (conn.extra_metadata or {}).get("meta_fetch_failure_kind") == "transient"
    assert (conn.extra_metadata or {}).get("meta_verification_unavailable") is True


def test_wa2_14_repeated_transient_refresh_does_not_demote(monkeypatch, db):
    t, conn = _connected_coexistence(db)
    sending = conn.sending_enabled
    _patch_phone_error(monkeypatch, {"code": 2, "message": "An unexpected error has occurred."})
    for _ in range(3):
        payload = _sync(conn, db, attempt_register=False, allow_demotion=False)
        db.refresh(conn)
        assert payload["status"] == "connected"
        assert conn.status == "connected"
        assert conn.sending_enabled is sending


@pytest.mark.parametrize(
    "err",
    [
        {"code": 2, "message": "Service temporarily unavailable"},
        {"code": 1, "http_status": 503, "message": "Unknown error"},
        {"code": 0, "type": "timeout", "message": "Read timeout"},
        {"code": 131000, "message": "Something went wrong"},
        {"code": 4, "message": "Application request limit reached"},
    ],
)
def test_wa2_15_transient_fetch_shapes_do_not_demote(monkeypatch, db, err):
    assert _meta_fetch_failure_kind(err) == "transient"
    _t, conn = _connected_coexistence(db)
    sending = conn.sending_enabled
    _patch_phone_error(monkeypatch, err)
    _sync(conn, db, attempt_register=False, allow_demotion=False)
    db.refresh(conn)
    assert conn.status == "connected"
    assert conn.sending_enabled is sending


def test_wa2_16_authoritative_hard_failure_can_demote(monkeypatch, db):
    err = {
        "code": 803,
        "error_subcode": _GRAPH_OBJECT_MISSING_SUBCODE,
        "message": "Unsupported get request. Object does not exist",
    }
    assert _meta_fetch_failure_kind(err) == "authoritative"
    _t, conn = _connected_coexistence(db)
    _patch_phone_error(monkeypatch, err)
    payload = _sync(conn, db, attempt_register=False, allow_demotion=False)
    db.refresh(conn)
    assert payload["status"] == "error"
    assert payload["connected"] is False
    assert conn.status == "error"
    assert conn.sending_enabled is False


_AMBIGUOUS_UNSUPPORTED_GET = {
    "code": 100,
    "message": (
        "Unsupported get request. Object with ID '123' does not exist, "
        "cannot be loaded due to missing permissions, or does not support this operation"
    ),
}


def test_wa2_16b_classifier_keeps_transient_and_hard_split():
    assert _meta_fetch_failure_kind({"code": 2}) == "transient"
    assert _meta_fetch_failure_kind({"http_status": 502, "message": "Bad Gateway"}) == "transient"
    assert _meta_fetch_failure_kind({"type": "timeout", "message": "timed out"}) == "transient"
    assert _meta_fetch_failure_kind({"code": 100, "message": "Invalid parameter"}) == "transient"
    assert _meta_fetch_failure_kind({"code": 100, "message": "Unsupported get request"}) == "transient"
    assert _meta_fetch_failure_kind(_AMBIGUOUS_UNSUPPORTED_GET) == "transient"
    assert _meta_fetch_failure_kind({"code": 803}) == "authoritative"
    assert _meta_fetch_failure_kind({"error_subcode": _GRAPH_OBJECT_MISSING_SUBCODE}) == "authoritative"
    assert _meta_fetch_failure_kind({"code": _GRAPH_OBJECT_MISSING_SUBCODE, "message": "Object does not exist"}) == "authoritative"
    assert _meta_fetch_failure_kind({"message": "Phone number has been deleted"}) == "authoritative"


def test_wa2_17_embedded_cloud_connected_transient_does_not_demote(monkeypatch, db):
    t = _tenant(db, name="حذاء رياضي أبيض")
    _ai_settings(db, t.id)
    conn = _conn(
        db,
        t.id,
        extra_metadata={},
        webhook_verified=True,
        status="activation_pending",
        sending_enabled=False,
    )
    db.commit()
    _patch_phone(monkeypatch, _cloud_phone(verified=True, status="CONNECTED"))
    with patch("routers.whatsapp_embedded.subscribe_phone_webhook", create=True):
        _sync(conn, db, attempt_register=False, allow_demotion=True)
    db.refresh(conn)
    assert conn.status == "connected"
    sending = conn.sending_enabled
    connected_at = conn.connected_at
    _patch_phone_error(monkeypatch, {"code": 2, "message": "Service temporarily unavailable"})
    payload = _sync(conn, db, attempt_register=False, allow_demotion=False)
    db.refresh(conn)
    assert payload["status"] == "connected"
    assert conn.status == "connected"
    assert conn.sending_enabled is sending
    assert conn.connected_at == connected_at
    assert (conn.extra_metadata or {}).get("connection_mode") != "coexistence"


def test_wa2_18_permission_ambiguous_unsupported_get_does_not_demote(monkeypatch, db):
    _t, conn = _connected_coexistence(db)
    sending = conn.sending_enabled
    connected_at = conn.connected_at
    _patch_phone_error(monkeypatch, _AMBIGUOUS_UNSUPPORTED_GET)
    payload = _sync(conn, db, attempt_register=False, allow_demotion=False)
    db.refresh(conn)
    assert payload["status"] == "connected"
    assert conn.status == "connected"
    assert conn.sending_enabled is sending
    assert conn.connected_at == connected_at


def test_wa2_16c_graph_object_missing_subcode_can_demote(monkeypatch, db):
    err = {
        "code": 100,
        "error_subcode": _GRAPH_OBJECT_MISSING_SUBCODE,
        "message": _AMBIGUOUS_UNSUPPORTED_GET["message"],
    }
    assert _meta_fetch_failure_kind(err) == "authoritative"
    _t, conn = _connected_coexistence(db)
    _patch_phone_error(monkeypatch, err)
    payload = _sync(conn, db, attempt_register=False, allow_demotion=False)
    db.refresh(conn)
    assert payload["status"] == "error"
    assert conn.status == "error"
    assert conn.sending_enabled is False
